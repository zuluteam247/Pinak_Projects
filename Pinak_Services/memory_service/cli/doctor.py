import os
import sqlite3
import subprocess
import time
import shutil
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional

import httpx
import numpy as np

from app.core.database import DatabaseManager
from app.services.memory_service import MemoryService


@dataclass
class DoctorReport:
    ok: bool = True
    issues: List[str] = field(default_factory=list)
    actions: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def add_issue(self, message: str) -> None:
        self.ok = False
        self.issues.append(message)

    def add_action(self, message: str) -> None:
        self.actions.append(message)

    def add_note(self, message: str) -> None:
        self.notes.append(message)


def _get_db_path() -> str:
    return "data/memory.db"


def _get_vector_path() -> str:
    return "data/vectors.index.npy"


def _get_health_url() -> str:
    return os.getenv("PINAK_HEALTH_URL", "http://127.0.0.1:8000/api/v1/health")


def _launchctl_available() -> bool:
    return os.path.exists("/bin/launchctl")


def _run_launchctl(args: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/launchctl", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _check_service_health(url: str, timeout: float = 2.0) -> bool:
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url)
            return resp.status_code == 200
    except Exception:
        return False


def _kickstart_service(report: DoctorReport) -> None:
    if not _launchctl_available():
        report.add_issue("launchctl unavailable; cannot restart service")
        return

    uid = os.getuid()
    label = "com.pinak.memory.server"
    plist = "/Users/abhi-macmini/Library/LaunchAgents/com.pinak.memory.server.plist"

    loaded = _run_launchctl(["print", f"gui/{uid}/{label}"]).returncode == 0
    if not loaded:
        result = _run_launchctl(["bootstrap", f"gui/{uid}", plist])
        if result.returncode == 0:
            report.add_action("bootstrapped launch agent com.pinak.memory.server")
        else:
            report.add_issue(f"launchctl bootstrap failed: {result.stderr.strip() or result.stdout.strip()}")
            return

    result = _run_launchctl(["kickstart", "-k", f"gui/{uid}/{label}"])
    if result.returncode == 0:
        report.add_action("kickstarted launch agent com.pinak.memory.server")
    else:
        report.add_issue(f"launchctl kickstart failed: {result.stderr.strip() or result.stdout.strip()}")


def _ensure_watchdog(report: DoctorReport, fix: bool) -> None:
    watchdog_path = "scripts/pinak-memory-watchdog.sh"
    if not os.path.exists(watchdog_path):
        report.add_issue(f"watchdog script missing at {watchdog_path}")
        return

    if os.access(watchdog_path, os.X_OK):
        report.add_note("watchdog script executable")
        return

    if fix:
        os.chmod(watchdog_path, 0o755)
        report.add_action("set watchdog script executable")
    else:
        report.add_issue("watchdog script not executable")


def _ensure_launch_agent(report: DoctorReport, label: str, plist_path: str, fix: bool) -> None:
    if not _launchctl_available():
        report.add_issue("launchctl unavailable; cannot manage launch agents")
        return
    if not os.path.exists(plist_path):
        report.add_issue(f"launch agent plist missing: {plist_path}")
        return
    uid = os.getuid()
    loaded = _run_launchctl(["print", f"gui/{uid}/{label}"]).returncode == 0
    if loaded:
        report.add_note(f"launch agent loaded: {label}")
        return
    if fix:
        result = _run_launchctl(["bootstrap", f"gui/{uid}", plist_path])
        if result.returncode == 0:
            report.add_action(f"bootstrapped launch agent {label}")
        else:
            report.add_issue(f"launchctl bootstrap failed ({label}): {result.stderr.strip() or result.stdout.strip()}")
    else:
        report.add_issue(f"launch agent not loaded: {label}")


def _ensure_backup(report: DoctorReport, fix: bool) -> None:
    backup_script = "scripts/pinak-memory-backup.sh"
    plist = "/Users/abhi-macmini/Library/LaunchAgents/com.pinak.memory.backup.plist"
    if not os.path.exists(backup_script):
        report.add_issue(f"backup script missing: {backup_script}")
    elif not os.access(backup_script, os.X_OK):
        if fix:
            os.chmod(backup_script, 0o755)
            report.add_action("set backup script executable")
        else:
            report.add_issue("backup script not executable")

    if not os.path.exists(plist):
        report.add_issue(f"backup launch agent missing: {plist}")

    if not shutil.which("rclone"):
        report.add_issue("rclone not installed; Google Drive backup will not run")
    else:
        try:
            result = subprocess.run(
                ["rclone", "listremotes"],
                capture_output=True,
                text=True,
                check=False,
            )
            if "gdrive:" not in (result.stdout or ""):
                report.add_issue("rclone installed but gdrive remote not configured")
        except Exception:
            report.add_issue("rclone remote check failed")


