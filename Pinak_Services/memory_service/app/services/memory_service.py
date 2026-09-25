import json
import sqlite3
import os
import hashlib
import datetime
import logging
import time
import concurrent.futures
from typing import Dict, List, Optional, Any, Union

import numpy as np

from app.core.schemas import MemoryCreate, MemoryRead, MemorySearchResult, ClientIssueCreate
from app.core.database import DatabaseManager
from app.core.schema_registry import SchemaRegistry
from app.services.vector_store import VectorStore

logger = logging.getLogger(__name__)

class _DeterministicEncoder:
    """Lightweight embedding encoder used for tests and local development."""

    def __init__(self, dimension: int = 384):
        self.embedding_dimension = dimension

    def encode(self, sentences: List[str]) -> np.ndarray:
        vectors = []
        for sentence in sentences:
            seed = int(hashlib.sha256(sentence.encode("utf-8")).hexdigest(), 16) % (2**32)
            rng = np.random.default_rng(seed)
            vector = rng.random(self.embedding_dimension, dtype=np.float32)
            vectors.append(vector)
        return np.array(vectors, dtype=np.float32)

class MemoryService:
    """
    Enterprise Memory Service Orchestrator.
    Manages SQLite (Metadata/Logs) and FAISS (Vectors).
    Implements Hybrid Search with Reciprocal Rank Fusion (RRF).
    """

    def __init__(self, config_path: Optional[str] = None, model: Optional[object] = None):
        config_path = config_path or os.getenv("PINAK_CONFIG_PATH", "app/core/config.json")
        self.config = self._load_config(config_path)

        # Paths
        self.data_root = self.config.get("data_root", "data")
        os.makedirs(self.data_root, exist_ok=True)
        self.db_path = os.path.join(self.data_root, "memory.db")
        self.vector_path = os.path.join(self.data_root, "vectors.index.npy")

        # Components
        self.db = DatabaseManager(self.db_path)
        self.schema_registry = SchemaRegistry()

        # Model
        self.embedding_backend = (os.getenv("PINAK_EMBEDDING_BACKEND") or "").lower()
        self.vector_enabled = self.embedding_backend not in ("qmd", "none", "off", "disabled")
        if self.vector_enabled:
            self.model = model or self._load_embedding_model(self.config.get("embedding_model"))
        else:
            self.model = model or _DeterministicEncoder()
        if hasattr(self.model, "get_sentence_embedding_dimension"):
            self.embedding_dim = self.model.get_sentence_embedding_dimension()
        else:
            self.embedding_dim = getattr(self.model, "embedding_dimension", 384)

        # Vector Store
        self.vector_store = VectorStore(self.vector_path, self.embedding_dim) if self.vector_enabled else None

    def _normalize_client_ids(
        self,
        client_id: Optional[str],
        client_name: Optional[str],
        agent_id: Optional[str],
        tenant: str,
        project_id: str,
        parent_client_id: Optional[str] = None,
        child_client_id: Optional[str] = None,
    ) -> Dict[str, Optional[str]]:
        effective_client_id = child_client_id or client_id or agent_id or "unknown"
        # Observe/track clients for registry visibility
        try:
            observe_target = client_id or effective_client_id
            if observe_target:
                self.db.observe_client(
                    client_id=observe_target,
                    client_name=client_name,
                    parent_client_id=parent_client_id,
                    tenant=tenant,
                    project_id=project_id,
                    metadata={"agent_id": agent_id},
                )
            if child_client_id:
                existing_child = self.db.get_client(child_client_id, tenant, project_id)
                self.db.observe_client(
                    client_id=child_client_id,
                    client_name=client_name,
                    parent_client_id=parent_client_id or client_id,
                    tenant=tenant,
                    project_id=project_id,
                    metadata={"agent_id": agent_id, "observed_via": "child_header"},
                )
                if not existing_child or existing_child.get("status") == "observed":
                    try:
                        self.db.add_client_issue(
                            client_id=child_client_id,
                            client_name=client_name,
                            agent_id=agent_id,
                            parent_client_id=parent_client_id or client_id,
                            child_client_id=child_client_id,
                            layer=None,
                            error_code="child_client_unregistered",
                            message="Child client_id observed but not registered; please register child client.",
                            payload={"child_client_id": child_client_id},
                            tenant=tenant,
                            project_id=project_id,
                        )
                    except Exception:
                        pass
        except Exception:
            pass
        if not client_id:
            try:
                self.db.add_client_issue(
                    client_id=effective_client_id,
                    client_name=client_name,
                    agent_id=agent_id,
                    parent_client_id=parent_client_id,
                    child_client_id=child_client_id,
                    layer=None,
                    error_code="missing_client_id",
                    message="Client ID missing; generated effective_client_id. Please register client_id.",
                    payload={"client_name": client_name, "agent_id": agent_id},
                    tenant=tenant,
                    project_id=project_id,
                )
            except Exception:
                pass
        return {
            "client_id": effective_client_id,
            "parent_client_id": parent_client_id,
            "child_client_id": child_client_id,
            "client_name": client_name,
        }

    def _trusted_clients_env(self) -> List[str]:
        raw = os.getenv("PINAK_TRUSTED_CLIENTS", "")
        return [c.strip() for c in raw.split(",") if c.strip()]

    def _is_trusted_client(self, client_id: Optional[str], tenant: str, project_id: str) -> bool:
        if not client_id:
            return False
        if client_id in self._trusted_clients_env():
            return True
        try:
            entry = self.db.get_client(client_id, tenant, project_id)
            return bool(entry and entry.get("status") == "trusted")
        except Exception:
            return False

    def _log_schema_errors(
        self,
        layer: str,
        errors: List[str],
        payload: Dict[str, Any],
        tenant: str,
        project_id: str,
        agent_id: Optional[str],
        client_name: Optional[str],
        client_id: Optional[str],
        parent_client_id: Optional[str],
        child_client_id: Optional[str],
    ) -> None:
        if not errors:
            return
        try:
            self.db.add_client_issue(
                client_id=client_id or agent_id or "unknown",
                client_name=client_name,
                agent_id=agent_id,
                parent_client_id=parent_client_id,
                child_client_id=child_client_id,
                layer=layer,
                error_code="schema_validation_failed",
                message="Schema validation failed",
                payload={**payload, "errors": errors},
                tenant=tenant,
                project_id=project_id,
            )
        except Exception:
            pass

    def verify_and_recover(self):
        """
        Check consistency between DB and Vector Store. Rebuild if necessary.
        """
        if not self.vector_enabled:
            logger.info("Vector store disabled (backend=%s); skipping verify/recover", self.embedding_backend)
            return
        # Compare the full ID multiset, not just row counts. Equal-sized but
        # mismatched snapshots silently return the wrong memory on retrieval.
        db_ids = []
        with self.db.get_cursor() as conn:
            for table in ("memories_semantic", "memories_episodic", "memories_procedural"):
                conn.execute(f"SELECT embedding_id FROM {table} WHERE embedding_id IS NOT NULL")
                db_ids.extend(int(row[0]) for row in conn.fetchall())
        with self.vector_store.lock:
            vector_ids = [int(value) for value in self.vector_store.ids]
        if sorted(db_ids) != sorted(vector_ids):
            logger.warning("Vector ID mismatch: DB=%s, vector=%s; rebuilding index", len(db_ids), len(vector_ids))
            self._rebuild_index()
        else:
            logger.info("System Consistent. %s memories loaded.", len(vector_ids))

    def _rebuild_index(self):
        """Re-encode all semantic/episodic/procedural memories and rebuild vector store."""
        if not self.vector_enabled:
            logger.info("Vector store disabled (backend=%s); rebuild skipped", self.embedding_backend)
            return
        with self.vector_store.batch_add():
            with self.vector_store.lock:
                self.vector_store.vectors = np.empty((0, self.embedding_dim), dtype=np.float32)
                self.vector_store.ids = np.array([], dtype=np.int64)

            def _page_rows(query: str, params: tuple):
                with self.db.get_cursor() as conn:
                    conn.execute(query, params)
                    return conn.fetchall()

            def _ingest_rows(rows, build_text):
                texts = []
                ids = []
                for row in rows:
                    embedding_id = row["embedding_id"]
                    if embedding_id is None:
                        continue
                    texts.append(build_text(row))
                    ids.append(embedding_id)
                if texts:
                    embeddings = self.model.encode(texts)
                    self.vector_store.add_vectors(embeddings, ids)

            # Semantic
            offset = 0
            limit = 100
            while True:
                rows = _page_rows(
                    "SELECT content, embedding_id FROM memories_semantic LIMIT ? OFFSET ?",
                    (limit, offset),
                )
                if not rows:
                    break
                _ingest_rows(rows, lambda r: r["content"])
                offset += len(rows)
                logger.info("Rebuilt %s semantic vectors...", offset)

            # Episodic
            offset = 0
            while True:
                rows = _page_rows(
                    "SELECT content, goal, outcome, embedding_id FROM memories_episodic LIMIT ? OFFSET ?",
                    (limit, offset),
                )
                if not rows:
                    break
                _ingest_rows(rows, lambda r: f"{r['content']} {r['goal'] or ''} {r['outcome'] or ''}")
                offset += len(rows)
                logger.info("Rebuilt %s episodic vectors...", offset)

            # Procedural
            offset = 0
            while True:
                rows = _page_rows(
                    "SELECT skill_name, trigger, description, embedding_id FROM memories_procedural LIMIT ? OFFSET ?",
                    (limit, offset),
                )
                if not rows:
                    break
                _ingest_rows(rows, lambda r: f"{r['skill_name']} {r['trigger'] or ''} {r['description'] or ''}")
                offset += len(rows)
                logger.info("Rebuilt %s procedural vectors...", offset)

    def _load_config(self, path):
        if not os.path.exists(path):
            return {}
        with open(path, 'r') as f:
            return json.load(f)

    def _load_embedding_model(self, name: Optional[str]):
        backend = os.getenv("PINAK_EMBEDDING_BACKEND")
        if backend == "dummy" or not name or name.lower() == "dummy":
            return _DeterministicEncoder()
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                "Real embeddings require the optional embeddings extra: "
                "uv sync --extra embeddings, or set PINAK_EMBEDDING_BACKEND=none for keyword-only search."
            ) from exc
        # Do not silently substitute fake vectors for a configured real model.
        # A missing model or failed download must stop startup rather than corrupt retrieval.
        return SentenceTransformer(name)

    # --- Core Memory Operations ---

    def add_memory(self, memory_data: MemoryCreate, tenant: str, project_id: str,
                   agent_id: Optional[str] = None, client_name: Optional[str] = None,
                   client_id: Optional[str] = None, parent_client_id: Optional[str] = None,
                   child_client_id: Optional[str] = None) -> MemoryRead:
        """Adds a semantic memory (Vector + DB)."""
        try:
            client_meta = self._normalize_client_ids(
                client_id=client_id,
                client_name=client_name,
                agent_id=agent_id,
                tenant=tenant,
                project_id=project_id,
                parent_client_id=parent_client_id,
                child_client_id=child_client_id,
            )
            content = memory_data.content
            tags = memory_data.tags or []
            schema_errors = self.schema_registry.validate_payload(
                "semantic",
                {"content": content, "tags": tags},
            )
            self._log_schema_errors(
                "semantic",
                schema_errors,
                {"content": content, "tags": tags},
                tenant,
                project_id,
                agent_id,
                client_meta["client_name"],
                client_meta["client_id"],
                client_meta["parent_client_id"],
                client_meta["child_client_id"],
            )

            embedding_id = None
            if self.vector_enabled:
                # 1. Generate Embedding
                embedding = self.model.encode([content])[0].astype("float32")

                # 2. Generate ID for Vector Store
                # We use a hash or a simpler counter to avoid huge int issues
                embedding_id = hash(content + str(time.time())) % (2**31 - 1)

                # 3. Add to Vector Store
                self.vector_store.add_vectors(np.array([embedding]), [embedding_id])

            # 4. Add to DB
            result = self.db.add_semantic(
                content,
                tags,
                tenant,
                project_id,
                embedding_id,
                agent_id=agent_id,
                client_id=client_meta["client_id"],
                client_name=client_meta["client_name"],
            )

            # 5. Save Vector Store (persist)
            if self.vector_enabled:
                self.vector_store.save()

            self.db.add_access_event(
                event_type="write",
                status="ok",
                tenant=tenant,
                project_id=project_id,
                agent_id=agent_id,
                client_name=client_meta["client_name"],
                client_id=client_meta["client_id"],
                parent_client_id=client_meta["parent_client_id"],
                child_client_id=client_meta["child_client_id"],
                target_layer="semantic",
                memory_id=result.get("id"),
                detail="semantic_add",
            )
            return MemoryRead(**result)
        except Exception as e:
            self.db.add_access_event(
                event_type="write",
                status="error",
                tenant=tenant,
                project_id=project_id,
                agent_id=agent_id,
                client_name=client_name,
                client_id=client_id,
                parent_client_id=parent_client_id,
                child_client_id=child_client_id,
                target_layer="semantic",
                detail=str(e),
            )
            try:
                self.db.add_client_issue(
                    client_id=client_id or agent_id or "unknown",
                    client_name=client_name,
                    agent_id=agent_id,
                    parent_client_id=parent_client_id,
                    child_client_id=child_client_id,
                    layer="semantic",
                    error_code="semantic_add_failed",
                    message=str(e),
                    payload={"content": memory_data.content},
                    tenant=tenant,
                    project_id=project_id,
                )
            except Exception:
                pass
            raise e

    def add_episodic(self, content: str, tenant: str, project_id: str,
                     salience: int = 0, goal: str = None, plan: List[str] = None,
                     outcome: str = None, tool_logs: List[Dict] = None,
                     agent_id: Optional[str] = None, client_name: Optional[str] = None,
                     client_id: Optional[str] = None, parent_client_id: Optional[str] = None,
                     child_client_id: Optional[str] = None) -> Dict[str, Any]:
        """Add episodic memory (Vector + DB)."""
        try:
            client_meta = self._normalize_client_ids(
                client_id=client_id,
                client_name=client_name,
                agent_id=agent_id,
                tenant=tenant,
                project_id=project_id,
                parent_client_id=parent_client_id,
                child_client_id=child_client_id,
            )
            schema_errors = self.schema_registry.validate_payload(
                "episodic",
                {
                    "content": content,
                    "salience": salience,
                    "goal": goal,
                    "plan": plan,
                    "outcome": outcome,
                    "tool_logs": tool_logs,
                },
            )
            self._log_schema_errors(
                "episodic",
                schema_errors,
                {
                    "content": content,
                    "salience": salience,
                    "goal": goal,
                    "plan": plan,
                    "outcome": outcome,
                    "tool_logs": tool_logs,
                },
                tenant,
                project_id,
                agent_id,
                client_meta["client_name"],
                client_meta["client_id"],
                client_meta["parent_client_id"],
                client_meta["child_client_id"],
            )
            embedding_id = None
            if self.vector_enabled:
                # 1. Generate Embedding from content + goal + outcome
                search_blob = f"{content} {goal or ''} {outcome or ''}"
                embedding = self.model.encode([search_blob])[0].astype("float32")
                embedding_id = hash(search_blob + str(time.time())) % (2**31 - 1)

                # 2. Add to Vector Store
                self.vector_store.add_vectors(np.array([embedding]), [embedding_id])

            # 3. Add to DB
            result = self.db.add_episodic(
                content,
                tenant,
                project_id,
                salience,
                goal,
                plan,
                tool_logs,
                outcome,
                embedding_id,
                agent_id=agent_id,
                client_id=client_meta["client_id"],
                client_name=client_meta["client_name"],
            )
            self.db.add_access_event(
                event_type="write",
                status="ok",
                tenant=tenant,
                project_id=project_id,
                agent_id=agent_id,
                client_name=client_meta["client_name"],
                client_id=client_meta["client_id"],
                parent_client_id=client_meta["parent_client_id"],
                child_client_id=client_meta["child_client_id"],
                target_layer="episodic",
                memory_id=result.get("id"),
                detail="episodic_add",
            )
            return result
        except Exception as exc:
            self.db.add_access_event(
                event_type="write",
                status="error",
                tenant=tenant,
                project_id=project_id,
                agent_id=agent_id,
                client_name=client_name,
                client_id=client_id,
                parent_client_id=parent_client_id,
                child_client_id=child_client_id,
                target_layer="episodic",
                detail=str(exc),
            )
            try:
                self.db.add_client_issue(
                    client_id=client_id or agent_id or "unknown",
                    client_name=client_name,
                    agent_id=agent_id,
                    parent_client_id=parent_client_id,
                    child_client_id=child_client_id,
                    layer="episodic",
                    error_code="episodic_add_failed",
                    message=str(exc),
                    payload={"content": content, "goal": goal, "outcome": outcome},
                    tenant=tenant,
                    project_id=project_id,
                )
            except Exception:
                pass
            raise

    def add_procedural(self, skill_name: str, steps: List[str], tenant: str, project_id: str,
                       description: str = None, trigger: str = None, code_snippet: str = None,
                       agent_id: Optional[str] = None, client_name: Optional[str] = None,
                       client_id: Optional[str] = None, parent_client_id: Optional[str] = None,
                       child_client_id: Optional[str] = None) -> Dict[str, Any]:
        """Add procedural memory (Vector + DB)."""
        try:
            client_meta = self._normalize_client_ids(
                client_id=client_id,
                client_name=client_name,
                agent_id=agent_id,
                tenant=tenant,
                project_id=project_id,
                parent_client_id=parent_client_id,
                child_client_id=child_client_id,
            )
            schema_errors = self.schema_registry.validate_payload(
                "procedural",
                {
                    "skill_name": skill_name,
                    "steps": steps,
                    "description": description,
                    "trigger": trigger,
                    "code_snippet": code_snippet,
                },
            )
            self._log_schema_errors(
                "procedural",
                schema_errors,
                {
                    "skill_name": skill_name,
                    "steps": steps,
                    "description": description,
                    "trigger": trigger,
                    "code_snippet": code_snippet,
                },
                tenant,
                project_id,
                agent_id,
                client_meta["client_name"],
                client_meta["client_id"],
                client_meta["parent_client_id"],
                client_meta["child_client_id"],
            )
            embedding_id = None
            if self.vector_enabled:
                # 1. Generate Embedding from skill_name + trigger + description
                search_blob = f"{skill_name} {trigger or ''} {description or ''}"
                embedding = self.model.encode([search_blob])[0].astype("float32")
                embedding_id = hash(search_blob + str(time.time())) % (2**31 - 1)

                # 2. Add to Vector Store
                self.vector_store.add_vectors(np.array([embedding]), [embedding_id])

            # 3. Add to DB
            result = self.db.add_procedural(
                skill_name,
                steps,
                tenant,
                project_id,
                description,
                trigger,
                code_snippet,
                embedding_id,
                agent_id=agent_id,
                client_id=client_meta["client_id"],
                client_name=client_meta["client_name"],
            )
            self.db.add_access_event(
                event_type="write",
                status="ok",
                tenant=tenant,
                project_id=project_id,
                agent_id=agent_id,
                client_name=client_meta["client_name"],
                client_id=client_meta["client_id"],
                parent_client_id=client_meta["parent_client_id"],
                child_client_id=client_meta["child_client_id"],
                target_layer="procedural",
                memory_id=result.get("id"),
                detail="procedural_add",
            )
            return result
        except Exception as exc:
            self.db.add_access_event(
                event_type="write",
                status="error",
                tenant=tenant,
                project_id=project_id,
                agent_id=agent_id,
                client_name=client_name,
                client_id=client_id,
                parent_client_id=parent_client_id,
                child_client_id=child_client_id,
                target_layer="procedural",
                detail=str(exc),
            )
            try:
                self.db.add_client_issue(
                    client_id=client_id or agent_id or "unknown",
                    client_name=client_name,
                    agent_id=agent_id,
                    parent_client_id=parent_client_id,
                    child_client_id=child_client_id,
                    layer="procedural",
                    error_code="procedural_add_failed",
                    message=str(exc),
                    payload={"skill_name": skill_name},
                    tenant=tenant,
                    project_id=project_id,
                )
            except Exception:
                pass
            raise

    def add_rag(self, query: str, external_source: str, content: str, tenant: str, project_id: str,
                agent_id: Optional[str] = None, client_name: Optional[str] = None,
                client_id: Optional[str] = None, parent_client_id: Optional[str] = None,
                child_client_id: Optional[str] = None) -> Dict[str, Any]:
        try:
            client_meta = self._normalize_client_ids(
                client_id=client_id,
                client_name=client_name,
                agent_id=agent_id,
                tenant=tenant,
                project_id=project_id,
                parent_client_id=parent_client_id,
                child_client_id=child_client_id,
            )
            schema_errors = self.schema_registry.validate_payload(
                "rag",
                {
                    "query": query,
                    "external_source": external_source,
                    "content": content,
                },
            )
            self._log_schema_errors(
                "rag",
                schema_errors,
                {
                    "query": query,
                    "external_source": external_source,
                    "content": content,
                },
                tenant,
                project_id,
                agent_id,
                client_meta["client_name"],
                client_meta["client_id"],
                client_meta["parent_client_id"],
                client_meta["child_client_id"],
            )
            result = self.db.add_rag(
                query,
                external_source,
                content,
                tenant,
                project_id,
                agent_id=agent_id,
                client_id=client_meta["client_id"],
                client_name=client_meta["client_name"],
            )
            self.db.add_access_event(
                event_type="write",
                status="ok",
                tenant=tenant,
                project_id=project_id,
                agent_id=agent_id,
                client_name=client_meta["client_name"],
                client_id=client_meta["client_id"],
                parent_client_id=client_meta["parent_client_id"],
                child_client_id=client_meta["child_client_id"],
                target_layer="rag",
                memory_id=result.get("id"),
                detail="rag_add",
            )
            return result
        except Exception as exc:
            self.db.add_access_event(
                event_type="write",
                status="error",
                tenant=tenant,
                project_id=project_id,
                agent_id=agent_id,
                client_name=client_name,
                client_id=client_id,
                parent_client_id=parent_client_id,
                child_client_id=child_client_id,
                target_layer="rag",
                detail=str(exc),
            )
            try:
                self.db.add_client_issue(
                    client_id=client_id or agent_id or "unknown",
                    client_name=client_name,
                    agent_id=agent_id,
                    parent_client_id=parent_client_id,
                    child_client_id=child_client_id,
                    layer="rag",
                    error_code="rag_add_failed",
                    message=str(exc),
                    payload={"query": query},
                    tenant=tenant,
                    project_id=project_id,
                )
            except Exception:
                pass
            raise

    # --- Hybrid Search (The "Magic") ---

    def search_memory(self, query: str, tenant: str, project_id: str, k: int = 5,
                      agent_id: Optional[str] = None, client_name: Optional[str] = None,
                      client_id: Optional[str] = None, parent_client_id: Optional[str] = None,
                      child_client_id: Optional[str] = None) -> List[MemorySearchResult]:
        """
        Legacy Semantic Search Endpoint.
        We upgrade this to use the Hybrid logic but filter for 'semantic' type to maintain backward compatibility if needed.
        Or better: Use Hybrid and return top results.
        """
        # Use Hybrid Search restricted to Semantic layer if we want strict backward compat,
        # but let's just use the full hybrid power.
        results = self.search_hybrid(query, tenant, project_id, limit=k)
        # Convert to MemorySearchResult
        out = []
        for r in results:
            # We map 'score' to 'distance' (inverted) or just use score.
            out.append(MemorySearchResult(
                id=r['id'],
                content=r['content'],
                tags=[r['type']], # Use type as tag
                distance=1.0 - r['score'], # Fake distance from score (0..1)
                created_at=r.get('created_at', ""),
                tenant=r.get('tenant', tenant),
                project_id=r.get('project_id', project_id),
                metadata=r
            ))
        self.db.add_access_event(
            event_type="read",
            status="ok",
            tenant=tenant,
            project_id=project_id,
            agent_id=agent_id,
            client_name=client_name,
            client_id=client_id,
            parent_client_id=parent_client_id,
            child_client_id=child_client_id,
            target_layer="semantic",
            query=query,
            result_count=len(out),
            detail="search_memory",
        )
        return out

    def search_hybrid(self, query: str, tenant: str, project_id: str, limit: int = 10, semantic_weight: float = 0.5) -> List[Dict[str, Any]]:
        """
        Performs Hybrid Search (Vector + Keyword) with Weighted Fusion.
        semantic_weight: 0.0 = pure keyword, 1.0 = pure semantic.
        """
        # 1. Keyword Search (SQLite FTS)
        keyword_results = self.db.search_keyword(query, tenant, project_id, limit=limit * 2)

        # 2. Vector Search (Semantic)
        distances, ids = self._safe_vector_search(query, limit, tenant, project_id)
        
        
        # Note: VectorStore search now returns flat lists (unlike nested FAISS)
        # Flatten the arrays to handle both 1D and 2D outputs robustly
        flat_ids = ids.flatten() if isinstance(ids, np.ndarray) else np.array(ids).flatten()
        flat_dists = distances.flatten() if isinstance(distances, np.ndarray) else np.array(distances).flatten()

        valid_ids = [int(i) for i in flat_ids if i != -1]
        valid_distances = [float(d) for d in flat_dists[:len(valid_ids)]]

        # Unified result retrieval (Semantic, Episodic, Procedural)
        vector_results_db = self.db.get_memories_by_embedding_ids(valid_ids, tenant, project_id)
        vector_map = {item['embedding_id']: item for item in vector_results_db}

        # 4. Score Normalization & Weighted Fusion
        fts_scores = {}
        for i, item in enumerate(keyword_results):
            # Rank based score: 1.0 for first, decaying
            score = 1.0 - (i / len(keyword_results))
            fts_scores[item['id']] = score

        vector_scores = {}
        for i, idx_raw in enumerate(valid_ids):
            idx = int(idx_raw)
            if idx == -1: continue
            
            if idx in vector_map:
                item = vector_map[idx]
                mid = item['id']
                dist = valid_distances[i]
                
                # Heuristic: 1 / (1 + dist)
                # Handle inf or huge dists (as 0.1 score baseline)
                if np.isinf(dist) or dist > 1e12:
                    score = 0.1
                else:
                    score = 1.0 / (1.0 + dist)
                
                vector_scores[mid] = score

        # 4. Weighted Fusion
        merged: Dict[str, Dict] = {}
        all_ids = set(fts_scores.keys()) | set(vector_scores.keys())
        final_scores = {}

        for mid in all_ids:
            # Get item data from either source
            item = None
            if mid in fts_scores:
                # Find in keyword_results
                item = next((x for x in keyword_results if x['id'] == mid), None)

            if not item and mid in vector_scores:
                # Find in vector results
                item = next((x for x in vector_results_db if x['id'] == mid), None)

            if item:
                if mid not in merged:
                    merged[mid] = item

                s_vec = vector_scores.get(mid, 0.0)
                s_fts = fts_scores.get(mid, 0.0)

                score = (semantic_weight * s_vec) + ((1.0 - semantic_weight) * s_fts)
                final_scores[mid] = score

        # Sort by Score DESC
        sorted_ids = sorted(final_scores.keys(), key=lambda x: final_scores[x], reverse=True)

        final_results = []
        for mid in sorted_ids[:limit]:
            item = merged[mid]
            item['score'] = final_scores[mid]
            final_results.append(item)

        return final_results

    def _safe_vector_search(self, query: str, limit: int, tenant: str, project_id: str) -> tuple:
        if not self.vector_enabled or not self.vector_store:
            return [], []
        if os.getenv("PINAK_VECTOR_SEARCH_DISABLED", "false").lower() in ("1", "true", "yes"):
            return [], []
        timeout_ms = int(os.getenv("PINAK_EMBEDDING_TIMEOUT_MS", "0") or "0")

        def _compute():
            allowed_ids = self.db.list_embedding_ids(tenant, project_id)
            if not allowed_ids:
                return [], []
            embedding = self.model.encode([query])[0].astype("float32")
            return self.vector_store.search(np.array([embedding]), k=limit * 2, allowed_ids=allowed_ids)

        if timeout_ms <= 0:
            try:
                return _compute()
            except Exception as exc:
                logger.warning("Vector search failed: %s", exc)
                return [], []

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_compute)
            try:
                return future.result(timeout=timeout_ms / 1000.0)
            except concurrent.futures.TimeoutError:
                logger.warning("Vector search timed out after %sms; falling back to keyword search", timeout_ms)
                return [], []
            except Exception as exc:
                logger.warning("Vector search failed: %s", exc)
                return [], []

    def intent_sniff(self, content: str, tenant: str, project_id: str) -> List[Dict[str, Any]]:
        """
        Background Observer Logic: Detects if the current 'Working' intent
        collides with known risks in ANY memory layer.
        """
        # 1. Intent Extraction (Keyword-based for speed)
        content_lower = content.lower()
        
        # Define intent clusters
        intents = {
            "deployment": ["deploy", "vercel", "production", "ship", "push"],
            "security": ["auth", "token", "secret", "key", "access", "password"],
            "infrastructure": ["database", "db", "server", "cluster", "aws", "gcp"]
        }
        
        found_intents = [name for name, keywords in intents.items() if any(k in content_lower for k in keywords)]
        
        if not found_intents:
            return []

        # 2. Targeted Risk Search
        # We construct a query that combines the found intent keywords with high-risk terms
        risk_terms = "failed error expired warning deprecated issue problem"
        search_query = f"{' '.join(found_intents)} {risk_terms}"
        
        # Use a high semantic weight to catch conceptual collisions
        results = self.search_hybrid(search_query, tenant, project_id, limit=5, semantic_weight=0.6)

        nudges = []
        seen_messages = set()
        
        for r in results:
            # Combine all textual fields for a thorough risk check
            text_to_check = " ".join([
                str(r.get('content', '')),
                str(r.get('description', '')),
                str(r.get('goal', '')),
                str(r.get('outcome', ''))
            ]).lower()
            
            # If historical context contains negative signals related to the intents
            if any(term in text_to_check for term in ["expired", "failed", "warning", "error", "deprecated"]):
                msg = f"Collision with {r.get('type', 'memory')}: {r['content'][:100]}..."
                if msg not in seen_messages:
                    nudges.append({
                        "type": "proactive_nudge",
                        "strength": "HIGH" if any(t in text_to_check for t in ["expired", "error"]) else "MEDIUM",
                        "message": msg,
                        "source_id": r['id'],
                        "layer": r.get('type', 'procedural') if r.get('type') == 'procedural' else r.get('type', 'semantic')
                    })
                    seen_messages.add(msg)

        return nudges

    def working_add(self, content: str, tenant: str, project_id: str,
                    agent_id: Optional[str] = None, client_name: Optional[str] = None,
                    client_id: Optional[str] = None, parent_client_id: Optional[str] = None,
                    child_client_id: Optional[str] = None):
        client_meta = self._normalize_client_ids(
            client_id=client_id,
            client_name=client_name,
            agent_id=agent_id,
            tenant=tenant,
            project_id=project_id,
            parent_client_id=parent_client_id,
            child_client_id=child_client_id,
        )
        schema_errors = self.schema_registry.validate_payload(
            "working",
            {"content": content},
        )
        self._log_schema_errors(
            "working",
            schema_errors,
            {"content": content},
            tenant,
            project_id,
            agent_id,
            client_meta["client_name"],
            client_meta["client_id"],
            client_meta["parent_client_id"],
            client_meta["child_client_id"],
        )
        # Add to DB
        res = self.db.add_working(
            content,
            tenant,
            project_id,
            agent_id=agent_id,
            client_id=client_meta["client_id"],
            client_name=client_meta["client_name"],
        )
        # Sniff for nudges
        nudges = self.intent_sniff(content, tenant, project_id)
        if nudges:
            res['nudges'] = nudges
        return res

    def retrieve_context(self, query: str, tenant: str, project_id: str, semantic_weight: float = 0.5,
                         agent_id: Optional[str] = None, client_name: Optional[str] = None,
                         client_id: Optional[str] = None, parent_client_id: Optional[str] = None,
                         child_client_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Unified Context Retrieval for Agents.
        Returns categorized memory relevant to the query.
        """
        client_meta = self._normalize_client_ids(
            client_id=client_id,
            client_name=client_name,
            agent_id=agent_id,
            tenant=tenant,
            project_id=project_id,
            parent_client_id=parent_client_id,
            child_client_id=child_client_id,
        )
        hybrid_hits = self.search_hybrid(query, tenant, project_id, limit=20, semantic_weight=semantic_weight)

        # Categorize
        context = {
            "semantic": [],
            "episodic": [],
            "procedural": [],
            "working": []
        }

        for hit in hybrid_hits:
            mtype = hit.get('type', 'unknown')
            if mtype in context:
                context[mtype].append(hit)
            else:
                # Fallback
                context["semantic"].append(hit)

        total_hits = sum(len(v) for v in context.values())
        self.db.add_access_event(
            event_type="read",
            status="ok",
            tenant=tenant,
            project_id=project_id,
            agent_id=agent_id,
            client_name=client_meta["client_name"],
            client_id=client_meta["client_id"],
            parent_client_id=client_meta["parent_client_id"],
            child_client_id=client_meta["child_client_id"],
            target_layer="hybrid",
            query=query,
            result_count=total_hits,
            detail="retrieve_context",
        )
        return context

    # --- Other Operations (Logs) ---
    def add_event(self, payload: Dict, tenant: str, project_id: str):
        return self.db.add_event(payload.get('event_type', 'unknown'), payload, tenant, project_id)

    def list_events(self, tenant: str, project_id: str, limit: int=100):
        return self.db.list_events(tenant, project_id, limit)

    def session_add(self, session_id: str, content: str, role: str, tenant: str, project_id: str,
                    agent_id: Optional[str] = None, client_name: Optional[str] = None,
                    client_id: Optional[str] = None, parent_client_id: Optional[str] = None,
                    child_client_id: Optional[str] = None):
        client_meta = self._normalize_client_ids(
            client_id=client_id,
            client_name=client_name,
            agent_id=agent_id,
            tenant=tenant,
            project_id=project_id,
            parent_client_id=parent_client_id,
            child_client_id=child_client_id,
        )
        return self.db.add_session(
            session_id,
            content,
            role,
            tenant,
            project_id,
            agent_id=agent_id,
            client_id=client_meta["client_id"],
            client_name=client_meta["client_name"],
            parent_client_id=client_meta["parent_client_id"],
            child_client_id=client_meta["child_client_id"],
        )

    def session_list(self, session_id: str, tenant: str, project_id: str, limit: int=100):
        return self.db.list_session(session_id, tenant, project_id, limit)

    def working_list(self, tenant: str, project_id: str, limit: int=100):
        return self.db.list_working(tenant, project_id, limit)

    # --- Observability ---
    def register_agent(self, agent_id: str, client_name: str, status: str, tenant: str, project_id: str,
                       hostname: Optional[str] = None, pid: Optional[str] = None,
                       meta: Optional[Dict[str, Any]] = None,
                       client_id: Optional[str] = None, parent_client_id: Optional[str] = None) -> Dict[str, Any]:
        return self.db.upsert_agent(
            agent_id=agent_id,
            client_name=client_name,
            status=status,
            tenant=tenant,
            project_id=project_id,
            hostname=hostname,
            pid=pid,
            meta=meta,
            client_id=client_id,
            parent_client_id=parent_client_id,
        )

    def list_agents(self, tenant: str, project_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        return self.db.list_agents(tenant, project_id, limit)

    def list_access_events(self, tenant: str, project_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        return self.db.list_access_events(tenant, project_id, limit)

    def register_client(self, client_id: str, tenant: str, project_id: str,
                        client_name: Optional[str] = None, parent_client_id: Optional[str] = None,
                        status: str = "registered", metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self.db.register_client(
            client_id=client_id,
            client_name=client_name,
            parent_client_id=parent_client_id,
            status=status,
            metadata=metadata,
            tenant=tenant,
            project_id=project_id,
        )
        return self.db.get_client(client_id, tenant, project_id) or {
            "client_id": client_id,
            "client_name": client_name,
            "parent_client_id": parent_client_id,
            "status": status,
            "tenant": tenant,
            "project_id": project_id,
        }

    def list_clients(self, tenant: str, project_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        return self.db.list_clients(tenant, project_id, limit)

    def client_summary(self, client_id: str, tenant: str, project_id: str, include_children: bool = True) -> Dict[str, Any]:
        if not client_id:
            raise ValueError("client_id is required")
        client = self.db.get_client(client_id, tenant, project_id) or {
            "client_id": client_id,
            "client_name": None,
            "parent_client_id": None,
            "status": "unknown",
            "metadata": {},
        }
        summary = self.db.get_client_layer_stats(client_id, tenant, project_id)
        summary["open_issues"] = self.db.count_client_issues(client_id, tenant, project_id, status="open")
        summary["pending_quarantine"] = self.db.count_quarantine(client_id, tenant, project_id, status="pending")

        children: List[Dict[str, Any]] = []
        combined_counts = summary["counts"].copy()
        combined_last_write = summary["last_write"].copy()
        combined_total = summary["total"]

        if include_children:
            for child in self.db.list_child_clients(client_id, tenant, project_id):
                child_id = child.get("client_id")
                if not child_id:
                    continue
                child_summary = self.db.get_client_layer_stats(child_id, tenant, project_id)
                child_summary["open_issues"] = self.db.count_client_issues(child_id, tenant, project_id, status="open")
                child_summary["pending_quarantine"] = self.db.count_quarantine(child_id, tenant, project_id, status="pending")

                for layer, count in child_summary["counts"].items():
                    combined_counts[layer] = combined_counts.get(layer, 0) + count
                    combined_total += count
                    if child_summary["last_write"].get(layer):
                        current = combined_last_write.get(layer)
                        if not current or child_summary["last_write"][layer] > current:
                            combined_last_write[layer] = child_summary["last_write"][layer]

                children.append({
                    "client_id": child_id,
                    "client_name": child.get("client_name"),
                    "status": child.get("status"),
                    "parent_client_id": child.get("parent_client_id"),
                    "last_seen": child.get("last_seen"),
                    "counts": child_summary["counts"],
                    "last_write": child_summary["last_write"],
                    "total": child_summary["total"],
                    "open_issues": child_summary["open_issues"],
                    "pending_quarantine": child_summary["pending_quarantine"],
                })

        return {
            "client": {
                "client_id": client.get("client_id"),
                "client_name": client.get("client_name"),
                "status": client.get("status"),
                "parent_client_id": client.get("parent_client_id"),
                "last_seen": client.get("last_seen"),
            },
            "summary": summary,
            "children": children,
            "combined": {
                "counts": combined_counts,
                "last_write": combined_last_write,
                "total": combined_total,
                "open_issues": summary["open_issues"] + sum(c["open_issues"] for c in children),
                "pending_quarantine": summary["pending_quarantine"] + sum(c["pending_quarantine"] for c in children),
            },
        }

    def add_client_issue(self, item: ClientIssueCreate, tenant: str, project_id: str,
                         agent_id: Optional[str] = None, client_name: Optional[str] = None,
                         client_id: Optional[str] = None, parent_client_id: Optional[str] = None,
                         child_client_id: Optional[str] = None) -> Dict[str, Any]:
        client_meta = self._normalize_client_ids(
            client_id=client_id,
            client_name=client_name,
            agent_id=agent_id,
            tenant=tenant,
            project_id=project_id,
            parent_client_id=parent_client_id,
            child_client_id=child_client_id,
        )
        issue = self.db.add_client_issue(
            client_id=client_meta["client_id"],
            message=item.message,
            tenant=tenant,
            project_id=project_id,
            error_code=item.error_code,
            client_name=client_meta["client_name"],
            agent_id=agent_id,
            parent_client_id=client_meta["parent_client_id"],
            child_client_id=client_meta["child_client_id"],
            layer=item.layer,
            payload=item.payload,
            metadata=item.metadata,
        )
        auto_resolve = os.getenv("PINAK_AUTO_RESOLVE_ISSUES", "")
        auto_codes = [c.strip() for c in auto_resolve.split(",") if c.strip()]
        if item.error_code in auto_codes and self._is_trusted_client(client_meta["client_id"], tenant, project_id):
            return self.resolve_client_issue(issue.get("id"), "auto-resolved by policy", "auto-policy")
        return issue

    def list_client_issues(self, tenant: str, project_id: str, status: str = "open", limit: int = 200) -> List[Dict[str, Any]]:
        return self.db.list_client_issues(tenant, project_id, status, limit)

    def resolve_client_issue(self, issue_id: str, resolution: str, reviewer: str) -> Dict[str, Any]:
        resolved = self.db.resolve_client_issue(issue_id, resolution, reviewer)
        if not resolved:
            return {"id": issue_id, "status": "missing"}
        return resolved

    def propose_memory(self, layer: str, payload: Dict[str, Any], tenant: str, project_id: str,
                       agent_id: Optional[str] = None, client_name: Optional[str] = None,
                       client_id: Optional[str] = None, parent_client_id: Optional[str] = None,
                       child_client_id: Optional[str] = None) -> Dict[str, Any]:
        client_meta = self._normalize_client_ids(
            client_id=client_id,
            client_name=client_name,
            agent_id=agent_id,
            tenant=tenant,
            project_id=project_id,
            parent_client_id=parent_client_id,
            child_client_id=child_client_id,
        )
        validation_errors = self.schema_registry.validate_payload(layer, payload)
        if validation_errors:
            try:
                self.db.add_client_issue(
                    client_id=client_meta["client_id"],
                    client_name=client_meta["client_name"],
                    agent_id=agent_id,
                    parent_client_id=client_meta["parent_client_id"],
                    child_client_id=client_meta["child_client_id"],
                    layer=layer,
                    error_code="schema_validation_failed",
                    message="Payload failed schema validation",
                    payload=payload,
                    metadata={"errors": validation_errors},
                    tenant=tenant,
                    project_id=project_id,
                )
            except Exception:
                pass
        auto_approve = os.getenv("PINAK_QUARANTINE_AUTO_APPROVE", "false").lower() in ("1", "true", "yes")
        if auto_approve and not validation_errors and self._is_trusted_client(client_meta["client_id"], tenant, project_id):
            self._apply_quarantine_payload(
                layer,
                payload,
                tenant,
                project_id,
                agent_id,
                client_meta["client_name"],
                client_id=client_meta["client_id"],
                parent_client_id=client_meta["parent_client_id"],
                child_client_id=client_meta["child_client_id"],
            )
            self.db.add_access_event(
                event_type="write",
                status="ok",
                tenant=tenant,
                project_id=project_id,
                agent_id=agent_id,
                client_name=client_meta["client_name"],
                client_id=client_meta["client_id"],
                parent_client_id=client_meta["parent_client_id"],
                child_client_id=client_meta["child_client_id"],
                target_layer=layer,
                detail="auto_approved",
            )
            return {"status": "approved", "layer": layer, "auto_approved": True}
        res = self.db.add_quarantine(
            layer,
            payload,
            tenant,
            project_id,
            agent_id=agent_id,
            client_id=client_meta["client_id"],
            client_name=client_meta["client_name"],
            validation_errors=validation_errors,
        )
        self.db.add_access_event(
            event_type="propose",
            status="ok" if not validation_errors else "error",
            tenant=tenant,
            project_id=project_id,
            agent_id=agent_id,
            client_name=client_meta["client_name"],
            client_id=client_meta["client_id"],
            parent_client_id=client_meta["parent_client_id"],
            child_client_id=client_meta["child_client_id"],
            target_layer=layer,
            detail="quarantine_propose",
        )
        return res

    def list_quarantine(self, tenant: str, project_id: str, status: str = "pending", limit: int = 100) -> List[Dict[str, Any]]:
        return self.db.list_quarantine(tenant, project_id, status, limit)

    def resolve_quarantine(self, item_id: str, status: str, reviewer: str,
                           tenant: str, project_id: str,
                           agent_id: Optional[str] = None, client_name: Optional[str] = None,
                           client_id: Optional[str] = None, parent_client_id: Optional[str] = None,
                           child_client_id: Optional[str] = None) -> Dict[str, Any]:
        item = self.db.resolve_quarantine(item_id, status, reviewer)
        if not item:
            return {"status": "missing", "id": item_id}
        layer = item.get("layer")
        payload = item.get("payload") or {}
        if status == "approved":
            self._apply_quarantine_payload(
                layer,
                payload,
                tenant,
                project_id,
                agent_id,
                client_name,
                client_id=item.get("client_id") or client_id,
                parent_client_id=parent_client_id,
                child_client_id=child_client_id,
            )
        self.db.add_access_event(
            event_type="write",
            status="ok",
            tenant=tenant,
            project_id=project_id,
            agent_id=agent_id,
            client_name=client_name,
            client_id=client_id,
            parent_client_id=parent_client_id,
            child_client_id=child_client_id,
            target_layer=layer,
            detail=f"quarantine_{status}",
        )
        return {"status": status, "id": item_id, "layer": layer}

    def _apply_quarantine_payload(self, layer: str, payload: Dict[str, Any], tenant: str, project_id: str,
                                  agent_id: Optional[str], client_name: Optional[str],
                                  client_id: Optional[str] = None, parent_client_id: Optional[str] = None,
                                  child_client_id: Optional[str] = None) -> None:
        if layer == "semantic":
            memory = MemoryCreate(content=payload.get("content", ""), tags=payload.get("tags") or [])
            self.add_memory(memory, tenant, project_id, agent_id, client_name, client_id, parent_client_id, child_client_id)
            return
        if layer == "episodic":
            self.add_episodic(
                payload.get("content", ""),
                tenant,
                project_id,
                payload.get("salience", 0),
                payload.get("goal"),
                payload.get("plan"),
                payload.get("outcome"),
                payload.get("tool_logs"),
                agent_id,
                client_name,
                client_id,
                parent_client_id,
                child_client_id,
            )
            return
        if layer == "procedural":
            self.add_procedural(
                payload.get("skill_name", ""),
                payload.get("steps") or [],
                tenant,
                project_id,
                payload.get("description"),
                payload.get("trigger"),
                payload.get("code_snippet"),
                agent_id,
                client_name,
                client_id,
                parent_client_id,
                child_client_id,
            )
            return
        if layer == "rag":
            self.add_rag(
                payload.get("query", ""),
                payload.get("external_source", ""),
                payload.get("content", ""),
                tenant,
                project_id,
                agent_id,
                client_name,
                client_id,
                parent_client_id,
                child_client_id,
            )
            return

    def update_memory(self, layer: str, memory_id: str, updates: Dict[str, Any], tenant: str, project_id: str) -> bool:
        """
        Updates memory in DB. If content changes in Semantic layer, re-embeds.
        """
        # Security: Prevent updating system fields
        forbidden_keys = {"id", "tenant", "project_id", "created_at", "embedding_id"}
        safe_updates = {k: v for k, v in updates.items() if k not in forbidden_keys}

        if not safe_updates:
            return False

        if layer == "semantic" and "content" in safe_updates and self.vector_enabled and self.vector_store:
            # 1. Fetch old record to get embedding_id
            old_item = self.db.get_memory(layer, memory_id, tenant, project_id)
            if old_item:
                emb_id = old_item.get("embedding_id")
                # 2. Re-embed
                new_embedding = self.model.encode([safe_updates["content"]])[0].astype("float32")
                # 3. Update Vector Store
                # Remove old vector first
                if emb_id:
                    self.vector_store.remove_ids([emb_id])
                    # Add new
                    self.vector_store.add_vectors(np.array([new_embedding]), [emb_id])
                    self.vector_store.save()

        success = self.db.update_memory(layer, memory_id, safe_updates, tenant, project_id)
        self.db.add_access_event(
            event_type="update",
            status="ok" if success else "error",
            tenant=tenant,
            project_id=project_id,
            target_layer=layer,
            memory_id=memory_id,
            detail="update_memory",
        )
        return success

    def delete_memory(self, layer: str, memory_id: str, tenant: str, project_id: str) -> bool:
        if layer == "semantic" and self.vector_enabled and self.vector_store:
            # Fetch embedding ID first
            item = self.db.get_memory(layer, memory_id, tenant, project_id)
            if item and item.get("embedding_id"):
                self.vector_store.remove_ids([item["embedding_id"]])
                self.vector_store.save()

        success = self.db.delete_memory(layer, memory_id, tenant, project_id)
        self.db.add_access_event(
            event_type="delete",
            status="ok" if success else "error",
            tenant=tenant,
            project_id=project_id,
            target_layer=layer,
            memory_id=memory_id,
            detail="delete_memory",
        )
        return success

# Functional adapters for compatibility with existing endpoints.py which calls 'svc_add_episodic' etc.
# Ideally endpoints should call service instance methods.
# I will fix endpoints.py to use the service methods directly.
