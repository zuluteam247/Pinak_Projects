"""Tests for Phase 1 slice 1b (P-86): server-side redaction and safe errors.

The contract these pin, in order of how much damage getting them wrong does:

  CW 5.1  redact BEFORE persist, and hash the REDACTED payload. A hash taken
          before redaction is a defect, so the hash is asserted against the
          redacted bytes and never against the raw ones.
  CW 4.5  privacy_class and retention_class are service-assigned, from the
          settled member lists, with the amendment's defaults.
  CW 5.5  error bodies carry no payload content, redacted or otherwise. The
          canary tests push secret-looking KEY NAMES and VALUES through every
          path that can produce a report or an error and assert that neither
          the original nor the redacted content comes back out.
  CW 6.4  unredacted payloads never reach an audit trail.
"""

import hashlib
import json

import pytest

from app.core.capture_envelope import MAX_PAYLOAD_DEPTH, canonical_json_bytes
from app.core.capture_redaction import (
    DEFAULT_PRIVACY_CLASS,
    PII_PRIVACY_CLASS,
    PRIVACY_CLASSES,
    RAW_STAGED_RETENTION_CLASS,
    RAW_STAGED_TTL_DAYS,
    REDACTION_PLACEHOLDERS,
    RETENTION_CLASSES,
    RedactionError,
    SECRET_PRIVACY_CLASS,
    redact_and_classify,
)


# --- CW 4.5: the settled member lists, exactly ------------------------------

def test_privacy_class_members_are_the_settled_list():
    assert PRIVACY_CLASSES == ("public", "internal", "confidential", "restricted")


def test_retention_class_members_are_the_settled_list():
    assert RETENTION_CLASSES == (
        "ephemeral", "session", "task", "durable", "legal_hold")


def test_withdrawn_draft_members_are_not_implemented():
    # The v0.2-era proposal (sensitive/secret, standard/extended/permanent) is
    # WITHDRAWN by the RFC and must not appear.
    for withdrawn in ("sensitive", "secret"):
        assert withdrawn not in PRIVACY_CLASSES
    for withdrawn in ("standard", "extended", "permanent"):
        assert withdrawn not in RETENTION_CLASSES


# --- CW 4.5: the defaults, as the amendment writes them ---------------------

def test_ordinary_work_is_internal():
    result = redact_and_classify({"text": "rewrote the retry loop", "lines": 42})
    assert result.privacy_class == DEFAULT_PRIVACY_CLASS == "internal"
    assert result.report.redaction_count == 0


def test_secret_match_is_restricted():
    result = redact_and_classify({"api_key": "hunter2"})
    assert result.privacy_class == SECRET_PRIVACY_CLASS == "restricted"


def test_raw_staged_event_is_task_with_thirty_day_ttl():
    result = redact_and_classify({"text": "ordinary"})
    assert result.retention_class == RAW_STAGED_RETENTION_CLASS == "task"
    assert result.retention_ttl_days == RAW_STAGED_TTL_DAYS == 30


def test_legal_hold_is_never_assigned_automatically():
    # legal_hold is explicit owner action only. Nothing in a capture envelope
    # may reach it, including a client that asks for it by name.
    for payload in (
        {"text": "please legal_hold this"},
        {"retention_class": "legal_hold"},
        {"nested": {"retention_class": "legal_hold"}},
    ):
        assert redact_and_classify(payload).retention_class == "task"


def test_public_is_never_assigned_by_1b():
    # public is a member of the enum, but the amendment states no rule that
    # assigns it, so 1b never does. confidential DOES get assigned, for
    # personal data: that rule comes from the 1b verification round, not from
    # the amendment, and is marked as awaiting owner ratification in the code.
    for payload in ({"text": "public announcement"},
                    {"text": "nothing sensitive here"},
                    {"api_key": "x"}):
        assert redact_and_classify(payload).privacy_class != "public"


# --- CW 5.1: redact before hash, and hash the redacted bytes ----------------

def test_hash_is_taken_over_the_redacted_payload_not_the_raw_one():
    raw = {"password": "correct horse battery staple", "text": "ok"}
    result = redact_and_classify(raw)

    raw_hash = hashlib.sha256(canonical_json_bytes(raw)).hexdigest()
    redacted_hash = hashlib.sha256(
        canonical_json_bytes(result.payload)).hexdigest()

    assert result.payload_hash == redacted_hash
    assert result.payload_hash != raw_hash, (
        "hashing before redaction is a defect under CW 5.1")


