import os
import pytest
import jwt
from fastapi import HTTPException
from app.core.security import require_auth_context, AuthContext
from fastapi.security import HTTPAuthorizationCredentials

@pytest.fixture
def jwt_secret():
    os.environ["PINAK_JWT_SECRET"] = "test-secret"
    yield "test-secret"
    if "PINAK_JWT_SECRET" in os.environ:
        del os.environ["PINAK_JWT_SECRET"]

def test_require_auth_context_missing_credentials():
    with pytest.raises(HTTPException) as exc:
        require_auth_context(None)
    assert exc.value.status_code == 401
    assert "Missing bearer token" in exc.value.detail

def test_require_auth_context_invalid_token(jwt_secret):
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="invalid-token")
    with pytest.raises(HTTPException) as exc:
        require_auth_context(creds)
    assert exc.value.status_code == 401
    assert "Invalid token" in exc.value.detail

def test_require_auth_context_expired_token(jwt_secret):
    token = jwt.encode({"exp": 0}, jwt_secret, algorithm="HS256")
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    with pytest.raises(HTTPException) as exc:
        require_auth_context(creds)
    assert exc.value.status_code == 401
    assert "Token expired" in exc.value.detail

def test_require_auth_context_missing_tenant(jwt_secret):
    token = jwt.encode({"project_id": "p1"}, jwt_secret, algorithm="HS256")
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    with pytest.raises(HTTPException) as exc:
        require_auth_context(creds)
    assert exc.value.status_code == 403
    assert "Tenant or project missing" in exc.value.detail

def test_require_auth_context_valid(jwt_secret):
    payload = {
        "sub": "user123",
        "tenant": "t1",
        "project_id": "p1",
        "roles": "admin",
        "scopes": ["memory.read", "memory.write"],
        "client_name": "codex"
    }
    token = jwt.encode(payload, jwt_secret, algorithm="HS256")
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    context = require_auth_context(creds)
    
    assert context.subject == "user123"
    assert context.tenant_id == "t1"
    assert context.project_id == "p1"
    assert context.roles == ["admin"] # auto-converted to list
    assert context.scopes == ["memory.read", "memory.write"]
    assert context.client_name == "codex"

def test_require_auth_context_header_overrides(jwt_secret):
    payload = {
        "sub": "user123",
        "tenant": "t1",
        "project_id": "p1",
        "roles": ["agent"],
        "scopes": ["memory.read"],
        "client_name": "token-client",
        "client_id": "token-client-id",
        "parent_client_id": "parent-client",
        "child_client_id": "child-client",
    }
    token = jwt.encode(payload, jwt_secret, algorithm="HS256")
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    context = require_auth_context(
        creds,
        client_id_header="token-client-id",
        client_name_header="header-client",
        parent_client_id_header="parent-client",
        child_client_id_alt="child-client",
    )

    assert context.client_id == "token-client-id"
    assert context.client_name == "header-client"
    assert context.parent_client_id == "parent-client"
    assert context.child_client_id == "child-client"
    assert context.effective_client_id == "child-client"
