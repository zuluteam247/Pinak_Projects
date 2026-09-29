"""Slice 1a: envelope schema, auth and tenant binding (P-85).

These tests pin the CW 5.1 order, the fail-closed codes in CW 8 and the phase
gate in CW 7.4. Nothing here asserts that capture accepts anything: a
schema-valid envelope must still be refused with 503 capture_disabled.
"""

import copy

import pytest

from app.core.capture_envelope import (
    MAX_ENVELOPE_BYTES,
    SUPPORTED_ENVELOPE_VERSIONS,
    CaptureRejection,
    canonical_json_bytes,
    validate_capture_envelope,
)

CLAIM = {
    "tenant_id": "tenant-alpha",
    "agent_id": "agent-fo",
    "client_id": "gemini-cli",
}


def _envelope(**overrides):
    envelope = {
        "tenant_id": "tenant-alpha",
        "agent_id": "agent-fo",
        "client_id": "gemini-cli",
        "client_version": "1.4.2",
        "session_id": "01J9ZQ7X8N2K4M6P8R0T2V4W6X",
        "task_id": "task-4471",
        "event_id": "01J9ZQ7X8N2K4M6P8R0T2V4W6Y",
        "sequence": 7,
        "client_ts": "2026-09-29T13:45:00Z",
        "actor": "agent",
        "event_type": "message",
        "payload": {"text": "hello"},
        "payload_hash": "a" * 64,
        "boundary_source": "session_start",
        "source": "cli",
        "provenance": {"captured_by": "cli-hook"},
        "envelope_version": "0.3",
    }
    envelope.update(overrides)
    for key, value in list(envelope.items()):
        if value is None:
            del envelope[key]
    return envelope


def _reject(envelope, claim=None):
    with pytest.raises(CaptureRejection) as excinfo:
        validate_capture_envelope(envelope, claim or CLAIM)
    return excinfo.value


def test_valid_envelope_passes_validation_and_reports_canonical_size():
    result = validate_capture_envelope(_envelope(), CLAIM)
    assert result.envelope_version == "0.3"
    assert result.tenant_id == "tenant-alpha"
    assert result.sequence == 7
    assert result.canonical_bytes == len(canonical_json_bytes(_envelope()))


# --- CW 2.2 identity binding, checked before anything else ---------------


@pytest.mark.parametrize("field_name", ["tenant_id", "agent_id", "client_id"])
def test_identity_mismatch_is_403_auth_binding_mismatch(field_name):
    rejection = _reject(_envelope(**{field_name: "someone-else"}))
    assert rejection.status_code == 403
    assert rejection.code == "auth_binding_mismatch"
    assert rejection.extra["field"] == field_name


def test_identity_binding_runs_before_schema_and_version_checks():
    # A body that is wrong in three ways still fails on identity first.
    rejection = _reject(_envelope(tenant_id="other", envelope_version="9.9", actor="ghost"))
    assert rejection.code == "auth_binding_mismatch"


def test_missing_identity_fields_are_schema_violations_not_silent_binding():
    rejection = _reject(_envelope(tenant_id=None))
    assert rejection.status_code == 422
    assert "tenant_id" in rejection.extra["missing_fields"]


# --- CW 5.3 envelope_version ---------------------------------------------


def test_unknown_envelope_version_is_400_and_names_supported_range():
    rejection = _reject(_envelope(envelope_version="0.9"))
    assert rejection.status_code == 400
    assert rejection.code == "unsupported_envelope_version"
    assert rejection.extra["supported_envelope_versions"] == list(SUPPORTED_ENVELOPE_VERSIONS)


def test_service_accepts_n_and_n_minus_one():
    for version in SUPPORTED_ENVELOPE_VERSIONS:
        assert validate_capture_envelope(
            _envelope(envelope_version=version), CLAIM
        ).envelope_version == version


# --- CW 4 field table and CW 5.2 closed enums -----------------------------


def test_missing_required_field_is_422_schema_violation():
    rejection = _reject(_envelope(provenance=None))
    assert rejection.status_code == 422
    assert rejection.code == "schema_violation"
    assert rejection.extra["missing_fields"] == ["provenance"]


def test_unknown_field_is_rejected_not_ignored():
    rejection = _reject(_envelope(body="legacy v0.2 name"))
    assert rejection.extra["unknown_fields"] == ["body"]


@pytest.mark.parametrize("field_name", ["received_ts", "privacy_class", "retention_class"])
def test_client_may_not_set_service_assigned_fields(field_name):
    rejection = _reject(_envelope(**{field_name: "internal"}))
    assert rejection.status_code == 422
    assert rejection.code == "client_assigned_service_field"
    assert rejection.extra["field"] == field_name


