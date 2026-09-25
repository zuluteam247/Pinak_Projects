from functools import lru_cache
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, status, HTTPException

from app.core.schemas import (
    MemoryCreate, MemoryRead, MemorySearchResult,
    EpisodicCreate, ProceduralCreate, RAGCreate, EventCreate, SessionCreate,
    ContextResponse, WorkingCreate, WorkingRead,
    AgentHeartbeatCreate, AgentRead, AccessEventRead, QuarantineItemRead,
    ClientIssueCreate, ClientIssueRead, ClientRegisterCreate, ClientRegisterRead
)
from app.core.security import AuthContext, require_auth_context, require_scope, require_role
from app.core.schema_registry import SchemaRegistry
from app.services.memory_service import MemoryService

@lru_cache
def _service_factory() -> MemoryService:
    return MemoryService()

def get_memory_service() -> MemoryService:
    """Dependency that provides a cached MemoryService instance."""
    return _service_factory()

router = APIRouter()

# --- Schemas ---

@router.get("/schema/{layer}")
def get_schema(
    layer: str,
    ctx: AuthContext = Depends(require_auth_context),
):
    require_scope(ctx, "memory.read")
    if not layer.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid layer name")
    registry = SchemaRegistry()
    schema = registry.load_schema(layer)
    if not schema:
        raise HTTPException(status_code=404, detail="Schema not found")
    return schema

@router.get("/schema")
def list_schemas(
    ctx: AuthContext = Depends(require_auth_context),
):
    require_scope(ctx, "memory.read")
    registry = SchemaRegistry()
    out = {}
    for layer in ["semantic", "episodic", "procedural", "rag", "working"]:
        schema = registry.load_schema(layer)
        if schema:
            out[layer] = schema
    return {
        "schemas": out,
    }

# --- Semantic Memory ---

@router.post("/add", response_model=MemoryRead, status_code=status.HTTP_201_CREATED)
def add_memory(
    memory: MemoryCreate,
    ctx: AuthContext = Depends(require_auth_context),
    service: MemoryService = Depends(get_memory_service),
):
    """Add a semantic memory (Vector + DB)."""
    require_scope(ctx, "memory.write")
    return service.add_memory(
        memory,
        ctx.tenant_id,
        ctx.project_id,
        ctx.subject,
        ctx.client_name,
        ctx.client_id,
        ctx.parent_client_id,
        ctx.child_client_id,
    )

@router.get("/search", response_model=List[MemorySearchResult])
def search_memory(
    query: str,
    k: int = 5,
    ctx: AuthContext = Depends(require_auth_context),
    service: MemoryService = Depends(get_memory_service),
):
    """Legacy Semantic Search."""
    require_scope(ctx, "memory.read")
    return service.search_memory(
        query,
        ctx.tenant_id,
        ctx.project_id,
        k,
        ctx.subject,
        ctx.client_name,
        ctx.client_id,
        ctx.parent_client_id,
        ctx.child_client_id,
    )

# --- Unified Retrieval ---

@router.get("/rag/search")
def search_rag(
    query: str,
    limit: int = 10,
    ctx: AuthContext = Depends(require_auth_context),
    service: MemoryService = Depends(get_memory_service),
):
    require_scope(ctx, "memory.read")
    return service.db.search_rag(query, ctx.tenant_id, ctx.project_id, limit)

@router.get("/retrieve_context", response_model=ContextResponse)
def retrieve_context(
    query: str,
    ctx: AuthContext = Depends(require_auth_context),
    service: MemoryService = Depends(get_memory_service),
):
    """
    Unified Endpoint for Agents.
    Performs Hybrid Search across all layers and returns categorized context.
    """
    require_scope(ctx, "memory.read")
    return service.retrieve_context(
        query,
        ctx.tenant_id,
        ctx.project_id,
        agent_id=ctx.subject,
        client_name=ctx.client_name,
        client_id=ctx.client_id,
        parent_client_id=ctx.parent_client_id,
        child_client_id=ctx.child_client_id,
    )

# --- Episodic Memory ---

@router.post("/episodic/add", status_code=status.HTTP_201_CREATED)
def add_episodic(
    item: EpisodicCreate,
    ctx: AuthContext = Depends(require_auth_context),
    service: MemoryService = Depends(get_memory_service),
):
    require_scope(ctx, "memory.write")
    return service.add_episodic(
        item.content, ctx.tenant_id, ctx.project_id,
        item.salience, item.goal, item.plan, item.outcome, item.tool_logs,
        ctx.subject, ctx.client_name,
        ctx.client_id, ctx.parent_client_id, ctx.child_client_id
    )

