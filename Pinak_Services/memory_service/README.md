# 🏹 Pinak Memory Service

**The Persistent Long-Term Context Substrate for AI Agents**

This service provides a unified, persistent memory layer for all AI agents operating within the Pinak ecosystem. It enables agents to "remember" past sessions, detect risks proactively, and maintain continuity across CLI and IDE environments.

## 🚀 Key Capabilities

### 1. **Persistent Long-Term Memory**
- **Semantic Layer**: Stores project identities and core constraints.
- **Episodic Layer**: Archives time-sequenced session summaries (Goals, Outcomes, Artifacts).
- **Procedural Layer**: Distills verified workflows and skill patterns.
- **Working Memory**: Ephemeral buffer for current task reasoning.

### 2. **Proactive Nudging** (New!)
- **Intent-Based Triggers**: Detects risky patterns in real-time based on agent intent.
- **Historical Risk Detection**: Surfaces past failures ("Nudges") relevant to the current task.
- **Cross-Layer Search**: Scans Semantic, Episodic, and Procedural layers simultaneously.

### 3. **Pinak Command Center (TUI)**
- **Agent Swarm View**: Real-time observability of all active and historical agent entities.
- **Deep Trace Analysis**: Browse session goals, outcomes, and timestamps.
- **System Health**: Live monitoring of database integrity and vector manifold state.

---

## 🛠️ Human Operations (Command Center)

### ✅ Install
```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 🟢 One-Command Startup
Launch the full Memory Command Center (Server + TUI) with a single command:
```bash
./pinak-memory
```
*Set a unique `PINAK_JWT_SECRET` first. The launcher rejects missing or known example secrets. This starts a local server and UI; it does not install a durable, round-the-clock service.*

### 🔍 Quick Context Search (CLI)
Query your persistent context directly without the UI:
```bash
export PINAK_JWT_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
uv run python cli/main.py search "disk cleanup strategies"
```

### 🩺 System Diagnostics
Run a full integrity check on the neural substrate:
```bash
uv run python cli/main.py doctor
```

---

## 🧩 Client Schemas & Identity

### Schemas/Templates (no fetch needed)
- **Local canonical**: `~/pinak-memory/schemas` and `~/pinak-memory/templates`
- **API**: `/api/v1/memory/schema` and `/api/v1/memory/schema/{layer}`

Doctor syncs these automatically (`cli/main.py doctor --fix`).

### Client Identity Model
- **Client ID** is expected on write requests (non‑blocking).
- Missing `client_id` is logged as a **Client Issue** and can be backfilled by Doctor.
- Child agents should pass `X-Pinak-Child-Client-Id` so lineage is visible.

---

## 🧠 Embeddings Backend

Choose behavior via `PINAK_EMBEDDING_BACKEND`:
- `none` → keyword-only search (stable, no model downloads).
- `dummy` → deterministic embeddings (tests/dev).
- `qmd` → use QMD’s embedding pipeline (run `qmd embed` after indexing).

> QMD expects `embeddinggemma-300M-Q8_0.gguf`. Ensure the model is present before enabling.

---

## 🔐 Admin Lockdown (Source Protection)
To prevent non‑admin changes to the memory service source, run:
```bash
scripts/pinak-lockdown.sh
```
This requires macOS admin password and applies immutable flags. Unlock with:
```bash
scripts/pinak-unlock.sh
```

---

## Integrity and readiness limits

`/api/v1/live` means the HTTP process responds. `/api/v1/health` returns HTTP 200 only after startup verification succeeds; while verification is pending, failed, or explicitly skipped, it returns HTTP 503. Memory API routes are also unavailable until readiness. The server launcher enables background verification by default, so callers must wait for readiness before sending memory requests.

Startup verification compares the multiset of database embedding IDs with the IDs in the NumPy vector snapshot and rebuilds the snapshot on a mismatch. This does **not** detect a vector with the correct ID but stale or corrupted vector values: the current snapshot has no independently checked per-record content digest. Backups and restore need a separate tested operating procedure; a passing health check does not prove backup recoverability.

The audit verifier checks the hash chain for retained rows. An attacker with write access to SQLite can delete the latest audit rows and leave a valid-looking shorter chain; local verification cannot detect that silent tail deletion. Stronger tamper evidence needs a separately controlled, append-only destination for the head digest, a defined anchoring cadence and custodian, and a restore-time comparison against that external anchor. No external anchor is configured here, and a head stored beside the mutable database would not close this gap.

## 💾 Daily Backups (Google Drive)
- Backup script: `scripts/pinak-memory-backup.sh`
- LaunchAgent: `com.pinak.memory.backup.plist`
- Requires **rclone** with a `gdrive:` remote.

---

## 🤖 Agent Operations (MCP Integration)

Agents interface with memory via the **Model Context Protocol (MCP)**. This abstracts authentication and vector complexity into simple tools.

### 1. Live Loading (Configuration)
The MCP server is now registered in your local Claude Desktop configuration. To activate it:

1.  **Restart Claude Desktop completely**.
2.  The tool `pinak-memory` will be available immediately.

Before configuring agents, install the MCP client (no repo access required):
```bash
scripts/pinak-install-mcp.sh
```

Custom location:
```bash
PINAK_MCP_HOME=/Users/Shared/pinak-memory scripts/pinak-install-mcp.sh
```

### 2. Manual Loading (Dev/Debug)
You can also run the MCP server directly for testing or inspection:
```bash
~/pinak-memory/bin/pinak-mcp
```

**Available Tools:**
- **`recall(query)`**: Semantic search across all memory layers. Used before starting tasks.
- **`remember_episode(...)`**: Stores execution results. Used after completing tasks.

### 3. Agent-Specific Configuration Reference
The following files have been automatically updated to include `pinak-memory`:

| Agent | Configuration File |
| :--- | :--- |
| **Claude Desktop** | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| **Google Gemini** | `~/.gemini/mcp.json` |
| **Google Antigravity** | `~/.gemini/antigravity/mcp_config.json` |
| **Codex CLI** | `~/.codex/mcp.json` (also uses `config.toml`, `auth.json`) |
| **Pi Coding Agent** | `~/.pi/agent/settings.json` |
| **AMP** | `~/.amp/mcp.json` |
| **OpenCode** | `~/.opencode/mcp_config.json` |
| **Cursor** | `~/.cursor/mcp.json` |

---

## 🛡️ Pinak Memory Protocol (PMP)
*Standard operating procedure for all agents.*

1.  **Sync on Wake**: Call `recall()` at session start.
2.  **Trace on Completion**: Log results to `remember_episode`.
3.  **Distill on Close**: Save reusable patterns to procedural memory.

---

## 📄 Client Onboarding (Quickstart)

### 1) Register Client
```bash
export PINAK_JWT_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
TOKEN=$(uv run python -m cli.main mint demo-tenant --project demo-project)

