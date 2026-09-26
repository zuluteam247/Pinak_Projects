import uuid
import datetime
import json
import os
from typing import Dict

import jwt
import pytest
from httpx import AsyncClient, ASGITransport
from asgi_lifespan import LifespanManager

from app.main import app
from app.api.v1.endpoints import get_memory_service
from app.services.memory_service import MemoryService


@pytest.fixture(autouse=True)
def _configure_environment(monkeypatch):
    monkeypatch.setenv("PINAK_JWT_SECRET", "test-secret")
    monkeypatch.setenv("PINAK_EMBEDDING_BACKEND", "dummy")
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("EMBEDDING_BACKEND", "dummy")


@pytest.fixture
def memory_service(tmp_path):
    config = {
        "embedding_model": "dummy",
        "data_root": str(tmp_path / "data"),
        "redis_host": "localhost",
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    service = MemoryService(config_path=str(config_path))
    return service


@pytest.fixture
def test_app(memory_service):
    app.dependency_overrides[get_memory_service] = lambda: memory_service
    yield app
    app.dependency_overrides.clear()


def _issue_token(tenant: str, project: str, subject: str = "tester") -> str:
    payload: Dict[str, object] = {
        "sub": subject,
        "tenant": tenant,
        "project_id": project,
        "roles": ["user"],
        "scopes": ["memory.read", "memory.write"],
        "iat": datetime.datetime.utcnow(),
        "exp": datetime.datetime.utcnow() + datetime.timedelta(minutes=5),
    }
    return jwt.encode({**payload, "iss": "pinak-memory", "aud": "pinak-memory-api", "jti": str(uuid.uuid4()), "exp": payload.get("exp", datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=5))}, "test-secret", algorithm="HS256")


@pytest.mark.asyncio
async def test_missing_token_is_rejected(test_app):
    async with LifespanManager(test_app):
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
            # Need to provide valid payload even for auth check usually, or it fails validation first
            # But Depends usually runs before Body validation? Let's see.
            response = await client.get("/api/v1/memory/events")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_memory_isolation_between_tenants(test_app, memory_service):
    token_a = _issue_token("tenant-a", "project-1")
    token_b = _issue_token("tenant-b", "project-1")

    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    async with LifespanManager(test_app):
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
            await client.post("/api/v1/memory/add", headers=headers_a, json={"content": "alpha memory", "tags": ["alpha"]})
            await client.post("/api/v1/memory/add", headers=headers_b, json={"content": "beta memory", "tags": ["beta"]})

            search_a = await client.get("/api/v1/memory/search", headers=headers_a, params={"query": "alpha"})
            search_b = await client.get("/api/v1/memory/search", headers=headers_b, params={"query": "beta"})

    assert search_a.status_code == 200
    assert search_b.status_code == 200
    # The search endpoint now returns Pydantic models.
    # We check if content matches.
    res_a = search_a.json()
    res_b = search_b.json()

    assert len(res_a) >= 1
    assert len(res_b) >= 1
    assert res_a[0]["content"] == "alpha memory"
    assert res_b[0]["content"] == "beta memory"

    # Verify Metadata if returned
    assert res_a[0]["tenant"] == "tenant-a"
    assert res_b[0]["tenant"] == "tenant-b"


@pytest.mark.asyncio
async def test_audit_log_persistence(test_app, memory_service):
    token = _issue_token("tenant-log", "project-log")
    headers = {"Authorization": f"Bearer {token}"}

    async with LifespanManager(test_app):
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
            for step in range(3):
                await client.post(
                    "/api/v1/memory/event",
                    headers=headers,
                    json={"event_type": "audit", "payload": {"step": step}},
                )

            # Verify retrieval
            response = await client.get("/api/v1/memory/events", headers=headers)
            assert response.status_code == 200
            data = response.json()
            assert len(data) == 3
            # Check LIFO order (DESC)
            assert json.loads(data[0]['payload'])['step'] == 2
            assert json.loads(data[2]['payload'])['step'] == 0


@pytest.mark.asyncio
async def test_session_and_working_memory_are_scoped(test_app):
    token_a = _issue_token("tenant-a", "project-1")
    token_b = _issue_token("tenant-b", "project-1")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    async with LifespanManager(test_app):
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
            await client.post(
                "/api/v1/memory/session/add",
                headers=headers_a,
                json={"session_id": "conversation", "content": "hello", "role": "user"},
            )
            await client.post(
                "/api/v1/memory/session/add",
                headers=headers_b,
                json={"session_id": "conversation", "content": "hola", "role": "user"},
            )

            await client.post(
                "/api/v1/memory/working/add",
                headers=headers_a,
                json={"content": "scratchpad"},
            )
            await client.post(
                "/api/v1/memory/working/add",
                headers=headers_b,
                json={"content": "notepad"},
            )

            session_a = await client.get(
                "/api/v1/memory/session/list",
                headers=headers_a,
                params={"session_id": "conversation"},
            )
            session_b = await client.get(
                "/api/v1/memory/session/list",
                headers=headers_b,
                params={"session_id": "conversation"},
            )

            working_a = await client.get("/api/v1/memory/working/list", headers=headers_a)
            working_b = await client.get("/api/v1/memory/working/list", headers=headers_b)

    assert session_a.status_code == 200
    assert session_b.status_code == 200
    assert working_a.status_code == 200
    assert working_b.status_code == 200

    assert all(entry["tenant"] == "tenant-a" for entry in session_a.json())
    assert all(entry["tenant"] == "tenant-b" for entry in session_b.json())
    assert all(entry["tenant"] == "tenant-a" for entry in working_a.json())
    assert all(entry["tenant"] == "tenant-b" for entry in working_b.json())


@pytest.mark.asyncio
async def test_invalid_token_is_rejected(test_app):
    headers = {"Authorization": "Bearer invalid"}
    async with LifespanManager(test_app):
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
            # list_episodic no longer exists in same path form, checking events
            response = await client.get("/api/v1/memory/events", headers=headers)
    assert response.status_code == 401