# --- Procedural Memory ---

@router.post("/procedural/add", status_code=status.HTTP_201_CREATED)
def add_procedural(
    item: ProceduralCreate,
    ctx: AuthContext = Depends(require_auth_context),
    service: MemoryService = Depends(get_memory_service),
):
    require_scope(ctx, "memory.write")
    return service.add_procedural(
        item.skill_name, item.steps, ctx.tenant_id, ctx.project_id,
        item.description, item.trigger, item.code_snippet,
        ctx.subject, ctx.client_name,
        ctx.client_id, ctx.parent_client_id, ctx.child_client_id
    )

# --- RAG ---

@router.post("/rag/add", status_code=status.HTTP_201_CREATED)
def add_rag(
    item: RAGCreate,
    ctx: AuthContext = Depends(require_auth_context),
    service: MemoryService = Depends(get_memory_service),
):
    require_scope(ctx, "memory.write")
    return service.add_rag(
        item.query, item.external_source, item.content, ctx.tenant_id, ctx.project_id,
        ctx.subject, ctx.client_name,
        ctx.client_id, ctx.parent_client_id, ctx.child_client_id
    )

# --- Logs & Events ---

@router.post("/event", status_code=status.HTTP_201_CREATED)
def add_event(
    item: EventCreate,
    ctx: AuthContext = Depends(require_auth_context),
    service: MemoryService = Depends(get_memory_service),
):
    require_scope(ctx, "memory.write")
    return service.add_event(
        {"event_type": item.event_type, **item.payload},
        ctx.tenant_id, ctx.project_id
    )

@router.get("/events")
def list_events(
    limit: int = 100,
    ctx: AuthContext = Depends(require_auth_context),
    service: MemoryService = Depends(get_memory_service),
):
    require_scope(ctx, "memory.read")
    return service.list_events(ctx.tenant_id, ctx.project_id, limit)

@router.post("/session/add", status_code=status.HTTP_201_CREATED)
def add_session(
    item: SessionCreate,
    ctx: AuthContext = Depends(require_auth_context),
    service: MemoryService = Depends(get_memory_service),
):
    require_scope(ctx, "memory.write")
    return service.session_add(
        item.session_id,
        item.content,
        item.role,
        ctx.tenant_id,
        ctx.project_id,
        ctx.subject,
        ctx.client_name,
        ctx.client_id,
        ctx.parent_client_id,
        ctx.child_client_id,
    )

@router.get("/session/list")
def list_session(
    session_id: str,
    limit: int = 100,
    ctx: AuthContext = Depends(require_auth_context),
    service: MemoryService = Depends(get_memory_service),
):
    require_scope(ctx, "memory.read")
    return service.session_list(session_id, ctx.tenant_id, ctx.project_id, limit)

@router.post("/working/add", response_model=WorkingRead, status_code=status.HTTP_201_CREATED)
def add_working(
    item: WorkingCreate,
    ctx: AuthContext = Depends(require_auth_context),
    service: MemoryService = Depends(get_memory_service),
):
    require_scope(ctx, "memory.write")
    return service.working_add(
        item.content,
        ctx.tenant_id,
        ctx.project_id,
        ctx.subject,
        ctx.client_name,
        ctx.client_id,
        ctx.parent_client_id,
        ctx.child_client_id,
    )

# --- Agent Observability ---

@router.post("/agent/heartbeat", response_model=AgentRead, status_code=status.HTTP_200_OK)
def agent_heartbeat(
    item: AgentHeartbeatCreate,
    ctx: AuthContext = Depends(require_auth_context),
    service: MemoryService = Depends(get_memory_service),
):
    require_scope(ctx, "memory.read")
    client_name = ctx.client_name or "unknown"
    agent_id = ctx.subject or "unknown"
    return service.register_agent(
        agent_id=agent_id,
        client_name=client_name,
        status=item.status,
        tenant=ctx.tenant_id,
        project_id=ctx.project_id,
        hostname=item.hostname,
        pid=item.pid,
        meta=item.meta,
        client_id=ctx.client_id,
        parent_client_id=ctx.parent_client_id,
    )

# --- Client Registry ---

