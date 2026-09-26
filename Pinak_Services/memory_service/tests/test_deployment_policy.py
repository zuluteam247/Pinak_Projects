"""Local deployment guardrails, without touching an actual deployment."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.memory_service import MemoryService


def _service(tmp_path, monkeypatch):
    monkeypatch.setenv("PINAK_EMBEDDING_BACKEND", "none")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"data_root": str(tmp_path / "data")}))
    monkeypatch.setenv("PINAK_CONFIG_PATH", str(config))
    return MemoryService(config_path=str(config))


def test_health_identifies_keyword_only_backend(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    with patch("app.main.get_memory_service", return_value=service):
        with TestClient(app) as client:
            response = client.get("/api/v1/health")
            assert response.status_code == 200
            assert response.json() == {
                "status": "ok", "embedding_backend": "none",
                "vector_enabled": False, "embedding_model": None,
            }


def test_second_writer_on_same_data_directory_is_refused(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    from app.main import lifespan
    from fastapi import FastAPI
    import asyncio

    async def exercise():
        with patch("app.main.get_memory_service", return_value=service):
            async with lifespan(FastAPI()):
                with pytest.raises(RuntimeError, match="already has a writer"):
                    async with lifespan(FastAPI()):
                        pass
        # Once the first owner exits, the lock is released.
        with patch("app.main.get_memory_service", return_value=service):
            async with lifespan(FastAPI()):
                pass

    asyncio.run(exercise())


def test_writer_lock_precedes_service_construction(tmp_path, monkeypatch):
    from app.main import lifespan
    from fastapi import FastAPI
    import asyncio
    import fcntl

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"data_root": str(tmp_path / "data")}))
    monkeypatch.setenv("PINAK_CONFIG_PATH", str(config))
    (tmp_path / "data").mkdir()
    fd = (tmp_path / "data" / ".pinak-writer.lock").open("w")
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with patch("app.main.get_memory_service") as factory:
            with pytest.raises(RuntimeError, match="already has a writer"):
                asyncio.run(lifespan(FastAPI()).__aenter__())
            factory.assert_not_called()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def test_compose_contract_matches_image_paths():
    root = Path(__file__).resolve().parents[2]
    compose = (root / "docker-compose.yml").read_text()
    dockerfile = (root / "memory_service" / "Dockerfile").read_text()
    assert "context: .." in compose
    assert "dockerfile: Pinak_Services/memory_service/Dockerfile" in compose
    assert "./memory_service/data:/code/Pinak_Services/memory_service/data" in compose
    assert "PINAK_JWT_SECRET: ${PINAK_JWT_SECRET:?" in compose
    assert "--workers\", \"1" in dockerfile
    assert "ENV PINAK_EMBEDDING_BACKEND=none" in dockerfile
    assert "ENV PINAK_DATA_ROOT=/code/Pinak_Services/memory_service/data" in dockerfile
    assert "redis:" not in compose.lower()
