import importlib.util
import json
from pathlib import Path
import pytest

MODULE = Path(__file__).resolve().parents[1] / "scripts" / "pinak_mcp_config.py"
spec = importlib.util.spec_from_file_location("pinak_mcp_config", MODULE)
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)


@pytest.mark.parametrize("client", config.CLIENTS)
def test_install_verify_idempotent_and_preserves_other_settings(tmp_path, client):
    rel, kind = config.CLIENTS[client]
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "json":
        path.write_text(json.dumps({"theme": "dark", "mcpServers": {"other": {"command": "other"}}}))
    else:
        path.write_text('model = "existing"\n[mcp_servers.other]\ncommand = "other"\n')
    launcher = tmp_path / "pinak-memory" / "bin" / "pinak-mcp"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\nexit 0\n")
    launcher.chmod(0o755)
    assert config.install(path, kind, launcher) == "installed"
    once = path.read_bytes()
    assert config.install(path, kind, launcher) == "unchanged"
    assert path.read_bytes() == once
    assert "configured" in config.verify(path, kind, launcher, check_runtime=False)
    if kind == "json":
        assert json.loads(once)["theme"] == "dark"
        assert json.loads(once)["mcpServers"]["other"]["command"] == "other"
    else:
        assert 'model = "existing"' in once.decode()
        assert config.tomllib.loads(once.decode())["mcp_servers"]["other"]["command"] == "other"


@pytest.mark.parametrize("kind,raw", [("json", "{"), ("toml", "[broken")])
def test_malformed_config_is_not_overwritten(tmp_path, kind, raw):
    path = tmp_path / "config"
    path.write_text(raw)
    with pytest.raises((ValueError, json.JSONDecodeError)):
        config.install(path, kind, tmp_path / "bin" / "pinak-mcp")
    assert path.read_text() == raw


def test_verify_rejects_missing_launcher_and_placeholder(tmp_path):
    path = tmp_path / "config.json"
    launcher = tmp_path / "bin" / "pinak-mcp"
    config.install(path, "json", launcher)
    with pytest.raises(ValueError, match="launcher missing"):
        config.verify(path, "json", launcher, check_runtime=False)
    data = json.loads(path.read_text())
    data["mcpServers"]["pinak-memory"]["env"] = {"PINAK_JWT_TOKEN": "SET_ME"}
    path.write_text(json.dumps(data))
    launcher.parent.mkdir()
    launcher.write_text("#!/bin/sh\nexit 0\n")
    launcher.chmod(0o755)
    with pytest.raises(ValueError, match="differs"):
        config.verify(path, "json", launcher, check_runtime=False)
