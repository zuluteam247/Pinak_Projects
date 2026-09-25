import os
import pytest
import sys
import types
from unittest.mock import patch

from app.services.memory_service import MemoryService


def test_backend_none_skips_model_load(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = {"data_root": str(data_dir)}

    monkeypatch.setenv("PINAK_EMBEDDING_BACKEND", "none")
    with patch("app.services.memory_service.MemoryService._load_config", return_value=config):
        svc = MemoryService()
        assert svc.vector_enabled is False


def test_update_delete_skip_vectors_when_disabled(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = {"data_root": str(data_dir)}

    monkeypatch.setenv("PINAK_EMBEDDING_BACKEND", "none")
    with patch("app.services.memory_service.MemoryService._load_config", return_value=config):
        svc = MemoryService()
        svc.model.encode = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("encode called"))

        item = svc.db.add_semantic("old", [], "t1", "p1", 111)
        assert svc.update_memory("semantic", item["id"], {"content": "new"}, "t1", "p1") is True
        assert svc.delete_memory("semantic", item["id"], "t1", "p1") is True


def test_real_model_without_extra_fails_closed(tmp_path, monkeypatch):
    import builtins
    config = {"data_root": str(tmp_path / "data"), "embedding_model": "real-model"}
    monkeypatch.delenv("PINAK_EMBEDDING_BACKEND", raising=False)
    original_import = builtins.__import__

    def unavailable(name, *args, **kwargs):
        if name == "sentence_transformers":
            raise ModuleNotFoundError("No module named 'sentence_transformers'")
        return original_import(name, *args, **kwargs)

    with patch("app.services.memory_service.MemoryService._load_config", return_value=config):
        with patch("builtins.__import__", side_effect=unavailable):
            with pytest.raises(RuntimeError, match="optional embeddings extra"):
                MemoryService()


def test_dummy_backend_never_imports_optional_model(tmp_path, monkeypatch):
    import builtins
    config = {"data_root": str(tmp_path / "data"), "embedding_model": "real-model"}
    monkeypatch.setenv("PINAK_EMBEDDING_BACKEND", "dummy")
    original_import = builtins.__import__

    def unavailable(name, *args, **kwargs):
        if name == "sentence_transformers":
            raise AssertionError("optional model imported under dummy backend")
        return original_import(name, *args, **kwargs)

    with patch("app.services.memory_service.MemoryService._load_config", return_value=config):
        with patch("builtins.__import__", side_effect=unavailable):
            assert MemoryService().vector_enabled


def test_real_model_loader_error_is_not_silently_dummy(tmp_path, monkeypatch):
    config = {"data_root": str(tmp_path / "data"), "embedding_model": "broken-model"}
    monkeypatch.delenv("PINAK_EMBEDDING_BACKEND", raising=False)
    def fail_model(_name):
        raise RuntimeError("model unavailable")

    fake_module = types.ModuleType("sentence_transformers")
    fake_module.SentenceTransformer = fail_model
    with patch.dict(sys.modules, {"sentence_transformers": fake_module}):
        with patch("app.services.memory_service.MemoryService._load_config", return_value=config):
            with pytest.raises(RuntimeError, match="model unavailable"):
                MemoryService()
