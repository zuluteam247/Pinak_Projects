
import sqlite3
import os
import json
import uuid
import datetime
import logging
import re
from contextlib import contextmanager
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

class DatabaseManager:
    def __init__(self, db_path: str):
        self.db_path = db_path
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with self.get_cursor() as conn:
            # 1. Semantic Memory (Knowledge)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memories_semantic (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    tags TEXT, -- JSON list
                    embedding_id INTEGER, -- Link to FAISS
                    agent_id TEXT,
                    client_id TEXT,
                    client_name TEXT,
                    tenant TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)
            # FTS for Semantic
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_semantic_fts 
                USING fts5(content, tags, content='memories_semantic', content_rowid='rowid');
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS memories_semantic_ai AFTER INSERT ON memories_semantic BEGIN
                  INSERT INTO memories_semantic_fts(rowid, content, tags) VALUES (new.rowid, new.content, new.tags);
                END;
            """)


            # 2. Episodic Memory (Events/Logs)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memories_episodic (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    goal TEXT,
                    outcome TEXT,
                    plan TEXT, -- JSON
                    steps TEXT, -- JSON list of executed steps
                    salience INTEGER,
                    embedding_id INTEGER, -- Link to FAISS
                    agent_id TEXT,
                    client_id TEXT,
                    client_name TEXT,
                    tenant TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_episodic_fts
                USING fts5(content, goal, outcome, content='memories_episodic', content_rowid='rowid');
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS memories_episodic_ai AFTER INSERT ON memories_episodic BEGIN
                  INSERT INTO memories_episodic_fts(rowid, content, goal, outcome) VALUES (new.rowid, new.content, new.goal, new.outcome);
                END;
            """)


            # 3. Procedural Memory (Skills)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memories_procedural (
                    id TEXT PRIMARY KEY,
                    skill_name TEXT NOT NULL,
                    trigger TEXT,
                    steps TEXT, -- JSON
                    description TEXT,
                    code_snippet TEXT,
                    embedding_id INTEGER, -- Link to FAISS
                    agent_id TEXT,
                    client_id TEXT,
                    client_name TEXT,
                    tenant TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)
            # FTS for Procedural
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_procedural_fts
                USING fts5(skill_name, trigger, steps, description, content='memories_procedural', content_rowid='rowid');
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS memories_procedural_ai AFTER INSERT ON memories_procedural BEGIN
                  INSERT INTO memories_procedural_fts(rowid, skill_name, trigger, steps, description) VALUES (new.rowid, new.skill_name, new.trigger, new.steps, new.description);
                END;
            """)


            # 4. RAG Memory (External Source)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memories_rag (
                    id TEXT PRIMARY KEY,
                    query TEXT,
                    external_source TEXT,
                    content TEXT,
                    agent_id TEXT,
                    client_id TEXT,
                    client_name TEXT,
                    tenant TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)

            # 5. Working Memory (Short-term)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS working_memory (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL, -- JSON
                    agent_id TEXT,
                    client_id TEXT,
                    client_name TEXT,
                    tenant TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)
            
            # --- LOGGING TABLES ---
            
            # 6. Event Log (Immutable Audit Trail)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS logs_events (
                    id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    payload TEXT, -- JSON
                    tenant TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    ts TEXT NOT NULL
                );
            """)

            # 7. Session Log (Chat History / Reasoning Trace)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS logs_session (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    role TEXT NOT NULL, -- user, assistant, system, tool
                    tool_calls TEXT, -- JSON
                    agent_id TEXT,
                    client_id TEXT,
                    client_name TEXT,
                    parent_client_id TEXT,
                    child_client_id TEXT,
                    tenant TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    ts TEXT NOT NULL
                );
            """)
            # 7b. Client Registry (Observed + Registered clients)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS clients_registry (
                    id TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    client_name TEXT,
                    parent_client_id TEXT,
                    status TEXT NOT NULL, -- observed|registered|trusted|blocked
                    metadata TEXT, -- JSON
                    tenant TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_seen TEXT
                );
            """)
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_clients_registry_unique
                ON clients_registry (client_id, tenant, project_id);
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_clients_registry_status
                ON clients_registry (status);
            """)
            # 8. Agent Registry (Live Presence)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS logs_agents (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    client_name TEXT NOT NULL,
                    client_id TEXT,
                    parent_client_id TEXT,
                    hostname TEXT,
                    pid TEXT,
                    status TEXT NOT NULL,
                    meta TEXT, -- JSON
                    tenant TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    last_seen TEXT NOT NULL
                );
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_logs_agents_last_seen
                ON logs_agents (last_seen);
            """)
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_logs_agents_agent_unique
                ON logs_agents (agent_id, client_name, tenant, project_id);
            """)

            # 9. Memory Access Logs (Reads/Writes)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS logs_access (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT,
                    client_name TEXT,
                    client_id TEXT,
                    parent_client_id TEXT,
                    child_client_id TEXT,
                    event_type TEXT NOT NULL, -- read|write|delete|update|propose
                    target_layer TEXT,
                    query TEXT,
                    memory_id TEXT,
                    result_count INTEGER,
                    status TEXT NOT NULL, -- ok|error
                    detail TEXT,
                    tenant TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    ts TEXT NOT NULL
                );
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_logs_access_ts
                ON logs_access (ts);
            """)

            # 10. Quarantine Queue (Pending Writes)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_quarantine (
                    id TEXT PRIMARY KEY,
                    layer TEXT NOT NULL,
                    payload TEXT NOT NULL, -- JSON
                    status TEXT NOT NULL, -- pending|approved|rejected
                    agent_id TEXT,
                    client_id TEXT,
                    client_name TEXT,
                    validation_errors TEXT, -- JSON list
                    tenant TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT,
                    reviewed_by TEXT
                );
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_memory_quarantine_status
                ON memory_quarantine (status);
            """)

            # Explicit cross-layer membership. IDs do not imply shared identity on their own.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_units (
                    id TEXT NOT NULL, tenant TEXT NOT NULL, project_id TEXT NOT NULL,
                    label TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY (id, tenant, project_id)
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_unit_members (
                    unit_id TEXT NOT NULL, layer TEXT NOT NULL, record_id TEXT NOT NULL,
                    tenant TEXT NOT NULL, project_id TEXT NOT NULL, role TEXT NOT NULL,
                    PRIMARY KEY (unit_id, layer, record_id, tenant, project_id),
                    UNIQUE (layer, record_id, tenant, project_id),
                    FOREIGN KEY (unit_id, tenant, project_id)
                      REFERENCES memory_units (id, tenant, project_id)
                );
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_memory_unit_record
                ON memory_unit_members (tenant, project_id, layer, record_id);
            """)
            # 11. Audit Log (Tamper-evident)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS logs_audit (
                    id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL, -- JSON
                    prev_hash TEXT,
                    hash TEXT NOT NULL,
                    ts TEXT NOT NULL
                );
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_logs_audit_ts
                ON logs_audit (ts);
            """)
            # 12. Client Issue Log (ingestion failures, schema errors, auth issues)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS logs_client_issues (
                    id TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    client_name TEXT,
                    agent_id TEXT,
                    parent_client_id TEXT,
                    child_client_id TEXT,
                    layer TEXT,
                    error_code TEXT NOT NULL,
                    message TEXT NOT NULL,
                    payload TEXT,
                    metadata TEXT,
                    status TEXT NOT NULL, -- open|resolved
                    tenant TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT,
                    resolved_by TEXT,
                    resolution TEXT
                );
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_logs_client_issues_status
                ON logs_client_issues (status);
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_logs_client_issues_ts
                ON logs_client_issues (created_at);
            """)
            self._ensure_column(conn, "working_memory", "expires_at", "TEXT")
            self._ensure_column(conn, "working_memory", "updated_at", "TEXT")
            self._ensure_column(conn, "logs_session", "expires_at", "TEXT")
            self._ensure_column(conn, "logs_session", "agent_id", "TEXT")
            self._ensure_column(conn, "logs_session", "client_id", "TEXT")
            self._ensure_column(conn, "logs_session", "client_name", "TEXT")
            self._ensure_column(conn, "logs_session", "parent_client_id", "TEXT")
            self._ensure_column(conn, "logs_session", "child_client_id", "TEXT")
            self._ensure_column(conn, "logs_access", "client_id", "TEXT")
            self._ensure_column(conn, "logs_access", "parent_client_id", "TEXT")
            self._ensure_column(conn, "logs_access", "child_client_id", "TEXT")
            self._ensure_column(conn, "logs_agents", "client_id", "TEXT")
            self._ensure_column(conn, "logs_agents", "parent_client_id", "TEXT")
            self._ensure_column(conn, "memory_quarantine", "agent_id", "TEXT")
            self._ensure_column(conn, "memory_quarantine", "client_id", "TEXT")
            self._ensure_column(conn, "memory_quarantine", "client_name", "TEXT")
            self._ensure_column(conn, "memory_quarantine", "validation_errors", "TEXT")
            self._ensure_column(conn, "memories_semantic", "agent_id", "TEXT")
            self._ensure_column(conn, "memories_semantic", "client_id", "TEXT")
            self._ensure_column(conn, "memories_semantic", "client_name", "TEXT")
            self._ensure_column(conn, "memories_episodic", "agent_id", "TEXT")
            self._ensure_column(conn, "memories_episodic", "client_id", "TEXT")
            self._ensure_column(conn, "memories_episodic", "client_name", "TEXT")
            self._ensure_column(conn, "memories_episodic", "steps", "TEXT")
            self._ensure_column(conn, "memories_procedural", "agent_id", "TEXT")
            self._ensure_column(conn, "memories_procedural", "client_id", "TEXT")
            self._ensure_column(conn, "memories_procedural", "client_name", "TEXT")
            self._ensure_column(conn, "memories_rag", "agent_id", "TEXT")
            self._ensure_column(conn, "memories_rag", "client_id", "TEXT")
            self._ensure_column(conn, "memories_rag", "client_name", "TEXT")
            self._ensure_column(conn, "working_memory", "agent_id", "TEXT")
            self._ensure_column(conn, "working_memory", "client_id", "TEXT")
            self._ensure_column(conn, "working_memory", "client_name", "TEXT")
            # Add FTS update/delete triggers only for complete schemas. Doctor can
            # open sparse legacy DBs and backfill IDs before their FTS migration.
            fts_tables = {
                "semantic": ("content", "tags"),
                "episodic": ("content", "goal", "outcome"),
                "procedural": ("skill_name", "trigger", "steps", "description"),
            }
            for layer, cols in fts_tables.items():
                table = f"memories_{layer}"
                if not all(self._column_exists(conn, table, col) for col in cols):
                    continue
                fts = f"{table}_fts"
                old_cols = ", ".join(f"old.{col}" for col in cols)
                new_cols = ", ".join(f"new.{col}" for col in cols)
                names = ", ".join(cols)
                conn.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_au AFTER UPDATE ON {table} BEGIN
                    INSERT INTO {fts}({fts}, rowid, {names}) VALUES ('delete', old.rowid, {old_cols});
                    INSERT INTO {fts}(rowid, {names}) VALUES (new.rowid, {new_cols});
                END""")
                conn.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_ad AFTER DELETE ON {table} BEGIN
                    INSERT INTO {fts}({fts}, rowid, {names}) VALUES ('delete', old.rowid, {old_cols});
                END""")


    def _column_exists(self, conn: sqlite3.Connection, table: str, column: str) -> bool:
        try:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        except sqlite3.OperationalError:
            return False
        for row in rows:
            name = row["name"] if isinstance(row, sqlite3.Row) else row[1]
            if name == column:
                return True
        return False

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, col_type: str) -> None:
        if self._column_exists(conn, table, column):
            return
        logger.info("Adding missing column %s.%s", table, column)
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")

    def has_column(self, table: str, column: str) -> bool:
        with self.get_cursor() as conn:
            return self._column_exists(conn, table, column)

    @contextmanager
    def get_cursor(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _sanitize_fts_query(self, query: str) -> str:
        terms = []
        for raw in (query or "").split():
            token = raw.strip().replace('"', "").replace("'", "")
            if not token:
                continue
            if re.search(r"[^A-Za-z0-9_]", token):
                terms.append(f"\"{token}\"")
            else:
                terms.append(token)
        return " ".join(terms) if terms else (query or "")

    # --- CRUD Operations ---

    def add_semantic(self, content: str, tags: list, tenant: str, project_id: str, embedding_id: int,
                     agent_id: Optional[str] = None, client_id: Optional[str] = None,
                     client_name: Optional[str] = None) -> Dict[str, Any]:
        mid = str(uuid.uuid4())
        created_at = datetime.datetime.now().isoformat()
        with self.get_cursor() as cur:
            cur.execute("""
                INSERT INTO memories_semantic (id, content, tags, embedding_id, agent_id, client_id, client_name, tenant, project_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (mid, content, json.dumps(tags), embedding_id, agent_id, client_id, client_name, tenant, project_id, created_at))
        return {
            "id": mid,
            "content": content,
            "tags": tags,
            "tenant": tenant,
            "project_id": project_id,
            "created_at": created_at,
            "agent_id": agent_id,
            "client_id": client_id,
            "client_name": client_name,
        }

    def search_keyword(self, query: str, tenant: str, project_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        # Searches across semantic, episodic, procedural FTS tables
        # Returns unified list of results
        results = []
        with self.get_cursor() as conn:
            fts_query = self._sanitize_fts_query(query)

            cur = conn.execute("""
                SELECT m.id, m.content, m.tags, 'semantic' as type
                FROM memories_semantic m
                JOIN memories_semantic_fts f ON m.rowid = f.rowid
                WHERE memories_semantic_fts MATCH ? AND m.tenant = ? AND m.project_id = ?
                ORDER BY f.rank LIMIT ?
            """, (fts_query, tenant, project_id, limit))
            sem_rows = [dict(row) for row in cur.fetchall()]
            for row in sem_rows:
                if row.get("tags"):
                    try:
                        row["tags"] = json.loads(row["tags"])
                    except Exception:
                        pass
            results.extend(sem_rows)

            # Episodic
            cur = conn.execute("""
                SELECT m.id, m.content, m.goal, m.outcome, m.plan, m.steps, 'episodic' as type
                FROM memories_episodic m
                JOIN memories_episodic_fts f ON m.rowid = f.rowid
                WHERE memories_episodic_fts MATCH ? AND m.tenant = ? AND m.project_id = ?
                ORDER BY f.rank LIMIT ?
            """, (fts_query, tenant, project_id, limit))
            epi_rows = [dict(row) for row in cur.fetchall()]
            for row in epi_rows:
                if row.get("plan"):
                    try:
                        row["plan"] = json.loads(row["plan"])
                    except Exception:
                        pass
                if row.get("steps"):
                    try:
                        row["steps"] = json.loads(row["steps"])
                        row["tool_logs"] = row["steps"]
                    except Exception:
                        pass
            results.extend(epi_rows)

            # Procedural
            cur = conn.execute("""
                SELECT m.id, m.skill_name as content, m.description, m.steps, 'procedural' as type 
                FROM memories_procedural m
                JOIN memories_procedural_fts f ON m.rowid = f.rowid
                WHERE memories_procedural_fts MATCH ? AND m.tenant = ? AND m.project_id = ?
                ORDER BY f.rank LIMIT ?
            """, (fts_query, tenant, project_id, limit))
            proc_rows = [dict(row) for row in cur.fetchall()]
            for row in proc_rows:
                if row.get("steps"):
                    try:
                        row["steps"] = json.loads(row["steps"])
                    except Exception:
                        pass
            results.extend(proc_rows)
            
        return results

    def search_rag(self, query: str, tenant: str, project_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Tenant-scoped RAG keyword lookup; no vector index for this layer."""
        words = [word for word in query.split() if word][:8]
        if not words:
            return []
        # Escape LIKE metacharacters; content/query/source are searched literally.
        with self.get_cursor() as conn:
            clauses = []
            params = []
            for word in words:
                pattern = "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                clauses.append("(query LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\' OR external_source LIKE ? ESCAPE '\\')")
                params.extend([pattern] * 3)
            sql = "SELECT *, 'rag' AS type FROM memories_rag WHERE tenant = ? AND project_id = ? AND (" + " OR ".join(clauses) + ") ORDER BY created_at DESC LIMIT ?"
            return [dict(row) for row in conn.execute(sql, (tenant, project_id, *params, max(1, min(limit, 100))))]

    def add_episodic(self, content: str, tenant: str, project_id: str,
                     salience: int = 0, goal: Optional[str] = None,
                     plan: Optional[list] = None, tool_logs: Optional[list] = None,
                     outcome: Optional[str] = None, embedding_id: Optional[int] = None,
                     agent_id: Optional[str] = None, client_id: Optional[str] = None,
                     client_name: Optional[str] = None) -> Dict[str, Any]:
        mid = str(uuid.uuid4())
        created_at = datetime.datetime.now().isoformat()
        steps = tool_logs or []
        with self.get_cursor() as cur:
            cur.execute("""
                INSERT INTO memories_episodic (id, content, goal, outcome, plan, steps, salience, embedding_id, agent_id, client_id, client_name, tenant, project_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (mid, content, goal, outcome, json.dumps(plan), json.dumps(steps), salience, embedding_id, agent_id, client_id, client_name, tenant, project_id, created_at))
        return {
            "id": mid,
            "goal": goal,
            "agent_id": agent_id,
            "client_id": client_id,
            "client_name": client_name,
        }

    # --- Observability ---

    def upsert_agent(self, agent_id: str, client_name: str, status: str, tenant: str, project_id: str,
                     hostname: Optional[str] = None, pid: Optional[str] = None, meta: Optional[Dict[str, Any]] = None,
                     client_id: Optional[str] = None, parent_client_id: Optional[str] = None) -> Dict[str, Any]:
        mid = str(uuid.uuid4())
        last_seen = datetime.datetime.now().isoformat()
        meta_json = json.dumps(meta or {})
        with self.get_cursor() as cur:
            cur.execute("""
                INSERT INTO logs_agents (id, agent_id, client_name, client_id, parent_client_id, hostname, pid, status, meta, tenant, project_id, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id, client_name, tenant, project_id)
                DO UPDATE SET
                    hostname = excluded.hostname,
                    pid = excluded.pid,
                    status = excluded.status,
                    meta = excluded.meta,
                    client_id = excluded.client_id,
                    parent_client_id = excluded.parent_client_id,
                    last_seen = excluded.last_seen
            """, (mid, agent_id, client_name, client_id, parent_client_id, hostname, pid, status, meta_json, tenant, project_id, last_seen))
        return {"agent_id": agent_id, "client_name": client_name, "status": status, "last_seen": last_seen, "client_id": client_id, "parent_client_id": parent_client_id, "tenant": tenant, "project_id": project_id, "hostname": hostname, "pid": pid, "meta": meta or {}}

    def list_agents(self, tenant: str, project_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        with self.get_cursor() as conn:
            cur = conn.execute("""
                SELECT agent_id, client_name, client_id, parent_client_id, hostname, pid, status, meta, tenant, project_id, last_seen
                FROM logs_agents
                WHERE tenant = ? AND project_id = ?
                ORDER BY last_seen DESC
                LIMIT ?
            """, (tenant, project_id, limit))
            rows = []
            for row in cur.fetchall():
                d = dict(row)
                if d.get("meta"):
                    try:
                        d["meta"] = json.loads(d["meta"])
                    except Exception:
                        d["meta"] = {}
                rows.append(d)
            return rows

    def add_access_event(self, event_type: str, status: str, tenant: str, project_id: str,
                         agent_id: Optional[str] = None, client_name: Optional[str] = None,
                         client_id: Optional[str] = None, parent_client_id: Optional[str] = None,
                         child_client_id: Optional[str] = None, target_layer: Optional[str] = None,
                         query: Optional[str] = None, memory_id: Optional[str] = None,
                         result_count: Optional[int] = None, detail: Optional[str] = None) -> Dict[str, Any]:
        mid = str(uuid.uuid4())
        ts = datetime.datetime.now().isoformat()
        with self.get_cursor() as cur:
            cur.execute("""
                INSERT INTO logs_access (id, agent_id, client_name, client_id, parent_client_id, child_client_id, event_type, target_layer, query, memory_id, result_count, status, detail, tenant, project_id, ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (mid, agent_id, client_name, client_id, parent_client_id, child_client_id, event_type, target_layer, query, memory_id, result_count, status, detail, tenant, project_id, ts))
        self.add_audit_event(
            event_type=f"access:{event_type}",
            payload={
                "agent_id": agent_id,
                "client_name": client_name,
                "client_id": client_id,
                "parent_client_id": parent_client_id,
                "child_client_id": child_client_id,
                "target_layer": target_layer,
                "query": query,
                "memory_id": memory_id,
                "result_count": result_count,
                "status": status,
                "detail": detail,
                "tenant": tenant,
                "project_id": project_id,
                "ts": ts,
            },
        )
        return {"id": mid, "event_type": event_type, "status": status, "ts": ts}

    def list_access_events(self, tenant: str, project_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        with self.get_cursor() as conn:
            cur = conn.execute("""
                SELECT agent_id, client_name, client_id, parent_client_id, child_client_id, event_type, target_layer, query, memory_id, result_count, status, detail, ts
                FROM logs_access
                WHERE tenant = ? AND project_id = ?
                ORDER BY ts DESC
                LIMIT ?
            """, (tenant, project_id, limit))
            return [dict(row) for row in cur.fetchall()]

    def add_quarantine(self, layer: str, payload: Dict[str, Any], tenant: str, project_id: str,
                       agent_id: Optional[str] = None, client_id: Optional[str] = None,
                       client_name: Optional[str] = None, validation_errors: Optional[List[str]] = None) -> Dict[str, Any]:
        mid = str(uuid.uuid4())
        created_at = datetime.datetime.now().isoformat()
        with self.get_cursor() as cur:
            cur.execute("""
                INSERT INTO memory_quarantine (id, layer, payload, status, agent_id, client_id, client_name, validation_errors, tenant, project_id, created_at)
                VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)
            """, (mid, layer, json.dumps(payload), agent_id, client_id, client_name, json.dumps(validation_errors or []), tenant, project_id, created_at))
        self.add_audit_event(
            event_type="quarantine:create",
            payload={"id": mid, "layer": layer, "tenant": tenant, "project_id": project_id},
        )
        return {"id": mid, "status": "pending", "layer": layer, "payload": payload, "tenant": tenant, "project_id": project_id, "created_at": created_at, "validation_errors": validation_errors or [], "agent_id": agent_id, "client_id": client_id, "client_name": client_name}

    def list_quarantine(self, tenant: str, project_id: str, status: str = "pending", limit: int = 100) -> List[Dict[str, Any]]:
        with self.get_cursor() as conn:
            cur = conn.execute("""
                SELECT id, layer, payload, status, tenant, project_id, created_at, reviewed_at, reviewed_by,
                       agent_id, client_id, client_name, validation_errors
                FROM memory_quarantine
                WHERE tenant = ? AND project_id = ? AND status = ?
                ORDER BY created_at DESC
                LIMIT ?
            """, (tenant, project_id, status, limit))
            rows = []
            for row in cur.fetchall():
                d = dict(row)
                if d.get("payload"):
                    try:
                        d["payload"] = json.loads(d["payload"])
                    except Exception:
                        d["payload"] = {}
                if d.get("validation_errors"):
                    try:
                        d["validation_errors"] = json.loads(d["validation_errors"])
                    except Exception:
                        d["validation_errors"] = []
                rows.append(d)
            return rows

    def resolve_quarantine(self, item_id: str, status: str, reviewer: str, tenant: str, project_id: str) -> Optional[Dict[str, Any]]:
        if status not in {"approved", "rejected"}:
            raise ValueError("Invalid review status")
        reviewed_at = datetime.datetime.now().isoformat()
        with self.get_cursor() as conn:
            conn.execute("""
                SELECT layer, payload, tenant, project_id, agent_id, client_id, client_name
                FROM memory_quarantine
                WHERE id = ? AND tenant = ? AND project_id = ? AND status = 'pending'
            """, (item_id, tenant, project_id))
            row = conn.fetchone()
            if not row:
                return None
            conn.execute("""
                UPDATE memory_quarantine
                SET status = ?, reviewed_at = ?, reviewed_by = ?
                WHERE id = ? AND tenant = ? AND project_id = ? AND status = 'pending'
            """, (status, reviewed_at, reviewer, item_id, tenant, project_id))
        self.add_audit_event(
            event_type=f"quarantine:{status}",
            payload={"id": item_id, "reviewed_by": reviewer},
        )
        d = dict(row)
        if d.get("payload"):
            try:
                d["payload"] = json.loads(d["payload"])
            except Exception:
                d["payload"] = {}
        return d

    _UNIT_TABLES = {
        "semantic": "memories_semantic", "episodic": "memories_episodic",
        "procedural": "memories_procedural", "rag": "memories_rag",
        "working": "working_memory", "session": "logs_session", "event": "logs_events",
    }

    def create_memory_unit(self, label: str, members: List[Dict[str, str]], tenant: str,
                           project_id: str) -> Dict[str, Any]:
        """Attach existing scoped records to one explicit unit, atomically.

        Only the admin API exposes this write. A member belongs to at most one
        unit; caller-supplied UUIDs or labels never establish identity alone.
        """
        if not isinstance(label, str) or not label.strip() or len(label) > 256:
            raise ValueError("Unit label must be 1-256 characters")
        if not isinstance(members, list) or not 1 <= len(members) <= 100:
            raise ValueError("Expected 1-100 members")
        validated = []
        for item in members:
            if not isinstance(item, dict) or set(item) != {"layer", "id", "role"}:
                raise ValueError("Member requires layer, id and role")
            layer, rid, role = item["layer"], item["id"], item["role"]
            if layer not in self._UNIT_TABLES or not isinstance(rid, str) or not rid:
                raise ValueError("Invalid member layer or ID")
            if not isinstance(role, str) or not role.strip() or len(role) > 64:
                raise ValueError("Member role must be 1-64 characters")
            validated.append((layer, rid, role.strip()))
        if len({(a, b) for a, b, _ in validated}) != len(validated):
            raise ValueError("Duplicate member")
        unit_id = str(uuid.uuid4())
        created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        try:
            with self.get_cursor() as cur:
                cur.execute("BEGIN IMMEDIATE")
                for layer, rid, _ in validated:
                    table = self._UNIT_TABLES[layer]  # constant allowlist, never client SQL
                    exists = cur.execute(
                        f"SELECT 1 FROM {table} WHERE id=? AND tenant=? AND project_id=?",
                        (rid, tenant, project_id),
                    ).fetchone()
                    if not exists:
                        raise ValueError("Member not found in this tenant/project")
                cur.execute("INSERT INTO memory_units VALUES (?, ?, ?, ?, ?)",
                            (unit_id, tenant, project_id, label.strip(), created_at))
                for layer, rid, role in validated:
                    cur.execute("INSERT INTO memory_unit_members VALUES (?, ?, ?, ?, ?, ?)",
                                (unit_id, layer, rid, tenant, project_id, role))
        except sqlite3.IntegrityError as exc:
            raise ValueError("Member already belongs to a unit") from exc
        self.add_audit_event("memory_unit:create", {"unit_id": unit_id, "member_count": len(validated)})
        return {"id": unit_id, "label": label.strip(), "members": members,
                "tenant": tenant, "project_id": project_id}

    def related_memory(self, layer: str, record_id: str, tenant: str,
                       project_id: str) -> Optional[Dict[str, Any]]:
        if layer not in self._UNIT_TABLES:
            raise ValueError("Unsupported layer")
        with self.get_cursor() as cur:
            # Stale memberships must never act as an alternate key for a deleted
            # record, even while other siblings still exist.
            source_table = self._UNIT_TABLES[layer]
            if not cur.execute(f"SELECT 1 FROM {source_table} WHERE id=? AND tenant=? AND project_id=?",
                               (record_id, tenant, project_id)).fetchone():
                return None
            unit = cur.execute("""
                SELECT u.id, u.label FROM memory_unit_members m JOIN memory_units u
                ON u.id=m.unit_id AND u.tenant=m.tenant AND u.project_id=m.project_id
                WHERE m.tenant=? AND m.project_id=? AND m.layer=? AND m.record_id=?
            """, (tenant, project_id, layer, record_id)).fetchone()
            if not unit:
                return None
            members = cur.execute("""
                SELECT layer, record_id, role FROM memory_unit_members
                WHERE unit_id=? AND tenant=? AND project_id=? ORDER BY layer, record_id
            """, (unit["id"], tenant, project_id)).fetchall()
            # Filter dangling records, including expired working/session rows.
            live = []
            for member in members:
                table = self._UNIT_TABLES[member["layer"]]
                row = cur.execute(f"SELECT * FROM {table} WHERE id=? AND tenant=? AND project_id=?",
                                  (member["record_id"], tenant, project_id)).fetchone()
                if row:
                    live.append({"layer": member["layer"], "role": member["role"],
                                 "record": dict(row)})
            return {"id": unit["id"], "label": unit["label"], "members": live}

    def add_audit_event(self, event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Append to the global chain atomically, ordered by SQLite insertion rowid."""
        import hashlib

        mid = str(uuid.uuid4())
        ts = datetime.datetime.now().isoformat()
        payload_json = json.dumps(payload, sort_keys=True)
        # Reserve the write lock before reading the previous hash. Without this,
        # simultaneous writers can both chain to the same predecessor.
        with self.get_cursor() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("SELECT hash FROM logs_audit ORDER BY rowid DESC LIMIT 1")
            prev = conn.fetchone()
            prev_hash = prev[0] if prev else ""
            digest = hashlib.sha256(f"{prev_hash}|{event_type}|{payload_json}|{ts}".encode("utf-8")).hexdigest()
            conn.execute("""
                INSERT INTO logs_audit (id, event_type, payload, prev_hash, hash, ts)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (mid, event_type, payload_json, prev_hash, digest, ts))
        return {"id": mid, "hash": digest, "ts": ts}

    def verify_audit_chain(self) -> Dict[str, Any]:
        """Verify the complete global chain; return the first broken row without its payload.

        This catches alterations to retained rows, not removal of a tail from a
        mutable SQLite database. Anchor the final digest externally for stronger
        tamper evidence. This method does not expose entries across tenants.
        """
        import hashlib

        previous = ""
        count = 0
        with self.get_cursor() as conn:
            conn.execute("BEGIN")  # one consistent read snapshot
            conn.execute("SELECT id, event_type, payload, prev_hash, hash, ts FROM logs_audit ORDER BY rowid")
            for row in conn.fetchall():
                count += 1
                expected = hashlib.sha256(
                    f"{previous}|{row['event_type']}|{row['payload']}|{row['ts']}".encode("utf-8")
                ).hexdigest()
                if row["prev_hash"] != previous or row["hash"] != expected:
                    return {"valid": False, "checked": count, "first_bad_id": row["id"]}
                previous = row["hash"]
        return {"valid": True, "checked": count, "head_hash": previous}

    def add_procedural(self, skill_name: str, steps: list, tenant: str, project_id: str,
                       description: Optional[str] = None, trigger: Optional[str] = None,
                       code_snippet: Optional[str] = None, embedding_id: Optional[int] = None,
                       agent_id: Optional[str] = None, client_id: Optional[str] = None,
                       client_name: Optional[str] = None) -> Dict[str, Any]:
        mid = str(uuid.uuid4())
        created_at = datetime.datetime.now().isoformat()
        with self.get_cursor() as cur:
            cur.execute("""
                INSERT INTO memories_procedural (id, skill_name, trigger, steps, description, code_snippet, embedding_id, agent_id, client_id, client_name, tenant, project_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (mid, skill_name, trigger, json.dumps(steps), description, code_snippet, embedding_id, agent_id, client_id, client_name, tenant, project_id, created_at))
        return {"id": mid, "skill_name": skill_name}

    def add_rag(self, query: str, external_source: str, content: str, tenant: str, project_id: str,
                agent_id: Optional[str] = None, client_id: Optional[str] = None,
                client_name: Optional[str] = None) -> Dict[str, Any]:
        mid = str(uuid.uuid4())
        created_at = datetime.datetime.now().isoformat()
        with self.get_cursor() as cur:
            cur.execute("""
                INSERT INTO memories_rag (id, query, external_source, content, agent_id, client_id, client_name, tenant, project_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (mid, query, external_source, content, agent_id, client_id, client_name, tenant, project_id, created_at))
        return {"id": mid, "query": query}

    def list_embedding_ids(self, tenant: str, project_id: str) -> set[int]:
        """Return only vectors owned by a tenant/project, across indexed layers."""
        with self.get_cursor() as cur:
            ids = set()
            for table in ("memories_semantic", "memories_episodic", "memories_procedural"):
                cur.execute(
                    f"SELECT embedding_id FROM {table} WHERE tenant = ? AND project_id = ? AND embedding_id IS NOT NULL",
                    (tenant, project_id),
                )
                ids.update(int(row[0]) for row in cur.fetchall())
            return ids

    def get_memories_by_embedding_ids(self, embedding_ids: List[int], tenant: str, project_id: str) -> List[Dict[str, Any]]:
        # Efficient retrieval for vector search results
        if not embedding_ids:
            return []
        
        placeholders = ','.join('?' for _ in embedding_ids)
        results = []
        
        tables = [
            ("memories_semantic", "semantic"),
            ("memories_episodic", "episodic"),
            ("memories_procedural", "procedural")
        ]
        
        with self.get_cursor() as conn:
            for table, mtype in tables:
                # Note: procedural uses skill_name as content in our mapping
                content_col = "skill_name" if mtype == "procedural" else "content"
                cur = conn.execute(f"""
                    SELECT *, '{mtype}' as type FROM {table}
                    WHERE embedding_id IN ({placeholders}) AND tenant = ? AND project_id = ?
                """, (*embedding_ids, tenant, project_id))
                for row in cur.fetchall():
                    d = dict(row)
                    if mtype == "procedural": d['content'] = d['skill_name']
                    if d.get('tags'): d['tags'] = json.loads(d['tags'])
                    if d.get('plan'): d['plan'] = json.loads(d['plan'])
                    if d.get('steps'): d['steps'] = json.loads(d['steps'])
                    results.append(d)
        return results

    def get_memory(self, layer: str, memory_id: str, tenant: str, project_id: str) -> Optional[Dict[str, Any]]:
        table_map = {
            "semantic": "memories_semantic",
            "episodic": "memories_episodic",
            "procedural": "memories_procedural",
            "rag": "memories_rag",
            "working": "working_memory",
        }
        table = table_map.get(layer)
        if not table:
            return None
        with self.get_cursor() as conn:
            cur = conn.execute(
                f"SELECT * FROM {table} WHERE id = ? AND tenant = ? AND project_id = ?",
                (memory_id, tenant, project_id),
            )
            row = cur.fetchone()
            if not row:
                return None
            d = dict(row)
            if d.get("tags"):
                try:
                    d["tags"] = json.loads(d["tags"])
                except Exception:
                    pass
            if d.get("plan"):
                try:
                    d["plan"] = json.loads(d["plan"])
                except Exception:
                    pass
            if d.get("steps"):
                try:
                    d["steps"] = json.loads(d["steps"])
                    d["tool_logs"] = d["steps"]
                except Exception:
                    pass
            return d

    def update_memory(self, layer: str, memory_id: str, updates: Dict[str, Any], tenant: str, project_id: str) -> bool:
        table_map = {
            "semantic": "memories_semantic",
            "episodic": "memories_episodic",
            "procedural": "memories_procedural",
            "rag": "memories_rag",
        }
        table = table_map.get(layer)
        if not table:
            raise ValueError("Invalid layer")
        if not updates:
            return False

        allowed = {
            "semantic": {"content", "tags"},
            "episodic": {"content", "goal", "outcome", "plan", "salience"},
            "procedural": {"skill_name", "trigger", "steps", "description", "code_snippet"},
            "rag": {"query", "external_source", "content"},
        }
        if not set(updates).issubset(allowed[layer]):
            raise ValueError("Unsupported update field")
        # Serialize JSON fields
        serialized = {}
        for key, value in updates.items():
            if key in ("tags", "plan", "steps") and value is not None:
                serialized[key] = json.dumps(value)
            else:
                serialized[key] = value

        set_clause = ", ".join([f"{k} = ?" for k in serialized.keys()])
        params = list(serialized.values()) + [memory_id, tenant, project_id]
        with self.get_cursor() as conn:
            cur = conn.execute(
                f"UPDATE {table} SET {set_clause} WHERE id = ? AND tenant = ? AND project_id = ?",
                params,
            )
            return cur.rowcount > 0

    def delete_memory(self, layer: str, memory_id: str, tenant: str, project_id: str) -> bool:
        table_map = {
            "semantic": "memories_semantic",
            "episodic": "memories_episodic",
            "procedural": "memories_procedural",
            "rag": "memories_rag",
        }
        table = table_map.get(layer)
        if not table:
            raise ValueError("Invalid layer")
        with self.get_cursor() as conn:
            cur = conn.execute(
                f"DELETE FROM {table} WHERE id = ? AND tenant = ? AND project_id = ?",
                (memory_id, tenant, project_id),
            )
            return cur.rowcount > 0

    def list_working(self, tenant: str, project_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        with self.get_cursor() as conn:
            cur = conn.execute("""
                SELECT * FROM working_memory WHERE tenant = ? AND project_id = ? ORDER BY updated_at DESC LIMIT ?
            """, (tenant, project_id, limit))
            return [dict(row) for row in cur.fetchall()]

    def add_event(self, event_type: str, payload: Dict, tenant: str, project_id: str) -> Dict[str, Any]:
        event_id = str(uuid.uuid4())
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self.get_cursor() as cur:
            cur.execute("""
                INSERT INTO logs_events (id, event_type, payload, tenant, project_id, ts)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (event_id, event_type, json.dumps(payload), tenant, project_id, ts))
        return {"id": event_id, "ts": ts}

    def list_events(self, tenant: str, project_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        with self.get_cursor() as conn:
            cur = conn.execute("""
                SELECT * FROM logs_events WHERE tenant = ? AND project_id = ? ORDER BY ts DESC LIMIT ?
            """, (tenant, project_id, limit))
            return [dict(row) for row in cur.fetchall()]

    def add_session(self, session_id: str, content: str, role: str, tenant: str, project_id: str,
                    agent_id: Optional[str] = None, client_id: Optional[str] = None,
                    client_name: Optional[str] = None, parent_client_id: Optional[str] = None,
                    child_client_id: Optional[str] = None) -> Dict[str, Any]:
        log_id = str(uuid.uuid4())
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self.get_cursor() as cur:
            cur.execute("""
                INSERT INTO logs_session (
                    id, session_id, content, role, agent_id, client_id, client_name,
                    parent_client_id, child_client_id, tenant, project_id, ts
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                log_id, session_id, content, role, agent_id, client_id, client_name,
                parent_client_id, child_client_id, tenant, project_id, ts
            ))
        return {"id": log_id, "ts": ts}

    def list_session(self, session_id: str, tenant: str, project_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        with self.get_cursor() as conn:
            cur = conn.execute("""
                SELECT * FROM logs_session WHERE session_id = ? AND tenant = ? AND project_id = ? ORDER BY ts ASC LIMIT ?
            """, (session_id, tenant, project_id, limit))
            return [dict(row) for row in cur.fetchall()]

    def observe_client(self, client_id: str, tenant: str, project_id: str,
                       client_name: Optional[str] = None, parent_client_id: Optional[str] = None,
                       metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Upsert a client record with status 'observed' if not present."""
        cid = str(uuid.uuid4())
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        meta_json = json.dumps(metadata or {})
        with self.get_cursor() as cur:
            cur.execute("""
                INSERT INTO clients_registry (
                    id, client_id, client_name, parent_client_id, status, metadata,
                    tenant, project_id, created_at, updated_at, last_seen
                ) VALUES (?, ?, ?, ?, 'observed', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_id, tenant, project_id)
                DO UPDATE SET
                    client_name = COALESCE(excluded.client_name, clients_registry.client_name),
                    parent_client_id = COALESCE(excluded.parent_client_id, clients_registry.parent_client_id),
                    metadata = CASE
                        WHEN excluded.metadata IS NOT NULL THEN excluded.metadata
                        ELSE clients_registry.metadata
                    END,
                    updated_at = excluded.updated_at,
                    last_seen = excluded.last_seen
            """, (
                cid, client_id, client_name, parent_client_id, meta_json,
                tenant, project_id, now, now, now
            ))
        return {"client_id": client_id, "status": "observed", "updated_at": now}

    def register_client(self, client_id: str, tenant: str, project_id: str,
                        client_name: Optional[str] = None, parent_client_id: Optional[str] = None,
                        status: str = "registered", metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        cid = str(uuid.uuid4())
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        meta_json = json.dumps(metadata or {})
        with self.get_cursor() as cur:
            cur.execute("""
                INSERT INTO clients_registry (
                    id, client_id, client_name, parent_client_id, status, metadata,
                    tenant, project_id, created_at, updated_at, last_seen
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_id, tenant, project_id)
                DO UPDATE SET
                    client_name = COALESCE(excluded.client_name, clients_registry.client_name),
                    parent_client_id = COALESCE(excluded.parent_client_id, clients_registry.parent_client_id),
                    status = excluded.status,
                    metadata = excluded.metadata,
                    updated_at = excluded.updated_at,
                    last_seen = excluded.last_seen
            """, (
                cid, client_id, client_name, parent_client_id, status, meta_json,
                tenant, project_id, now, now, now
            ))
        return {"client_id": client_id, "status": status, "updated_at": now}

    def get_client(self, client_id: str, tenant: str, project_id: str) -> Optional[Dict[str, Any]]:
        with self.get_cursor() as conn:
            cur = conn.execute("""
                SELECT * FROM clients_registry WHERE client_id = ? AND tenant = ? AND project_id = ?
            """, (client_id, tenant, project_id))
            row = cur.fetchone()
            if not row:
                return None
            data = dict(row)
            if data.get("metadata"):
                try:
                    data["metadata"] = json.loads(data["metadata"])
                except Exception:
                    data["metadata"] = {}
            return data

    def list_clients(self, tenant: str, project_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        with self.get_cursor() as conn:
            cur = conn.execute("""
                SELECT * FROM clients_registry WHERE tenant = ? AND project_id = ?
                ORDER BY (last_seen IS NULL), last_seen DESC, updated_at DESC
                LIMIT ?
            """, (tenant, project_id, limit))
            rows = []
            for row in cur.fetchall():
                data = dict(row)
                if data.get("metadata"):
                    try:
                        data["metadata"] = json.loads(data["metadata"])
                    except Exception:
                        data["metadata"] = {}
                rows.append(data)
            return rows

    def list_child_clients(self, parent_client_id: str, tenant: str, project_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        with self.get_cursor() as conn:
            cur = conn.execute("""
                SELECT * FROM clients_registry
                WHERE parent_client_id = ? AND tenant = ? AND project_id = ?
                ORDER BY (last_seen IS NULL), last_seen DESC, updated_at DESC
                LIMIT ?
            """, (parent_client_id, tenant, project_id, limit))
            rows = []
            for row in cur.fetchall():
                data = dict(row)
                if data.get("metadata"):
                    try:
                        data["metadata"] = json.loads(data["metadata"])
                    except Exception:
                        data["metadata"] = {}
                rows.append(data)
            return rows

    def get_client_layer_stats(self, client_id: str, tenant: str, project_id: str) -> Dict[str, Any]:
        tables = {
            "semantic": ("memories_semantic", "created_at"),
            "episodic": ("memories_episodic", "created_at"),
            "procedural": ("memories_procedural", "created_at"),
            "rag": ("memories_rag", "created_at"),
            "working": ("working_memory", "updated_at"),
        }
        counts: Dict[str, int] = {}
        last_write: Dict[str, Optional[str]] = {}
        with self.get_cursor() as conn:
            for layer, (table, ts_col) in tables.items():
                cur = conn.execute(
                    f"SELECT COUNT(*), MAX({ts_col}) FROM {table} WHERE tenant = ? AND project_id = ? AND client_id = ?",
                    (tenant, project_id, client_id),
                )
                row = cur.fetchone()
                counts[layer] = int(row[0] or 0)
                last_write[layer] = row[1]
        return {
            "counts": counts,
            "last_write": last_write,
            "total": sum(counts.values()),
        }

    def count_client_issues(self, client_id: str, tenant: str, project_id: str, status: str = "open") -> int:
        with self.get_cursor() as conn:
            cur = conn.execute("""
                SELECT COUNT(*) FROM logs_client_issues
                WHERE client_id = ? AND tenant = ? AND project_id = ? AND status = ?
            """, (client_id, tenant, project_id, status))
            row = cur.fetchone()
            return int(row[0] or 0)

    def count_quarantine(self, client_id: str, tenant: str, project_id: str, status: str = "pending") -> int:
        with self.get_cursor() as conn:
            cur = conn.execute("""
                SELECT COUNT(*) FROM memory_quarantine
                WHERE client_id = ? AND tenant = ? AND project_id = ? AND status = ?
            """, (client_id, tenant, project_id, status))
            row = cur.fetchone()
            return int(row[0] or 0)

    def add_working(self, content: str, tenant: str, project_id: str,
                    agent_id: Optional[str] = None, client_id: Optional[str] = None,
                    client_name: Optional[str] = None) -> Dict[str, Any]:
        wid = str(uuid.uuid4())
        updated_at = datetime.datetime.now().isoformat()
        # Single key for simple working memory
        key = "current_context"
        with self.get_cursor() as cur:
            cur.execute("""
                INSERT OR REPLACE INTO working_memory (id, session_id, key, value, agent_id, client_id, client_name, tenant, project_id, updated_at)
                VALUES (
                    COALESCE((SELECT id FROM working_memory WHERE session_id='global' AND key=?), ?),
                    'global', ?, ?, ?, ?, ?, ?, ?, ?
                )
            """, (key, wid, key, content, agent_id, client_id, client_name, tenant, project_id, updated_at))
        return {
            "id": wid,
            "content": content,
            "tenant": tenant,
            "project_id": project_id,
            "created_at": updated_at,
        }

    def add_client_issue(self, client_id: str, message: str, tenant: str, project_id: str,
                         error_code: str, client_name: Optional[str] = None,
                         agent_id: Optional[str] = None, parent_client_id: Optional[str] = None,
                         child_client_id: Optional[str] = None, layer: Optional[str] = None,
                         payload: Optional[Dict[str, Any]] = None, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        issue_id = str(uuid.uuid4())
        created_at = datetime.datetime.now().isoformat()
        with self.get_cursor() as cur:
            cur.execute("""
                INSERT INTO logs_client_issues (
                    id, client_id, client_name, agent_id, parent_client_id, child_client_id,
                    layer, error_code, message, payload, metadata, status, tenant, project_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)
            """, (
                issue_id,
                client_id,
                client_name,
                agent_id,
                parent_client_id,
                child_client_id,
                layer,
                error_code,
                message,
                json.dumps(payload) if payload is not None else None,
                json.dumps(metadata) if metadata is not None else None,
                tenant,
                project_id,
                created_at,
            ))
        self.add_audit_event(
            event_type="client_issue:create",
            payload={
                "id": issue_id,
                "client_id": client_id,
                "client_name": client_name,
                "agent_id": agent_id,
                "error_code": error_code,
                "tenant": tenant,
                "project_id": project_id,
                "ts": created_at,
            },
        )
        return {
            "id": issue_id,
            "client_id": client_id,
            "client_name": client_name,
            "agent_id": agent_id,
            "parent_client_id": parent_client_id,
            "child_client_id": child_client_id,
            "layer": layer,
            "error_code": error_code,
            "message": message,
            "payload": payload,
            "metadata": metadata,
            "status": "open",
            "tenant": tenant,
            "project_id": project_id,
            "created_at": created_at,
            "resolved_at": None,
            "resolved_by": None,
            "resolution": None,
        }

    def list_client_issues(self, tenant: str, project_id: str, status: str = "open", limit: int = 200) -> List[Dict[str, Any]]:
        with self.get_cursor() as conn:
            cur = conn.execute("""
                SELECT id, client_id, client_name, agent_id, parent_client_id, child_client_id,
                       layer, error_code, message, payload, metadata, status, tenant, project_id,
                       created_at, resolved_at, resolved_by, resolution
                FROM logs_client_issues
                WHERE tenant = ? AND project_id = ? AND status = ?
                ORDER BY created_at DESC
                LIMIT ?
            """, (tenant, project_id, status, limit))
            rows = []
            for row in cur.fetchall():
                d = dict(row)
                if d.get("payload"):
                    try:
                        d["payload"] = json.loads(d["payload"])
                    except Exception:
                        d["payload"] = {}
                if d.get("metadata"):
                    try:
                        d["metadata"] = json.loads(d["metadata"])
                    except Exception:
                        d["metadata"] = {}
                rows.append(d)
            return rows

    def resolve_client_issue(self, issue_id: str, resolution: str, reviewer: str, tenant: str, project_id: str) -> Optional[Dict[str, Any]]:
        resolved_at = datetime.datetime.now().isoformat()
        with self.get_cursor() as conn:
            cur = conn.execute(
                "UPDATE logs_client_issues SET status = 'resolved', resolved_at = ?, resolved_by = ?, resolution = ? WHERE id = ? AND tenant = ? AND project_id = ? AND status = 'open'",
                (resolved_at, reviewer, resolution, issue_id, tenant, project_id),
            )
            if cur.rowcount == 0:
                return None
            cur = conn.execute("""
                SELECT id, client_id, client_name, agent_id, parent_client_id, child_client_id,
                       layer, error_code, message, payload, metadata, status, tenant, project_id,
                       created_at, resolved_at, resolved_by, resolution
                FROM logs_client_issues
                WHERE id = ?
            """, (issue_id,))
            row = cur.fetchone()
            if not row:
                return {"id": issue_id, "status": "resolved", "resolved_at": resolved_at, "resolved_by": reviewer}
            data = dict(row)
            if data.get("payload"):
                try:
                    data["payload"] = json.loads(data["payload"])
                except Exception:
                    data["payload"] = {}
            if data.get("metadata"):
                try:
                    data["metadata"] = json.loads(data["metadata"])
                except Exception:
                    data["metadata"] = {}
            resolved_payload = data
        self.add_audit_event(
            event_type="client_issue:resolved",
            payload={"id": issue_id, "reviewer": reviewer, "resolution": resolution, "ts": resolved_at},
        )
        return resolved_payload

    def get_working(self, tenant: str, project_id: str) -> Dict[str, Any]:
        with self.get_cursor() as conn:
            cur = conn.execute("""
                SELECT value, updated_at FROM working_memory 
                WHERE session_id='global' AND key='current_context' AND tenant = ? AND project_id = ?
            """, (tenant, project_id))
            row = cur.fetchone()
            if row:
                return {"content": row['value'], "updated_at": row['updated_at']}
            return None
