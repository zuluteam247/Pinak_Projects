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
binding, the readiness guard and the rate-limit path that CW 2.2, 2.3 and 8
require of capture. Mounting capture there inherits all of it and keeps one
auth surface to test and audit. A parallel top-level route would fork those
guards at exactly the point where a fork is most expensive.

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
