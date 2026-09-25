from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any, Union

class MemoryCreate(BaseModel):
    content: str
    tags: Optional[List[str]] = None

class MemoryRead(BaseModel):
    id: str
    content: str
    tags: List[str] = []
    tenant: str
    project_id: str
    created_at: str
    client_id: Optional[str] = None
    client_name: Optional[str] = None
    agent_id: Optional[str] = None

    model_config = {
        "from_attributes": True
    }

class MemorySearchResult(MemoryRead):
    distance: float
    metadata: Optional[Dict[str, Any]] = None

class EpisodicCreate(BaseModel):
    content: str
    salience: int = 0
    goal: Optional[str] = None
    plan: Optional[List[str]] = None
    outcome: Optional[str] = None
    tool_logs: Optional[List[Dict[str, Any]]] = None
    embedding_id: Optional[int] = None

class ProceduralCreate(BaseModel):
    skill_name: str
    steps: List[str]
    description: Optional[str] = None
    trigger: Optional[str] = None
    code_snippet: Optional[str] = None
    embedding_id: Optional[int] = None

class RAGCreate(BaseModel):
    query: str
    external_source: str
    content: str

class EventCreate(BaseModel):
    event_type: str
    payload: Dict[str, Any]

class SessionCreate(BaseModel):
    session_id: str
    content: str
    role: str = "user"

class WorkingCreate(BaseModel):
    content: str

class AgentHeartbeatCreate(BaseModel):
    status: str = "active"
    hostname: Optional[str] = None
    pid: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None

class AgentRead(BaseModel):
    agent_id: str
    client_name: str
    client_id: Optional[str] = None
    parent_client_id: Optional[str] = None
    hostname: Optional[str] = None
    pid: Optional[str] = None
    status: str
    meta: Dict[str, Any] = {}
    tenant: str
    project_id: str
    last_seen: str

class AccessEventRead(BaseModel):
    agent_id: Optional[str] = None
    client_name: Optional[str] = None
    client_id: Optional[str] = None
    parent_client_id: Optional[str] = None
    child_client_id: Optional[str] = None
    event_type: str
    target_layer: Optional[str] = None
    query: Optional[str] = None
    memory_id: Optional[str] = None
    result_count: Optional[int] = None
    status: str
    detail: Optional[str] = None
    ts: str

class QuarantineItemRead(BaseModel):
    id: str
    layer: str
    payload: Dict[str, Any]
    status: str
    tenant: str
    project_id: str
    created_at: str
    reviewed_at: Optional[str] = None
    reviewed_by: Optional[str] = None
    client_id: Optional[str] = None
    client_name: Optional[str] = None
    agent_id: Optional[str] = None
    validation_errors: Optional[List[str]] = None

class ClientIssueCreate(BaseModel):
    error_code: str
    message: str
    layer: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None

class ClientIssueRead(BaseModel):
    id: str
    client_id: str
    client_name: Optional[str] = None
    agent_id: Optional[str] = None
    parent_client_id: Optional[str] = None
    child_client_id: Optional[str] = None
    layer: Optional[str] = None
    error_code: str
    message: str
    payload: Optional[Dict[str, Any]] = None
    status: str
    created_at: str
    resolved_at: Optional[str] = None
    resolved_by: Optional[str] = None
    resolution: Optional[str] = None

class ClientRegisterCreate(BaseModel):
    client_id: str
    client_name: Optional[str] = None
    parent_client_id: Optional[str] = None
    status: Optional[str] = "registered"
    metadata: Optional[Dict[str, Any]] = None

class ClientRegisterRead(BaseModel):
    client_id: str
    client_name: Optional[str] = None
    parent_client_id: Optional[str] = None
    status: str
    metadata: Optional[Dict[str, Any]] = None
    tenant: str
    project_id: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    last_seen: Optional[str] = None

class Nudge(BaseModel):
    type: str = "proactive_nudge"
    strength: str
    message: str
    source_id: str
    layer: str

class WorkingRead(BaseModel):
    id: str
    content: str
    tenant: str
    project_id: str
    created_at: str
    nudges: List[Nudge] = []

class ContextResponse(BaseModel):
    rag: List[Dict[str, Any]] = []
    semantic: List[Dict[str, Any]]
    episodic: List[Dict[str, Any]]
    procedural: List[Dict[str, Any]]
    working: List[Dict[str, Any]]