def _ensure_schema_assets(report: DoctorReport, fix: bool) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    repo_schema_dir = repo_root / "schemas"
    repo_template_dir = repo_root / "templates"
    home_root = Path(os.path.expanduser("~/pinak-memory"))
    home_schema_dir = home_root / "schemas"
    home_template_dir = home_root / "templates"

    required_schemas = [
        "semantic.schema.json",
        "episodic.schema.json",
        "procedural.schema.json",
        "rag.schema.json",
        "working.schema.json",
    ]

    if not home_schema_dir.exists():
        if fix:
            home_schema_dir.mkdir(parents=True, exist_ok=True)
            report.add_action(f"created schema dir at {home_schema_dir}")
        else:
            report.add_issue(f"schema dir missing: {home_schema_dir}")

    for name in required_schemas:
        dest = home_schema_dir / name
        if dest.exists():
            continue
        src = repo_schema_dir / name
        if fix and src.exists():
            shutil.copy2(src, dest)
            report.add_action(f"copied schema {name} to {home_schema_dir}")
        else:
            report.add_issue(f"schema missing: {dest}")

    if not home_template_dir.exists():
        if fix:
            home_template_dir.mkdir(parents=True, exist_ok=True)
            report.add_action(f"created templates dir at {home_template_dir}")
        else:
            report.add_issue(f"templates dir missing: {home_template_dir}")

    if repo_template_dir.exists():
        for template in repo_template_dir.glob("*.json"):
            dest = home_template_dir / template.name
            if not dest.exists() and fix:
                shutil.copy2(template, dest)
                report.add_action(f"copied template {template.name} to {home_template_dir}")

    access_policy = home_root / "ACCESS_POLICY.md"
    if not access_policy.exists():
        if fix:
            access_policy.write_text(
                "# Pinak Memory Access Policy\\n\\n"
                "- This directory contains client schemas and templates for pinak-memory.\\n"
                "- Do not modify service source code without admin approval.\\n"
                "- Use scripts/pinak-lockdown.sh to require macOS admin password for write access.\\n"
            )
            report.add_action(f"created access policy at {access_policy}")
        else:
            report.add_issue(f"access policy missing: {access_policy}")


def _ensure_lockdown_policy(report: DoctorReport, fix: bool) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    lock_script = repo_root / "scripts" / "pinak-lockdown.sh"
    unlock_script = repo_root / "scripts" / "pinak-unlock.sh"
    if not lock_script.exists():
        report.add_issue(f"lockdown script missing: {lock_script}")
    if not unlock_script.exists():
        report.add_issue(f"unlock script missing: {unlock_script}")
    if os.access(repo_root, os.W_OK):
        report.add_issue("source tree is writable; consider running scripts/pinak-lockdown.sh")


def _check_llm_runtime(report: DoctorReport) -> None:
    mlx_ok = False
    try:
        import mlx  # type: ignore
        mlx_ok = True
    except Exception:
        mlx_ok = False
    if not mlx_ok:
        python = shutil.which("python3.14") or shutil.which("python3")
        if python:
            result = subprocess.run([python, "-c", "import mlx"], capture_output=True, text=True, check=False)
            if result.returncode == 0:
                mlx_ok = True
                report.add_note(f"MLX available via {python}")
    if mlx_ok:
        report.add_note("MLX available for doctor agent")
    else:
        if os.path.exists("/opt/homebrew/bin/gemini"):
            report.add_note("MLX not found; gemini CLI available for doctor agent")
        else:
            report.add_issue("No local LLM runtime found (MLX missing; gemini CLI not found)")


def _check_embedding_backend(report: DoctorReport) -> None:
    backend = os.getenv("PINAK_EMBEDDING_BACKEND", "")
    if backend.lower() == "qmd":
        if not shutil.which("qmd"):
            report.add_issue("PINAK_EMBEDDING_BACKEND=qmd but qmd is not installed")
        else:
            report.add_note("qmd embedding backend selected")


def _ensure_db(report: DoctorReport, fix: bool) -> None:
    db_path = _get_db_path()
    if not os.path.exists(db_path):
        if fix:
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            DatabaseManager(db_path)
            report.add_action(f"created database at {db_path}")
        else:
            report.add_issue(f"database not found at {db_path}")
        return

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("PRAGMA integrity_check")
        res = cursor.fetchone()[0]
        conn.close()
        if res != "ok":
            report.add_issue(f"db integrity check failed: {res}")
        else:
            report.add_note("database integrity ok")
    except Exception as exc:
        report.add_issue(f"db integrity error: {exc}")

    if fix:
        DatabaseManager(db_path)
        report.add_action("ensured core schema tables/columns")


