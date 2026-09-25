"""Audit integrity and deterministic insertion order regression coverage."""
from concurrent.futures import ThreadPoolExecutor
from app.core.database import DatabaseManager


def test_verify_audit_chain_and_detect_tamper(tmp_path):
    db = DatabaseManager(str(tmp_path / 'audit.db'))
    assert db.verify_audit_chain() == {'valid': True, 'checked': 0, 'head_hash': ''}
    first = db.add_audit_event('first', {'tenant': 'a'})
    second = db.add_audit_event('second', {'tenant': 'b'})
    verified = db.verify_audit_chain()
    assert verified == {'valid': True, 'checked': 2, 'head_hash': second['hash']}
    with db.get_cursor() as cur:
        cur.execute("UPDATE logs_audit SET payload = ? WHERE id = ?", ('{}', first['id']))
    assert db.verify_audit_chain() == {'valid': False, 'checked': 1, 'first_bad_id': first['id']}


def test_concurrent_audit_appends_form_one_chain(tmp_path):
    db = DatabaseManager(str(tmp_path / 'audit.db'))
    with ThreadPoolExecutor(max_workers=5) as executor:
        list(executor.map(lambda i: db.add_audit_event('parallel', {'i': i}), range(10)))
    assert db.verify_audit_chain()['valid'] is True
    assert db.verify_audit_chain()['checked'] == 10
