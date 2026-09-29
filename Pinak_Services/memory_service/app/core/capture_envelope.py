"""Capture-wire envelope validation for Phase 1 slice 1a (P-85).

Contract: RFC-0001 v0.3, frozen 29 Sep 2026 at fork commit
46cf5853e6e9eef69be892b0266459353b535ac3 (13,677 bytes, SHA-256
047402fbbbb9c300a4907125e2607f5a6ac313924a9f283b14066bb1b72f622b),
the owner rulings recorded at P-79 #comment-7cdb7154, and PRD v3
(docs/prd-capture-foundation-v3.md, 14,705 bytes, SHA-256
9630c95174366241bbd5df16b17faecefe0d9ae51e317e2cf6a4fb70b0299bda).

Scope of 1a: envelope schema, authenticated tenant/client/agent binding, and
strict rejection. This module performs NO persistence, NO redaction and NO
hashing of payloads. Redaction is 1b, recall-blind staging and TTL are 1c;
until both pass the joint gate the route answers 503 capture_disabled (CW 7.4)
with zero writes anywhere, session mint and alias included.

Order of operations is CW 5.1 and is not negotiable:
  authenticate and bind identity -> validate schema -> enforce size ceiling
  -> redact (1b) -> persist (1c) -> hash the redacted payload (1b).
"""

from __future__ import annotations

import math
import re
import rfc8785

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# Owner ruling 1 (P-79 #comment-7cdb7154): the 256 KiB ceiling counts canonical
# serialized envelope bytes, not raw wire bytes. A defensive raw-body preflight
# stays an implementation detail and never replaces this count.
MAX_ENVELOPE_BYTES = 256 * 1024

# CW 5.3 / PRD v3 §4: the contract wants N and N-1 with migration tests. 1a
# ships N only. N-1 acceptance and the transition/migration tests are P-89
# (1e, "Size ceiling and envelope version handling"); accepting 0.2 here with
# no migration test would be an unbacked claim, so 1a rejects it with 400 and
# names the range it does support.
ENVELOPE_VERSION = "0.3"
SUPPORTED_ENVELOPE_VERSIONS = (ENVELOPE_VERSION,)
# Recorded, not implemented here: 1e owns N-1 acceptance and the N/N-1
# transition tests.
VERSION_MIGRATION_OWNER = "P-89 (1e)"

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

# CW 5.2 / PRD v3 §3: all four member lists are owner-ratified and closed.
# An unknown member of any of them is 422.
ACTORS = frozenset({"user", "agent", "tool", "system", "policy"})
EVENT_TYPES = frozenset(
    {"message", "tool_call", "tool_result", "response", "decision", "boundary"}
)
BOUNDARY_SOURCES = frozenset(
    {
        "session_start",
        "session_end",
        "task_switch",
        "client_hook",
        "service_timeout",
        "explicit_marker",
    }
)
SOURCES = frozenset({"hook", "cli", "api", "import", "system"})

CLOSED_ENUMS: Tuple[Tuple[str, frozenset], ...] = (
    ("actor", ACTORS),
    ("event_type", EVENT_TYPES),
    ("boundary_source", BOUNDARY_SOURCES),
    ("source", SOURCES),
)

# CW 4.4: hidden reasoning is not a payload field of any event_type, decision
# included, and no adapter may add one. The ban is recursive: a nested object
# or array may not smuggle one either.
FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {"hidden_reasoning", "reasoning_trace", "chain_of_thought", "cot", "scratchpad"}
)
MAX_PAYLOAD_DEPTH = 32

# CW 4.3 / PRD v3 §3: client evidence never self-certifies provenance and
# never raises trust. A client-asserted authoritative key inside provenance is
# rejected rather than silently downgraded.
FORBIDDEN_PROVENANCE_KEYS = frozenset(
    {
        "privacy_class",
        "retention_class",
        "received_ts",
        "trust",
        "trust_level",
        "authority",
        "verified",
        "service_verified",
        "legal_hold",
    }
)

# PRD v3 §2: task_id may be null only in the recorded pre-binding window.
TASK_BINDING_BOUND = "bound"
TASK_BINDING_PRE_BINDING = "pre_binding"
# The window is open only while the session has not yet been attached to a
# task. An envelope declares it by sending task_id: null; the service records
# the observation on the validated envelope (and, from 1c, in the audit trail).
# Attachment itself is an explicit, audited assertion and is not inferred here.
PRE_BINDING_EVENT_TYPES = frozenset({"message", "tool_call", "tool_result",
                                     "response", "decision", "boundary"})

_ULID_RE = re.compile(r"^[0-7][0123456789ABCDEFGHJKMNPQRSTVWXYZ]{25}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$"
)
_MAX_SEQUENCE = 2**64 - 1
_MAX_SAFE_INTEGER = 2**53 - 1


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


