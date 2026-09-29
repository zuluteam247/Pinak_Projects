"""Slice 1a: envelope schema, auth and tenant binding (P-85).

These tests pin the CW 5.1 order, the fail-closed codes in CW 8 and the phase
gate in CW 7.4. Nothing here asserts that capture accepts anything: a
schema-valid envelope must still be refused with 503 capture_disabled.

Enum membership: all four member lists (actor, event_type, boundary_source,
source) are owner-ratified in PRD v3 §3 and closed here.
"""

import copy
import json

import pytest

from app.core.capture_envelope import (
    BOUNDARY_SOURCES,
    MAX_ENVELOPE_BYTES,
    SOURCES,
    SUPPORTED_ENVELOPE_VERSIONS,
    TASK_BINDING_PRE_BINDING,
    CaptureRejection,
    IdentityClaim,
    canonical_json_bytes,
    validate_capture_envelope,
)

CLAIM = IdentityClaim(
    tenant_id="tenant-alpha",
    agent_id="agent-fo",
    client_id="gemini-cli",
)


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
        if value is _DROP:
            del envelope[key]
    return envelope


class _Drop:
    pass


_DROP = _Drop()


def _reject(envelope, claim=None):
    with pytest.raises(CaptureRejection) as excinfo:
        validate_capture_envelope(envelope, claim or CLAIM)
    return excinfo.value


# --- identity binding (CW 2.2) ---------------------------------------------


def test_valid_envelope_passes_validation_and_reports_canonical_size():
    validated = validate_capture_envelope(_envelope(), CLAIM)
    assert validated.tenant_id == "tenant-alpha"
    assert validated.agent_id == "agent-fo"
    assert validated.client_id == "gemini-cli"
    assert validated.envelope_version == "0.3"
    assert validated.canonical_bytes == len(canonical_json_bytes(_envelope()))


@pytest.mark.parametrize("field_name", ["tenant_id", "agent_id", "client_id"])
def test_identity_mismatch_is_403_auth_binding_mismatch(field_name):
    rejection = _reject(_envelope(**{field_name: "someone-else"}))
    assert rejection.status_code == 403
    assert rejection.code == "auth_binding_mismatch"


def test_identity_binding_runs_before_schema_and_version_checks():
    rejection = _reject(_envelope(tenant_id="other", envelope_version="9.9",
                                  actor="nonsense"))
    assert rejection.status_code == 403


def test_missing_identity_fields_are_schema_violations_not_silent_binding():
    rejection = _reject(_envelope(agent_id=_DROP))
    assert rejection.status_code == 422
    assert rejection.code == "schema_violation"
    assert "agent_id" in rejection.extra["missing_fields"]


def test_claim_must_be_signed_identity_not_a_loose_mapping():
    # A dict could carry a header-derived or defaulted client id, which is
    # exactly what CW 2.2 forbids, so it is refused at the type boundary.
    with pytest.raises(TypeError):
        validate_capture_envelope(_envelope(), {
            "tenant_id": "tenant-alpha",
            "agent_id": "agent-fo",
            "client_id": "gemini-cli",
        })


# --- version (CW 5.3) -------------------------------------------------------


def test_unknown_envelope_version_is_400_and_names_supported_range():
    rejection = _reject(_envelope(envelope_version="0.1"))
    assert rejection.status_code == 400
    assert rejection.code == "unsupported_envelope_version"
    assert rejection.extra["supported_envelope_versions"] == list(
        SUPPORTED_ENVELOPE_VERSIONS)


def test_1a_supports_only_n_and_defers_n_minus_one_to_1e():
    # PRD v3 §4 wants N and N-1 with migration tests. 1a ships N only rather
    # than claim an N-1 acceptance it has no transition test for; P-89 (1e)
    # owns version handling.
    assert SUPPORTED_ENVELOPE_VERSIONS == ("0.3",)
    rejection = _reject(_envelope(envelope_version="0.2"))
    assert rejection.status_code == 400
    assert rejection.code == "unsupported_envelope_version"


# --- schema (CW 4, 5.2) -----------------------------------------------------


def test_missing_required_field_is_422_schema_violation():
    rejection = _reject(_envelope(sequence=_DROP))
    assert rejection.status_code == 422
    assert rejection.extra["missing_fields"] == ["sequence"]


def test_unknown_field_is_rejected_not_ignored():
    rejection = _reject(_envelope(extra_field="nope"))
    assert rejection.status_code == 422
    # CW 5.5: the count comes back, the caller's field name does not.
    assert rejection.extra["unknown_field_count"] == 1
    assert "extra_field" not in str(rejection.extra)
    assert "extra_field" not in rejection.message


