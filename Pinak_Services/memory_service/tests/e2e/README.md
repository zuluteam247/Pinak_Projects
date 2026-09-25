# Disposable external-client verification

These are harnesses, not proof that a desktop/autonomous agent is configured. The HTTP harness starts in a separate process; the MCP harness establishes genuine stdio protocol sessions, lists tools and calls them. Neither connects to a Mac, Gemini app, or production service.

## Run from `Pinak_Services/memory_service`

Use a fresh temporary directory and a **new local-only secret**. Do not use a real user dataset or a live service. The examples bind only to 127.0.0.1 and do not publish tokens. No `--reload` or production deployment.

```
uv sync --extra tests --extra mcp
export PINAK_JWT_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
export PINAK_EMBEDDING_BACKEND=none
export PINAK_CONFIG_PATH="$(mktemp)"
export PINAK_E2E_DATA_ROOT="$(mktemp -d)"
python3 -c 'import json,os; json.dump({"data_root":os.environ["PINAK_E2E_DATA_ROOT"],"embedding_model":"dummy"},open(os.environ["PINAK_CONFIG_PATH"],"w"))'
uv run uvicorn app.main:app --host 127.0.0.1 --port 18765
```

In a second shell with the *same* exported environment:

```
uv run python tests/e2e/seven_layer_http.py
PINAK_MCP_PYTHON="$(pwd)/.venv/bin/python" uv run python tests/e2e/mcp_surface.py
uv run pytest tests/test_mcp_security_regression.py -q
PINAK_SECURITY_RESULTS=/tmp/security-results.json uv run python tests/e2e/security_http.py
PINAK_MCP_PYTHON="$(pwd)/.venv/bin/python" PINAK_INJECTION_RESULTS=/tmp/injection-results.json uv run python tests/e2e/prompt_injection.py
```

Stop Uvicorn after the run. The shell secret and data directory are disposable. The MCP harness purposely calls admin mutation and delete on records it just created in the isolated fixture DB, and uses a scoped synthetic admin JWT minted from the local test secret. Do not point it at a shared service. The script's 204 delete adapter returns an explicit status for the fixture; the underlying API returns 204. It also exercises failed writes by a read-only token, cross-tenant reads/edits/deletes, repeated quarantine review, and blocked agent admin calls.

## Recorded evidence

- `seven_layer_initial_results.json`: original 25 September 2026 HTTP run, six ID-matched read/write layers, RAG write 201 but hybrid GET omitted its ID. This is a historical failure record, not an expected result after the patch. It used a deterministic dummy embedding backend and isolated fixture data.
- `seven_layer_retest_results.json`: separate-process HTTP run after the patch, seven ID-matched read/write layers and RAG included in hybrid retrieval; keyword-only backend (no vectors), isolated fixture data.
- `mcp_surface_results.json`: 47 protocol tool calls with input, output, client role, and errors. UUIDs, timestamps and counts vary per run. The test used FastMCP 2.14.3 with a disposable venv. Some intentional negative calls raise errors; classify by role and expected policy rather than asserting every `error` is false.

No JWT, password, or source corpus is stored in these artifacts. The scripts obtain the signing secret at runtime and do not print it. The MCP surface harness's `PINAK_JWT_TOKEN` is passed privately to its child process and never written to the result JSON.

`security_http.py` is another local-only harness (`PINAK_SECURITY_RESULTS=/tmp/security-results.json uv run python tests/e2e/security_http.py`). Recorded `security_http_results.json` covers 22 synthetic cases: auth failures, scope/role/header claims, tenant/project reads and admin mutations, SQL-like fields, Unicode, 12 concurrent event writes and chain consistency. Initial penetration run exposed two gaps: 1.1 MB content was accepted without a size cap, and a cross-tenant quarantine approval returned 200 with `status: missing`. The follow-up patch bounds bodies to 1 MiB and returns 404 for missing/unscoped review IDs. The latest results must be read against the commit they came from; no pre-patch result counts as a pass. This harness operates only on its fixture IDs and refuses a non-loopback API URL.

`prompt_injection.py` stores a synthetic malicious instruction in all seven write paths and episodic quarantine, then recalls through stdio MCP. The result includes the malicious words as *data* with a warning, source and ID. A second-tenant token retrieves zero matches. `prompt_injection_results.json` is the recorded transport/provenance result. This is **not proof that an autonomous consuming model refuses the instruction**: this harness only called `recall`, not a separate model offered tools or unrelated private data. An agent-level red-team run must supply a consuming agent with only synthetic unrelated data and a fake outbound sink, record its tool trace, and show it did not fetch, disclose or change scope. Do not claim that escaping JSON or labeling untrusted text guarantees this outcome.