@dataclass(frozen=True)
class IdentityClaim:
    """Identity taken from the signed token only (CW 2.2).

    Every field here is a signed claim. Headers and body fields are hints to
    compare against these values; neither can populate them, and there is no
    'unknown' fallback: a missing agent or client claim is 403, not a minted
    identity.
    """

    tenant_id: str
    agent_id: str
    client_id: str

    def as_dict(self) -> Dict[str, str]:
        return {
            "tenant_id": self.tenant_id,
            "agent_id": self.agent_id,
            "client_id": self.client_id,
        }


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
    task_binding: str = TASK_BINDING_BOUND
    task_id: Optional[str] = None
    # CW 4.3: client evidence is carried as a lower-trust hint and is never
    # treated as authoritative. The authoritative payload hash is computed by
    # the service over the redacted payload in 1b; this is only what the
    # client said, kept for comparison and audit.
    client_evidence: Dict[str, Any] = field(default_factory=dict)
    fields: Dict[str, Any] = field(default_factory=dict)


def _ecmascript_number(value: Any) -> Any:
    """Coerce a parsed JSON number into the ECMAScript number domain.

    RFC 8785 canonicalizes numbers as IEEE-754 doubles, because that is what a
    JSON value is once ECMAScript has parsed it. Python's json keeps arbitrary
    precision integers, so 9007199254740993 survives here where JSON.parse
    would already have rounded it to 9007199254740992. Rounding at the edge of
    the canonicalizer keeps our bytes identical to the reference output instead
    of preserving digits no conforming implementation can see.
    """

    if isinstance(value, bool):
        return value
    if isinstance(value, int) and abs(value) > _MAX_SAFE_INTEGER:
        as_double = float(value)
        if not math.isfinite(as_double):
            raise ValueError("integer is outside the IEEE-754 double range")
        return as_double
    return value


def _to_ecmascript_domain(value: Any) -> Any:
    """Apply the number coercion above through the whole structure."""

    if isinstance(value, dict):
        return {key: _to_ecmascript_domain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_ecmascript_domain(item) for item in value]
    return _ecmascript_number(value)


def canonical_json_bytes(value: Any) -> bytes:
    """RFC 8785 (JCS) canonical serialization, UTF-8 encoded.

    Owner ruling 2 puts JCS plus SHA-256 on the redacted payload; that hashing
    lands in 1b. 1a uses the same canonical form to count the bytes of the
    complete envelope, so the count and the later hash agree on one
    serialization rather than two.

    The serializer is the vetted `rfc8785` implementation, not a hand-rolled
    one. A previous hand-rolled pass here got two number cases wrong: it
    printed 1e-06 where ECMAScript prints 0.000001, and it preserved integers
    past 2**53 that ECMAScript rounds. Either one makes the canonical size and
    the 1b hash input disagree with every conforming implementation, which is
    exactly the divergence the canonical form exists to prevent.

    json.dumps(sort_keys=True) is NOT JCS either: it sorts by code point
    rather than UTF-16 code unit, prints 1.0 as "1.0" and -0.0 as "-0.0".
    """

    try:
        return rfc8785.dumps(_to_ecmascript_domain(value))
    except rfc8785.CanonicalizationError as exc:
        raise ValueError(str(exc)) from exc


def _reject(status_code: int, code: str, message: str, **extra: Any) -> None:
    raise CaptureRejection(status_code, code, message, extra or None)


def _check_identity_binding(body: Dict[str, Any], claim: IdentityClaim) -> None:
    """CW 2.2: body fields are hints to match against, never a source of identity.

    A mismatch is 403 auth_binding_mismatch and never a silent overwrite.
    """

    bound = claim.as_dict()
    for field_name in ("tenant_id", "agent_id", "client_id"):
        asserted = body.get(field_name)
        if asserted is None:
            continue
        if not isinstance(asserted, str) or asserted != bound[field_name]:
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


