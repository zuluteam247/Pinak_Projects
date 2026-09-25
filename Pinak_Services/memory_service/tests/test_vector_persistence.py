"""Crash-safe index snapshots and restart behavior."""
import numpy as np
import pytest
from unittest.mock import patch
from app.services.vector_store import VectorStore


def test_restart_loads_complete_index(tmp_path):
    path = str(tmp_path / 'vectors.index')
    store = VectorStore(path, 2)
    store.add_vectors(np.array([[1, 2]], dtype=np.float32), [7])
    store.save()
    restarted = VectorStore(path, 2)
    assert restarted.total == 1
    assert restarted.reconstruct(7).tolist() == [1, 2]
    assert not list(tmp_path.glob('.vectors-*'))


def test_failed_swap_preserves_last_good_index(tmp_path):
    path = str(tmp_path / 'vectors.index')
    store = VectorStore(path, 2)
    store.add_vectors(np.array([[1, 2]], dtype=np.float32), [7])
    store.save()
    old_bytes = (tmp_path / 'vectors.index').read_bytes()
    store.add_vectors(np.array([[3, 4]], dtype=np.float32), [8])
    with patch('app.services.vector_store.os.replace', side_effect=OSError('swap failed')):
        with pytest.raises(OSError, match='swap failed'):
            store.save()
    assert (tmp_path / 'vectors.index').read_bytes() == old_bytes
    assert store.needs_save
    assert not list(tmp_path.glob('.vectors-*'))
    store.save()
    assert VectorStore(path, 2).total == 2


def test_cli_doctor_reads_numpy_snapshot(tmp_path, monkeypatch, capsys):
    import sqlite3
    from app.core.database import DatabaseManager
    from cli import main as cli
    db_path = str(tmp_path / "memory.db")
    DatabaseManager(db_path)
    index_path = str(tmp_path / "vectors.index.npy")
    store = VectorStore(index_path, 2)
    store.save()
    # Empty stores only create a snapshot once modified.
    store.add_vectors(np.array([[1, 2]], dtype=np.float32), [17])
    store.save()
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO memories_semantic (id, content, tenant, project_id, created_at, embedding_id) VALUES (?, ?, ?, ?, ?, ?)", ("m1", "one", "t", "p", "now", 17))
    monkeypatch.setattr(cli, "_get_db_path", lambda: db_path)
    monkeypatch.setattr(cli, "_get_vector_path", lambda: index_path)
    cli.doctor()
    output = capsys.readouterr().out
    assert "Vector Index OK (Size: 1)" in output
    assert "Inconsistency detected" not in output
