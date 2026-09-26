"""Launchers and JWT helpers must not silently use a public signing key."""
from pathlib import Path
import os
import subprocess
import jwt
import pytest
from typer.testing import CliRunner
from app.core.security import _get_secret
from cli.main import app

ROOT = Path(__file__).resolve().parents[1]

@pytest.mark.parametrize('secret', [None, '', 'secret', 'dev-secret-change-me'])
def test_service_rejects_missing_or_known_secret(monkeypatch, secret):
    if secret is None:
        monkeypatch.delenv('PINAK_JWT_SECRET', raising=False)
    else:
        monkeypatch.setenv('PINAK_JWT_SECRET', secret)
    with pytest.raises(RuntimeError, match='non-default'):
        _get_secret()

@pytest.mark.parametrize('launcher', ['pinak-memory', 'scripts/pinak-memory-server.sh'])
def test_launcher_fails_before_start_without_secret(launcher):
    env = os.environ.copy()
    env.pop('PINAK_JWT_SECRET', None)
    result = subprocess.run(['bash', str(ROOT / launcher)], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=8)
    assert result.returncode == 1
    assert 'PINAK_JWT_SECRET' in result.stderr


def test_cli_mint_requires_secret_and_has_scopes(monkeypatch):
    monkeypatch.delenv('PINAK_JWT_SECRET', raising=False)
    runner = CliRunner()
    assert runner.invoke(app, ['mint', 'tenant']).exit_code != 0
    monkeypatch.setenv('PINAK_JWT_SECRET', 'a-local-unique-test-key')
    result = runner.invoke(app, ['mint', 'tenant'])
    assert result.exit_code == 0
    payload = jwt.decode(result.stdout.strip(), 'a-local-unique-test-key', algorithms=['HS256'], audience='pinak-memory-api')
    assert payload['scopes'] == ['memory.read', 'memory.write']


def test_cli_search_uses_read_only_scope(monkeypatch):
    import httpx
    monkeypatch.setenv('PINAK_JWT_SECRET', 'a-local-unique-test-key')
    token_payloads = []
    class FakeResponse:
        def raise_for_status(self): pass
        def json(self): return {'context_by_layer': {}}
    class FakeClient:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def get(self, url, params, headers):
            token_payloads.append(jwt.decode(headers['Authorization'][7:], 'a-local-unique-test-key', algorithms=['HS256'], audience='pinak-memory-api'))
            return FakeResponse()
    monkeypatch.setattr(httpx, 'Client', FakeClient)
    result = CliRunner().invoke(app, ['search', 'item'])
    assert result.exit_code == 0
    assert token_payloads[0]['scopes'] == ['memory.read']
