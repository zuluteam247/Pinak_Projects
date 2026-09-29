"""Server-side redaction and class assignment for Phase 1 slice 1b (P-86).

Contract: RFC-0001 v0.3, frozen 29 Sep 2026 at fork commit
46cf5853e6e9eef69be892b0266459353b535ac3 (13,677 bytes, SHA-256
047402fbbbb9c300a4907125e2607f5a6ac313924a9f283b14066bb1b72f622b),
and the consolidated amendment recorded at P-79 #comment-4da4012a, which
SETTLES the privacy_class and retention_class member lists and defaults.

Scope of 1b, taken from the P-86 card: server-side redaction and safe error
responses, applied BEFORE first persistence and BEFORE any canonical hash is
taken. No memory-type classification here; that is later-phase work. This
module performs NO persistence and opens NO gate: the capture route still
answers 503 capture_disabled until 1c passes the joint gate too (CW 7.4).

Order of operations is CW 5.1 and is not negotiable:
  authenticate and bind identity -> validate schema -> enforce size ceiling
  -> REDACT (here) -> persist (1c) -> hash the redacted payload (here).

CW 5.1 also states plainly that a canonical payload_hash computed before
redaction is a defect. redact_and_classify() is therefore the only supported
producer of the bytes that get hashed, and it returns the hash with them so a
caller cannot accidentally hash the raw payload.

CW 5.5: error bodies carry no payload content, redacted or otherwise. Every
finding this module reports is a code plus a count. Nothing derived from
caller-supplied field names or values crosses the wire, which is why
RedactionReport carries counts and category codes only and has no field-name
list of any kind.
"""

from __future__ import annotations

import hashlib
import re

from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from .capture_envelope import (
    MAX_PAYLOAD_DEPTH,
    canonical_json_bytes,
)

# CW 4.5, settled by the amendment and restated in the RFC unchanged. These
# lists are not open and are not ours to extend.
PRIVACY_CLASSES: Tuple[str, ...] = (
    "public",
    "internal",
    "confidential",
    "restricted",
)
RETENTION_CLASSES: Tuple[str, ...] = (
    "ephemeral",
    "session",
    "task",
    "durable",
    "legal_hold",
)

# The amendment's defaults, as written:
#   - Ordinary work is internal.
#   - A secret or credential match is restricted, and is redacted before
#     persistence.
#   - A raw staged event is task, with a 30-day TTL, unless it is promoted or
#     attached to an open task.
#   - legal_hold is set only on explicit owner action.
# public and confidential are members of the enum, but the amendment states no
# rule that assigns either automatically. 1b therefore never assigns them: a
# classifier that invented a rule for them would be claiming a policy nobody
# ratified. When the owner settles one, it lands here.
DEFAULT_PRIVACY_CLASS = "internal"
SECRET_PRIVACY_CLASS = "restricted"
RAW_STAGED_RETENTION_CLASS = "task"
RAW_STAGED_TTL_DAYS = 30
# Recorded, not implemented here: 1c owns the TTL sweep, and legal_hold is an
# explicit owner action with no automatic path into this module.
RETENTION_TTL_OWNER = "P-87 (1c)"
LEGAL_HOLD_REQUIRES_OWNER_ACTION = True

# Redaction categories. The placeholder is a fixed constant per category: it
# never echoes the length, shape or any residue of what it replaced, because a
# length-preserving mask leaks the secret's size.
CATEGORY_SECRET = "secret"
CATEGORY_CREDENTIAL = "credential"
CATEGORY_PRIVATE_KEY = "private_key"
CATEGORY_TOKEN = "token"

REDACTION_PLACEHOLDERS: Dict[str, str] = {
    CATEGORY_SECRET: "[redacted:secret]",
    CATEGORY_CREDENTIAL: "[redacted:credential]",
    CATEGORY_PRIVATE_KEY: "[redacted:private_key]",
    CATEGORY_TOKEN: "[redacted:token]",
}

# Personal data. Kept apart from the secret categories because it carries a
# different class: a secret is restricted by the amendment's own words, while a
# direct personal identifier is confidential. See assign_privacy_class.
CATEGORY_PII = "personal_data"
CATEGORY_PAYMENT = "payment_instrument"

REDACTION_PLACEHOLDERS[CATEGORY_PII] = "[redacted:personal_data]"
REDACTION_PLACEHOLDERS[CATEGORY_PAYMENT] = "[redacted:payment_instrument]"

PII_CATEGORIES = frozenset({CATEGORY_PII, CATEGORY_PAYMENT})
# Any non-PII category is a secret or credential match, so it makes the
# envelope restricted.
SECRET_CATEGORIES = frozenset(REDACTION_PLACEHOLDERS) - PII_CATEGORIES
PII_PRIVACY_CLASS = "confidential"

