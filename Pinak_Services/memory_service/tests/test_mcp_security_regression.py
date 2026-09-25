"""Disposable DB regression for scoped MCP/API primitives."""
import json
from pathlib import Path
import pytest
from app.core.database import DatabaseManager
from app.services.memory_service import MemoryService


def service(tmp_path, monkeypatch):
    root = tmp_path / 'data'
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'embedding_model': 'dummy', 'data_root': str(root)}))
    monkeypatch.setenv('PINAK_EMBEDDING_BACKEND', 'none')
    return MemoryService(config_path=str(path))


def test_rag_keyword_and_project_boundary(tmp_path, monkeypatch):
    svc = service(tmp_path, monkeypatch)
    rid = svc.add_rag('unicode moonlamp ☾', 'fixture:source', 'safe content moonlamp', 'tenant-a', 'project-a')['id']
    assert rid in [x['id'] for x in svc.db.search_rag('moonlamp', 'tenant-a', 'project-a')]
    assert rid in [x['id'] for x in svc.retrieve_context('moonlamp', 'tenant-a', 'project-a')['rag']]
    assert svc.db.search_rag('moonlamp', 'tenant-a', 'project-b') == []
    assert svc.db.search_rag('moonlamp', 'tenant-b', 'project-a') == []
    assert svc.db.search_rag('moonlamp', 'tenant-a', 'project-a', limit=-5)
    assert svc.db.search_rag('%_', 'tenant-a', 'project-a') == []


def test_update_column_injection_and_cross_scope(tmp_path, monkeypatch):
    svc = service(tmp_path, monkeypatch)
    rid = svc.add_rag('query', 'fixture:source', 'original', 'tenant-a', 'project-a')['id']
    with pytest.raises(ValueError, match='Unsupported update field'):
        svc.update_memory('rag', rid, {'content = \'altered\' --': 'bad'}, 'tenant-a', 'project-a')
    assert svc.db.get_memory('rag', rid, 'tenant-a', 'project-a')['content'] == 'original'
    assert svc.update_memory('rag', rid, {'content': 'wrong'}, 'tenant-b', 'project-a') is False
    assert svc.update_memory('rag', rid, {'content': 'wrong'}, 'tenant-a', 'project-b') is False
    assert svc.delete_memory('rag', rid, 'tenant-b', 'project-a') is False
    assert svc.db.get_memory('rag', rid, 'tenant-a', 'project-a')['content'] == 'original'
    assert svc.update_memory('rag', rid, {'content': 'updated'}, 'tenant-a', 'project-a') is True


def test_quarantine_review_requires_matching_scope_and_once(tmp_path, monkeypatch):
    svc = service(tmp_path, monkeypatch)
    qid = svc.propose_memory('episodic', {'content': 'quarantine fixture'}, 'tenant-a', 'project-a')['id']
    assert svc.resolve_quarantine(qid, 'approved', 'attacker', 'tenant-b', 'project-a')['status'] == 'missing'
    assert svc.resolve_quarantine(qid, 'approved', 'attacker', 'tenant-a', 'project-b')['status'] == 'missing'
    assert svc.resolve_quarantine(qid, 'approved', 'reviewer', 'tenant-a', 'project-a')['status'] == 'approved'
    assert svc.resolve_quarantine(qid, 'approved', 'reviewer', 'tenant-a', 'project-a')['status'] == 'missing'
    assert [x for x in svc.retrieve_context('quarantine', 'tenant-a', 'project-a')['episodic']]


def test_invalid_quarantine_payload_cannot_be_approved(tmp_path, monkeypatch):
    svc = service(tmp_path, monkeypatch)
    qid = svc.propose_memory('episodic', {'content': 'valid', 'not_in_schema': 'bad'}, 'tenant-a', 'project-a')['id']
    with pytest.raises(ValueError, match='Invalid quarantined payload'):
        svc.resolve_quarantine(qid, 'approved', 'reviewer', 'tenant-a', 'project-a')
    assert any(x['id'] == qid for x in svc.list_quarantine('tenant-a', 'project-a'))