def test_canonical_bytes_are_the_redacted_bytes():
    result = redact_and_classify({"token": "xox" + "b-1234567890-abcdefghij"})
    assert result.canonical_bytes == canonical_json_bytes(result.payload)
    assert b"xox" + b"b-1234567890" not in result.canonical_bytes


def test_secret_never_survives_into_the_hashed_bytes():
    secret = "AKIA" + "IOSFODNN7EXAMPLE"
    result = redact_and_classify({"note": f"the key is {secret}"})
    assert secret.encode() not in result.canonical_bytes


# --- Redaction by key name --------------------------------------------------

@pytest.mark.parametrize("key", [
    "password", "passwd", "passphrase", "secret", "api_key", "apiKey",
    "API-KEY", "access_token", "refresh_token", "id_token", "bearer_token",
    "session_token", "auth_token", "authorization", "credential",
    "credentials", "private_key", "secret_key", "client_secret",
    "signing_key", "encryption_key", "connection_string", "dsn",
    "aws_secret_access_key", "user_password",
])
def test_secret_key_names_redact_their_value(key):
    result = redact_and_classify({key: "whatever-the-value-is"})
    assert "whatever-the-value-is" not in json.dumps(result.payload)
    assert result.privacy_class == "restricted"
    assert result.report.redaction_count == 1


def test_key_name_redacts_non_string_values_too():
    # A number under "password" is still a password.
    result = redact_and_classify({"password": 1234567, "pin_secret": True})
    dumped = json.dumps(result.payload)
    assert "1234567" not in dumped
    assert result.privacy_class == "restricted"


def test_null_under_a_secret_key_stays_null():
    result = redact_and_classify({"password": None})
    assert result.payload == {"password": None}
    assert result.report.redaction_count == 0
    assert result.privacy_class == "internal"


def test_ordinary_keys_are_left_alone():
    payload = {"tokenizer": "bpe", "password_policy_version": 3,
               "description": "we rotate keys quarterly"}
    result = redact_and_classify(payload)
    assert result.payload["tokenizer"] == "bpe"
    assert result.payload["description"] == "we rotate keys quarterly"


# --- Redaction by value shape, including inside free prose ------------------

# Every fixture below is assembled from fragments at import time rather than
# written out as a literal. The strings are fake, but a literal that matches a
# real vendor token shape trips GitHub push protection and blocks the branch,
# and pinning a test to a secret-shaped literal is a bad habit to leave in a
# repo that captures transcripts for a living. Splitting the prefix keeps the
# scanner quiet while the redactor still sees the exact shape it must catch.
_FAKE_SECRETS = [
    "AKIA" + "IOSFODNN7EXAMPLE",
    "ASIA" + "Y34FZKBOKMUTVV7A",
    "gh" + "p_" + "a" * 36,
    "github" + "_pat_" + "b" * 30,
    "xox" + "b-123456789012-abcdefghijklmnop",
    "sk" + "_live_" + "c" * 24,
    "rk" + "_test_" + "d" * 24,
    "AIza" + "e" * 35,
    "sk-" + "proj-" + "f" * 32,
    "eyJhbGciOiJIUzI1NiJ9." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0." + "dBjftJeZ4CVPmB92K27uhbUJU1p1r",
    "Bearer " + "abcdefghijklmnopqrstuvwxyz0123",
    "Basic " + "YWxhZGRpbjpvcGVuc2VzYW1l0000",
    "postgres://admin:" + "s3cr3t" + "@db.internal:5432/app",
]


@pytest.mark.parametrize("secret", _FAKE_SECRETS)
def test_secret_shapes_are_redacted_wherever_they_appear(secret):
    result = redact_and_classify({"text": f"here it is: {secret} , thanks"})
    dumped = json.dumps(result.payload)
    assert secret not in dumped
    assert result.privacy_class == "restricted"
    assert result.report.redaction_count >= 1


def test_prose_around_a_secret_survives():
    result = redact_and_classify(
        {"text": "deploy failed, the key " + "AKIA" + "IOSFODNN7EXAMPLE" + " was rotated"})
    text = result.payload["text"]
    assert "deploy failed" in text and "was rotated" in text
    assert "AKIA" + "IOSFODNN7EXAMPLE" not in text