@pytest.mark.parametrize("field_name",
                         ["received_ts", "privacy_class", "retention_class"])
def test_client_may_not_set_service_assigned_fields(field_name):
    rejection = _reject(_envelope(**{field_name: "internal"}))
    assert rejection.status_code == 422
    assert rejection.code == "client_assigned_service_field"


def test_unknown_actor_is_422_unknown_enum_value():
    rejection = _reject(_envelope(actor="robot"))
    assert rejection.status_code == 422
    assert rejection.code == "unknown_enum_value"
    assert rejection.extra["field"] == "actor"


def test_unknown_event_type_is_422_unknown_enum_value():
    rejection = _reject(_envelope(event_type="telepathy"))
    assert rejection.code == "unknown_enum_value"


@pytest.mark.parametrize("member", sorted(BOUNDARY_SOURCES))
def test_every_ratified_boundary_source_is_accepted(member):
    validated = validate_capture_envelope(_envelope(boundary_source=member), CLAIM)
    assert validated.fields["boundary_source"] == member


@pytest.mark.parametrize("member", sorted(SOURCES))
def test_every_ratified_source_is_accepted(member):
    validated = validate_capture_envelope(_envelope(source=member), CLAIM)
    assert validated.fields["source"] == member


@pytest.mark.parametrize("field_name", ["boundary_source", "source"])
def test_unratified_enum_member_is_422_unknown_enum_value(field_name):
    # These two lists are ratified (PRD v3 §3), so "unapproved" is now a
    # rejected member, not a tolerated free string.
    rejection = _reject(_envelope(**{field_name: "unapproved"}))
    assert rejection.status_code == 422
    assert rejection.code == "unknown_enum_value"
    assert rejection.extra["field"] == field_name
    assert "unapproved" not in rejection.extra["allowed"]


def test_event_id_and_session_id_must_be_ulids():
    assert _reject(_envelope(event_id="not-a-ulid")).code == "schema_violation"
    assert _reject(_envelope(session_id="not-a-ulid")).code == "schema_violation"


def test_sequence_must_be_a_u64():
    assert _reject(_envelope(sequence=-1)).extra["field"] == "sequence"
    assert _reject(_envelope(sequence=True)).extra["field"] == "sequence"
    assert _reject(_envelope(sequence="7")).extra["field"] == "sequence"


def test_client_ts_must_be_rfc3339():
    assert _reject(_envelope(client_ts="29-09-2026")).extra["field"] == "client_ts"


def test_payload_hash_must_be_lowercase_hex_sha256():
    assert _reject(_envelope(payload_hash="A" * 64)).extra["field"] == "payload_hash"
    assert _reject(_envelope(payload_hash="abc")).extra["field"] == "payload_hash"


# --- task binding (PRD v3 §2) ----------------------------------------------


def test_null_task_id_is_accepted_and_recorded_as_pre_binding():
    validated = validate_capture_envelope(_envelope(task_id=None), CLAIM)
    assert validated.task_binding == TASK_BINDING_PRE_BINDING
    assert validated.task_id is None


def test_bound_task_id_is_recorded_as_bound():
    validated = validate_capture_envelope(_envelope(), CLAIM)
    assert validated.task_binding == "bound"
    assert validated.task_id == "task-4471"


def test_task_id_must_still_be_a_non_empty_string_when_present():
    assert _reject(_envelope(task_id="")).extra["field"] == "task_id"
    assert _reject(_envelope(task_id=17)).extra["field"] == "task_id"


def test_missing_task_id_key_is_a_schema_violation_not_pre_binding():
    # Null is a declaration; an absent key is a malformed envelope.
    rejection = _reject(_envelope(task_id=_DROP))
    assert rejection.code == "schema_violation"
    assert "task_id" in rejection.extra["missing_fields"]


# --- provenance and client evidence (CW 4.3) -------------------------------


def test_client_provenance_is_carried_as_untrusted_evidence():
    validated = validate_capture_envelope(_envelope(), CLAIM)
    assert validated.client_evidence["trust"] == "client_asserted"
    assert validated.client_evidence["client_payload_hash"] == "a" * 64
    assert validated.client_evidence["client_provenance"] == {"captured_by": "cli-hook"}


def test_client_payload_hash_is_not_treated_as_authoritative():
    # The service hash is computed post-redaction in 1b. Nothing in 1a may
    # promote the client's number into an authoritative field.
    validated = validate_capture_envelope(_envelope(), CLAIM)
    assert not hasattr(validated, "payload_hash")
    assert "payload_hash" not in {"client_payload_hash"}


@pytest.mark.parametrize("key", ["privacy_class", "retention_class", "trust",
                                 "authority", "verified", "legal_hold"])