def _get_vector_index_size(vec_path: str) -> Optional[int]:
    if not os.path.exists(vec_path):
        return None
    from app.services.vector_snapshot import read_snapshot
    _, ids = read_snapshot(vec_path)
    return len(ids)


def _get_db_vector_count(db_path: str) -> int:
    if not os.path.exists(db_path):
        return 0
    total = 0
    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        for table in ["memories_semantic", "memories_episodic", "memories_procedural"]:
            try:
                cur.execute(f"SELECT count(*) FROM {table} WHERE embedding_id IS NOT NULL")
                total += cur.fetchone()[0]
            except Exception:
                continue
    return total


def _ensure_vectors(report: DoctorReport, fix: bool, allow_heavy: bool) -> None:
    vec_path = _get_vector_path()
    db_path = _get_db_path()
    try:
        index_size = _get_vector_index_size(vec_path)
    except ValueError as exc:
        report.add_issue(f"vector snapshot invalid: {exc}")
        return
    if index_size is None:
        if fix and allow_heavy:
            service = MemoryService()
            service._rebuild_index()
            report.add_action("rebuilt vector index from db (all layers)")
        else:
            report.add_issue(f"vector index not found at {vec_path}")
        return

    db_count = _get_db_vector_count(db_path)
    if db_count != index_size:
        report.add_issue(
            f"vector/db mismatch: db has {db_count} embeddings, index has {index_size}"
        )
        if fix and allow_heavy:
            service = MemoryService()
            service._rebuild_index()
            report.add_action("rebuilt vector index from db (all layers)")
    else:
        report.add_note(f"vector index ok (size {index_size})")


def _backfill_missing_clients(report: DoctorReport, fix: bool) -> None:
    db_path = _get_db_path()
    if not os.path.exists(db_path):
        return

    tables = [
        "memories_semantic",
        "memories_episodic",
        "memories_procedural",
        "memories_rag",
        "memories_working",
        "memories_session",
    ]
    missing_total = 0
    updated_total = 0

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        for table in tables:
            try:
                cur.execute(f"PRAGMA table_info({table})")
                cols = {row[1] for row in cur.fetchall()}
            except Exception:
                continue
            if "client_id" not in cols:
                continue
            cur.execute(
                f"""
                SELECT count(*) FROM {table}
                WHERE client_id IS NULL OR client_id = ''
                """
            )
            missing = cur.fetchone()[0]
            if not missing:
                continue
            missing_total += missing
            if fix:
                cur.execute(
                    f"""
                    UPDATE {table}
                    SET client_id = 'unknown'
                    WHERE client_id IS NULL OR client_id = ''
                    """
                )
                updated_total += cur.rowcount
                if "client_name" in cols:
                    cur.execute(
                        f"""
                        UPDATE {table}
                        SET client_name = 'unknown'
                        WHERE client_name IS NULL OR client_name = ''
                        """
                    )

        if fix:
            conn.commit()

    if missing_total and not fix:
        report.add_issue(f"{missing_total} memory rows missing client_id (run doctor --fix)")
    if fix and updated_total:
        report.add_action(f"backfilled client_id for {updated_total} memory rows")


def run_doctor(fix: bool = False, allow_heavy: bool = False) -> DoctorReport:
    report = DoctorReport()
    health_url = _get_health_url()

    if not _check_service_health(health_url):
        report.add_issue(f"service health check failed ({health_url})")
        if fix:
            _kickstart_service(report)
            for _ in range(15):
                time.sleep(2)
                if _check_service_health(health_url):
                    report.add_action("service health restored")
                    break
            else:
                report.add_issue("service still unhealthy after restart attempt")
    else:
        report.add_note("service health ok")

    _ensure_watchdog(report, fix)
    _ensure_launch_agent(
        report,
        "com.pinak.memory.watchdog",
        "/Users/abhi-macmini/Library/LaunchAgents/com.pinak.memory.watchdog.plist",
        fix,
    )
    _ensure_launch_agent(
        report,
        "com.pinak.memory.doctor",
        "/Users/abhi-macmini/Library/LaunchAgents/com.pinak.memory.doctor.plist",
        fix,
    )
    _ensure_launch_agent(
        report,
        "com.pinak.memory.backup",
        "/Users/abhi-macmini/Library/LaunchAgents/com.pinak.memory.backup.plist",
        fix,
    )
    _ensure_backup(report, fix)
    _ensure_db(report, fix)
    _backfill_missing_clients(report, fix)
    _ensure_vectors(report, fix, allow_heavy)
    _ensure_schema_assets(report, fix)
    _ensure_lockdown_policy(report, fix)
    _check_llm_runtime(report)

    return report
