# ADR-0002: capture route placement (P-85, slice 1a)

Status: accepted, 29 Sep 2026
Contract: RFC-0001 v0.3 frozen at `46cf5853e6e9eef69be892b0266459353b535ac3`
(13,677 bytes, SHA-256 `047402fbbbb9c300a4907125e2607f5a6ac313924a9f283b14066bb1b72f622b`),
CW 2.1. Owner rulings at P-79 `#comment-7cdb7154`.

## Decision

Capture is `POST /api/v1/memory/capture`, under the existing memory router.
It is not a new top-level `POST /v1/capture` in this slice.

P-85 asks for an explicit written decision rather than a silent one, so this is
the record.

## Why

The existing router already carries the auth dependency, the tenant and client
binding and the readiness guard that CW 2.2 and 2.3 require of capture.
Mounting capture there inherits all of it and keeps one auth surface to test
and audit. A parallel top-level route would fork those guards at exactly the
point where a fork is most expensive.

Rate limiting is NOT inherited, and an earlier draft of this ADR said it was.
Read live in `app/main.py` at this commit: the app installs a `RequestSizeLimit`
middleware and a `block_memory_until_verified` readiness middleware, plus
request-observability logging. Neither is a per-route request-rate limiter, and
no 429 path exists on this router today. CW 8 puts rate limiting at 429; that
remains unimplemented for capture and is owned outside 1a. Nothing in slice 1a
should be read as providing it.

The cost is a longer path that reads as if capture were a memory write. It is
not: capture stages raw events and is recall-blind (CW 7.1), while
`/api/v1/memory/*` is proposed and promoted memory. That distinction lives in
the docs and the tests, not the URL.

## Consequences

- A later move to `/v1/capture` is a rename plus a redirect, taken once the
  pipeline is real, and it does not change the envelope contract.
- No client may treat the current path as stable beyond Phase 1.
- Until 1b (server-side redaction) and 1c (recall-blind staging and TTL) exist
  and pass the joint gate, the route validates and then returns 503
  `capture_disabled` with zero writes. It never returns 202 (CW 7.4).
- `POST /api/v1/memory/capture/session` records the session-open contract
  (service-minted ULID, client echo, optional `client_session_ref` alias) and
  is inert for the same reason: minting a session id or an alias is a write,
  and CW 7.4 puts every write, session mint and alias included, behind the
  joint gate. The route validates the request and returns the same 503; the
  mint itself lands with 1c.
- Identity for capture is bound from signed token claims only
  (`agent_id`, `client_id`). `AuthContext.effective_client_id` is deliberately
  not used here: it falls back to a request header, then `sub`, then the
  literal `"unknown"`, which would let an unsigned header decide who authored
  an event. A token missing either claim gets 403 `auth_claim_missing`.
- Envelope version: 1a accepts `0.3` only. CW 5.3 wants N and N-1 with
  migration tests; the N-1 acceptance and those transition tests belong to
  P-89 (1e), so 1a rejects `0.2` with 400 rather than claim support it cannot
  demonstrate.
- Canonical serialization is RFC 8785 (JCS), implemented in
  `canonical_json_bytes`, not `json.dumps(sort_keys=True)`. The two disagree on
  number forms (`1.0`, `-0.0`) and on sort order for non-BMP keys, and the
  ceiling count has to use the same serialization the 1b hash will.