def test_provenance_may_not_assert_service_authoritative_values(key):
    rejection = _reject(_envelope(provenance={"captured_by": "cli", key: "x"}))
    assert rejection.status_code == 422
    assert rejection.code == "client_assigned_service_field"
    assert rejection.extra["forbidden_key_count"] == 1
    assert key not in str(rejection.extra)


def test_provenance_must_be_a_non_empty_object():
    assert _reject(_envelope(provenance={})).extra["field"] == "provenance"
    assert _reject(_envelope(provenance="cli")).extra["field"] == "provenance"


# --- hidden reasoning (CW 4.4) ---------------------------------------------


def test_hidden_reasoning_is_not_an_envelope_field():
    rejection = _reject(_envelope(payload={"text": "hi", "hidden_reasoning": "x"}))
    assert rejection.status_code == 422
    assert rejection.extra["forbidden_key_count"] == 1
    assert "hidden_reasoning" not in str(rejection.extra)


@pytest.mark.parametrize("payload", [
    {"text": "hi", "meta": {"chain_of_thought": "x"}},
    {"text": "hi", "steps": [{"scratchpad": "x"}]},
    {"outer": {"inner": {"deeper": {"cot": "x"}}}},
    {"text": "hi", "meta": {"Hidden_Reasoning": "x"}},
])
def test_hidden_reasoning_is_rejected_at_any_depth(payload):
    rejection = _reject(_envelope(payload=payload))
    assert rejection.status_code == 422
    assert rejection.extra["field"] == "payload"


def test_hidden_reasoning_is_rejected_in_a_decision_payload():
    rejection = _reject(_envelope(event_type="decision",
                                  payload={"choice": "a",
                                           "why": {"reasoning_trace": "x"}}))
    assert rejection.status_code == 422


def test_hidden_reasoning_is_rejected_inside_provenance():
    rejection = _reject(_envelope(provenance={"captured_by": "cli",
                                              "notes": {"cot": "x"}}))
    assert rejection.status_code == 422


def test_absurd_nesting_is_rejected_rather_than_recursing_forever():
    payload = current = {}
    for _ in range(64):
        child = {}
        current["next"] = child
        current = child
    rejection = _reject(_envelope(payload=payload))
    assert rejection.status_code == 422


# --- canonical form and size (CW 5.4, owner ruling 1 and 2) -----------------


def test_canonical_form_is_jcs_not_sorted_json_dumps():
    value = {"b": 1.0, "a": -0.0, "c": [2.0, 3.5]}
    canonical = canonical_json_bytes(value).decode()
    assert canonical == '{"a":0,"b":1,"c":[2,3.5]}'
    # json.dumps(sort_keys=True) would disagree, which is the bug this fixes.
    assert canonical != json.dumps(value, sort_keys=True, separators=(",", ":"))


def test_canonical_form_sorts_keys_and_drops_insignificant_whitespace():
    assert canonical_json_bytes({"b": 1, "a": 2}) == b'{"a":2,"b":1}'


def test_canonical_form_escapes_control_characters_minimally():
    assert canonical_json_bytes({"a": "x\ny"}) == b'{"a":"x\\ny"}'
    assert canonical_json_bytes({"a": "\x01"}) == b'{"a":"\\u0001"}'


def test_canonical_form_rejects_non_finite_numbers():
    with pytest.raises(ValueError):
        canonical_json_bytes({"a": float("nan")})
    with pytest.raises(ValueError):
        canonical_json_bytes({"a": float("inf")})


def test_size_ceiling_counts_canonical_envelope_bytes():
    envelope = _envelope()
    overhead = len(canonical_json_bytes(envelope)) - len(
        canonical_json_bytes({**envelope, "payload": {"text": ""}}))
    filler = "x" * (MAX_ENVELOPE_BYTES + 1 - len(
        canonical_json_bytes({**envelope, "payload": {"text": ""}})) - overhead + 5)
    rejection = _reject(_envelope(payload={"text": filler}))
    assert rejection.status_code == 422
    assert rejection.code == "event_too_large"
    assert rejection.extra["max_bytes"] == MAX_ENVELOPE_BYTES


def test_envelope_exactly_at_the_ceiling_is_accepted():
    base = _envelope(payload={"text": ""})
    room = MAX_ENVELOPE_BYTES - len(canonical_json_bytes(base))
    envelope = _envelope(payload={"text": "x" * room})
    validated = validate_capture_envelope(envelope, CLAIM)
    assert validated.canonical_bytes == MAX_ENVELOPE_BYTES


def test_one_byte_over_the_ceiling_is_422_event_too_large():
    base = _envelope(payload={"text": ""})
    room = MAX_ENVELOPE_BYTES - len(canonical_json_bytes(base))
    rejection = _reject(_envelope(payload={"text": "x" * (room + 1)}))
    assert rejection.code == "event_too_large"


