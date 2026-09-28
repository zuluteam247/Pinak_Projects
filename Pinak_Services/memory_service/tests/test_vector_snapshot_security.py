"""Index bytes are data, never executable code or a silent empty recovery."""
import numpy as np
import pytest

from app.services.vector_store import VectorStore
from app.services.vector_snapshot import read_snapshot


def test_reject_legacy_pickle_and_preserve_bytes(tmp_path):
    path = tmp_path / "vectors.index.npy"
    np.save(path, {"vectors": np.array([[1, 2]], dtype=np.float32),
                   "ids": np.array([7], dtype=np.int64)})
    before = path.read_bytes()
    with pytest.raises(ValueError, match="Legacy or unsupported"):
        VectorStore(str(path), 2)
    assert path.read_bytes() == before


def test_reject_corrupt_index_without_overwriting(tmp_path):
    path = tmp_path / "vectors.index.npy"
    path.write_bytes(b"broken")
    with pytest.raises(ValueError):
        VectorStore(str(path), 2)
    assert path.read_bytes() == b"broken"


def test_safe_round_trip_and_strict_shapes(tmp_path):
    path = tmp_path / "vectors.index.npy"
    store = VectorStore(str(path), 2)
    store.add_vectors(np.array([[1., 2.]], dtype=np.float32), [7]); store.save()
    assert read_snapshot(path, 2)[1].tolist() == [7]
    assert VectorStore(str(path), 2).reconstruct(7).tolist() == [1., 2.]
    with pytest.raises(ValueError, match="array header"):
        VectorStore(str(path), 3)
    with open(path, "wb") as stream:
        np.savez(stream, format_version=np.array(1), vectors=np.array([[float('nan'), 2.]], dtype=np.float32), ids=np.array([7], dtype=np.int64))
    with pytest.raises(ValueError):
        VectorStore(str(path), 2)


def test_reject_duplicate_members_and_object_array(tmp_path):
    import warnings
    import zipfile
    path = tmp_path / "bad.index"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("vectors.npy", b"x")
            z.writestr("vectors.npy", b"y")
            z.writestr("ids.npy", b"z")
    with pytest.raises(ValueError):
        read_snapshot(path)
    with open(path, "wb") as stream:
        np.savez(stream, format_version=np.array(1, dtype=np.int64),
                 vectors=np.array([{'bad': 'value'}], dtype=object),
                 ids=np.array([1], dtype=np.int64))
    with pytest.raises(ValueError):
        read_snapshot(path)


def test_reject_oversized_zip_header_and_truncated_zip(tmp_path):
    import zipfile
    from app.services import vector_snapshot as snapshot
    path = tmp_path / "bad.index"
    with open(path, "wb") as stream:
        np.savez(stream, format_version=np.array(1, dtype=np.int64),
                 vectors=np.array([[1., 2.]], dtype=np.float32),
                 ids=np.array([1], dtype=np.int64))
    original = path.read_bytes()
    with pytest.raises(ValueError):
        path.write_bytes(original[:-14]); read_snapshot(path, 2)
    path.write_bytes(original)
    with pytest.raises(ValueError, match="file size"):
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(snapshot, "MAX_FILE_BYTES", 10)
            read_snapshot(path, 2)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("format_version.npy", b"x")
        archive.writestr("vectors.npy", b"x" * 4096)
        archive.writestr("ids.npy", b"x")
    with pytest.raises(ValueError, match="encoding or size"):
        read_snapshot(path)


def test_reject_symlink_and_duplicate_policy(tmp_path):
    path = tmp_path / "snapshot"
    with open(path, "wb") as stream:
        np.savez(stream, format_version=np.array(1, dtype=np.int64),
                 vectors=np.array([[1., 2.], [3., 4.]], dtype=np.float32),
                 ids=np.array([7, 7], dtype=np.int64))
    assert read_snapshot(path, 2)[1].tolist() == [7, 7]  # startup repairs duplicate IDs
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(ValueError):
        read_snapshot(link, 2)
    with pytest.raises(ValueError, match="dimension"):
        read_snapshot(path, 0)


def test_duplicate_snapshot_ids_reencoded_by_service(tmp_path, monkeypatch):
    import json
    from app.services.memory_service import MemoryService
    from app.core.database import DatabaseManager
    data = tmp_path / "data"; data.mkdir()
    db = DatabaseManager(str(data / "memory.db"))
    db.add_semantic("canonical", [], "t", "p", 7)
    path = data / "vectors.index.npy"
    with open(path, "wb") as stream:
        np.savez(stream, format_version=np.array(1, dtype=np.int64),
                 vectors=np.zeros((2, 384), dtype=np.float32),
                 ids=np.array([7, 7], dtype=np.int64))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"data_root": str(data), "embedding_model": "dummy"}))
    monkeypatch.setenv("PINAK_EMBEDDING_BACKEND", "dummy")
    svc = MemoryService(config_path=str(config))
    svc.verify_and_recover()
    assert svc.vector_store.ids.tolist() == [7]
    assert not np.array_equal(svc.vector_store.reconstruct(7), np.zeros(384))
    assert read_snapshot(path, 384)[1].tolist() == [7]


def test_zero_row_snapshot_and_diagnostics(tmp_path, monkeypatch, capsys):
    import sqlite3
    from app.core.database import DatabaseManager
    from cli import main as cli
    path = tmp_path / "vectors.index.npy"
    store = VectorStore(str(path), 2)
    store.add_vectors(np.ones((1, 2), dtype=np.float32), [1])
    store.remove_ids([1]); store.save()
    assert read_snapshot(path, 2)[0].shape == (0, 2)
    db = tmp_path / "memory.db"; DatabaseManager(str(db))
    monkeypatch.setattr(cli, "_get_db_path", lambda: str(db))
    monkeypatch.setattr(cli, "_get_vector_path", lambda: str(path))
    cli.doctor()
    assert "Vector Index OK (Size: 0)" in capsys.readouterr().out


def test_reject_near_limit_before_numpy_load(tmp_path, monkeypatch):
    import zipfile
    from unittest.mock import patch
    from app.services import vector_snapshot as snapshot
    path = tmp_path / "large.index"
    with open(path, "wb") as stream:
        np.savez(stream, format_version=np.array(1, dtype=np.int64),
                 vectors=np.zeros((2, 8), dtype=np.float32),
                 ids=np.array([1, 2], dtype=np.int64))
    with patch.object(snapshot.np, "load", wraps=np.load) as load:
        monkeypatch.setattr(snapshot, "MAX_TOTAL_BYTES", 20)
        with pytest.raises(ValueError, match="allocation"):
            read_snapshot(path, 8)
        load.assert_not_called()


def test_backup_rejects_wrong_ids_and_bad_shape(tmp_path):
    from app.core.database import DatabaseManager
    from scripts.backup_verified import ids_match
    db = tmp_path / "memory.db"; DatabaseManager(str(db))
    index = tmp_path / "vectors.index.npy"
    with open(index, "wb") as stream:
        np.savez(stream, format_version=np.array(1, dtype=np.int64),
                 vectors=np.ones((1, 2), dtype=np.float32),
                 ids=np.array([7], dtype=np.int64))
    with pytest.raises(AssertionError, match="ID mismatch"):
        ids_match(db, index)
    with open(index, "wb") as stream:
        np.savez(stream, format_version=np.array(1, dtype=np.int64),
                 vectors=np.ones((1, 0), dtype=np.float32),
                 ids=np.array([7], dtype=np.int64))
    with pytest.raises(ValueError):
        ids_match(db, index)