@router.post("/client/register", response_model=ClientRegisterRead, status_code=status.HTTP_201_CREATED)
def register_client(
    item: ClientRegisterCreate,
    ctx: AuthContext = Depends(require_auth_context),
    service: MemoryService = Depends(get_memory_service),
):
    require_scope(ctx, "memory.write")
    status_value = item.status or "registered"
    if status_value in {"trusted", "blocked"}:
        require_role(ctx, "admin")
    return service.register_client(
        client_id=item.client_id,
        client_name=item.client_name,
        parent_client_id=item.parent_client_id,
        status=status_value,
        metadata=item.metadata,
        tenant=ctx.tenant_id,
        project_id=ctx.project_id,
    )

@router.get("/client/list", response_model=List[ClientRegisterRead])
def list_clients(
    limit: int = 200,
    ctx: AuthContext = Depends(require_auth_context),
    service: MemoryService = Depends(get_memory_service),
):
    require_scope(ctx, "memory.read")
    return service.list_clients(ctx.tenant_id, ctx.project_id, limit)

@router.get("/client/summary")
def client_summary(
    include_children: bool = True,
    ctx: AuthContext = Depends(require_auth_context),
    service: MemoryService = Depends(get_memory_service),
):
    require_scope(ctx, "memory.read")
    client_id = ctx.client_id or ctx.effective_client_id
    if not client_id:
        raise HTTPException(status_code=400, detail="client_id is required")
    return service.client_summary(client_id, ctx.tenant_id, ctx.project_id, include_children=include_children)

@router.get("/agent/list", response_model=List[AgentRead])
def list_agents(
    limit: int = 200,
    ctx: AuthContext = Depends(require_auth_context),
    service: MemoryService = Depends(get_memory_service),
):
    require_scope(ctx, "memory.read")
    return service.list_agents(ctx.tenant_id, ctx.project_id, limit)

@router.get("/access/list", response_model=List[AccessEventRead])
def list_access_events(
    limit: int = 200,
    ctx: AuthContext = Depends(require_auth_context),
    service: MemoryService = Depends(get_memory_service),
):
    require_scope(ctx, "memory.read")
    return service.list_access_events(ctx.tenant_id, ctx.project_id, limit)

# --- Client Issues ---

@router.post("/client/issues", response_model=ClientIssueRead, status_code=status.HTTP_201_CREATED)
def add_client_issue(
    item: ClientIssueCreate,
    ctx: AuthContext = Depends(require_auth_context),
    service: MemoryService = Depends(get_memory_service),
):
    require_scope(ctx, "memory.write")
    return service.add_client_issue(
        item,
        ctx.tenant_id,
        ctx.project_id,
        ctx.subject,
        ctx.client_name,
        ctx.client_id,
        ctx.parent_client_id,
        ctx.child_client_id,
    )

@router.get("/client/issues", response_model=List[ClientIssueRead])
def list_client_issues(
    status_filter: str = "open",
    limit: int = 200,
    ctx: AuthContext = Depends(require_auth_context),
    service: MemoryService = Depends(get_memory_service),
):
    require_scope(ctx, "memory.read")
    return service.list_client_issues(ctx.tenant_id, ctx.project_id, status_filter, limit)

@router.post("/client/issues/{issue_id}/resolve", response_model=ClientIssueRead)
def resolve_client_issue(
    issue_id: str,
    resolution: Dict[str, Any] = Body(...),
    ctx: AuthContext = Depends(require_auth_context),
    service: MemoryService = Depends(get_memory_service),
):
    require_scope(ctx, "memory.admin")
    require_role(ctx, "admin")
    return service.resolve_client_issue(issue_id, resolution.get("resolution", "resolved"), ctx.subject or "admin", ctx.tenant_id, ctx.project_id)

# --- Quarantine ---

@router.post("/quarantine/propose/{layer}", response_model=QuarantineItemRead, status_code=status.HTTP_202_ACCEPTED)
def propose_quarantine(
    layer: str,
    payload: Dict[str, Any] = Body(...),
    ctx: AuthContext = Depends(require_auth_context),
    service: MemoryService = Depends(get_memory_service),
):
    require_scope(ctx, "memory.write")
    if layer not in {"semantic", "episodic", "procedural", "rag"}:
        raise HTTPException(status_code=400, detail="Unsupported quarantine layer")
    return service.propose_memory(
        layer,
        payload,
        ctx.tenant_id,
        ctx.project_id,
        ctx.subject,
        ctx.client_name,
        ctx.client_id,
        ctx.parent_client_id,
        ctx.child_client_id,
    )

