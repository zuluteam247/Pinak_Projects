"""Tests for Phase 1 slice 1c (P-87): recall-blind staging and 30-day raw TTL.

Contract: RFC-0001 v0.3, PRD v3 §5-§6, owner ruling #6 (tombstones), and
Zulu spec #2e233d57.
"""

import asyncio
import datetime
import hashlib
import pytest

from app.core.capture_envelope import canonical_json_bytes
from app.core.capture_redaction import redact_and_classify
from app.core.capture_staging import (
    CaptureStagingStore,
    SlotConflictError,
    TombstoneRecord,
    UnredactedInputError,
    parse_utc_iso,
)
from app.core.database import DatabaseManager


def make_valid_staged_envelope(
    tenant_id="tenant-alpha",
    client_id="client-1",
    session_id="01J8Y000000000000000000001",
    sequence=1,
    event_id="01J8Y000000000000000000002",
    task_id="task-100",
    payload=None,
    received_ts=None,
    privacy_class=None,
    retention_class=None,
):
    if payload is None:
        payload = {"message": "hello world", "status": "ok"}
    redaction = redact_and_classify(payload)
    if received_ts is None:
        received_ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return {
        "tenant_id": tenant_id,
        "client_id": client_id,
        "session_id": session_id,
        "sequence": sequence,
        "event_id": event_id,
        "task_id": task_id,
        "actor": "user",
        "event_type": "message",
        "payload": redaction.payload,
        "payload_hash": redaction.payload_hash,
        "privacy_class": privacy_class or redaction.privacy_class,
        "retention_class": retention_class or redaction.retention_class,
        "received_ts": received_ts,
    }


# -------------------------------------------------------------------------
# DEFECT 1: Redaction Guard Verification
# -------------------------------------------------------------------------

