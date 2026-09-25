#!/usr/bin/env python3
"""Install or verify Pinak's local stdio MCP entry without handling credentials.

This utility never claims that a desktop client is running or has loaded the entry.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib

CLIENTS = {
    "claude-desktop": ("Library/Application Support/Claude/claude_desktop_config.json", "json"),
    "gemini-cli": (".gemini/settings.json", "json"),
    "antigravity": (".gemini/config/mcp_config.json", "json"),
    "cursor": (".cursor/mcp.json", "json"),
    "codex": (".codex/config.toml", "toml"),
}
NAME = "pinak-memory"


def _load(path, kind):
    if not path.exists():
        return {}
    raw = path.read_bytes()
    data = json.loads(raw) if kind == "json" else tomllib.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"configuration root must be an object: {path}")
    return data


def _entry(data, kind):
    servers = data.get("mcpServers" if kind == "json" else "mcp_servers", {})
    if not isinstance(servers, dict):
        raise ValueError("MCP servers must be a table/object")
    return servers.get(NAME)


def _expected(launcher):
    return {"command": str(launcher), "args": []}


def _write_atomic(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".pinak-mcp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def install(path, kind, launcher):
    data = _load(path, kind)
    before = _entry(data, kind)
    if before is not None and before != _expected(launcher):
        raise ValueError("existing Pinak entry differs; refusing to replace it")
    if before == _expected(launcher):
        return "unchanged"
    if kind == "json":
        data.setdefault("mcpServers", {})[NAME] = _expected(launcher)
        _write_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    else:
        # Preserve all unrelated TOML bytes (including comments and ordering).
        old = path.read_text(encoding="utf-8") if path.exists() else ""
        block = f'\n[mcp_servers."{NAME}"]\ncommand = {json.dumps(str(launcher))}\nargs = []\n'
        _write_atomic(path, old.rstrip() + "\n" + block)
    return "installed"


def verify(path, kind, launcher, check_runtime=True):
    if not path.is_file():
        raise ValueError(f"missing config: {path}")
    data = _load(path, kind)
    entry = _entry(data, kind)
    if entry != _expected(launcher):
        raise ValueError("Pinak entry absent or differs from expected command/args")
    if not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise ValueError(f"launcher missing or not executable: {launcher}")
    if check_runtime:
        # The copied launcher runs python from PINAK_MCP_PYTHON or python3.
        # Do not execute MCP tools or contact the API just to check dependencies.
        python = os.environ.get("PINAK_MCP_PYTHON", "python3")
        result = subprocess.run([python, "-c", "import fastmcp, httpx"], capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise ValueError("MCP Python lacks fastmcp/httpx; configure PINAK_MCP_PYTHON with an isolated runtime")
    return "configured (client load and API authentication unverified)"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "verify"))
    parser.add_argument("--client", choices=tuple(CLIENTS), required=True)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--pinak-home", type=Path)
    parser.add_argument("--no-runtime-check", action="store_true", help="for offline config tests only")
    args = parser.parse_args(argv)
    rel, kind = CLIENTS[args.client]
    home = args.home.expanduser().resolve()
    target = (args.pinak_home or home / "pinak-memory").expanduser().resolve()
    launcher = target / "bin" / "pinak-mcp"
    path = home / rel
    try:
        if args.action == "install":
            print(f"{args.client}: {install(path, kind, launcher)} at {path}")
        else:
            print(f"{args.client}: {verify(path, kind, launcher, not args.no_runtime_check)} at {path}")
    except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        print(f"{args.client}: not verified: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