# Key names that make the VALUE a secret regardless of what the value looks
# like. Matched case-insensitively against the key with separators removed, so
# api_key, apiKey, API-KEY and apikey are one rule rather than four.
_SECRET_KEY_NAMES: Tuple[Tuple[str, str], ...] = (
    ("password", CATEGORY_CREDENTIAL),
    ("passwd", CATEGORY_CREDENTIAL),
    ("passphrase", CATEGORY_CREDENTIAL),
    ("secret", CATEGORY_SECRET),
    ("apikey", CATEGORY_CREDENTIAL),
    ("apisecret", CATEGORY_CREDENTIAL),
    ("accesskey", CATEGORY_CREDENTIAL),
    ("accesskeyid", CATEGORY_CREDENTIAL),
    ("secretaccesskey", CATEGORY_CREDENTIAL),
    ("accesstoken", CATEGORY_TOKEN),
    ("refreshtoken", CATEGORY_TOKEN),
    ("idtoken", CATEGORY_TOKEN),
    ("bearertoken", CATEGORY_TOKEN),
    ("sessiontoken", CATEGORY_TOKEN),
    ("authtoken", CATEGORY_TOKEN),
    ("token", CATEGORY_TOKEN),
    ("authorization", CATEGORY_CREDENTIAL),
    ("credential", CATEGORY_CREDENTIAL),
    ("credentials", CATEGORY_CREDENTIAL),
    ("privatekey", CATEGORY_PRIVATE_KEY),
    ("secretkey", CATEGORY_SECRET),
    ("clientsecret", CATEGORY_SECRET),
    ("signingkey", CATEGORY_SECRET),
    ("encryptionkey", CATEGORY_SECRET),
    ("sessionkey", CATEGORY_SECRET),
    ("connectionstring", CATEGORY_CREDENTIAL),
    ("dsn", CATEGORY_CREDENTIAL),
    # Personal identifiers named by their key. For several of these the value
    # shape alone is too weak to match on (a bare nine-digit number, a passport
    # code), so the key name is what carries them.
    ("ssn", CATEGORY_PII),
    ("socialsecurity", CATEGORY_PII),
    ("socialsecuritynumber", CATEGORY_PII),
    ("nationalid", CATEGORY_PII),
    ("aadhaar", CATEGORY_PII),
    ("aadhar", CATEGORY_PII),
    ("pannumber", CATEGORY_PII),
    ("passport", CATEGORY_PII),
    ("passportnumber", CATEGORY_PII),
    ("driverlicense", CATEGORY_PII),
    ("drivinglicence", CATEGORY_PII),
    ("taxid", CATEGORY_PII),
    ("dateofbirth", CATEGORY_PII),
    ("dob", CATEGORY_PII),
    ("cardnumber", CATEGORY_PAYMENT),
    ("creditcard", CATEGORY_PAYMENT),
    ("cardnum", CATEGORY_PAYMENT),
    ("cvv", CATEGORY_PAYMENT),
    ("cvc", CATEGORY_PAYMENT),
    ("iban", CATEGORY_PAYMENT),
    ("accountnumber", CATEGORY_PAYMENT),
    ("routingnumber", CATEGORY_PAYMENT),
    ("sortcode", CATEGORY_PAYMENT),
)

_KEY_SEPARATORS = re.compile(r"[^a-z0-9]+")

