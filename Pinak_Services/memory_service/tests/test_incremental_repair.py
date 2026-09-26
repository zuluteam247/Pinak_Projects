"""DB-first writes and scoped index recovery under disposable fixture data."""
import json
import sqlite3
from unittest.mock import patch

import numpy as np
import pytest

from app.services.memory_service import MemoryService
from app.core.schemas import MemoryCreate
from app.services.vector_store import VectorStore
from app.core.database import DatabaseManager


def service(tmp_path, monkeypatch):
    monkeypatch.setenv("PINAK_EMBEDDING_BACKEND", "dummy")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"data_root": str(tmp_path / "data"), "embedding_model": "dummy"}))
    return MemoryService(config_path=str(config))


def test_database_first_on_vector_failure(tmp_path, monkeypatch):
    svc = service(tmp_path, monkeypatch)
    with patch.object(svc.vector_store, "add_vectors", side_effect=RuntimeError("index failed")):
        with pytest.raises(RuntimeError, match="index failed"):
            svc.add_memory(MemoryCreate(content="db first", tags=[]), "t", "p")
    with svc.db.get_cursor() as conn:
        row = conn.execute("SELECT embedding_id FROM memories_semantic WHERE content=?", ("db first",)).fetchone()
    assert row is not None and row[0] is not None
    assert svc.vector_store.total == 0
    svc.verify_and_recover()
    assert svc.vector_store.ids.tolist() == [row[0]]


@pytest.mark.parametrize("kind", ["semantic", "episodic", "procedural"])
def test_id_allocation_is_63_bit_and_not_reused(tmp_path, monkeypatch, kind):
    svc = service(tmp_path, monkeypatch)
    svc.db.add_semantic("existing", [], "t", "p", 17)
    from types import SimpleNamespace
    ids = iter([17, 42])
    with patch("app.services.memory_service.uuid4", side_effect=lambda: SimpleNamespace(int=next(ids))):
        if kind == "semantic":
            item = svc.add_memory(MemoryCreate(content="new", tags=[]), "t", "p")
            layer = "memories_semantic"
            new_id = item.id
        elif kind == "episodic":
            item = svc.add_episodic("new", "t", "p")
            layer = "memories_episodic"
            new_id = item["id"]
        else:
            item = svc.add_procedural("new", ["step"], "t", "p")
            layer = "memories_procedural"
            new_id = item["id"]
    with svc.db.get_cursor() as conn:
        stored = conn.execute(f"SELECT embedding_id FROM {layer} WHERE id=?", (new_id,)).fetchone()[0]
    assert stored == 42


def test_incremental_repair_preserves_valid_vector(tmp_path, monkeypatch):
    svc = service(tmp_path, monkeypatch)
    svc.db.add_semantic("valid", [], "t", "p", 1)
    svc.db.add_episodic("missing", "t", "p", embedding_id=2)
    existing = np.ones((1, svc.embedding_dim), dtype=np.float32)
    svc.vector_store.add_vectors(existing, [1])
    svc.vector_store.add_vectors(np.zeros_like(existing), [999])
    with patch.object(svc.model, "encode", wraps=svc.model.encode) as encode:
        svc.verify_and_recover()
    assert encode.call_count == 1
    assert encode.call_args.args[0] == ["missing  "]
    assert set(svc.vector_store.ids.tolist()) == {1, 2}
    assert np.array_equal(svc.vector_store.reconstruct(1), existing[0])
    restarted = VectorStore(svc.vector_path, svc.embedding_dim)
    assert set(restarted.ids.tolist()) == {1, 2}


def test_duplicate_database_ids_fail_closed(tmp_path, monkeypatch):
    svc = service(tmp_path, monkeypatch)
    svc.db.add_semantic("one", [], "t", "p", 7)
    svc.db.add_episodic("two", "t", "p", embedding_id=7)
    with pytest.raises(RuntimeError, match="Duplicate DB embedding id"):
        svc.verify_and_recover()


def test_wal_timeout_and_short_transaction(tmp_path):
    db = DatabaseManager(str(tmp_path / "memory.db"))
    with db.get_cursor() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        conn.execute("INSERT INTO memories_semantic (id, content, tenant, project_id, created_at) VALUES ('rolledback', 'x', 't', 'p', 'now')")
    with pytest.raises(RuntimeError):
        with db.get_cursor() as conn:
            conn.execute("INSERT INTO memories_semantic (id, content, tenant, project_id, created_at) VALUES ('never', 'x', 't', 'p', 'now')")
            raise RuntimeError("abort")
    with sqlite3.connect(db.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM memories_semantic WHERE id='rolledback'").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM memories_semantic WHERE id='never'").fetchone()[0] == 0


def test_database_insert_failure_never_mutates_vector(tmp_path, monkeypatch):
    svc = service(tmp_path, monkeypatch)
    with patch.object(svc.db, "add_semantic", side_effect=sqlite3.OperationalError("locked")):
        with pytest.raises(sqlite3.OperationalError):
            svc.add_memory(MemoryCreate(content="not inserted", tags=[]), "t", "p")
    assert svc.vector_store.total == 0
    with svc.db.get_cursor() as conn:
        assert conn.execute("SELECT count(*) FROM memories_semantic WHERE content='not inserted'").fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["episodic", "procedural"])
def test_other_layer_vector_failure_keeps_repairable_row(tmp_path, monkeypatch, kind):
    svc = service(tmp_path, monkeypatch)
    with patch.object(svc.vector_store, "add_vectors", side_effect=RuntimeError("vector failed")):
        with pytest.raises(RuntimeError, match="vector failed"):
            if kind == "episodic":
                svc.add_episodic("episode", "t", "p")
            else:
                svc.add_procedural("procedure", ["one"], "t", "p")
    table = "memories_episodic" if kind == "episodic" else "memories_procedural"
    with svc.db.get_cursor() as conn:
        emb_id = conn.execute(f"SELECT embedding_id FROM {table}").fetchone()[0]
    assert emb_id is not None
    svc.verify_and_recover()
    assert svc.vector_store.ids.tolist() == [emb_id]


def test_duplicate_vector_id_reencoded_once(tmp_path, monkeypatch):
    svc = service(tmp_path, monkeypatch)
    svc.db.add_semantic("canonical", [], "t", "p", 5)
    zeros = np.zeros((2, svc.embedding_dim), dtype=np.float32)
    svc.vector_store.add_vectors(zeros, [5, 5])
    svc.verify_and_recover()
    assert svc.vector_store.ids.tolist() == [5]
    assert not np.array_equal(svc.vector_store.reconstruct(5), zeros[0])


def test_sqlite_writer_contention_waits_then_commits(tmp_path):
    import threading
    db = DatabaseManager(str(tmp_path / "memory.db"))
    ready = threading.Event()
    done = threading.Event()
    errors = []

    def second_writer():
        try:
            ready.set()
            db.add_semantic("second", [], "t", "p", 9)
        except Exception as exc:
            errors.append(exc)
        finally:
            done.set()

    with db.get_cursor() as conn:
        conn.execute("INSERT INTO memories_semantic (id, content, tenant, project_id, created_at) VALUES ('first', 'x', 't', 'p', 'now')")
        thread = threading.Thread(target=second_writer)
        thread.start()
        assert ready.wait(2)
        assert not done.wait(0.1)
    assert done.wait(3)
    thread.join(timeout=2)
    assert not errors
    with db.get_cursor() as conn:
        assert conn.execute("SELECT count(*) FROM memories_semantic WHERE content='second'").fetchone()[0] == 1