def _scan_hidden_reasoning(value: Any, depth: int = 0) -> None:
    """CW 4.4, applied recursively. Key names only, never content."""

    if depth > MAX_PAYLOAD_DEPTH:
        _reject(422, "schema_violation", "payload nesting is too deep",
                field="payload", max_depth=MAX_PAYLOAD_DEPTH)
    if isinstance(value, dict):
        forbidden = sorted(
            key for key in value
            if isinstance(key, str) and key.strip().lower() in FORBIDDEN_PAYLOAD_KEYS
        )
        if forbidden:
            # CW 5.5: the count travels, the names do not. A rejected key name
            # is attacker-supplied content and can itself be sensitive, so it
            # is never echoed back in the error body.
            _reject(422, "schema_violation",
                    "Hidden reasoning is not an envelope field",
                    field="payload", forbidden_key_count=len(forbidden))
        for item in value.values():
            _scan_hidden_reasoning(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _scan_hidden_reasoning(item, depth + 1)


def _check_task_binding(body: Dict[str, Any]) -> str:
    """PRD v3 §2: task_id may be null only in the recorded pre-binding window.

    Null is a declaration that this event predates task attachment. It is
    accepted and recorded as such; it never attaches a task, and attachment
    stays an explicit, audited assertion (1c and later). Anything other than a
    non-empty string or null is a schema violation.
    """

    task_id = body["task_id"]
    if task_id is None:
        if body.get("event_type") not in PRE_BINDING_EVENT_TYPES:
            _reject(422, "schema_violation",
                    "task_id may be null only in the pre-binding window",
                    field="task_id")
        return TASK_BINDING_PRE_BINDING
    if not isinstance(task_id, str) or not task_id or len(task_id) > 256:
        _reject(422, "schema_violation",
                "task_id must be a non-empty string or null in the pre-binding window",
                field="task_id")
    return TASK_BINDING_BOUND


def _check_schema(body: Dict[str, Any]) -> str:
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

    unknown = set(body) - set(REQUIRED_FIELDS)
    if unknown:
        # CW 5.5: unknown field names come from the caller. Report how many
        # there were, never what they were called.
        _reject(422, "schema_violation", "Unknown envelope fields",
                unknown_field_count=len(unknown))

    for field_name in ("tenant_id", "agent_id", "client_id", "client_version"):
        value = body[field_name]
        if not isinstance(value, str) or not value or len(value) > 256:
            _reject(422, "schema_violation", f"{field_name} must be a non-empty string",
                    field=field_name)

    task_binding = _check_task_binding(body)

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

    # CW 5.2: every one of the four ratified member lists is closed, and an
    # unknown member of any of them is 422.
    for field_name, allowed in CLOSED_ENUMS:
        value = body[field_name]
        if not isinstance(value, str) or value not in allowed:
            _reject(422, "unknown_enum_value", f"Unknown {field_name}",
                    field=field_name, allowed=sorted(allowed))

    payload = body["payload"]
    if not isinstance(payload, dict):
        _reject(422, "schema_violation", "payload must be an object", field="payload")
    # CW 4.4, reported by key name only. No payload content leaves here.
    _scan_hidden_reasoning(payload)

    payload_hash = body["payload_hash"]
    if not isinstance(payload_hash, str) or not _SHA256_RE.match(payload_hash):
        _reject(422, "schema_violation",
                "payload_hash must be lowercase hex SHA-256", field="payload_hash")

    provenance = body["provenance"]
    if not isinstance(provenance, dict) or not provenance:
        _reject(422, "schema_violation", "provenance must be a non-empty object",
                field="provenance")
    asserted = sorted(
        key for key in provenance
        if isinstance(key, str) and key.strip().lower() in FORBIDDEN_PROVENANCE_KEYS
    )
    if asserted:
        # CW 4.3: client evidence never self-certifies provenance or raises
        # trust. Rejected, not silently downgraded.
        _reject(422, "client_assigned_service_field",
                "provenance may not assert service-authoritative values",
                field="provenance", forbidden_key_count=len(asserted))
    _scan_hidden_reasoning(provenance)

    return task_binding


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


def validate_capture_envelope(body: Any, claim: IdentityClaim) -> ValidatedEnvelope:
    """Run CW 5.1 steps one to three. Raises CaptureRejection on any failure."""

    if not isinstance(claim, IdentityClaim):
        # Identity has to arrive as signed claims. A loose mapping could carry
        # a header-derived or defaulted value, which is exactly what CW 2.2
        # forbids, so it is a programming error rather than a client error.
        raise TypeError("claim must be an IdentityClaim built from signed token claims")

    if not isinstance(body, dict):
        _reject(422, "schema_violation", "Envelope must be a JSON object")

    _check_identity_binding(body, claim)
    version = _check_envelope_version(body)
    task_binding = _check_schema(body)
    size = _check_size(body)

    return ValidatedEnvelope(
        envelope_version=version,
        tenant_id=claim.tenant_id,
        agent_id=claim.agent_id,
        client_id=claim.client_id,
        session_id=body["session_id"],
        event_id=body["event_id"],
        sequence=body["sequence"],
        canonical_bytes=size,
        task_binding=task_binding,
        task_id=body["task_id"],
        client_evidence={
            "client_payload_hash": body["payload_hash"],
            "client_provenance": dict(body["provenance"]),
            "trust": "client_asserted",
        },
        fields=dict(body),
    )
