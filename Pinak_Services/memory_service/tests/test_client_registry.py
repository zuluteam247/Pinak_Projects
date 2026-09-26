import datetime
import uuid
import jwt
import os
import pytest
from fastapi.testclient import TestClient
from app.main import app


@pytest.fixture
def auth_token():
    secret = "test-secret"
    os.environ["PINAK_JWT_SECRET"] = secret
    payload = {
        "tenant": "t1",
        "project_id": "p1",
        "roles": ["user"],
        "scopes": ["memory.read", "memory.write"],
        "client_name": "test-client",
    }
    return jwt.encode({**payload, "iss": "pinak-memory", "aud": "pinak-memory-api", "jti": str(uuid.uuid4()), "exp": payload.get("exp", datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=5))}, secret, algorithm="HS256")


@pytest.fixture
def admin_token():
    secret = "test-secret"
    os.environ["PINAK_JWT_SECRET"] = secret
    payload = {
        "tenant": "t1",
        "project_id": "p1",
        "roles": ["admin"],
        "scopes": ["memory.read", "memory.write", "memory.admin"],
        "client_name": "admin-client",
    }
    return jwt.encode({**payload, "iss": "pinak-memory", "aud": "pinak-memory-api", "jti": str(uuid.uuid4()), "exp": payload.get("exp", datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=5))}, secret, algorithm="HS256")


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_client_register_and_list(client, auth_token):
    headers = {"Authorization": f"Bearer {auth_token}"}
    payload = {
        "client_id": "client-1",
        "client_name": "Client One",
        "parent_client_id": "parent-1",
        "status": "registered",
        "metadata": {"note": "test"},
    }
    resp = client.post("/api/v1/memory/client/register", json=payload, headers=headers)
    assert resp.status_code == 201
    data = resp.json()
    assert data["client_id"] == "client-1"

    resp_list = client.get("/api/v1/memory/client/list", headers=headers)
    assert resp_list.status_code == 200
    ids = {row["client_id"] for row in resp_list.json()}
    assert "client-1" in ids


def test_client_register_trusted_requires_admin(client, auth_token, admin_token):
    headers = {"Authorization": f"Bearer {auth_token}"}
    payload = {"client_id": "client-2", "status": "trusted"}
    resp = client.post("/api/v1/memory/client/register", json=payload, headers=headers)
    assert resp.status_code == 403

    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    resp_admin = client.post("/api/v1/memory/client/register", json=payload, headers=admin_headers)
    assert resp_admin.status_code == 201
