"""Token validation, revocation, access minimization and retention boundaries."""
import datetime
import json
import uuid

import jwt
from fastapi.testclient import TestClient
from app.main import app
from app.core.database import DatabaseManager

SECRET = "sandbox-test-key-at-least-thirty-two-chars"
NOW = datetime.datetime.now(datetime.timezone.utc)


def claims(**updates):
    value = {
        "sub": "fixture", "tenant": "a", "project_id": "p",
        "roles": ["agent"], "scopes": ["memory.read"],
        "exp": NOW + datetime.timedelta(minutes=5), "iss": "pinak-memory",
        "aud": "pinak-memory-api", "jti": str(uuid.uuid4()),
    }
    value.update(updates)
    return value


def test_required_claims_issuer_audience_and_revocation(tmp_path, monkeypatch):
    monkeypatch.setenv("PINAK_JWT_SECRET", SECRET)
    monkeypatch.setenv("PINAK_EMBEDDING_BACKEND", "none")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"data_root": str(tmp_path / "data"), "embedding_model": "dummy"}))
    monkeypatch.setenv("PINAK_CONFIG_PATH", str(config))
    from app.api.v1.endpoints import _service_factory
    _service_factory.cache_clear()
    try:
        with TestClient(app) as client:
            url = "/api/v1/memory/events"
            for missing in ("exp", "iss", "aud", "jti"):
                bad = claims()
                del bad[missing]
                response = client.get(url, headers={"Authorization": "Bearer " + jwt.encode(bad, SECRET, algorithm="HS256")})
                assert response.status_code == 401, (missing, response.text)
            for bad in (claims(iss="other-issuer"), claims(aud="other-api"), claims(jti=15), claims(jti="")):
                response = client.get(url, headers={"Authorization": "Bearer " + jwt.encode(bad, SECRET, algorithm="HS256")})
                assert response.status_code == 401, (bad, response.text)
            good = claims()
            headers = {"Authorization": "Bearer " + jwt.encode(good, SECRET, algorithm="HS256")}
            assert client.get(url, headers=headers).status_code == 200
            app.state.memory_service.db.revoke_jti(good["jti"], good["exp"])
            assert client.get(url, headers=headers).status_code == 401
            fresh = claims()
            assert client.get(url, headers={"Authorization": "Bearer " + jwt.encode(fresh, SECRET, algorithm="HS256")}).status_code == 200
    finally:
        _service_factory.cache_clear()


def test_access_truncates_both_stores_and_only_prunes_access(tmp_path):
    db = DatabaseManager(str(tmp_path / "memory.db"))
    query = "private marker " + "x" * 500
    db.add_access_event("search", "ok", "tenant", "project", query=query)
    with db.get_cursor() as cur:
        row = cur.execute("SELECT query, ts FROM logs_access").fetchone()
        payload = json.loads(cur.execute("SELECT payload FROM logs_audit WHERE event_type='access:search'").fetchone()[0])
        assert row["query"] == payload["query"] == query[:256]
        assert query not in json.dumps(payload)
        cur.execute("UPDATE logs_access SET ts=?", ((NOW - datetime.timedelta(days=91)).isoformat(),))
    assert db.prune_access_events(NOW - datetime.timedelta(days=90)) == 1
    assert db.verify_audit_chain()["valid"] is True
    assert db.verify_audit_chain()["checked"] == 1
    with db.get_cursor() as cur:
        assert cur.execute("SELECT count(*) FROM logs_access").fetchone()[0] == 0
    expired = NOW - datetime.timedelta(minutes=1)
    db.revoke_jti("expired-token", expired)
    assert db.is_jti_revoked("expired-token") is False
    assert db.prune_expired_jti() == 1
