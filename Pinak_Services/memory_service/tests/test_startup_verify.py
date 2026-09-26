import json
import threading
import time
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from app.main import app


def test_skip_verify_on_startup(monkeypatch, tmp_path):
    monkeypatch.setenv("PINAK_SKIP_VERIFY_ON_STARTUP", "1")
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"data_root": str(tmp_path)}))
    monkeypatch.setenv("PINAK_CONFIG_PATH", str(cfg))
    with patch("app.main.get_memory_service") as get_service:
        svc = MagicMock()
        svc.data_root = str(tmp_path)
        get_service.return_value = svc
        with TestClient(app) as client:
            resp = client.get("/")
            assert resp.status_code == 200
            assert client.get("/api/v1/live").status_code == 200
            assert client.get("/api/v1/health").status_code == 503
            assert client.get("/api/v1/memory/schema").status_code == 503
    svc.verify_and_recover.assert_not_called()


def test_background_readiness_waits_for_verification(monkeypatch, tmp_path):
    monkeypatch.delenv("PINAK_SKIP_VERIFY_ON_STARTUP", raising=False)
    monkeypatch.setenv("PINAK_VERIFY_IN_BACKGROUND", "1")
    started = threading.Event()
    finish = threading.Event()
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"data_root": str(tmp_path)}))
    monkeypatch.setenv("PINAK_CONFIG_PATH", str(cfg))
    with patch("app.main.get_memory_service") as get_service:
        svc = MagicMock()
        svc.data_root = str(tmp_path)
        get_service.return_value = svc
        def verify():
            started.set()
            assert finish.wait(5)
        svc.verify_and_recover.side_effect = verify
        with TestClient(app) as client:
            try:
                assert started.wait(5)
                assert client.get("/api/v1/health").status_code == 503
                assert client.get("/api/v1/memory/schema").status_code == 503
                assert client.get("/api/v1/live").status_code == 200
            finally:
                finish.set()
            for _ in range(100):
                if client.get("/api/v1/health").status_code == 200:
                    break
                time.sleep(0.01)
            assert client.get("/api/v1/health").status_code == 200


def test_background_verification_failure_stays_unready(monkeypatch, tmp_path):
    monkeypatch.delenv("PINAK_SKIP_VERIFY_ON_STARTUP", raising=False)
    monkeypatch.setenv("PINAK_VERIFY_IN_BACKGROUND", "1")
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"data_root": str(tmp_path)}))
    monkeypatch.setenv("PINAK_CONFIG_PATH", str(cfg))
    with patch("app.main.get_memory_service") as get_service:
        svc = MagicMock()
        svc.data_root = str(tmp_path)
        get_service.return_value = svc
        svc.verify_and_recover.side_effect = RuntimeError("failed verification")
        with TestClient(app) as client:
            for _ in range(100):
                response = client.get("/api/v1/health")
                if response.json()["detail"]["verification"] == "failed":
                    break
                time.sleep(0.01)
            assert response.status_code == 503
            assert response.json()["detail"]["verification"] == "failed"
            assert client.get("/api/v1/memory/schema").status_code == 503
            assert client.get("/api/v1/live").status_code == 200
