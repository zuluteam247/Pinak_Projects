# Proposal: versioned embedding manifest

Status: design only, 25 September 2026. No schema or runtime change has been made. The current fork compares database embedding IDs with NumPy snapshot IDs at startup, but a vector with the right ID and stale values passes. The snapshot contains `ids` and `vectors` only. The source rows hold no content-to-vector digest. [Current source](https://github.com/zuluteam247/Pinak_Projects/tree/main/Pinak_Services/memory_service/app).

## Contract and storage

Use a versioned snapshot manifest, rather than treating matching counts as integrity. Each embedding entry binds `(layer, memory_id, embedding_id, tenant, project_id)` to:

- `source_sha256`: SHA-256 of a length-delimited, versioned UTF-8 encoding of exactly the text fed to the encoder. Today that is semantic `content`; episodic `content + " " + (goal or "") + " " + (outcome or "")`; procedural `skill_name + " " + (trigger or "") + " " + (description or "")`. Include the formula version so changing this logic forces a migration.
- `vector_sha256`: SHA-256 over the canonical little-endian float32 vector bytes and declared dimension, not NumPy pickle serialization. Include encoder/backend identity, model revision or immutable model checksum, output dimension, and normalization settings in a `model_fingerprint`. An encoder name alone is not a reproducible model identity.
- A unique embedding ID and generation number. Reject duplicates across layers; the current generated ID is a hash modulo `2**31-1`, so collisions must be handled, not assumed impossible.

The manifest can live in a versioned NumPy snapshot envelope, with SQLite storing committed generation and the source hashes in an `embedding_manifest` table keyed by a stable row identity and unique embedding ID. Prefer a non-pickle, validated snapshot encoding as a separate security migration: current `np.load(..., allow_pickle=True)` must not be interpreted as authenticated data. Keep the SQLite schema migration transactional and indexed. A digest in the same writable trust boundary detects accidental drift and partial writes, **not** an attacker who can edit DB, vectors, and manifest together. Cryptographic tamper evidence requires an independent anchor or signing key outside that boundary.

## Write, recovery, and migration sequence

1. Before migration, take and restore-test a consistent SQLite/vector backup. In a schema migration transaction, add version/generation metadata and manifest rows marked `unverified`; do not calculate a hash from legacy vectors and label it proven.
2. Gate memory routes and readiness. Stream DB source rows from a consistent read snapshot; re-encode legacy rows **once** in bounded batches using the selected pinned model. Write a complete temporary snapshot with IDs, vectors, manifest and generation; fsync file and containing directory, then atomically replace it. Verify dimensions, duplicate IDs, all source/vector digests, row count, model fingerprint and a sample search before marking the generation committed in SQLite. Preserve old snapshot/DB backup until validation succeeds. The exact two-resource cutover needs a crash-recovery journal or generation protocol; neither SQLite commit nor file rename alone makes the pair atomic.
3. New writes/updates/deletes must update source, manifest and vector snapshot under a serialized per-service write path. An interrupted operation leaves a pending generation, not a silently ready store. On restart, compare committed DB generation with the snapshot, replay or rebuild affected rows from DB, then expose readiness. Do not serve memory routes during reconciliation. Test failure after each write boundary, including delayed vector saves and process kill.
4. Migration failure leaves the old backup intact and the service unready for vector-backed memory calls. If the model or revision is missing, fail closed rather than substituting dummy vectors. Keyword-only mode may be an explicit separate operator choice, not an automatic fallback.

## Startup cost and limits

Normal startup streams IDs plus source text and hashes each source once, and hashes each vector once: O(N + total source bytes + total vector bytes), with bounded memory and no model inference. Disk reads of all vectors can be costly at scale; record actual throughput and set a startup SLO before implementation. A stronger incremental checkpoint can skip unchanged generations only if the checkpoint is trustworthy and crash semantics are tested. One-time migration does O(N) model inference and can be long and memory-intensive; make it an explicit maintenance window with progress, resumable batches, disk-space checks and rollback. Model revision changes require re-embedding, not a metadata-only hash refresh.

A same-ID stale vector becomes detectable when its byte digest differs from the committed manifest; a DB content change without a matching manifest update becomes detectable when the source digest differs. These checks do not prove the semantic quality or truth of the embedding or protect against a fully privileged attacker rewriting all local records. Tests should cover both directions, duplicate IDs, collisions, cross-layer mapping, model mismatch, corruption, partial writes, old snapshots, empty stores and crash during cutover.

Decision before code: choose the supported embedding model/version policy, rollout window and restore target. No change to production or Mac services is proposed here.
