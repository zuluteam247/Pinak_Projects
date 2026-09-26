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
        "client_name": "test-client",
        "client_id": "parent",
        "child_client_id": "child",
        "iat": datetime.datetime.utcnow(),
        "exp": datetime.datetime.utcnow() + datetime.timedelta(minutes=5),
    }
    return jwt.encode({**payload, "iss": "pinak-memory", "aud": "pinak-memory-api", "jti": str(uuid.uuid4()), "exp": payload.get("exp", datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=5))}, "test-secret", algorithm="HS256")


@pytest.mark.asyncio
async def test_client_summary_includes_children(test_app):
    token = _issue_token("t1", "p1")
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Pinak-Client-Id": "parent",
        "X-Pinak-Client-Name": "Parent",
    }
    child_headers = {
        **headers,
        "X-Pinak-Child-Client-Id": "child",
    }

    async with LifespanManager(test_app):
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
            # Register parent and child clients
            await client.post(
                "/api/v1/memory/client/register",
                headers=headers,
                json={"client_id": "parent", "client_name": "Parent", "status": "registered"},
            )
            await client.post(
                "/api/v1/memory/client/register",
                headers=headers,
                json={"client_id": "child", "client_name": "Child", "parent_client_id": "parent", "status": "registered"},
            )

            # Parent semantic memory
            resp_parent = await client.post(
                "/api/v1/memory/add",
                headers=headers,
                json={"content": "parent memory", "tags": ["a"]},
            )
            assert resp_parent.status_code == 201

            # Child episodic memory
            resp_child = await client.post(
                "/api/v1/memory/episodic/add",
                headers=child_headers,
                json={"content": "child event", "goal": "g"},
            )
            assert resp_child.status_code == 201

            summary = await client.get("/api/v1/memory/client/summary", headers=headers)
            assert summary.status_code == 200
            data = summary.json()

    assert data["client"]["client_id"] == "parent"
    assert data["summary"]["counts"]["semantic"] == 1
    assert data["summary"]["counts"]["episodic"] == 0

    children = {row["client_id"]: row for row in data["children"]}
    assert "child" in children
    assert children["child"]["counts"]["episodic"] == 1

    assert data["combined"]["counts"]["semantic"] == 1
    assert data["combined"]["counts"]["episodic"] == 1