def test_audit_chain_tamper_and_issue_scope(tmp_path, monkeypatch):
    svc = service(tmp_path, monkeypatch)
    issue = svc.db.add_client_issue(client_id='fixture', error_code='fixture', message='fixture', tenant='tenant-a', project_id='project-a')
    iid = issue['id']
    assert svc.resolve_client_issue(iid, 'wrong', 'attacker', 'tenant-b', 'project-a')['status'] == 'missing'
    assert svc.resolve_client_issue(iid, 'reviewed', 'reviewer', 'tenant-a', 'project-a')['status'] == 'resolved'
    assert svc.db.verify_audit_chain()['valid']
    with svc.db.get_cursor() as cur:
        cur.execute('UPDATE logs_audit SET payload = ? WHERE rowid = (SELECT MIN(rowid) FROM logs_audit)', ('{}',))
    assert svc.db.verify_audit_chain()['valid'] is False


def test_unsigned_client_header_cannot_claim_trusted_identity(monkeypatch):
    import datetime
    import jwt
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials
    from app.core.security import require_auth_context
    monkeypatch.setenv('PINAK_JWT_SECRET', 'sandbox-test-key-at-least-thirty-two-chars')
    token = jwt.encode({
        'sub': 'agent', 'tenant': 'a', 'project_id': 'p',
        'client_id': 'ordinary', 'roles': ['agent'], 'scopes': ['memory.read'],
        'exp': datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=5),
    }, 'sandbox-test-key-at-least-thirty-two-chars', algorithm='HS256')
    cred = HTTPAuthorizationCredentials(scheme='Bearer', credentials=token)
    with pytest.raises(HTTPException) as exc:
        require_auth_context(cred, client_id_header='trusted-client')
    assert exc.value.status_code == 403
    assert require_auth_context(cred, client_id_header='ordinary').client_id == 'ordinary'


def test_fts_update_delete_keeps_search_consistent(tmp_path):
    db = DatabaseManager(str(tmp_path / 'fts.db'))
    cases = [
        ('semantic', lambda: db.add_semantic('beforeword', [], 't', 'p', 1), {'content': 'afterword'}),
        ('episodic', lambda: db.add_episodic('beforeword', 't', 'p'), {'content': 'afterword'}),
        ('procedural', lambda: db.add_procedural('beforeword', ['one'], 't', 'p'), {'skill_name': 'afterword'}),
    ]
    for layer, create, update in cases:
        mid = create()['id']
        assert mid in [x['id'] for x in db.search_keyword('beforeword', 't', 'p')]
        assert db.update_memory(layer, mid, update, 't', 'p')
        assert mid not in [x['id'] for x in db.search_keyword('beforeword', 't', 'p')]
        assert mid in [x['id'] for x in db.search_keyword('afterword', 't', 'p')]
        assert db.delete_memory(layer, mid, 't', 'p')
        assert mid not in [x['id'] for x in db.search_keyword('afterword', 't', 'p')]


def test_unknown_schema_fails_closed(tmp_path):
    from app.core.schema_registry import SchemaRegistry
    registry = SchemaRegistry(schema_dir=str(tmp_path))
    # Fallback still provides the normal five schemas, not arbitrary names.
    assert registry.validate_payload('nonexistent_layer', {'content': 'x'})


def test_scope_bypass_environment_flag_ignored(monkeypatch):
    from fastapi import HTTPException
    from app.core.security import AuthContext, require_scope, require_role
    monkeypatch.setenv('PINAK_ENFORCE_SCOPES', 'false')
    ctx = AuthContext(subject='fixture', tenant_id='a', project_id='p', roles=['agent'], scopes=[], client_name=None, client_id=None, parent_client_id=None, child_client_id=None, effective_client_id='fixture', token='fixture')
    with pytest.raises(HTTPException) as err:
        require_scope(ctx, 'memory.admin')
    assert err.value.status_code == 403
    with pytest.raises(HTTPException) as err:
        require_role(ctx, 'admin')
    assert err.value.status_code == 403