curl -sS -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  http://127.0.0.1:8000/api/v1/memory/client/register \
  -d '{
    "client_id": "codex",
    "client_name": "codex-cli",
    "status": "trusted"
  }'
```

### 2) Write Memory (Semantic)
```bash
curl -sS -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "X-Pinak-Client-Id: codex" \
  -H "X-Pinak-Client-Name: codex-cli" \
  http://127.0.0.1:8000/api/v1/memory/add \
  -d '{
    "content": "User prefers single-sprint delivery.",
    "tags": ["preference", "delivery"]
  }'
```

### 3) Child Agent (optional)
```bash
curl -sS -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "X-Pinak-Client-Id: codex" \
  -H "X-Pinak-Child-Client-Id: codex-subagent-1" \
  http://127.0.0.1:8000/api/v1/memory/episodic/add \
  -d '{
    "content": "Summarized the deployment plan.",
    "timestamp": "2026-01-31T12:00:00Z"
  }'
```

### 4) Retrieve Context
```bash
curl -sS \
  -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8000/api/v1/memory/retrieve_context?query=deployment plan"
```

### 5) Client Summary (Session Banner)
```bash
curl -sS \
  -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8000/api/v1/memory/client/summary"
```

### Remote API (Tailscale)
Current server Tailscale IP:
```
http://100.66.59.92:8000/api/v1/memory
```
Headers:
- `Authorization: Bearer <token>`
- `X-Pinak-Client-Id`
- `X-Pinak-Client-Name`
- `X-Pinak-Child-Client-Id` (optional)

Manual approval: first‑time clients are `registered`. Mark them **trusted** in TUI → Clients tab.

Prefer `PINAK_JWT_TOKEN` for clients (no shared secret required). Tokens now require `exp`, `iss`, `aud`, and a unique `jti`; default issuer is `pinak-memory`, default audience is `pinak-memory-api`, overridden by `PINAK_JWT_ISSUER` and `PINAK_JWT_AUDIENCE`. Legacy tokens without these claims are rejected. Rotate or reissue them before upgrading the service. CLI development tokens expire after one hour; MCP and ingestion-script tokens expire after 15 minutes. Revoke an issued `jti` by calling `DatabaseManager.revoke_jti(jti, expires_at)` against the service's `data/memory.db` (or configured `PINAK_DATA_ROOT/memory.db`) in a trusted operator context; there is no public revocation endpoint. Revocation takes effect at the next authenticated request. Back up the revocation table with the DB, and do not serve requests from a snapshot missing active revocations.

Access-event queries are truncated to 256 characters before storage in both `logs_access` and the immutable audit payload. The hourly cleanup keeps `logs_access` for 90 days by default; set `PINAK_ACCESS_LOG_RETENTION_DAYS` to a positive integer to change that interval. This is not retroactive redaction of historical audit payloads, which may still contain full queries. The audit chain is not pruned: finite audit retention requires a future epoch rollover with externally held checkpoints and a restore-time verification rule. OpenTimestamps anchoring remains a separate, un-deployed design; no external anchor is configured by this batch.

### Schemas & Templates
- Local canonical: `~/pinak-memory/schemas`, `~/pinak-memory/templates`
- API: `/api/v1/memory/schema`, `/api/v1/memory/schema/{layer}`

Doctor syncs these automatically:
```bash
python -m cli.main doctor --fix
```

### Lockdown / Unlock (Source Protection)
```bash
sudo scripts/pinak-lockdown.sh
sudo scripts/pinak-unlock.sh
```

### Future: Token Rotation
Planned: short‑lived token issuer + rotation endpoint per client.

---

**Status**: ✅ Active & Stable | **Memories**: 19 | **Vector Engine**: Numpy-Accelerated
