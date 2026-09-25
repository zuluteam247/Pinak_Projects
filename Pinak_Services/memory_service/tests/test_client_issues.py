import datetime
import json
import os
from typing import Dict

import jwt
import pytest
from fastapi.testclient import TestClient

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
    return MemoryService(config_path=str(config_path))


@pytest.fixture
def client(memory_service):
    app.dependency_overrides[get_memory_service] = lambda: memory_service
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _issue_token(tenant: str, project: str) -> str:
    payload: Dict[str, object] = {
        "sub": "tester",
        "tenant": tenant,
        "project_id": project,
        "roles": ["user"],
        "scopes": ["memory.read", "memory.write"],
        "client_name": "test-client",
        "client_id": "real-client",
        "iat": datetime.datetime.utcnow(),
        "exp": datetime.datetime.utcnow() + datetime.timedelta(minutes=5),
    }
    return jwt.encode(payload, "test-secret", algorithm="HS256")


def test_add_client_issue_returns_required_fields(client):
    token = _issue_token("t1", "p1")
    headers = {"Authorization": f"Bearer {token}", "X-Pinak-Client-Id": "real-client"}
    payload = {"error_code": "schema_validation_failed", "message": "bad payload", "layer": "semantic"}

    resp = client.post("/api/v1/memory/client/issues", json=payload, headers=headers)
    assert resp.status_code == 201
    data = resp.json()
    assert data["client_id"] == "real-client"
    assert data["error_code"] == "schema_validation_failed"
    assert data["message"] == "bad payload"
    assert data["status"] == "open"
