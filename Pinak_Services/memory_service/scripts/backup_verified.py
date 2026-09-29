"""Offline/online snapshot backup with ID consistency and checksum verification.

No cloud upload or scheduler is installed by this script. A quiescent writer
makes a consistent pair easiest; concurrent writes cause a fail-closed mismatch.
"""
import argparse
from collections import Counter
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

# Also support the README's direct `python scripts/backup_verified.py` route.
if __package__ in (None, ""):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.vector_snapshot import read_snapshot

TABLES = ("memories_semantic", "memories_episodic", "memories_procedural")


def digest(path):
    hasher = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def ids_match(database, vectors):
    with sqlite3.connect(database) as conn:
        db_ids = Counter()
        for table in TABLES:
            db_ids.update(row[0] for row in conn.execute(
                f"SELECT embedding_id FROM {table} WHERE embedding_id IS NOT NULL"))
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise AssertionError("SQLite integrity check failed")
    snapshot_vectors, snapshot_ids = read_snapshot(vectors)
    vector_ids = Counter(int(value) for value in snapshot_ids)
    if len(snapshot_vectors) != sum(vector_ids.values()):
        raise AssertionError("Vector shape mismatch")
    if db_ids != vector_ids:
        raise AssertionError("Database/vector ID mismatch: pause writes and retry")
    return sum(db_ids.values())


def create_backup(data_dir, backup_root):
    data_dir, backup_root = Path(data_dir), Path(backup_root)
    if data_dir.resolve() == backup_root.resolve() or data_dir.resolve() in backup_root.resolve().parents:
        raise ValueError("Backup destination must be outside source directory")
    db, index = data_dir / "memory.db", data_dir / "vectors.index.npy"
    if not db.is_file():
        raise FileNotFoundError("SQLite DB is required")
    backup_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".pinak-staging-", dir=backup_root))
    try:
        with sqlite3.connect(db) as source, sqlite3.connect(staging / "memory.db") as target:
            source.backup(target)
        if index.is_file():
            shutil.copyfile(index, staging / "vectors.index.npy")
            count = ids_match(staging / "memory.db", staging / "vectors.index.npy")
            file_names = ("memory.db", "vectors.index.npy")
        else:
            with sqlite3.connect(staging / "memory.db") as conn:
                if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise AssertionError("SQLite integrity check failed")
                count = sum(conn.execute(f"SELECT count(*) FROM {table} WHERE embedding_id IS NOT NULL")
                            .fetchone()[0] for table in TABLES)
            if count != 0:
                raise AssertionError("Missing vector snapshot for an indexed database")
            file_names = ("memory.db",)
        manifest = {"created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "vectors": count, "files": {name: digest(staging / name) for name in file_names}}
        (staging / "manifest.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
        final = backup_root / ("backup-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S")
                               + "-" + os.urandom(4).hex())
        staging.rename(final)
        return final
    except Exception:
        shutil.rmtree(staging)
        raise


def verify_backup(directory):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Backup directory must be a real directory")
    manifest_path = directory / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("Backup manifest must be a regular file")
    manifest = json.loads(manifest_path.read_text())
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
        raise AssertionError("Invalid backup manifest")
    names = set(manifest["files"])
    if names not in ({"memory.db"}, {"memory.db", "vectors.index.npy"}):
        raise AssertionError("Invalid backup manifest file set")
    if not isinstance(manifest.get("vectors"), int) or manifest["vectors"] < 0:
        raise AssertionError("Invalid backup vector count")
    for name in names:
        expected = manifest["files"][name]
        if not isinstance(expected, str) or len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
            raise AssertionError("Invalid backup digest")
        item = directory / name
        if item.is_symlink() or not item.is_file():
            raise AssertionError("Backup member must be a regular file")
    if {name for name in ("memory.db", "vectors.index.npy") if (directory / name).is_file()} != names:
        raise AssertionError("Backup file set mismatch")
    for name in names:
        if digest(directory / name) != manifest["files"][name]:
            raise AssertionError(f"Checksum mismatch: {name}")
    if "vectors.index.npy" in names:
        if ids_match(directory / "memory.db", directory / "vectors.index.npy") != manifest["vectors"]:
            raise AssertionError("Backup vector count mismatch")
    else:
        with sqlite3.connect(directory / "memory.db") as conn:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise AssertionError("SQLite integrity check failed")
            if manifest["vectors"] != 0:
                raise AssertionError("Unexpected vector count in keyword-only backup")
            if not all(conn.execute(f"SELECT count(*) FROM {table} WHERE embedding_id IS NOT NULL")
                       .fetchone()[0] == 0 for table in TABLES):
                raise AssertionError("Missing vector snapshot for an indexed database")
    return manifest


def main():
    parser = argparse.ArgumentParser(description="Create/verify a restorable Pinak memory snapshot")
    parser.add_argument("command", choices=("create", "verify"))
    parser.add_argument("--data-dir", default=os.getenv("PINAK_DATA_ROOT", "data"))
    parser.add_argument("--backup-root", default="../pinak-memory-backups")
    parser.add_argument("--backup-dir")
    args = parser.parse_args()
    if args.command == "create":
        location = create_backup(args.data_dir, args.backup_root)
        verify_backup(location)
        print(location)
    else:
        if not args.backup_dir:
            parser.error("--backup-dir is required for verify")
        print(json.dumps(verify_backup(args.backup_dir), sort_keys=True))


if __name__ == "__main__":
    main()