# Value shapes that are secrets wherever they appear, including inside free
# prose, because an agent transcript pastes keys into message bodies far more
# often than it puts them in a helpfully-named field.
_VALUE_PATTERNS: Tuple[Tuple[re.Pattern, str], ...] = (
    # PEM private key block of any flavour (RSA, EC, OPENSSH, PGP). The
    # terminated form is matched first; the unterminated one runs to the end
    # of the string, because a truncated block still carries key material and
    # a non-greedy match with an optional END matches the header alone and
    # leaves the body sitting in the payload.
    (re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY(?: BLOCK)?-----"
                r".*?-----END[A-Z ]*PRIVATE KEY(?: BLOCK)?-----",
                re.DOTALL), CATEGORY_PRIVATE_KEY),
    (re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY(?: BLOCK)?-----.*",
                re.DOTALL), CATEGORY_PRIVATE_KEY),
    # AWS access key id and the secret that usually rides with it.
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), CATEGORY_CREDENTIAL),
    # GitHub tokens: classic, fine-grained, app, refresh, OAuth.
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b"), CATEGORY_TOKEN),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,255}\b"), CATEGORY_TOKEN),
    # Slack tokens.
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"), CATEGORY_TOKEN),
    # Stripe live and test keys.
    (re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}\b"), CATEGORY_CREDENTIAL),
    # Google API key.
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), CATEGORY_CREDENTIAL),
    # OpenAI-style project keys.
    (re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}\b"), CATEGORY_CREDENTIAL),
    # A JWT: three base64url segments, header segment starting with the usual
    # {"alg" prefix so ordinary dotted identifiers do not match.
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"),
     CATEGORY_TOKEN),
    # An Authorization header pasted whole.
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_\-\.=]{16,}"), CATEGORY_CREDENTIAL),
    (re.compile(r"(?i)\bBasic\s+[A-Za-z0-9+/=]{16,}"), CATEGORY_CREDENTIAL),
    # Credentials embedded in a URL authority: scheme://user:pass@host.
    (re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s/@:]+:[^\s/@]+@"), CATEGORY_CREDENTIAL),
    # US Social Security number. The area part cannot be 000, 666 or 900-999,
    # and neither the group nor the serial may be all zeroes, so a placeholder
    # like 000-00-0000 in a fixture is left alone.
    (re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"), CATEGORY_PII),
    # India Aadhaar: twelve digits, first never 0 or 1, usually spaced in fours.
    (re.compile(r"\b[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}\b"), CATEGORY_PII),
    # India PAN.
    (re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), CATEGORY_PII),
    # IBAN.
    (re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"), CATEGORY_PAYMENT),
    # Email address.
    (re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), CATEGORY_PII),
)

# Payment cards are matched separately. The shape alone, thirteen to nineteen
# digits, hits ordinary long numbers constantly, so a candidate is only redacted
# once it passes the Luhn check the card networks themselves use.
_CARD_CANDIDATE = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = ord(char) - 48
        if index % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


