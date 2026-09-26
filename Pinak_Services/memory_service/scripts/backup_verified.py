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

import numpy as np

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
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "SQLite integrity check failed"
    with open(vectors, "rb") as handle:
        data = np.load(handle, allow_pickle=True).item()
    vector_ids = Counter(int(value) for value in data["ids"])
    assert len(data["vectors"]) == sum(vector_ids.values()), "Vector shape mismatch"
    assert db_ids == vector_ids, "Database/vector ID mismatch: pause writes and retry"
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
                assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                count = sum(conn.execute(f"SELECT count(*) FROM {table} WHERE embedding_id IS NOT NULL")
                            .fetchone()[0] for table in TABLES)
            assert count == 0, "Missing vector snapshot for an indexed database"
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
    manifest = json.loads((directory / "manifest.json").read_text())
    for name in manifest["files"]:
        assert digest(directory / name) == manifest["files"][name], f"Checksum mismatch: {name}"
    assert set(manifest["files"]) in ({"memory.db"}, {"memory.db", "vectors.index.npy"})
    assert {name for name in ("memory.db", "vectors.index.npy") if (directory / name).is_file()} == set(manifest["files"])
    if "vectors.index.npy" in manifest["files"]:
        assert ids_match(directory / "memory.db", directory / "vectors.index.npy") == manifest["vectors"]
    else:
        with sqlite3.connect(directory / "memory.db") as conn:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert manifest["vectors"] == 0
            assert all(conn.execute(f"SELECT count(*) FROM {table} WHERE embedding_id IS NOT NULL")
                       .fetchone()[0] == 0 for table in TABLES)
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
