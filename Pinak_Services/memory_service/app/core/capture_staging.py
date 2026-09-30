"""Recall-blind staging and 30-day raw TTL for Phase 1 slice 1c (P-87).

Contract: RFC-0001 v0.3, frozen 29 Sep 2026 at fork commit 46cf5853,
PRD v3 §5-§6, owner ruling #6 (tombstones), and Zulu spec #2e233d57.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import logging
import math
import re
import sqlite3
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from app.core.capture_envelope import MAX_PAYLOAD_DEPTH, canonical_json_bytes
from app.core.capture_redaction import (
    PRIVACY_CLASSES,
    RETENTION_CLASSES,
    RAW_STAGED_TTL_DAYS,
    redact_and_classify,
)

logger = logging.getLogger(__name__)

RFC3339_AWARE_REGEX = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)

PLACEHOLDER_KEY_PATTERN = re.compile(
    r"^\[redacted:(?:secret|credential|token|private_key|personal_data|payment_instrument)\](?:\.\d+)?$"
)


class UnredactedInputError(ValueError):
    """Raised when unredacted or unclassified payload reaches staging."""
    pass


class SlotConflictError(Exception):
    """Raised when same authenticated slot has conflicting payload hash (409)."""
    def __init__(self, key: Tuple[str, str, str, int], existing_hash: str, new_hash: str, received_ts: str):
        self.key = key
        self.existing_hash = existing_hash
        self.new_hash = new_hash
        self.received_ts = received_ts
        super().__init__(f"Conflict in slot {key}: hash {existing_hash} != {new_hash}")

    def sanitized_record(self) -> Dict[str, Any]:
        """Amendment §1: key, both hashes, receipt time, NEVER payload."""
        return {
            "tenant_id": self.key[0],
            "client_id": self.key[1],
            "session_id": self.key[2],
            "sequence": self.key[3],
            "existing_payload_hash": self.existing_hash,
            "new_payload_hash": self.new_hash,
            "received_ts": self.received_ts,
        }


@dataclass(frozen=True)
class TombstoneRecord:
    tenant_id: str
    client_id: str
    session_id: str
    sequence: int
    payload_hash: str
    expired_at: str

    def slot_key(self) -> Tuple[str, str, str, int]:
        return (self.tenant_id, self.client_id, self.session_id, self.sequence)


def parse_utc_iso(ts_str: Any) -> datetime.datetime:
    """Parse an RFC 3339 string into a timezone-aware UTC datetime.

    Strictly requires full ISO/RFC 3339 format with explicit timezone offset (Z or +/-HH:MM).
    Rejects naive datetimes, date-only strings, empty/missing strings, and malformed input.
    """
    if not isinstance(ts_str, str) or not ts_str.strip():
        raise UnredactedInputError(f"Missing or invalid received_ts: expected aware RFC 3339 string, got {ts_str!r}")

    raw = ts_str.strip()
    if not RFC3339_AWARE_REGEX.match(raw):
        raise UnredactedInputError(
            f"Invalid RFC 3339 timestamp '{ts_str}': must be fully-formed aware datetime with timezone offset (e.g. '2026-09-30T12:00:00Z' or '2026-09-30T12:00:00+05:30')"
        )

    try:
        clean = raw.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(clean)
    except Exception as err:
        raise UnredactedInputError(f"Malformed RFC 3339 datetime '{ts_str}': {err}")

    if dt.tzinfo is None:
        raise UnredactedInputError(f"Naive datetime rejected: '{ts_str}' must include timezone offset")

    return dt.astimezone(datetime.timezone.utc)


def _contains_unredacted_content(obj: Any, depth: int = 0) -> bool:
    """Inspect every original key/value without renaming or discarding subtrees."""
    if depth > MAX_PAYLOAD_DEPTH:
        raise UnredactedInputError("Payload nesting exceeds the redaction depth limit")
    if isinstance(obj, dict):
        for key, value in obj.items():
            # A genuine 1b placeholder key is already safe. Its value must still
            # be inspected, including nested raw content under forged keys.
            if not PLACEHOLDER_KEY_PATTERN.fullmatch(str(key)):
                if redact_and_classify({key: None}).report.redaction_count:
                    return True
            if _contains_unredacted_content(value, depth + 1):
                return True
        return False
    if isinstance(obj, list):
        return any(_contains_unredacted_content(value, depth + 1) for value in obj)
    return bool(redact_and_classify(obj).report.redaction_count)


class CaptureStagingStore:
    """Tenant-isolated, recall-blind staging store for captured raw events."""

    def __init__(self, db_path: str = ":memory:"):
        self.db_path = db_path
        self._lock = threading.RLock()
        self._mem_conn = None
        self._worker_thread: Optional[threading.Thread] = None
        self._stop_event: Optional[threading.Event] = None
        if db_path == ":memory:":
            self._mem_conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._mem_conn.row_factory = sqlite3.Row
        else:
            import os
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._init_schema()

    def _get_conn(self) -> sqlite3.Connection:
        if self._mem_conn is not None:
            return self._mem_conn
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS capture_staging (
                    tenant_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_id TEXT NOT NULL,
                    task_id TEXT,
                    actor TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_redacted TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    privacy_class TEXT NOT NULL,
                    retention_class TEXT NOT NULL,
                    received_ts TEXT NOT NULL,
                    expires_at TEXT,
                    is_legal_hold INTEGER NOT NULL DEFAULT 0,
                    is_open_task INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, client_id, session_id, sequence)
                );
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_capture_staging_expiry 
                ON capture_staging(tenant_id, expires_at);
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS capture_tombstones (
                    tenant_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    payload_hash TEXT NOT NULL,
                    expired_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, client_id, session_id, sequence)
                );
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_capture_tombstones_tenant 
                ON capture_tombstones(tenant_id);
            """)

    def stage_event(
        self,
        envelope: Dict[str, Any],
        is_open_task: bool = False,
        is_legal_hold: bool = False,
    ) -> Dict[str, Any]:
        privacy_class = envelope.get("privacy_class")
        if privacy_class not in PRIVACY_CLASSES:
            raise UnredactedInputError(f"Invalid or missing privacy_class: {privacy_class}")

        retention_class = envelope.get("retention_class")
        if retention_class not in RETENTION_CLASSES:
            raise UnredactedInputError(f"Invalid or missing retention_class: {retention_class}")

        payload = envelope.get("payload")
        if payload is None:
            raise UnredactedInputError("Payload is missing")

        # Inspect original content directly. No generated keys can hide values.
        if _contains_unredacted_content(payload):
            raise UnredactedInputError("Payload contains unredacted content")

        payload_str = json.dumps(payload)
        if any(h in payload_str for h in ("[redacted:secret]", "[redacted:credential]", "[redacted:private_key]", "[redacted:token]")):
            if privacy_class != "restricted":
                raise UnredactedInputError(
                    f"Privacy class '{privacy_class}' conflicts with secret content in payload"
                )
        if any(h in payload_str for h in ("[redacted:personal_data]", "[redacted:payment_instrument]")):
            if privacy_class not in ("confidential", "restricted"):
                raise UnredactedInputError(
                    f"Privacy class '{privacy_class}' conflicts with personal data in payload"
                )

        canonical_bytes = canonical_json_bytes(payload)
        expected_hash = hashlib.sha256(canonical_bytes).hexdigest()
        provided_hash = envelope.get("payload_hash")

        if provided_hash != expected_hash:
            raise UnredactedInputError(
                f"Payload hash mismatch: expected {expected_hash}, got {provided_hash}"
            )

        tenant_id = envelope["tenant_id"]
        client_id = envelope["client_id"]
        session_id = envelope["session_id"]
        sequence = int(envelope["sequence"])
        event_id = envelope["event_id"]
        task_id = envelope.get("task_id")
        actor = envelope["actor"]
        event_type = envelope["event_type"]

        # 2. Defect 2: Strict UTC parsing of received_ts; reject naive, date-only, missing
        received_ts_raw = envelope.get("received_ts")
        if received_ts_raw is None or not str(received_ts_raw).strip():
            raise UnredactedInputError(
                "Missing required envelope field 'received_ts': receipt timestamp is mandatory and must be aware RFC 3339"
            )
        rec_dt_utc = parse_utc_iso(received_ts_raw)
        received_ts = rec_dt_utc.isoformat()

        # 3. Defect: legal_hold as retention_class without boolean flag
        effective_legal_hold = bool(is_legal_hold or (retention_class == "legal_hold"))
        effective_open_task = bool(is_open_task)

        with self._lock, self._get_conn() as conn:
            # Check tombstones first
            tombstone_row = conn.execute(
                """
                SELECT expired_at, payload_hash FROM capture_tombstones
                WHERE tenant_id = ? AND client_id = ? AND session_id = ? AND sequence = ?
                """,
                (tenant_id, client_id, session_id, sequence),
            ).fetchone()

            if tombstone_row:
                if tombstone_row["payload_hash"] == expected_hash:
                    return {
                        "status": "tombstone_duplicate",
                        "tenant_id": tenant_id,
                        "session_id": session_id,
                        "sequence": sequence,
                        "expired_at": tombstone_row["expired_at"],
                    }
                else:
                    raise SlotConflictError(
                        (tenant_id, client_id, session_id, sequence),
                        tombstone_row["payload_hash"],
                        expected_hash,
                        received_ts,
                    )

            # Check existing staging
            existing = conn.execute(
                """
                SELECT payload_hash, is_legal_hold, is_open_task, expires_at FROM capture_staging
                WHERE tenant_id = ? AND client_id = ? AND session_id = ? AND sequence = ?
                """,
                (tenant_id, client_id, session_id, sequence),
            ).fetchone()

            if existing:
                if existing["payload_hash"] == expected_hash:
                    new_hold = 1 if (effective_legal_hold or existing["is_legal_hold"]) else 0
                    new_open = 1 if (effective_open_task or existing["is_open_task"]) else 0
                    
                    if (new_hold and not existing["is_legal_hold"]) or (new_open and not existing["is_open_task"]):
                        conn.execute(
                            """
                            UPDATE capture_staging
                            SET is_legal_hold = ?, is_open_task = ?, expires_at = NULL
                            WHERE tenant_id = ? AND client_id = ? AND session_id = ? AND sequence = ?
                            """,
                            (new_hold, new_open, tenant_id, client_id, session_id, sequence),
                        )
                        return {
                            "status": "updated_flags",
                            "tenant_id": tenant_id,
                            "session_id": session_id,
                            "sequence": sequence,
                            "is_legal_hold": bool(new_hold),
                            "is_open_task": bool(new_open),
                            "expires_at": None,
                        }

                    return {
                        "status": "duplicate",
                        "tenant_id": tenant_id,
                        "session_id": session_id,
                        "sequence": sequence,
                        "is_legal_hold": bool(existing["is_legal_hold"]),
                        "is_open_task": bool(existing["is_open_task"]),
                        "expires_at": existing["expires_at"],
                    }
                else:
                    raise SlotConflictError(
                        (tenant_id, client_id, session_id, sequence),
                        existing["payload_hash"],
                        expected_hash,
                        received_ts,
                    )

            expires_at = None
            if not effective_legal_hold and not effective_open_task:
                expires_dt_utc = rec_dt_utc + datetime.timedelta(days=RAW_STAGED_TTL_DAYS)
                expires_at = expires_dt_utc.isoformat()

            conn.execute(
                """
                INSERT INTO capture_staging (
                    tenant_id, client_id, session_id, sequence, event_id, task_id,
                    actor, event_type, payload_redacted, payload_hash, privacy_class,
                    retention_class, received_ts, expires_at, is_legal_hold, is_open_task,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tenant_id,
                    client_id,
                    session_id,
                    sequence,
                    event_id,
                    task_id,
                    actor,
                    event_type,
                    payload_str,
                    expected_hash,
                    privacy_class,
                    retention_class,
                    received_ts,
                    expires_at,
                    1 if effective_legal_hold else 0,
                    1 if effective_open_task else 0,
                    datetime.datetime.now(datetime.timezone.utc).isoformat(),
                ),
            )

        return {
            "status": "staged",
            "tenant_id": tenant_id,
            "session_id": session_id,
            "sequence": sequence,
            "event_id": event_id,
            "payload_hash": expected_hash,
            "expires_at": expires_at,
            "is_legal_hold": effective_legal_hold,
            "is_open_task": effective_open_task,
        }

    def release_legal_hold(self, tenant_id: str, client_id: str, session_id: str, sequence: int) -> bool:
        """Release an active legal hold on a staged event."""
        with self._lock, self._get_conn() as conn:
            row = conn.execute(
                """
                SELECT received_ts, is_open_task FROM capture_staging
                WHERE tenant_id = ? AND client_id = ? AND session_id = ? AND sequence = ?
                """,
                (tenant_id, client_id, session_id, sequence),
            ).fetchone()
            if not row:
                return False

            is_open_task = row["is_open_task"]
            new_expires_at = None
            if not is_open_task:
                rec_dt = parse_utc_iso(row["received_ts"])
                new_expires_at = (rec_dt + datetime.timedelta(days=RAW_STAGED_TTL_DAYS)).isoformat()

            conn.execute(
                """
                UPDATE capture_staging
                SET is_legal_hold = 0, expires_at = ?
                WHERE tenant_id = ? AND client_id = ? AND session_id = ? AND sequence = ?
                """,
                (new_expires_at, tenant_id, client_id, session_id, sequence),
            )
            return True

    def close_task(self, tenant_id: str, client_id: str, session_id: str, sequence: int) -> bool:
        """Close an open task association on a staged event."""
        with self._lock, self._get_conn() as conn:
            row = conn.execute(
                """
                SELECT received_ts, is_legal_hold FROM capture_staging
                WHERE tenant_id = ? AND client_id = ? AND session_id = ? AND sequence = ?
                """,
                (tenant_id, client_id, session_id, sequence),
            ).fetchone()
            if not row:
                return False

            is_legal_hold = row["is_legal_hold"]
            new_expires_at = None
            if not is_legal_hold:
                rec_dt = parse_utc_iso(row["received_ts"])
                new_expires_at = (rec_dt + datetime.timedelta(days=RAW_STAGED_TTL_DAYS)).isoformat()

            conn.execute(
                """
                UPDATE capture_staging
                SET is_open_task = 0, expires_at = ?
                WHERE tenant_id = ? AND client_id = ? AND session_id = ? AND sequence = ?
                """,
                (new_expires_at, tenant_id, client_id, session_id, sequence),
            )
            return True

    def list_staged_events(self, tenant_id: str) -> List[Dict[str, Any]]:
        with self._lock, self._get_conn() as conn:
            rows = conn.execute(
                """
                SELECT tenant_id, client_id, session_id, sequence, event_id, task_id,
                       actor, event_type, payload_redacted, payload_hash, privacy_class,
                       retention_class, received_ts, expires_at, is_legal_hold, is_open_task
                FROM capture_staging
                WHERE tenant_id = ?
                ORDER BY session_id, sequence
                """,
                (tenant_id,),
            ).fetchall()

            return [
                {
                    "tenant_id": row["tenant_id"],
                    "client_id": row["client_id"],
                    "session_id": row["session_id"],
                    "sequence": row["sequence"],
                    "event_id": row["event_id"],
                    "task_id": row["task_id"],
                    "actor": row["actor"],
                    "event_type": row["event_type"],
                    "payload": json.loads(row["payload_redacted"]),
                    "payload_hash": row["payload_hash"],
                    "privacy_class": row["privacy_class"],
                    "retention_class": row["retention_class"],
                    "received_ts": row["received_ts"],
                    "expires_at": row["expires_at"],
                    "is_legal_hold": bool(row["is_legal_hold"]),
                    "is_open_task": bool(row["is_open_task"]),
                }
                for row in rows
            ]

    def cleanup_expired(self, current_time: Optional[datetime.datetime] = None) -> List[TombstoneRecord]:
        """Purge raw staged events older than 30 days and emit audit tombstones."""
        if current_time is None:
            current_time = datetime.datetime.now(datetime.timezone.utc)
        elif current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=datetime.timezone.utc)
        else:
            current_time = current_time.astimezone(datetime.timezone.utc)

        tombstones: List[TombstoneRecord] = []
        with self._lock, self._get_conn() as conn:
            candidate_rows = conn.execute(
                """
                SELECT tenant_id, client_id, session_id, sequence, payload_hash, expires_at
                FROM capture_staging
                WHERE expires_at IS NOT NULL 
                  AND is_legal_hold = 0 
                  AND is_open_task = 0
                """
            ).fetchall()

            to_expire = []
            for r in candidate_rows:
                exp_str = r["expires_at"]
                try:
                    exp_dt = parse_utc_iso(exp_str)
                except Exception:
                    continue
                if exp_dt <= current_time:
                    to_expire.append(r)

            for r in to_expire:
                orig_expiry = r["expires_at"]
                t = TombstoneRecord(
                    tenant_id=r["tenant_id"],
                    client_id=r["client_id"],
                    session_id=r["session_id"],
                    sequence=r["sequence"],
                    payload_hash=r["payload_hash"],
                    expired_at=orig_expiry,
                )
                tombstones.append(t)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO capture_tombstones (
                        tenant_id, client_id, session_id, sequence, payload_hash, expired_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (t.tenant_id, t.client_id, t.session_id, t.sequence, t.payload_hash, t.expired_at),
                )
                conn.execute(
                    """
                    DELETE FROM capture_staging
                    WHERE tenant_id = ? AND client_id = ? AND session_id = ? AND sequence = ?
                    """,
                    (t.tenant_id, t.client_id, t.session_id, t.sequence),
                )

        return tombstones

    def list_tombstones(self, tenant_id: str) -> List[TombstoneRecord]:
        with self._lock, self._get_conn() as conn:
            rows = conn.execute(
                """
                SELECT tenant_id, client_id, session_id, sequence, payload_hash, expired_at
                FROM capture_tombstones
                WHERE tenant_id = ?
                ORDER BY session_id, sequence
                """,
                (tenant_id,),
            ).fetchall()
            return [
                TombstoneRecord(
                    tenant_id=r["tenant_id"],
                    client_id=r["client_id"],
                    session_id=r["session_id"],
                    sequence=r["sequence"],
                    payload_hash=r["payload_hash"],
                    expired_at=r["expired_at"],
                )
                for r in rows
            ]

    async def run_cleanup_loop(self, interval_seconds: int = 3600, stop_event: Optional[asyncio.Event] = None) -> None:
        """Async periodic cleanup loop."""
        if not isinstance(interval_seconds, (int, float)) or not math.isfinite(interval_seconds) or interval_seconds <= 0:
            raise ValueError(f"interval_seconds must be a positive number, got {interval_seconds}")
        logger.info(f"Capture staging TTL cleanup loop started (interval: {interval_seconds}s)")
        while stop_event is None or not stop_event.is_set():
            try:
                purged = self.cleanup_expired()
                if purged:
                    logger.info(f"Purged {len(purged)} expired staged events into tombstones")
            except Exception as e:
                logger.error(f"Error in capture staging TTL cleanup: {e}", exc_info=True)
            try:
                if stop_event:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
                else:
                    await asyncio.sleep(interval_seconds)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                break

    def start_background_scheduler(self, interval_seconds: float = 3600) -> threading.Thread:
        """Start a daemon thread running periodic TTL cleanup."""
        if not isinstance(interval_seconds, (int, float)) or not math.isfinite(interval_seconds) or interval_seconds <= 0:
            raise ValueError(f"interval_seconds must be a positive number, got {interval_seconds}")
        with self._lock:
            if self._worker_thread is not None and self._worker_thread.is_alive():
                raise RuntimeError("Background TTL cleanup scheduler is already running")

            self._stop_event = threading.Event()

            def _worker():
                while not self._stop_event.wait(timeout=interval_seconds):
                    try:
                        self.cleanup_expired()
                    except Exception as e:
                        logger.error(f"Error in background TTL worker: {e}", exc_info=True)

            t = threading.Thread(target=_worker, daemon=True, name="CaptureStagingTTLWorker")
            self._worker_thread = t
            t.start()
            return t

    def stop_background_scheduler(self, timeout: float = 5.0) -> bool:
        """Stop running daemon thread."""
        with self._lock:
            stop_event = self._stop_event
            worker_thread = self._worker_thread
            if stop_event is not None:
                stop_event.set()
        # Cleanup also needs _lock. Never hold it while waiting for the worker.
        if worker_thread is not None:
            worker_thread.join(timeout=timeout)
            return not worker_thread.is_alive()
        return True
