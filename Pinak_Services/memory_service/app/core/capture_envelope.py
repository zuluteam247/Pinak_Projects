"""Capture-wire envelope validation for Phase 1 slice 1a (P-85).

Contract: RFC-0001 v0.3, frozen 29 Sep 2026 at fork commit
46cf5853e6e9eef69be892b0266459353b535ac3 (13,677 bytes, SHA-256
047402fbbbb9c300a4907125e2607f5a6ac313924a9f283b14066bb1b72f622b),
plus the owner rulings recorded at P-79 #comment-7cdb7154.

Scope of 1a: envelope schema, authenticated tenant/client binding, and strict
rejection. This module performs NO persistence, NO redaction and NO hashing of
payloads. Redaction is 1b, recall-blind staging and TTL are 1c; until both pass
the joint gate the route answers 503 capture_disabled (CW 7.4).

Order of operations is CW 5.1 and is not negotiable:
  authenticate and bind identity -> validate schema -> enforce size ceiling
  -> redact (1b) -> persist (1c) -> hash the redacted payload (1b).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# Owner ruling 1 (P-79 #comment-7cdb7154): the 256 KiB ceiling counts canonical
# serialized envelope bytes, not raw wire bytes. A defensive raw-body preflight
# stays an implementation detail and never replaces this count.
MAX_ENVELOPE_BYTES = 256 * 1024

# CW 5.3: the service accepts N and N-1 and ships migration tests for the pair.
SUPPORTED_ENVELOPE_VERSIONS = ("0.3", "0.2")

# CW 4 field table, adopted whole. Order matters only for readability.
REQUIRED_FIELDS = (
    "tenant_id",
    "agent_id",
    "client_id",
    "client_version",
    "session_id",
    "task_id",
    "event_id",
    "sequence",
    "client_ts",
    "actor",
    "event_type",
    "payload",
    "payload_hash",
    "boundary_source",
    "source",
    "provenance",
    "envelope_version",
)

# CW 4.3 / 4.5: the service assigns these, the client never does. CW 4 also
# lists received_ts as service-assigned and authoritative.
SERVICE_ASSIGNED_FIELDS = ("received_ts", "privacy_class", "retention_class")

# CW 5.2 closed enums that ARE settled.
ACTORS = frozenset({"user", "agent", "tool", "system", "policy"})
EVENT_TYPES = frozenset(
    {"message", "tool_call", "tool_result", "response", "decision", "boundary"}
)

# CW 4.6: boundary_source and source member lists are NOT owner-ratified, so 1a
# must not implement them as approved values. They are required, typed and
# bounded here; membership is deliberately not enforced until the owner rules.
OPEN_ENUM_FIELDS = ("boundary_source", "source")
OPEN_ENUM_MAX_LEN = 64

# CW 4.4: hidden reasoning is not a payload field of any event_type and no
# adapter may add one.
FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {"hidden_reasoning", "reasoning_trace", "chain_of_thought", "cot", "scratchpad"}
)

_ULID_RE = re.compile(r"^[0-7][0123456789ABCDEFGHJKMNPQRSTVWXYZ]{25}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$"
)
_MAX_SEQUENCE = 2**64 - 1


class CaptureRejection(Exception):
    """A fail-closed rejection. Carries no payload content (CW 5.5)."""

    def __init__(self, status_code: int, code: str, message: str,
                 extra: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra or {}

    def as_detail(self) -> Dict[str, Any]:
        detail: Dict[str, Any] = {"code": self.code, "message": self.message}
        detail.update(self.extra)
        return detail


@dataclass
class ValidatedEnvelope:
    """A schema-valid, identity-bound envelope. Nothing here is persisted."""

    envelope_version: str
    tenant_id: str
    agent_id: str
    client_id: str
    session_id: str
    event_id: str
    sequence: int
    canonical_bytes: int
    fields: Dict[str, Any] = field(default_factory=dict)


def canonical_json_bytes(value: Any) -> bytes:
    """RFC 8785 style canonical serialization, restricted to what the envelope
    can contain: objects with string keys sorted by code point, arrays, strings,
    numbers, booleans and null, no insignificant whitespace, UTF-8 output.

    Owner ruling 2 puts RFC 8785 JCS plus SHA-256 on the redacted payload; that
    hashing lands in 1b. 1a uses the same canonical form only to count bytes.
    """

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _reject(status_code: int, code: str, message: str, **extra: Any) -> None:
    raise CaptureRejection(status_code, code, message, extra or None)


def _check_identity_binding(body: Dict[str, Any], claim: Dict[str, Optional[str]]) -> None:
    """CW 2.2: body fields are hints to match against, never a source of identity.

    A mismatch is 403 auth_binding_mismatch and never a silent overwrite.
    """

    for field_name in ("tenant_id", "agent_id", "client_id"):
        asserted = body.get(field_name)
        bound = claim.get(field_name)
        if asserted is None:
            continue
        if not isinstance(asserted, str) or asserted != bound:
            _reject(
                403,
                "auth_binding_mismatch",
                f"{field_name} does not match the authenticated claim",
                field=field_name,
            )


def _check_envelope_version(body: Dict[str, Any]) -> str:
    version = body.get("envelope_version")
    if not isinstance(version, str) or version not in SUPPORTED_ENVELOPE_VERSIONS:
        # CW 5.3: 400, and the response names the supported range.
        _reject(
            400,
            "unsupported_envelope_version",
            "Unsupported envelope_version",
            supported_envelope_versions=list(SUPPORTED_ENVELOPE_VERSIONS),
        )
    return version  # type: ignore[return-value]


def _check_schema(body: Dict[str, Any]) -> None:
    for field_name in SERVICE_ASSIGNED_FIELDS:
        if field_name in body:
            _reject(
                422,
                "client_assigned_service_field",
                f"{field_name} is assigned by the service and may not be sent",
                field=field_name,
            )

    missing = [name for name in REQUIRED_FIELDS if name not in body]
    if missing:
        _reject(422, "schema_violation", "Required envelope fields are missing",
                missing_fields=sorted(missing))

    unknown = sorted(set(body) - set(REQUIRED_FIELDS))
    if unknown:
        _reject(422, "schema_violation", "Unknown envelope fields",
                unknown_fields=unknown)

    for field_name in ("tenant_id", "agent_id", "client_id", "client_version", "task_id"):
        value = body[field_name]
        if not isinstance(value, str) or not value or len(value) > 256:
            _reject(422, "schema_violation", f"{field_name} must be a non-empty string",
                    field=field_name)

    for field_name in ("session_id", "event_id"):
        value = body[field_name]
        # CW 4.1: event_id is a ULID. CW 3.1 plus owner ruling 5: session_id is
        # service-minted and echoed by the client, so its shape is checked here.
        if not isinstance(value, str) or not _ULID_RE.match(value):
            _reject(422, "schema_violation", f"{field_name} must be a ULID",
                    field=field_name)

    sequence = body["sequence"]
    if isinstance(sequence, bool) or not isinstance(sequence, int) \
            or sequence < 0 or sequence > _MAX_SEQUENCE:
        _reject(422, "schema_violation", "sequence must be a u64", field="sequence")

    client_ts = body["client_ts"]
    if not isinstance(client_ts, str) or not _RFC3339_RE.match(client_ts):
        _reject(422, "schema_violation", "client_ts must be an RFC3339 timestamp",
                field="client_ts")

    if body["actor"] not in ACTORS:
        # CW 5.2: unknown value in a closed enum is 422.
        _reject(422, "unknown_enum_value", "Unknown actor", field="actor",
                allowed=sorted(ACTORS))

    if body["event_type"] not in EVENT_TYPES:
        _reject(422, "unknown_enum_value", "Unknown event_type", field="event_type",
                allowed=sorted(EVENT_TYPES))

    for field_name in OPEN_ENUM_FIELDS:
        value = body[field_name]
        if not isinstance(value, str) or not value or len(value) > OPEN_ENUM_MAX_LEN:
            _reject(422, "schema_violation",
                    f"{field_name} must be a non-empty string", field=field_name)

    payload = body["payload"]
    if not isinstance(payload, dict):
        _reject(422, "schema_violation", "payload must be an object", field="payload")
    forbidden = sorted(FORBIDDEN_PAYLOAD_KEYS.intersection(payload))
    if forbidden:
        # CW 4.4, reported by key name only. No payload content leaves here.
        _reject(422, "schema_violation", "Hidden reasoning is not an envelope field",
                field="payload", forbidden_keys=forbidden)

    payload_hash = body["payload_hash"]
    if not isinstance(payload_hash, str) or not _SHA256_RE.match(payload_hash):
        _reject(422, "schema_violation",
                "payload_hash must be lowercase hex SHA-256", field="payload_hash")

    provenance = body["provenance"]
    if not isinstance(provenance, dict) or not provenance:
        _reject(422, "schema_violation", "provenance must be a non-empty object",
                field="provenance")


def _check_size(body: Dict[str, Any]) -> int:
    try:
        size = len(canonical_json_bytes(body))
    except (TypeError, ValueError) as exc:
        raise CaptureRejection(
            422, "schema_violation", "Envelope is not canonically serializable"
        ) from exc
    if size > MAX_ENVELOPE_BYTES:
        # CW 5.4: never truncated.
        _reject(422, "event_too_large", "Envelope exceeds the size ceiling",
                max_bytes=MAX_ENVELOPE_BYTES)
    return size


def validate_capture_envelope(body: Any, claim: Dict[str, Optional[str]]) -> ValidatedEnvelope:
    """Run CW 5.1 steps one to three. Raises CaptureRejection on any failure."""

    if not isinstance(body, dict):
        _reject(422, "schema_violation", "Envelope must be a JSON object")

    _check_identity_binding(body, claim)
    version = _check_envelope_version(body)
    _check_schema(body)
    size = _check_size(body)

    return ValidatedEnvelope(
        envelope_version=version,
        tenant_id=str(claim["tenant_id"]),
        agent_id=str(claim["agent_id"]),
        client_id=str(claim["client_id"]),
        session_id=body["session_id"],
        event_id=body["event_id"],
        sequence=body["sequence"],
        canonical_bytes=size,
        fields=dict(body),
    )