@router.get("/quarantine/list", response_model=List[QuarantineItemRead])
def list_quarantine(
    status_filter: str = "pending",
    limit: int = 100,
    ctx: AuthContext = Depends(require_auth_context),
    service: MemoryService = Depends(get_memory_service),
):
    require_scope(ctx, "memory.admin")
    require_role(ctx, "admin")
    return service.list_quarantine(ctx.tenant_id, ctx.project_id, status_filter, limit)

@router.post("/quarantine/approve/{item_id}")
def approve_quarantine(
    item_id: str,
    ctx: AuthContext = Depends(require_auth_context),
    service: MemoryService = Depends(get_memory_service),
):
    require_scope(ctx, "memory.admin")
    require_role(ctx, "admin")
    reviewer = ctx.subject or "admin"
    try:
        result = service.resolve_quarantine(item_id, "approved", reviewer, ctx.tenant_id, ctx.project_id, ctx.subject, ctx.client_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result.get("status") == "missing":
        raise HTTPException(status_code=404, detail="Quarantine item not found")
    return result

@router.post("/quarantine/reject/{item_id}")
def reject_quarantine(
    item_id: str,
    ctx: AuthContext = Depends(require_auth_context),
    service: MemoryService = Depends(get_memory_service),
):
    require_scope(ctx, "memory.admin")
    require_role(ctx, "admin")
    reviewer = ctx.subject or "admin"
    result = service.resolve_quarantine(item_id, "rejected", reviewer, ctx.tenant_id, ctx.project_id, ctx.subject, ctx.client_name)
    if result.get("status") == "missing":
        raise HTTPException(status_code=404, detail="Quarantine item not found")
    return result

@router.get("/working/list")
def list_working(
    limit: int = 100,
    ctx: AuthContext = Depends(require_auth_context),
    service: MemoryService = Depends(get_memory_service),
):
    require_scope(ctx, "memory.read")
    return service.working_list(ctx.tenant_id, ctx.project_id, limit)

@router.post("/maintenance/verify")
def verify_maintenance(
    ctx: AuthContext = Depends(require_auth_context),
    service: MemoryService = Depends(get_memory_service),
):
    require_scope(ctx, "memory.admin")
    require_role(ctx, "admin")
    # This verifies the global audit chain and vector index. It does not
    # condense memories; keep the distinction visible in the response.
    audit = service.db.verify_audit_chain()
    if not audit.get("valid"):
        raise HTTPException(status_code=409, detail={"audit": audit})
    service.verify_and_recover()
    return {"audit": audit, "vectors_checked": service.vector_enabled, "condensed": False}

@router.get("/{layer}/{memory_id}")
def get_memory_by_id(
    layer: str,
    memory_id: str,
    ctx: AuthContext = Depends(require_auth_context),
    service: MemoryService = Depends(get_memory_service),
):
    require_scope(ctx, "memory.read")
    if layer not in {"semantic", "episodic", "procedural", "rag", "working"}:
        raise HTTPException(status_code=400, detail="Unsupported layer")
    item = service.db.get_memory(layer, memory_id, ctx.tenant_id, ctx.project_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    return item

@router.put("/{layer}/{memory_id}", status_code=status.HTTP_200_OK)
def update_memory(
    layer: str,
    memory_id: str,
    updates: Dict[str, Any] = Body(...),
    ctx: AuthContext = Depends(require_auth_context),
    service: MemoryService = Depends(get_memory_service),
):
    require_scope(ctx, "memory.admin")
    require_role(ctx, "admin")
    if layer not in {"semantic", "episodic", "procedural", "rag"}:
        raise HTTPException(status_code=400, detail="Unsupported layer")
    try:
        success = service.update_memory(layer, memory_id, updates, ctx.tenant_id, ctx.project_id)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not success:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"status": "updated", "id": memory_id}

@router.delete("/{layer}/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_memory(
    layer: str,
    memory_id: str,
    ctx: AuthContext = Depends(require_auth_context),
    service: MemoryService = Depends(get_memory_service),
):
    require_scope(ctx, "memory.admin")
    require_role(ctx, "admin")
    if layer not in {"semantic", "episodic", "procedural", "rag"}:
        raise HTTPException(status_code=400, detail="Unsupported layer")
    success = service.delete_memory(layer, memory_id, ctx.tenant_id, ctx.project_id)
    if not success:
        raise HTTPException(status_code=404, detail="Memory not found")