def test_unknown_actor_is_422_unknown_enum_value():
    rejection = _reject(_envelope(actor="ghost"))
    assert rejection.code == "unknown_enum_value"
    assert rejection.extra["field"] == "actor"


def test_unknown_event_type_is_422_unknown_enum_value():
    assert _reject(_envelope(event_type="thought")).extra["field"] == "event_type"


def test_open_enums_are_typed_but_membership_is_not_enforced_yet():
    # CW 4.6: boundary_source and source member lists are not owner-ratified,
    # so 1a must not implement them as approved values.
    validate_capture_envelope(
        _envelope(boundary_source="not_yet_ratified", source="not_yet_ratified"), CLAIM
    )
    assert _reject(_envelope(source="")).extra["field"] == "source"


def test_event_id_and_session_id_must_be_ulids():
    assert _reject(_envelope(event_id="550e8400-e29b-41d4-a716-446655440000")).extra["field"] == "event_id"
    assert _reject(_envelope(session_id="not-a-ulid")).extra["field"] == "session_id"


def test_sequence_must_be_a_u64():
    assert _reject(_envelope(sequence=-1)).extra["field"] == "sequence"
    assert _reject(_envelope(sequence=True)).extra["field"] == "sequence"
    assert _reject(_envelope(sequence="7")).extra["field"] == "sequence"
    assert validate_capture_envelope(_envelope(sequence=0), CLAIM).sequence == 0


def test_client_ts_must_be_rfc3339():
    assert _reject(_envelope(client_ts="29-09-2026 13:45")).extra["field"] == "client_ts"


def test_payload_hash_must_be_lowercase_hex_sha256():
    assert _reject(_envelope(payload_hash="A" * 64)).extra["field"] == "payload_hash"
    assert _reject(_envelope(payload_hash="abc")).extra["field"] == "payload_hash"


def test_hidden_reasoning_is_not_an_envelope_field():
    # CW 4.4: no adapter may add one, and the rejection names the key only.
    rejection = _reject(_envelope(payload={"text": "hi", "hidden_reasoning": "secret"}))
    assert rejection.extra["forbidden_keys"] == ["hidden_reasoning"]
    assert "secret" not in str(rejection.as_detail())


# --- CW 5.4 size ceiling, counted on the canonical envelope ---------------


def test_size_ceiling_counts_canonical_envelope_bytes():
    base = _envelope()
    overhead = len(canonical_json_bytes(base)) - len(base["payload"]["text"])
    at_limit = copy.deepcopy(base)
    at_limit["payload"]["text"] = "x" * (MAX_ENVELOPE_BYTES - overhead)
    assert len(canonical_json_bytes(at_limit)) == MAX_ENVELOPE_BYTES
    assert validate_capture_envelope(at_limit, CLAIM).canonical_bytes == MAX_ENVELOPE_BYTES

    over = copy.deepcopy(at_limit)
    over["payload"]["text"] += "x"
    assert len(canonical_json_bytes(over)) == MAX_ENVELOPE_BYTES + 1
    rejection = _reject(over)
    assert rejection.status_code == 422
    assert rejection.code == "event_too_large"
    assert rejection.extra["max_bytes"] == 262144


def test_size_check_runs_after_schema_validation():
    # CW 5.1: schema, then size. An oversize envelope that is also malformed
    # fails on the schema, not the ceiling.
    over = _envelope(actor="ghost")
    over["payload"]["text"] = "x" * (MAX_ENVELOPE_BYTES * 2)
    assert _reject(over).code == "unknown_enum_value"


def test_canonical_form_sorts_keys_and_drops_insignificant_whitespace():
    assert canonical_json_bytes({"b": 1, "a": [1, {"d": 2, "c": 3}]}) == \
        b'{"a":[1,{"c":3,"d":2}],"b":1}'


# --- CW 5.5 error bodies carry no payload content -------------------------


def test_error_bodies_never_echo_payload_content():
    secret = "sk-live-do-not-log"
    for envelope in (
        _envelope(actor="ghost", payload={"text": secret}),
        _envelope(envelope_version="9.9", payload={"text": secret}),
        _envelope(tenant_id="other", payload={"text": secret}),
    ):
        rejection = _reject(envelope)
        assert secret not in str(rejection.as_detail())
        assert secret not in str(rejection)


def test_non_object_envelope_is_rejected():
    assert _reject(["not", "an", "object"]).status_code == 422
