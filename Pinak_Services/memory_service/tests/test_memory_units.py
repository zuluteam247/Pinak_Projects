"""Scoped cross-layer identity, long-event recall, and HTTP authorization."""
import jwt
from pathlib import Path
from fastapi.testclient import TestClient
from app.main import app
from app.api.v1.endpoints import get_memory_service
from app.core.database import DatabaseManager


def _token(tenant='a', project='p', role='admin', scopes=None):
    return jwt.encode({'sub': 'fixture', 'tenant': tenant, 'project_id': project,
                       'roles': [role], 'scopes': scopes or ['memory.read', 'memory.admin']},
                      'test-secret', algorithm='HS256')


def _headers(**kwargs):
    return {'Authorization': 'Bearer ' + _token(**kwargs)}


def test_long_event_siblings_and_scope(tmp_path):
    db = DatabaseManager(str(tmp_path / 'unit.db'))
    # A long incident is represented by relevant records, not seven cloned texts.
    episode = db.add_episodic('A long incident from detection to restoration', 'a', 'p',
                              goal='restore service', outcome='restored')['id']
    procedure = db.add_procedural('Runbook after incident', ['Check alerts', 'Confirm recovery'], 'a', 'p')['id']
    rag = db.add_rag('Incident notes', 'fixture:postmortem', 'A cited source paragraph', 'a', 'p')['id']
    event = db.add_event('incident:resolved', {'ref': 'synthetic'}, 'a', 'p')['id']
    same_text_other = db.add_episodic('A long incident from detection to restoration', 'b', 'p')['id']
    members = [{'layer': 'episodic', 'id': episode, 'role': 'narrative'},
               {'layer': 'procedural', 'id': procedure, 'role': 'lesson'},
               {'layer': 'rag', 'id': rag, 'role': 'source'},
               {'layer': 'event', 'id': event, 'role': 'milestone'}]
    assert db.related_memory('episodic', episode, 'a', 'p') is None
    try:
        db.create_memory_unit('Bad cross-tenant attempt', members +
                              [{'layer': 'episodic', 'id': same_text_other, 'role': 'forged'}], 'a', 'p')
        assert False, 'cross-tenant member accepted'
    except ValueError as exc:
        assert 'not found' in str(exc)
    assert db.related_memory('episodic', episode, 'a', 'p') is None  # transaction rolled back
    unit = db.create_memory_unit('Synthetic long incident', members, 'a', 'p')
    assert {x['layer'] for x in db.related_memory('episodic', episode, 'a', 'p')['members']} == {
        'episodic', 'procedural', 'rag', 'event'}
    assert db.related_memory('event', event, 'a', 'p')['id'] == unit['id']
    assert db.related_memory('episodic', episode, 'b', 'p') is None
    assert db.related_memory('episodic', episode, 'a', 'other') is None
    assert db.related_memory('episodic', same_text_other, 'b', 'p') is None
    try:
        db.create_memory_unit('Duplicate membership', [members[0]], 'a', 'p')
        assert False, 'duplicate accepted'
    except ValueError as exc:
        assert 'already belongs' in str(exc)
    assert db.verify_audit_chain()['valid']
    # Deleted source IDs cannot be used as a stale link back into sibling data.
    assert db.delete_memory('episodic', episode, 'a', 'p')
    assert db.related_memory('episodic', episode, 'a', 'p') is None
    assert {x['layer'] for x in db.related_memory('event', event, 'a', 'p')['members']} == {
        'procedural', 'rag', 'event'}


def test_http_admin_link_read_scope_and_expiry(tmp_path, monkeypatch):
    monkeypatch.setenv("PINAK_JWT_SECRET", "test-secret")
    db = DatabaseManager(str(tmp_path / 'api.db'))
    a = db.add_episodic('fixture incident', 'a', 'p')['id']
    b = db.add_rag('fixture', 'fixture:source', 'source paragraph', 'a', 'p')['id']
    class Service:
        pass
    service = Service()
    service.db = db
    app.dependency_overrides[get_memory_service] = lambda: service
    app.state.verification_status = 'ready'
    try:
        with TestClient(app) as client:
            payload = {'label': 'Incident', 'members': [
                {'layer': 'episodic', 'id': a, 'role': 'narrative'},
                {'layer': 'rag', 'id': b, 'role': 'source'}]}
            url = '/api/v1/memory/units'
            assert client.post(url, json=payload, headers=_headers(role='agent')).status_code == 403
            assert client.post(url, json=payload, headers=_headers(tenant='b')).status_code == 400
            made = client.post(url, json=payload, headers=_headers())
            assert made.status_code == 201, made.text
            related = '/api/v1/memory/units/related/rag/' + b
            assert client.get(related, headers=_headers(role='agent', scopes=['memory.read'])).json()['id'] == made.json()['id']
            assert client.get(related, headers=_headers(tenant='b')).status_code == 404
            assert client.get(related, headers=_headers(project='other')).status_code == 404
            assert client.get(related, headers=_headers(scopes=['memory.admin'])).status_code == 403
            assert client.get('/api/v1/memory/units/related/garbage/' + b, headers=_headers()).status_code == 400
    finally:
        app.dependency_overrides.clear()


def test_mcp_recall_expands_reviewed_siblings(monkeypatch):
    import importlib.util
    import sys
    from types import ModuleType
    class FakeMCP:
        def __init__(self, *_): pass
        def tool(self): return lambda f: f
    fake = ModuleType('fastmcp')
    fake.FastMCP = FakeMCP
    monkeypatch.setitem(sys.modules, 'fastmcp', fake)
    spec = importlib.util.spec_from_file_location('pinak_memory_mcp_fixture',
        Path(__file__).resolve().parents[1] / 'client' / 'pinak_memory_mcp.py')
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    def fake_request(method, path, **kwargs):
        if path == '/memory/retrieve_context':
            return {'semantic': [], 'episodic': [{'id': 'ep1', 'content': 'fixture',
                    'goal': 'test', 'outcome': 'done'}], 'procedural': [], 'rag': [], 'working': []}
        if path == '/memory/units/related/episodic/ep1':
            return {'id': 'unit-1', 'members': [
                {'layer': 'episodic', 'role': 'narrative', 'record': {'id': 'ep1'}},
                {'layer': 'rag', 'role': 'source', 'record': {'id': 'rag1', 'content': 'cited fixture'}},
                {'layer': 'event', 'role': 'milestone', 'record': {'id': 'evt1', 'event_type': 'done'}}]}
        raise AssertionError('unexpected lookup: ' + path)
    monkeypatch.setattr(bridge, '_api_request', fake_request)
    monkeypatch.setattr(bridge, '_session_banner', lambda: '')
    monkeypatch.setattr(bridge, '_status_notice', lambda: '')
    rendered = bridge._recall_impl('fixture')
    assert 'RELATED RECORDS' in rendered
    assert 'rag1' in rendered and 'evt1' in rendered
    assert 'UNTRUSTED MEMORY DATA' in rendered