def test_pem_private_key_block_is_redacted():
    pem = ("-----BEGIN RSA PRIVATE KEY-----\n"
           "MIIEowIBAAKCAQEAxyz\nabcdef\n"
           "-----END RSA PRIVATE KEY-----")
    result = redact_and_classify({"text": pem})
    assert "MIIEowIBAAKCAQEAxyz" not in json.dumps(result.payload)
    assert result.privacy_class == "restricted"


def test_placeholder_does_not_leak_the_length_of_what_it_replaced():
    short = redact_and_classify({"password": "a"})
    long = redact_and_classify({"password": "a" * 512})
    assert short.payload["password"] == long.payload["password"]
    assert short.payload["password"] in REDACTION_PLACEHOLDERS.values()


# --- Recursion: depth, arrays, and the 1a walker's limit --------------------

def test_redaction_is_recursive_through_objects_and_arrays():
    payload = {"steps": [{"env": {"api_key": "sekrit"}},
                         {"args": ["--token", "gh" + "p_" + "z" * 36]}]}
    result = redact_and_classify(payload)
    dumped = json.dumps(result.payload)
    assert "sekrit" not in dumped
    assert "gh" + "p_" not in dumped
    assert result.privacy_class == "restricted"


def test_secret_at_the_deepest_allowed_level_is_still_redacted():
    payload: dict = {"password": "deep-secret"}
    for _ in range(MAX_PAYLOAD_DEPTH - 2):
        payload = {"nested": payload}
    result = redact_and_classify(payload)
    assert "deep-secret" not in json.dumps(result.payload)
    assert result.privacy_class == "restricted"


def test_a_payload_too_deep_to_walk_is_rejected_not_partially_redacted():
    payload: dict = {"password": "deep-secret"}
    for _ in range(MAX_PAYLOAD_DEPTH + 5):
        payload = {"nested": payload}
    with pytest.raises(RedactionError) as excinfo:
        redact_and_classify(payload)
    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "payload_too_deep"


def test_key_forced_category_applies_to_everything_under_it():
    # A whole credentials object, not just its leaves, is a secret.
    result = redact_and_classify(
        {"credentials": {"user": "root", "pass": "toor", "hosts": ["a", "b"]}})
    dumped = json.dumps(result.payload)
    for leaked in ("root", "toor", '"a"', '"b"'):
        assert leaked not in dumped


# --- CW 5.5 / CW 6.4: nothing caller-supplied comes back out ----------------

SECRET_KEY_NAME = "my_password_is_hunter2"
SECRET_VALUE = "AKIA" + "IOSFODNN7EXAMPLE"


def test_report_carries_counts_and_categories_only():
    result = redact_and_classify({SECRET_KEY_NAME: SECRET_VALUE})
    detail = result.report.as_detail()
    rendered = json.dumps(detail)
    assert SECRET_KEY_NAME not in rendered
    assert SECRET_VALUE not in rendered
    for placeholder in REDACTION_PLACEHOLDERS.values():
        assert placeholder not in rendered
    assert set(detail) == {"redaction_count", "redacted_categories"}


def test_report_has_no_field_name_list_at_all():
    result = redact_and_classify({SECRET_KEY_NAME: SECRET_VALUE})
    rendered = json.dumps(result.report.as_detail())
    for banned in ("field", "fields", "keys", "unknown_fields", "forbidden_keys"):
        assert banned not in rendered


def test_redaction_error_body_carries_no_payload_content():
    payload: dict = {SECRET_KEY_NAME: SECRET_VALUE}
    for _ in range(MAX_PAYLOAD_DEPTH + 5):
        payload = {SECRET_KEY_NAME: payload}
    with pytest.raises(RedactionError) as excinfo:
        redact_and_classify(payload)
    rendered = json.dumps(excinfo.value.as_detail())
    assert SECRET_KEY_NAME not in rendered
    assert SECRET_VALUE not in rendered


def test_counts_are_accurate_per_category():
    result = redact_and_classify({
        "api_key": "one",
        "note": "two keys: " + "AKIA" + "IOSFODNN7EXAMPLE" + " and " + "gh" + "p_" + "y" * 36,
    })
    assert result.report.redaction_count == 3
    assert result.report.found_secret is True


