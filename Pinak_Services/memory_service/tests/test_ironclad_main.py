import datetime
import uuid
import pytest
import jwt
import os
from fastapi.testclient import TestClient
from app.main import app

@pytest.fixture
def auth_token():
    secret = "test-secret"
    payload = {
        "tenant": "t1",
        "project_id": "p1",
        "roles": ["admin"],
        "scopes": ["memory.read", "memory.write", "memory.admin"],
    }
    token = jwt.encode({**payload, "iss": "pinak-memory", "aud": "pinak-memory-api", "jti": str(uuid.uuid4()), "exp": payload.get("exp", datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=5))}, secret, algorithm="HS256")
    return token

@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c

def test_app_lifespan(client):
    response = client.get("/")
    assert response.status_code == 200

def test_retrieve_context(client, auth_token):
    headers = {"Authorization": f"Bearer {auth_token}"}
    response = client.get("/api/v1/memory/retrieve_context", params={"query": "test"}, headers=headers)
    assert response.status_code == 200

def test_add_episodic(client, auth_token):
    headers = {"Authorization": f"Bearer {auth_token}"}
    payload = {"content": "event", "salience": 5, "goal": "win", "plan": ["a"], "outcome": "won", "tool_logs": []}
    response = client.post("/api/v1/memory/episodic/add", json=payload, headers=headers)
    assert response.status_code == 201

def test_add_procedural(client, auth_token):
    headers = {"Authorization": f"Bearer {auth_token}"}
    payload = {"skill_name": "punch", "steps": ["fist", "hit"], "trigger": "fight", "code_snippet": "print(1)"}
    response = client.post("/api/v1/memory/procedural/add", json=payload, headers=headers)
    assert response.status_code == 201

def test_add_rag(client, auth_token):
    headers = {"Authorization": f"Bearer {auth_token}"}
    payload = {"query": "how to", "external_source": "wiki", "content": "do it"}
    response = client.post("/api/v1/memory/rag/add", json=payload, headers=headers)
    assert response.status_code == 201

def test_update_memory_404(client, auth_token):
    headers = {"Authorization": f"Bearer {auth_token}"}
    response = client.put("/api/v1/memory/semantic/nonexistent", json={"content": "new"}, headers=headers)
    assert response.status_code == 404

def test_delete_memory_404(client, auth_token):
    headers = {"Authorization": f"Bearer {auth_token}"}
    response = client.delete("/api/v1/memory/semantic/nonexistent", headers=headers)
    assert response.status_code == 404

def test_list_events(client, auth_token):
    headers = {"Authorization": f"Bearer {auth_token}"}
    response = client.get("/api/v1/memory/events", headers=headers)
    assert response.status_code == 200

def test_list_session(client, auth_token):
    headers = {"Authorization": f"Bearer {auth_token}"}
    response = client.get("/api/v1/memory/session/list", params={"session_id": "s1"}, headers=headers)
    assert response.status_code == 200

def test_list_working(client, auth_token):
    headers = {"Authorization": f"Bearer {auth_token}"}
    response = client.get("/api/v1/memory/working/list", headers=headers)
    assert response.status_code == 200
