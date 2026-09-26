import uuid
import datetime
import jwt
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(autouse=True)
def _set_secret(monkeypatch):
    monkeypatch.setenv("PINAK_JWT_SECRET", "test-secret")


def _issue_token():
    payload = {
        "sub": "tester",
        "tenant": "t1",
        "project_id": "p1",
        "roles": ["user"],
        "scopes": ["memory.read"],
        "iat": datetime.datetime.utcnow(),
        "exp": datetime.datetime.utcnow() + datetime.timedelta(minutes=5),
    }
    return jwt.encode({**payload, "iss": "pinak-memory", "aud": "pinak-memory-api", "jti": str(uuid.uuid4()), "exp": payload.get("exp", datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=5))}, "test-secret", algorithm="HS256")


def test_schema_requires_auth():
    with TestClient(app) as client:
        resp = client.get("/api/v1/memory/schema")
    assert resp.status_code == 401


def test_schema_list_hides_paths():
    token = _issue_token()
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        resp = client.get("/api/v1/memory/schema", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "schema_dir" not in data
    assert "fallback_dir" not in data
    assert "schemas" in data


def test_schema_layer_sanitization():
    token = _issue_token()
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        resp = client.get("/api/v1/memory/schema/..evil", headers=headers)
    assert resp.status_code == 400


def test_schema_layer_ok():
    token = _issue_token()
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        resp = client.get("/api/v1/memory/schema/semantic", headers=headers)
    assert resp.status_code == 200
    assert resp.json().get("type") == "object"