def test_redaction_guard_rejects_raw_password_probe():
    store = CaptureStagingStore()
    raw_payload = {"password": "SuperSecretPassword123"}
    canonical = canonical_json_bytes(raw_payload)
    raw_hash = hashlib.sha256(canonical).hexdigest()

    probe_env = {
        "tenant_id": "tenant-alpha",
        "client_id": "client-1",
        "session_id": "01J8Y000000000000000000001",
        "sequence": 1,
        "event_id": "01J8Y000000000000000000002",
        "task_id": "task-1",
        "actor": "user",
        "event_type": "message",
        "payload": raw_payload,
        "payload_hash": raw_hash,
        "privacy_class": "internal",
        "retention_class": "task",
        "received_ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }

    with pytest.raises(UnredactedInputError) as exc_info:
        store.stage_event(probe_env)
    assert "unredacted" in str(exc_info.value).lower()
    assert len(store.list_staged_events("tenant-alpha")) == 0


def test_redaction_guard_rejects_unredacted_credentials_and_tokens():
    store = CaptureStagingStore()
    raw_payload = {"api_key": "AKIAIOSFODNN7EXAMPLE"}
    canonical = canonical_json_bytes(raw_payload)
    raw_hash = hashlib.sha256(canonical).hexdigest()

    env = make_valid_staged_envelope()
    env["payload"] = raw_payload
    env["payload_hash"] = raw_hash
    env["privacy_class"] = "internal"

    with pytest.raises(UnredactedInputError):
        store.stage_event(env)


def test_redaction_guard_rejects_internal_class_for_redacted_secrets():
    store = CaptureStagingStore()
    redacted_payload = {"[redacted:credential]": "[redacted:credential]"}
    canonical = canonical_json_bytes(redacted_payload)
    payload_hash = hashlib.sha256(canonical).hexdigest()

    env = make_valid_staged_envelope(payload={"password": "secret"})
    env["payload"] = redacted_payload
    env["payload_hash"] = payload_hash
    env["privacy_class"] = "internal"

    with pytest.raises(UnredactedInputError):
        store.stage_event(env)


# -------------------------------------------------------------------------
# DEFECT 2: TTL Real UTC Comparison & Strict Date Validation
# -------------------------------------------------------------------------

def test_ttl_rejects_not_a_date_string():
    store = CaptureStagingStore()
    env = make_valid_staged_envelope(received_ts="not-a-date")
    with pytest.raises(UnredactedInputError) as exc_info:
        store.stage_event(env)
    assert "invalid" in str(exc_info.value).lower()
    assert len(store.list_staged_events("tenant-alpha")) == 0


def test_ttl_handles_utc_offset_plus_0530_accurately():
    store = CaptureStagingStore()
    ts_plus_530 = "2026-08-01T12:00:00+05:30"
    env = make_valid_staged_envelope(received_ts=ts_plus_530)
    res = store.stage_event(env)

    exact_expiry_utc = datetime.datetime(2026, 8, 31, 6, 30, 0, tzinfo=datetime.timezone.utc)
    assert res["expires_at"] == exact_expiry_utc.isoformat()

    just_before = exact_expiry_utc - datetime.timedelta(seconds=10)
    purged = store.cleanup_expired(just_before)
    assert len(purged) == 0
    assert len(store.list_staged_events("tenant-alpha")) == 1

    just_after = exact_expiry_utc + datetime.timedelta(seconds=10)
    purged = store.cleanup_expired(just_after)
    assert len(purged) == 1
    assert len(store.list_staged_events("tenant-alpha")) == 0


def test_ttl_handles_utc_offset_minus_0700_accurately():
    store = CaptureStagingStore()
    ts_minus_7 = "2026-08-01T12:00:00-07:00"
    env = make_valid_staged_envelope(received_ts=ts_minus_7)
    res = store.stage_event(env)

    exact_expiry_utc = datetime.datetime(2026, 8, 31, 19, 0, 0, tzinfo=datetime.timezone.utc)
    assert res["expires_at"] == exact_expiry_utc.isoformat()

    six_hours_early = exact_expiry_utc - datetime.timedelta(hours=6)
    purged = store.cleanup_expired(six_hours_early)
    assert len(purged) == 0
    assert len(store.list_staged_events("tenant-alpha")) == 1

    purged_final = store.cleanup_expired(exact_expiry_utc + datetime.timedelta(seconds=5))
    assert len(purged_final) == 1
    assert len(store.list_staged_events("tenant-alpha")) == 0


# -------------------------------------------------------------------------
# DEFECTS 3 & 4: Legal Hold & Open Task Retention & Retry Updates
# -------------------------------------------------------------------------

def test_legal_hold_retention_class_without_boolean_is_not_deleted():
    store = CaptureStagingStore()
    base_time = datetime.datetime(2026, 8, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)
    env = make_valid_staged_envelope(
        received_ts=base_time.isoformat(),
        retention_class="legal_hold",
    )
    res = store.stage_event(env, is_legal_hold=False)
    assert res["expires_at"] is None
    assert res["is_legal_hold"] is True

    tombstones = store.cleanup_expired(base_time + datetime.timedelta(days=100))
    assert len(tombstones) == 0
    assert len(store.list_staged_events("tenant-alpha")) == 1


def test_retry_with_legal_hold_flag_updates_existing_row():
    store = CaptureStagingStore()
    base_time = datetime.datetime(2026, 8, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)
    env = make_valid_staged_envelope(received_ts=base_time.isoformat())

    res1 = store.stage_event(env)
    assert res1["status"] == "staged"
    assert res1["expires_at"] is not None

    res2 = store.stage_event(env, is_legal_hold=True)
    assert res2["status"] == "updated_flags"
    assert res2["is_legal_hold"] is True
    assert res2["expires_at"] is None

    staged = store.list_staged_events("tenant-alpha")
    assert len(staged) == 1
    assert staged[0]["is_legal_hold"] is True
    assert staged[0]["expires_at"] is None

    tombstones = store.cleanup_expired(base_time + datetime.timedelta(days=100))
    assert len(tombstones) == 0


def test_retry_with_open_task_flag_updates_existing_row():
    store = CaptureStagingStore()
    base_time = datetime.datetime(2026, 8, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)
    env = make_valid_staged_envelope(received_ts=base_time.isoformat())

    store.stage_event(env)
    res_update = store.stage_event(env, is_open_task=True)
    assert res_update["status"] == "updated_flags"
    assert res_update["is_open_task"] is True
    assert res_update["expires_at"] is None


# -------------------------------------------------------------------------
# DEFECT 5: Tombstone Records Original Expiry Date
# -------------------------------------------------------------------------

def test_tombstone_records_original_expiry_not_cleanup_time():
    store = CaptureStagingStore()
    base_time = datetime.datetime(2026, 8, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)
    env = make_valid_staged_envelope(received_ts=base_time.isoformat())
    res = store.stage_event(env)
    expected_original_expiry = res["expires_at"]

    late_cleanup_time = base_time + datetime.timedelta(days=60)
    tombstones = store.cleanup_expired(late_cleanup_time)
    assert len(tombstones) == 1
    assert tombstones[0].expired_at == expected_original_expiry
    assert tombstones[0].expired_at != late_cleanup_time.isoformat()


# -------------------------------------------------------------------------
# Core Invariants: Tenant Isolation, Recall Blindness, Dedupe, Scheduler
# -------------------------------------------------------------------------

def test_staging_persists_valid_redacted_event():
    store = CaptureStagingStore()
    env = make_valid_staged_envelope()
    res = store.stage_event(env)
    assert res["status"] == "staged"
    assert res["payload_hash"] == env["payload_hash"]

    events = store.list_staged_events("tenant-alpha")
    assert len(events) == 1
    assert events[0]["sequence"] == 1
    assert events[0]["payload"] == env["payload"]


def test_tenant_isolation_is_enforced():
    store = CaptureStagingStore()
    env_a = make_valid_staged_envelope(tenant_id="tenant-alpha", sequence=1)
    env_b = make_valid_staged_envelope(tenant_id="tenant-beta", sequence=1)

    store.stage_event(env_a)
    store.stage_event(env_b)

    events_a = store.list_staged_events("tenant-alpha")
    events_b = store.list_staged_events("tenant-beta")
    events_c = store.list_staged_events("tenant-gamma")

    assert len(events_a) == 1
    assert events_a[0]["tenant_id"] == "tenant-alpha"
    assert len(events_b) == 1
    assert events_b[0]["tenant_id"] == "tenant-beta"
    assert len(events_c) == 0


def test_recall_blindness_staged_events_never_appear_in_memory_recall(tmp_path):
    db_file = str(tmp_path / "memory.db")
    db_mgr = DatabaseManager(db_file)
    staging_store = CaptureStagingStore(db_file)

    env = make_valid_staged_envelope(tenant_id="tenant-secure", payload={"secret_text": "classified memo"})
    staging_store.stage_event(env)

    staged = staging_store.list_staged_events("tenant-secure")
    assert len(staged) == 1

    with db_mgr.get_cursor() as conn:
        semantic_count = conn.execute("SELECT count(*) FROM memories_semantic").fetchone()[0]
        episodic_count = conn.execute("SELECT count(*) FROM memories_episodic").fetchone()[0]
        assert semantic_count == 0
        assert episodic_count == 0


def test_tombstone_aware_dedupe_seam():
    store = CaptureStagingStore()
    base_time = datetime.datetime(2026, 8, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)
    env = make_valid_staged_envelope(sequence=1, received_ts=base_time.isoformat())

    store.stage_event(env)
    store.cleanup_expired(base_time + datetime.timedelta(days=31))
    assert len(store.list_staged_events("tenant-alpha")) == 0

    replay_res = store.stage_event(env)
    assert replay_res["status"] == "tombstone_duplicate"
    assert len(store.list_staged_events("tenant-alpha")) == 0

    conflicting_env = make_valid_staged_envelope(sequence=1, payload={"tampered": "content"})
    with pytest.raises(SlotConflictError) as exc_info:
        store.stage_event(conflicting_env)
    record = exc_info.value.sanitized_record()
    assert "tampered" not in str(record)
    assert record["tenant_id"] == "tenant-alpha"
    assert record["sequence"] == 1


@pytest.mark.asyncio
async def test_async_cleanup_loop():
    store = CaptureStagingStore()
    base_time = datetime.datetime(2026, 8, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)
    env = make_valid_staged_envelope(sequence=1, received_ts=base_time.isoformat())
    store.stage_event(env)

    stop_event = asyncio.Event()
    loop_task = asyncio.create_task(store.run_cleanup_loop(interval_seconds=1, stop_event=stop_event))
    await asyncio.sleep(0.05)
    stop_event.set()
    await loop_task


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("collision_key", ["safe_staging_key_0", "safe_staging_key_1", "safe_staging_key_999"])
def test_guard_never_discards_subtrees_on_inspection_key_collision(reverse, nested, collision_key):
    entries = [
        ("[redacted:credential]", {"password": "synthetic-probe-value"}),
        (collision_key, "ordinary"),
    ]
    if reverse:
        entries.reverse()
    payload = dict(entries)
    if nested:
        payload = {"items": [payload]}
    env = make_valid_staged_envelope()
    env.update(payload=payload, payload_hash=hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
               privacy_class="restricted")
    store = CaptureStagingStore()
    with pytest.raises(UnredactedInputError):
        store.stage_event(env)
    assert store.list_staged_events("tenant-alpha") == []


@pytest.mark.parametrize("payload", [
    {"secret": "probe"}, {"password": "probe"}, {"access_token": "probe"},
    {"private_key": "probe"}, {"ssn": "probe"}, {"cardnumber": "probe"},
    {"password": "one", "pass_word": "two"}, {"items": [{"password": "probe"}]},
])
def test_guard_accepts_genuine_1b_outputs(payload):
    store = CaptureStagingStore()
    env = make_valid_staged_envelope(payload=payload)
    assert store.stage_event(env)["status"] == "staged"
    assert store.list_staged_events("tenant-alpha")[0]["payload"] == env["payload"]


@pytest.mark.parametrize("interval", [0, -1, "bad", None, float("nan"), float("inf"), -float("inf")])
def test_cleanup_workers_reject_invalid_intervals_before_start(interval):
    store = CaptureStagingStore()
    with pytest.raises(ValueError):
        store.start_background_scheduler(interval)
    assert store._worker_thread is None


@pytest.mark.asyncio
@pytest.mark.parametrize("interval", [float("nan"), float("inf"), -float("inf")])
async def test_async_cleanup_rejects_nonfinite_intervals(interval):
    with pytest.raises(ValueError):
        await CaptureStagingStore().run_cleanup_loop(interval)


def test_stop_does_not_join_under_cleanup_lock():
    import threading
    store = CaptureStagingStore()
    entered = threading.Event()
    proceed = threading.Event()
    finished = threading.Event()
    result = []
    original = store.cleanup_expired

    def controlled_cleanup():
        entered.set()
        assert proceed.wait(2)
        return original()

    store.cleanup_expired = controlled_cleanup
    worker = store.start_background_scheduler(0.001)
    assert entered.wait(2)

    def stop_worker():
        result.append(store.stop_background_scheduler(timeout=2))
        finished.set()

    stopper = threading.Thread(target=stop_worker)
    stopper.start()
    assert store._stop_event.wait(2)
    acquired = store._lock.acquire(timeout=0.2)
    if acquired:
        store._lock.release()
    proceed.set()
    stopper.join(3)
    worker.join(3)
    assert acquired, "stop must release the cleanup lock before joining"
    assert finished.is_set() and result == [True]
    assert not worker.is_alive()
