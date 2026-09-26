"""API bounds, scoped recall, vector candidate indexing and timeout pooling."""
import datetime
import json
from concurrent.futures import Future
from unittest.mock import patch

import jwt
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.database import DatabaseManager
from app.services.memory_service import MemoryService
from app.services.vector_store import VectorStore


def test_search_api_rejects_out_of_range_k(tmp_path, monkeypatch):
    from app.api.v1.endpoints import _service_factory
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"data_root": str(tmp_path / "data"), "embedding_model": "dummy"}))
    monkeypatch.setenv("PINAK_CONFIG_PATH", str(config))
    monkeypatch.setenv("PINAK_EMBEDDING_BACKEND", "none")
    monkeypatch.setenv("PINAK_JWT_SECRET", "test-secret")
    _service_factory.cache_clear()
    claims = {"sub": "fixture", "tenant": "a", "project_id": "p", "scopes": ["memory.read"],
              "exp": datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=5),
              "iss": "pinak-memory", "aud": "pinak-memory-api", "jti": "api-perf-test"}
    headers = {"Authorization": "Bearer " + jwt.encode(claims, "test-secret", algorithm="HS256")}
    try:
        with TestClient(app) as client:
            for value in ("-1", "0", "101", "999999"):
                assert client.get("/api/v1/memory/search", params={"query": "item", "k": value}, headers=headers).status_code == 422
                assert client.get("/api/v1/memory/rag/search", params={"query": "item", "limit": value}, headers=headers).status_code == 422
            assert client.get("/api/v1/memory/search", params={"query": "item", "k": "1"}, headers=headers).status_code == 200
    finally:
        _service_factory.cache_clear()


def test_working_recall_is_literal_scoped_and_skips_expired(tmp_path, monkeypatch):
    monkeypatch.setenv("PINAK_EMBEDDING_BACKEND", "none")
    db = DatabaseManager(str(tmp_path / "memory.db"))
    own = db.add_working("moonlamp _cache", "a", "p")
    assert [row["id"] for row in db.search_working("moonlamp", "a", "p")] == [own["id"]]
    assert db.search_working("moonlamp", "b", "p") == []
    assert db.search_working("%_", "a", "p") == []
    with db.get_cursor() as cur:
        cur.execute("UPDATE working_memory SET expires_at=? WHERE tenant='a'", ("2000-01-01T00:00:00",))
    assert db.search_working("moonlamp", "a", "p") == []
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"data_root": str(tmp_path)}))
    service = MemoryService(config_path=str(config))
    with db.get_cursor() as cur:
        cur.execute("UPDATE working_memory SET expires_at=NULL WHERE tenant='a'")
    assert any(hit["type"] == "working" for hit in service.search_hybrid("moonlamp", "a", "p"))


def test_vector_candidate_map_tracks_add_remove_reload(tmp_path):
    store = VectorStore(str(tmp_path / "vectors.npy"), 2)
    store.add_vectors(np.array([[0, 0], [1, 1]], dtype=np.float32), [1, 2])
    assert store.search(np.zeros(2, dtype=np.float32), allowed_ids={2, 99})[1] == [2]
    assert store.search(np.zeros(2, dtype=np.float32), k=0)[1] == []
    store.remove_ids([1]); store.save()
    assert store.reconstruct(1) is None
    reopened = VectorStore(str(tmp_path / "vectors.npy"), 2)
    assert reopened.search(np.zeros(2, dtype=np.float32), allowed_ids={2})[1] == [2]
    reopened.reset()
    assert reopened.search(np.zeros(2, dtype=np.float32), allowed_ids={2})[1] == []


def test_timed_query_reuses_bounded_executor(tmp_path, monkeypatch):
    monkeypatch.setenv("PINAK_EMBEDDING_BACKEND", "dummy")
    monkeypatch.setenv("PINAK_EMBEDDING_TIMEOUT_MS", "10")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"data_root": str(tmp_path / "data"), "embedding_model": "dummy"}))
    service = MemoryService(config_path=str(config))
    done = Future(); done.set_result(([], []))
    with patch("app.services.memory_service._VECTOR_SEARCH_EXECUTOR") as pool:
        pool.submit.return_value = done
        service._safe_vector_search("probe", 5, "a", "p")
        service._safe_vector_search("probe", 5, "a", "p")
        assert pool.submit.call_count == 2


def test_timed_query_saturation_falls_back_without_submitting(tmp_path, monkeypatch):
    monkeypatch.setenv("PINAK_EMBEDDING_BACKEND", "dummy")
    monkeypatch.setenv("PINAK_EMBEDDING_TIMEOUT_MS", "10")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"data_root": str(tmp_path / "data"), "embedding_model": "dummy"}))
    service = MemoryService(config_path=str(config))
    with patch("app.services.memory_service._VECTOR_SEARCH_SLOTS") as slots, patch(
        "app.services.memory_service._VECTOR_SEARCH_EXECUTOR"
    ) as pool:
        slots.acquire.return_value = False
        assert service._safe_vector_search("probe", 5, "a", "p") == ([], [])
        pool.submit.assert_not_called()


def test_legacy_working_table_search(tmp_path):
    import sqlite3
    path = str(tmp_path / "legacy.db")
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE working_memory (id TEXT PRIMARY KEY, content TEXT, tenant TEXT, project_id TEXT, ts TEXT, expires_at TEXT)")
        conn.execute("INSERT INTO working_memory VALUES ('old', 'moonlamp', 'a', 'p', '2026-01-01T00:00:00', NULL)")
    db = DatabaseManager(path)
    assert [row["id"] for row in db.search_working("moonlamp", "a", "p")] == ["old"]
    assert db.search_working("moonlamp", "b", "p") == []