class RedactionError(Exception):
    """Raised when the payload cannot be redacted safely.

    Carries a code and counts only. CW 5.5 forbids payload content in error
    bodies, and a field name supplied by the caller is payload content: it can
    itself be the secret ("my_password_is_hunter2" as a key). Nothing
    caller-supplied is ever attached to this exception.
    """

    def __init__(self, status_code: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra

    def as_detail(self) -> Dict[str, Any]:
        detail: Dict[str, Any] = {"code": self.code, "message": self.message}
        detail.update(self.extra)
        return detail


@dataclass(frozen=True)
class RedactionReport:
    """What the redactor did, in counts and category codes only.

    Deliberately has no field-name list and no sample of redacted content. If
    you find yourself wanting to add one for debugging, that is the exact leak
    CW 5.5 and CW 6.4 forbid; count it instead.
    """

    redaction_count: int = 0
    counts_by_category: Dict[str, int] = field(default_factory=dict)

    @property
    def categories(self) -> Tuple[str, ...]:
        return tuple(sorted(self.counts_by_category))

    @property
    def found_secret(self) -> bool:
        return any(c in SECRET_CATEGORIES for c in self.counts_by_category)

    @property
    def found_personal_data(self) -> bool:
        return any(c in PII_CATEGORIES for c in self.counts_by_category)

    def as_detail(self) -> Dict[str, Any]:
        """Wire-safe summary: counts and category codes, never content."""
        return {
            "redaction_count": self.redaction_count,
            "redacted_categories": list(self.categories),
        }


@dataclass(frozen=True)
class RedactedPayload:
    """The redacted payload plus the bytes and hash taken FROM it.

    canonical_bytes and payload_hash are produced here, after redaction, so a
    caller cannot hash the raw payload by accident. CW 5.1: a canonical
    payload_hash computed before redaction is a defect.
    """

    payload: Any
    privacy_class: str
    retention_class: str
    canonical_bytes: bytes
    payload_hash: str
    report: RedactionReport

    @property
    def retention_ttl_days(self) -> int:
        return RAW_STAGED_TTL_DAYS


def _normalize_key(key: str) -> str:
    return _KEY_SEPARATORS.sub("", key.lower())


def _category_for_key(key: Any) -> str:
    """Return the redaction category a key name implies, or "" for none."""

    if not isinstance(key, str):
        return ""
    normalized = _normalize_key(key)
    if not normalized:
        return ""
    for name, category in _SECRET_KEY_NAMES:
        if normalized == name or normalized.endswith(name):
            return category
    return ""


def _redact_string_value(value: str) -> Tuple[str, Dict[str, int]]:
    """Replace secret-shaped substrings inside a string.

    Returns the cleaned string and a per-category count. Prose around the
    secret survives, which is what makes the transcript still useful; only the
    matched span is replaced, with a fixed-width placeholder.
    """

    counts: Dict[str, int] = {}
    cleaned = value
    for pattern, category in _VALUE_PATTERNS:
        placeholder = REDACTION_PLACEHOLDERS[category]
        cleaned, hits = pattern.subn(placeholder, cleaned)
        if hits:
            counts[category] = counts.get(category, 0) + hits

    def _card(match: "re.Match") -> str:
        digits = "".join(ch for ch in match.group(0) if ch.isdigit())
        if _luhn_ok(digits):
            counts[CATEGORY_PAYMENT] = counts.get(CATEGORY_PAYMENT, 0) + 1
            return REDACTION_PLACEHOLDERS[CATEGORY_PAYMENT]
        return match.group(0)

    cleaned = _CARD_CANDIDATE.sub(_card, cleaned)
    return cleaned, counts


def _merge(into: Dict[str, int], more: Dict[str, int]) -> None:
    for category, count in more.items():
        into[category] = into.get(category, 0) + count


def _redact(value: Any, counts: Dict[str, int], depth: int = 0,
            forced_category: str = "") -> Any:
    """Walk the payload and redact, recursively, to MAX_PAYLOAD_DEPTH.

    The depth limit is the 1a walker's, reused deliberately: a payload that
    1a accepted cannot be too deep for the redactor, and a payload the
    redactor cannot fully walk must never reach persistence. Exceeding it is a
    rejection, never a silent partial pass.
    """

    if depth > MAX_PAYLOAD_DEPTH:
        raise RedactionError(
            422,
            "payload_too_deep",
            "Payload nesting exceeds the maximum depth the redactor will walk.",
            max_depth=MAX_PAYLOAD_DEPTH,
        )

    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, item in value.items():
            category = forced_category or _category_for_key(key)
            # The KEY is payload content too. A caller that writes
            # {"AKIA...": "see attached"} has put the secret in the field name,
            # and redacting only values leaves it in the canonical bytes and
            # therefore in the hash. So the key goes through the same value-shape
            # pass before it is written back.
            safe_key = key
            if isinstance(key, str):
                safe_key, key_hits = _redact_string_value(key)
                _merge(counts, key_hits)
                if safe_key != key and safe_key in out:
                    # Two distinct keys redacted down to the same placeholder.
                    # Collapsing them would silently drop a branch, so the later
                    # one is suffixed by position rather than lost.
                    safe_key = f"{safe_key}.{len(out)}"
            out[safe_key] = _redact(item, counts, depth + 1, category)
        return out

    if isinstance(value, (list, tuple)):
        return [_redact(item, counts, depth + 1, forced_category) for item in value]

    if forced_category:
        # The key named this a secret, so the whole value goes, whatever shape
        # it has. A number or a bool under "password" is still a password.
        if value is None:
            return None
        _merge(counts, {forced_category: 1})
        return REDACTION_PLACEHOLDERS[forced_category]

    if isinstance(value, str):
        cleaned, hits = _redact_string_value(value)
        _merge(counts, hits)
        return cleaned

    return value


def assign_privacy_class(report: RedactionReport) -> str:
    """CW 4.5: a secret or credential match is restricted; ordinary work is internal.

    Personal data sits between the two and is classified confidential. The
    amendment settles confidential as a member of the enum but states no rule
    that assigns it, so this rule is NOT the amendment's: it comes from the 1b
    verification round, where direct personal identifiers were found surviving
    a redaction pass that only looked for secrets. It is recorded here as an
    implementation decision awaiting owner ratification, and it is one line to
    change if the owner wants personal data classified differently. Precedence
    is strict: a payload carrying both a secret and personal data is restricted.
    """

    if report.found_secret:
        return SECRET_PRIVACY_CLASS
    if report.found_personal_data:
        return PII_PRIVACY_CLASS
    return DEFAULT_PRIVACY_CLASS


def assign_retention_class() -> str:
    """CW 4.5: a raw staged event is task, with a 30-day TTL.

    Promotion and attachment to an open task change this later and are not 1b
    decisions. legal_hold is set only on explicit owner action and has no
    automatic path here, which is why this function takes no arguments: there
    is nothing in a capture envelope that may move it.
    """

    return RAW_STAGED_RETENTION_CLASS


def redact_and_classify(payload: Any) -> RedactedPayload:
    """Redact a validated payload, classify it, then hash the redacted bytes.

    The single entry point for CW 5.1's REDACT step. Call it after schema
    validation and the size ceiling, and before anything is written.
    """

    counts: Dict[str, int] = {}
    redacted = _redact(payload, counts)
    report = RedactionReport(
        redaction_count=sum(counts.values()),
        counts_by_category=dict(counts),
    )
    canonical = canonical_json_bytes(redacted)
    return RedactedPayload(
        payload=redacted,
        privacy_class=assign_privacy_class(report),
        retention_class=assign_retention_class(),
        canonical_bytes=canonical,
        payload_hash=hashlib.sha256(canonical).hexdigest(),
        report=report,
    )
