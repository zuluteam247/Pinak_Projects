"""Security utilities for enforcing JWT-based authentication."""

from dataclasses import dataclass
from typing import List, Optional
import os

import jwt
from fastapi import Depends, HTTPException, status, Header, Request
from fastapi.params import Header as HeaderParam
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer


@dataclass
class AuthContext:
    """Represents the authenticated request context."""

    subject: Optional[str]
    tenant_id: str
    project_id: str
    roles: List[str]
    scopes: List[str]
    client_name: Optional[str]
    client_id: Optional[str]
    parent_client_id: Optional[str]
    child_client_id: Optional[str]
    effective_client_id: str
    token: str


_http_bearer = HTTPBearer(auto_error=False)


def _get_secret() -> str:
    secret = os.getenv("PINAK_JWT_SECRET")
    if not secret or secret in {"secret", "dev-secret-change-me"}:
        raise RuntimeError("PINAK_JWT_SECRET must be set to a non-default signing secret")
    return secret


def require_auth_context(
    credentials: HTTPAuthorizationCredentials = Depends(_http_bearer),
    client_id_header: Optional[str] = Header(default=None, alias="X-Pinak-Client-Id"),
    client_name_header: Optional[str] = Header(default=None, alias="X-Pinak-Client-Name"),
    parent_client_id_header: Optional[str] = Header(default=None, alias="X-Pinak-Parent-Client-Id"),
    child_client_id: Optional[str] = Header(default=None, alias="X-Pinak-Child-Id"),
    child_client_id_alt: Optional[str] = Header(default=None, alias="X-Pinak-Child-Client-Id"),
    request: Request = None,
) -> AuthContext:
    """FastAPI dependency that validates a JWT and extracts tenant context."""

    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")

    token = credentials.credentials
    secret = _get_secret()
    algorithm = os.getenv("PINAK_JWT_ALGORITHM", "HS256")

    issuer = os.getenv("PINAK_JWT_ISSUER", "pinak-memory")
    audience = os.getenv("PINAK_JWT_AUDIENCE", "pinak-memory-api")
    try:
        payload = jwt.decode(token, secret, algorithms=[algorithm],
                             issuer=issuer, audience=audience,
                             options={"require": ["exp", "iss", "aud", "jti"]})
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc

    jti = payload.get("jti")
    if not isinstance(jti, str) or not jti or len(jti) > 256:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token id")
    if request is not None:
        # Use the exact live service DB, including dependency overrides in tests.
        service = getattr(request.app.state, "memory_service", None)
        if service is None:
            raise HTTPException(status_code=503, detail="Memory service unavailable")
        db = service.db
    else:
        # Direct calls to this dependency are used by unit tests.
        from app.api.v1.endpoints import get_memory_service
        db = get_memory_service().db
    if db.is_jti_revoked(jti):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token revoked")

    tenant = payload.get("tenant") or payload.get("tenant_id")
    project_id = payload.get("project_id") or payload.get("project")
    if not tenant or not project_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tenant or project missing in token")

    roles = payload.get("roles") or []
    if not isinstance(roles, list):
        roles = [str(roles)]

    def _normalize_header(value: Optional[str]) -> Optional[str]:
        if isinstance(value, HeaderParam):
            return None
        return value

    client_id_header = _normalize_header(client_id_header)
    client_name_header = _normalize_header(client_name_header)
    parent_client_id_header = _normalize_header(parent_client_id_header)
    child_client_id = _normalize_header(child_client_id)
    child_client_id_alt = _normalize_header(child_client_id_alt)

    # Headers are provenance hints, never authority to impersonate a trusted client.
    # Each asserted ID must match the signed claim; a missing claim cannot be
    # populated by an untrusted header. Admins can register a client separately.
    for header_value, claim_value in (
        (client_id_header, payload.get("client_id") or payload.get("cid")),
        (parent_client_id_header, payload.get("parent_client_id") or payload.get("parent_client")),
        (child_client_id or child_client_id_alt, payload.get("child_client_id")),
    ):
        if header_value and header_value != claim_value:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Client identity header does not match token")
    if child_client_id and child_client_id_alt and child_client_id != child_client_id_alt:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Conflicting child identity headers")
    resolved_child_id = child_client_id or child_client_id_alt
    client_name = client_name_header or payload.get("client_name") or payload.get("client")
    client_id = client_id_header or payload.get("client_id") or payload.get("cid") or client_name
    parent_client_id = parent_client_id_header or payload.get("parent_client_id") or payload.get("parent_client")
    effective_client_id = resolved_child_id or client_id or payload.get("sub") or "unknown"

    return AuthContext(
        subject=payload.get("sub"),
        tenant_id=str(tenant),
        project_id=str(project_id),
        roles=[str(role) for role in roles],
        scopes=[str(scope) for scope in (payload.get("scopes") or [])],
        client_name=client_name,
        client_id=client_id,
        parent_client_id=parent_client_id,
        child_client_id=resolved_child_id,
        effective_client_id=effective_client_id,
        token=token,
    )


def require_scope(ctx: AuthContext, scope: str) -> None:
    if scope not in ctx.scopes:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Missing scope: {scope}")


def require_role(ctx: AuthContext, role: str) -> None:
    if role not in ctx.roles:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Missing role: {role}")
