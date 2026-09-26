"""Migration, backup/restore, and private metrics guardrails."""
import json
import sqlite3
import numpy as np
import jwt
import datetime
import pytest
from fastapi.testclient import TestClient

from app.core.database import DatabaseManager
from app.services.vector_store import VectorStore
from app.main import app
from scripts.backup_verified import create_backup, verify_backup


def test_migrations_applied_once_and_changed_history_refused(tmp_path, monkeypatch):
    data = tmp_path / "memory.db"
    db = DatabaseManager(str(data))
    with db.get_cursor() as conn:
        rows = conn.execute("SELECT version, checksum FROM schema_migrations ORDER BY version").fetchall()
        assert [row[0] for row in rows] == [1, 2]
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='idx_logs_access_scope_ts'").fetchone()
    DatabaseManager(str(data))
    with sqlite3.connect(data) as conn:
        assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 2
        conn.execute("UPDATE schema_migrations SET checksum='tampered' WHERE version=1")
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        DatabaseManager(str(data))


def test_backup_roundtrip_and_tamper_detection(tmp_path):
    data = tmp_path / "data"; data.mkdir()
    db = DatabaseManager(str(data / "memory.db"))
    db.add_semantic("snapshot", [], "a", "p", 17)
    index = VectorStore(str(data / "vectors.index.npy"), 2)
    index.add_vectors(np.array([[1, 2]], dtype=np.float32), [17]); index.save()
    backup = create_backup(data, tmp_path / "backups")
    assert verify_backup(backup)["vectors"] == 1
    with sqlite3.connect(backup / "memory.db") as conn:
        assert conn.execute("SELECT content FROM memories_semantic WHERE embedding_id=17").fetchone()[0] == "snapshot"
    with open(backup / "vectors.index.npy", "ab") as handle:handle.write(b"corrupt")
    with pytest.raises(AssertionError, match="Checksum mismatch"):
        verify_backup(backup)


def test_backup_mismatch_leaves_no_incomplete_result(tmp_path):
    data = tmp_path / "data"; data.mkdir()
    db = DatabaseManager(str(data / "memory.db"))
    db.add_semantic("no vector", [], "a", "p", 17)
    index = VectorStore(str(data / "vectors.index.npy"), 2)
    index.add_vectors(np.array([[1, 2]], dtype=np.float32), [18]); index.save()
    with pytest.raises(AssertionError, match="ID mismatch"):
        create_backup(data, tmp_path / "backups")
    assert list((tmp_path / "backups").iterdir()) == []


def test_metrics_requires_admin_scope_and_has_bounded_labels(tmp_path, monkeypatch):
    from app.api.v1.endpoints import _service_factory
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"data_root": str(tmp_path / "data"), "embedding_model": "dummy"}))
    monkeypatch.setenv("PINAK_CONFIG_PATH", str(config))
    monkeypatch.setenv("PINAK_EMBEDDING_BACKEND", "none")
    monkeypatch.setenv("PINAK_JWT_SECRET", "test-secret")
    _service_factory.cache_clear()
    def auth(scopes, roles):
        claims = {"sub": "fixture", "tenant": "a", "project_id": "p", "scopes": scopes,
                  "roles": roles, "exp": datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=5),
                  "iss": "pinak-memory", "aud": "pinak-memory-api", "jti": "ops-" + str(len(scopes)) + str(roles)}
        return {"Authorization": "Bearer " + jwt.encode(claims, "test-secret", algorithm="HS256")}
    try:
        with TestClient(app) as client:
            assert client.get("/api/v1/metrics").status_code == 401
            assert client.get("/api/v1/metrics", headers=auth(["memory.read"], ["user"])).status_code == 403
            response = client.get("/api/v1/metrics", headers=auth(["memory.admin"], ["admin"]))
            assert response.status_code == 200
            assert response.headers.get("X-Request-ID")
            output = response.json()
            assert set(output) == {"verification", "vectors", "access_rows", "requests"}
            assert not any("ops-" in label for label in output["requests"])
    finally:
        _service_factory.cache_clear()


def test_keyword_only_backup_without_vector_file(tmp_path):
    data = tmp_path / "data"; data.mkdir()
    DatabaseManager(str(data / "memory.db"))
    backup = create_backup(data, tmp_path / "backups")
    assert verify_backup(backup)["files"].keys() == {"memory.db"}
    assert verify_backup(backup)["vectors"] == 0
