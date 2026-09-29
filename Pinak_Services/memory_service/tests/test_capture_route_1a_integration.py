"""Slice 1a integration tests: the capture route through the real app (P-85).

The unit suite calls the validator directly. These go through FastAPI so the
auth dependency, the scope check, the readiness guard and the phase gate are
all in the path, and they assert the thing that matters most about 1a: under
the 503 gate, nothing anywhere is written.
"""

import datetime
import hashlib
import json
import os
import uuid
from typing import Any, Dict, Optional

import jwt
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from app.api.v1.endpoints import get_memory_service
from app.main import app
from app.services.memory_service import MemoryService

TENANT = "tenant-alpha"
PROJECT = "project-1"
AGENT = "agent-fo"
CLIENT = "gemini-cli"


@pytest.fixture(autouse=True)
def _configure_environment(monkeypatch):
    monkeypatch.setenv("PINAK_JWT_SECRET", "test-secret")
    monkeypatch.setenv("PINAK_EMBEDDING_BACKEND", "dummy")


@pytest.fixture
def data_root(tmp_path):
    return tmp_path / "data"


@pytest.fixture
def memory_service(tmp_path, data_root):
    config = {
        "embedding_model": "dummy",
        "data_root": str(data_root),
        "redis_host": "localhost",
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    return MemoryService(config_path=str(config_path))


@pytest.fixture
def test_app(memory_service):
    app.dependency_overrides[get_memory_service] = lambda: memory_service
    yield app
    app.dependency_overrides.clear()


def _token(scopes=("memory.read", "memory.write"), agent_id: Optional[str] = AGENT,
           client_id: Optional[str] = CLIENT, tenant: str = TENANT) -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    payload: Dict[str, Any] = {
        "sub": "tester",
        "tenant": tenant,
        "project_id": PROJECT,
        "roles": ["user"],
        "scopes": list(scopes),
        "iat": now,
        "exp": now + datetime.timedelta(minutes=5),
        "iss": "pinak-memory",
        "aud": "pinak-memory-api",
        "jti": str(uuid.uuid4()),
    }
    if agent_id is not None:
        payload["agent_id"] = agent_id
    if client_id is not None:
        payload["client_id"] = client_id
    return jwt.encode(payload, "test-secret", algorithm="HS256")


def _envelope(**overrides) -> Dict[str, Any]:
    envelope = {
        "tenant_id": TENANT,
        "agent_id": AGENT,
        "client_id": CLIENT,
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
    return envelope


def _store_fingerprint(service: MemoryService, root) -> Dict[str, Any]:
    """Everything a capture write could possibly touch, before and after.

    Every table's row count plus a digest of every file under the data root.
    A minted session, an alias row, an audit row or a staged event would all
    move one of these.
    """

    counts: Dict[str, int] = {}
    with service.db.get_cursor() as cursor:
        tables = [row[0] for row in cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        for table in sorted(tables):
            counts[table] = cursor.execute(
                f"SELECT COUNT(*) FROM \"{table}\"").fetchone()[0]

    files: Dict[str, str] = {}
    if os.path.isdir(root):
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in sorted(filenames):
                path = os.path.join(dirpath, name)
                if name.endswith(("-wal", "-shm")):
                    # SQLite journal churn is not a capture write; the table
                    # counts above are what actually settle that question.
                    continue
                with open(path, "rb") as handle:
                    files[os.path.relpath(path, root)] = hashlib.sha256(
                        handle.read()).hexdigest()
    return {"tables": counts, "files": files}


async def _post(client, path, body, token=None, headers=None):
    request_headers = dict(headers or {})
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    return await client.post(path, json=body, headers=request_headers)


@pytest_asyncio.fixture
async def client(test_app):
    async with LifespanManager(test_app):
        async with AsyncClient(transport=ASGITransport(app=test_app),
                               base_url="http://test") as http_client:
            yield http_client


# --- auth and scope (CW 2.2, CW 8) -----------------------------------------


@pytest.mark.asyncio
async def test_capture_without_a_token_is_401(client):
    response = await _post(client, "/api/v1/memory/capture", _envelope())
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_capture_with_a_bad_signature_is_401(client):
    now = datetime.datetime.now(datetime.timezone.utc)
    forged = jwt.encode({"sub": "tester", "tenant": TENANT, "project_id": PROJECT,
                         "scopes": ["memory.write"], "agent_id": AGENT,
                         "client_id": CLIENT, "iss": "pinak-memory",
                         "aud": "pinak-memory-api", "jti": str(uuid.uuid4()),
                         "exp": now + datetime.timedelta(minutes=5)},
                        "not-the-secret", algorithm="HS256")
    response = await _post(client, "/api/v1/memory/capture", _envelope(), forged)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_capture_without_the_write_scope_is_403(client):
    response = await _post(client, "/api/v1/memory/capture", _envelope(),
                           _token(scopes=("memory.read",)))
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_capture_without_a_signed_agent_claim_is_403(client):
    # The old code fell back to sub, then to the literal "unknown", which
    # bound an event to an identity nobody signed for.
    response = await _post(client, "/api/v1/memory/capture", _envelope(),
                           _token(agent_id=None))
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "auth_claim_missing"
    assert response.json()["detail"]["missing_claims"] == ["agent_id"]


@pytest.mark.asyncio
async def test_capture_without_a_signed_client_claim_is_403(client):
    response = await _post(client, "/api/v1/memory/capture", _envelope(),
                           _token(client_id=None))
    assert response.status_code == 403
    assert response.json()["detail"]["missing_claims"] == ["client_id"]


@pytest.mark.asyncio
async def test_a_header_cannot_supply_the_client_identity(client):
    # A client id header is a hint. With no signed claim it must not become
    # the bound identity.
    response = await _post(client, "/api/v1/memory/capture", _envelope(),
                           _token(client_id=None),
                           headers={"X-Pinak-Client-Id": CLIENT})
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_body_identity_that_contradicts_the_claim_is_403(client):
    response = await _post(client, "/api/v1/memory/capture",
                           _envelope(tenant_id="tenant-beta"), _token())
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "auth_binding_mismatch"


# --- the phase gate (CW 7.4) ------------------------------------------------


@pytest.mark.asyncio
async def test_a_schema_valid_envelope_is_503_capture_disabled_never_202(client):
    response = await _post(client, "/api/v1/memory/capture", _envelope(), _token())
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "capture_disabled"


@pytest.mark.asyncio
async def test_503_carries_no_retry_after_header(client):
    response = await _post(client, "/api/v1/memory/capture", _envelope(), _token())
    assert "retry-after" not in {key.lower() for key in response.headers}


@pytest.mark.asyncio
async def test_session_open_is_503_and_mints_nothing(client):
    response = await _post(client, "/api/v1/memory/capture/session",
                           {"client_session_ref": "cli-run-7"}, _token())
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "capture_disabled"
    assert "session_id" not in response.text


# --- schema failures through the app ---------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("override,expected", [
    ({"envelope_version": "0.2"}, 400),
    ({"envelope_version": "9.9"}, 400),
    ({"actor": "robot"}, 422),
    ({"boundary_source": "unapproved"}, 422),
    ({"source": "unapproved"}, 422),
    ({"payload": {"text": "hi", "meta": {"cot": "x"}}}, 422),
    ({"received_ts": "2026-09-29T13:45:00Z"}, 422),
])
async def test_schema_failures_surface_with_the_contract_status(client, override,
                                                                expected):
    response = await _post(client, "/api/v1/memory/capture",
                           _envelope(**override), _token())
    assert response.status_code == expected


@pytest.mark.asyncio
async def test_null_task_id_reaches_the_gate_rather_than_a_422(client):
    response = await _post(client, "/api/v1/memory/capture",
                           _envelope(task_id=None), _token())
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_error_bodies_do_not_echo_payload_content(client):
    secret = "sk-live-do-not-log"
    response = await _post(client, "/api/v1/memory/capture",
                           _envelope(actor="robot", payload={"text": secret}),
                           _token())
    assert response.status_code == 422
    assert secret not in response.text


# --- zero writes, before and after (CW 7.4) --------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [
    {"body": {}, "token": True, "status": 503},
    {"body": {"task_id": None}, "token": True, "status": 503},
    {"body": {"actor": "robot"}, "token": True, "status": 422},
    {"body": {"tenant_id": "tenant-beta"}, "token": True, "status": 403},
    {"body": {"envelope_version": "0.2"}, "token": True, "status": 400},
    {"body": {}, "token": False, "status": 401},
])
async def test_capture_writes_nothing_anywhere(client, memory_service, data_root,
                                               case):
    before = _store_fingerprint(memory_service, data_root)
    response = await _post(client, "/api/v1/memory/capture",
                           _envelope(**case["body"]),
                           _token() if case["token"] else None)
    assert response.status_code == case["status"]
    after = _store_fingerprint(memory_service, data_root)
    assert after["tables"] == before["tables"]
    assert after["files"] == before["files"]


@pytest.mark.asyncio
async def test_session_open_writes_nothing_anywhere(client, memory_service,
                                                    data_root):
    before = _store_fingerprint(memory_service, data_root)
    response = await _post(client, "/api/v1/memory/capture/session",
                           {"client_session_ref": "cli-run-7"}, _token())
    assert response.status_code == 503
    after = _store_fingerprint(memory_service, data_root)
    assert after["tables"] == before["tables"]
    assert after["files"] == before["files"]


@pytest.mark.asyncio
async def test_repeated_capture_attempts_stay_write_free(client, memory_service,
                                                         data_root):
    before = _store_fingerprint(memory_service, data_root)
    for sequence in range(5):
        response = await _post(client, "/api/v1/memory/capture",
                               _envelope(sequence=sequence), _token())
        assert response.status_code == 503
    after = _store_fingerprint(memory_service, data_root)
    assert after["tables"] == before["tables"]
    assert after["files"] == before["files"]