# --- Round 2: the two gaps zulu's probes found ------------------------------
#
# Both were real. Neither was a shape the redactor failed to recognise; both
# were places the redactor never looked.


def test_ssn_in_a_value_is_redacted_and_classified_confidential():
    result = redact_and_classify({"note": "his ssn is 123-45-6789, filed already"})
    dumped = json.dumps(result.payload)
    assert "123-45-6789" not in dumped
    assert "filed already" in dumped
    assert result.privacy_class == PII_PRIVACY_CLASS == "confidential"


@pytest.mark.parametrize("key", [
    "ssn", "social_security_number", "aadhaar", "pan_number", "passport",
    "date_of_birth", "dob", "national_id", "tax_id", "driver_license",
])
def test_personal_identifier_key_names_redact_their_value(key):
    result = redact_and_classify({key: "whatever-it-holds"})
    assert "whatever-it-holds" not in json.dumps(result.payload)
    assert result.privacy_class == "confidential"


@pytest.mark.parametrize("key", ["card_number", "credit_card", "cvv", "iban",
                                 "account_number", "routing_number"])
def test_payment_key_names_redact_their_value(key):
    result = redact_and_classify({key: "4111111111111111"})
    assert "4111111111111111" not in json.dumps(result.payload)
    assert result.privacy_class == "confidential"


def test_card_number_passing_luhn_is_redacted_in_prose():
    result = redact_and_classify({"text": "paid with 4111 1111 1111 1111 today"})
    dumped = json.dumps(result.payload)
    assert "4111" not in dumped
    assert "today" in dumped


def test_long_number_failing_luhn_is_left_alone():
    # An ordinary long identifier is not a card and must survive, or every
    # sequence number in a transcript gets shredded.
    result = redact_and_classify({"text": "build id 1234567890123456 finished"})
    assert "1234567890123456" in json.dumps(result.payload)


def test_email_address_is_redacted():
    result = redact_and_classify({"text": "ping abhijeet@example.com about it"})
    dumped = json.dumps(result.payload)
    assert "abhijeet@example.com" not in dumped
    assert result.privacy_class == "confidential"


def test_personal_data_at_depth_is_redacted():
    payload = {"steps": [{"applicant": {"ssn": "123-45-6789"}}]}
    result = redact_and_classify(payload)
    assert "123-45-6789" not in json.dumps(result.payload)


def test_secret_outranks_personal_data_in_the_class():
    result = redact_and_classify(
        {"ssn": "123-45-6789", "api_key": "whatever"})
    assert result.privacy_class == "restricted"


# --- Gap two: the secret lived in the KEY, not the value --------------------

def test_secret_in_a_key_name_is_redacted_from_the_payload():
    leaked = "AKIA" + "IOSFODNN7EXAMPLE"
    result = redact_and_classify({leaked: "see attached"})
    assert leaked not in json.dumps(result.payload)
    assert result.privacy_class == "restricted"


def test_secret_in_a_key_name_never_reaches_the_hashed_bytes():
    # This is the one that mattered: a masked value with an unmasked key still
    # canonicalizes the secret straight into payload_hash.
    leaked = "gh" + "p_" + "q" * 36
    result = redact_and_classify({leaked: "token rotated"})
    assert leaked.encode() not in result.canonical_bytes


def test_personal_data_in_a_key_name_is_redacted_too():
    result = redact_and_classify({"contact-abhijeet@example.com": "called"})
    assert "abhijeet@example.com" not in json.dumps(result.payload)


def test_secret_in_a_nested_key_name_is_redacted():
    leaked = "xox" + "b-123456789012-abcdefghijklmnop"
    result = redact_and_classify({"outer": [{leaked: 1}]})
    assert leaked not in json.dumps(result.payload)


def test_two_keys_redacting_to_the_same_placeholder_keep_both_branches():
    # Collapsing them would silently drop a branch of the payload.
    first = "AKIA" + "IOSFODNN7EXAMPL1"
    second = "AKIA" + "IOSFODNN7EXAMPL2"
    result = redact_and_classify({first: "one", second: "two"})
    values = json.dumps(result.payload)
    assert "one" in values and "two" in values
    assert len(result.payload) == 2


def test_ordinary_keys_are_not_rewritten():
    result = redact_and_classify({"event_type": "message", "sequence": 4})
    assert set(result.payload) == {"event_type", "sequence"}