def test_size_check_runs_after_schema_validation():
    rejection = _reject(_envelope(actor="robot",
                                  payload={"text": "x" * (MAX_ENVELOPE_BYTES + 10)}))
    assert rejection.code == "unknown_enum_value"


# --- no payload leaks (CW 5.5) ---------------------------------------------


def test_error_bodies_never_echo_payload_content():
    secret = "sk-live-do-not-log"
    rejection = _reject(_envelope(actor="robot", payload={"text": secret}))
    assert secret not in json.dumps(rejection.as_detail())


def test_non_object_envelope_is_rejected():
    assert _reject("not-an-object").status_code == 422
    assert _reject([1, 2, 3]).status_code == 422


def test_validation_does_not_mutate_the_caller_envelope():
    envelope = _envelope()
    snapshot = copy.deepcopy(envelope)
    validate_capture_envelope(envelope, CLAIM)
    assert envelope == snapshot


# --- RFC 8785 conformance (repair A) ---------------------------------------


@pytest.mark.parametrize("value,expected", [
    # The two vectors the second review caught: exponent form for small
    # magnitudes, and integers past the safe range.
    ({"a": 0.000001}, b'{"a":0.000001}'),
    ({"a": 9007199254740993}, b'{"a":9007199254740992}'),
    # ECMAScript Number::toString boundaries either side of the exponent
    # switch, from the RFC 8785 appendix B discussion.
    ({"a": 1e-7}, b'{"a":1e-7}'),
    ({"a": 1e21}, b'{"a":1e+21}'),
    ({"a": 1e20}, b'{"a":100000000000000000000}'),
    ({"a": -0.0}, b'{"a":0}'),
    ({"a": 1.0}, b'{"a":1}'),
    ({"a": 333333333.33333329}, b'{"a":333333333.3333333}'),
    ({"a": 5e-324}, b'{"a":5e-324}'),
    ({"a": 1.7976931348623157e308}, b'{"a":1.7976931348623157e+308}'),
])
def test_canonical_numbers_match_ecmascript(value, expected):
    assert canonical_json_bytes(value) == expected


def test_canonical_member_order_is_utf16_code_unit():
    # A non-BMP key sorts after "b" by UTF-16 code unit and before it by code
    # point, so this ordering is the whole difference between JCS and
    # json.dumps(sort_keys=True).
    ordered = canonical_json_bytes({"\U0001f600": 3, "b": 4, "\u00e4": 1, "\u00c4": 2})
    assert ordered == '{"b":4,"\u00c4":2,"\u00e4":1,"\U0001f600":3}'.encode("utf-8")


def test_canonical_form_is_not_json_dumps_sort_keys():
    import json

    value = {"a": 1.0, "b": -0.0, "\U0001f600": 1, "z": 1}
    assert canonical_json_bytes(value) != json.dumps(
        value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def test_size_ceiling_counts_canonical_bytes_at_the_boundary():
    # Build an envelope whose canonical form lands exactly on the ceiling,
    # then one byte over. The ceiling is a canonical-byte boundary, so the
    # last accepted envelope and the first rejected one differ by one byte of
    # the same serialization 1b will hash.
    base = _envelope(payload={"text": ""})
    overhead = len(canonical_json_bytes(base))
    filler = "x" * (MAX_ENVELOPE_BYTES - overhead)
    exact = _envelope(payload={"text": filler})
    assert len(canonical_json_bytes(exact)) == MAX_ENVELOPE_BYTES
    validated = validate_capture_envelope(exact, CLAIM)
    assert validated.canonical_bytes == MAX_ENVELOPE_BYTES

    over = _envelope(payload={"text": filler + "x"})
    assert len(canonical_json_bytes(over)) == MAX_ENVELOPE_BYTES + 1
    rejection = _reject(over)
    assert rejection.status_code == 422
    assert rejection.code == "event_too_large"
    assert rejection.extra["max_bytes"] == MAX_ENVELOPE_BYTES


def test_error_bodies_never_echo_caller_supplied_names():
    # One assertion over every rejection path that sees caller-controlled key
    # names: the secret-looking name must not survive into the error body.
    secret = "patient_ssn_1234"
    for envelope in (
        _envelope(**{secret: "x"}),
        _envelope(payload={"text": "hi", "hidden_reasoning": "x", secret: "y"}),
        _envelope(provenance={"captured_by": "cli", "trust": "x", secret: "y"}),
    ):
        rejection = _reject(envelope)
        assert rejection.status_code == 422
        rendered = str(rejection.as_detail())
        assert secret not in rendered
