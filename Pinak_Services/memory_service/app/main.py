
from fastapi import FastAPI, HTTPException, Request
from contextlib import asynccontextmanager
import fcntl
import json
import asyncio
import logging
import sys
import os
import time
import uuid
import threading
from collections import defaultdict

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# Per-process counters, not cluster-wide Prometheus metrics. Do not label
# tokens, queries, tenant names or raw paths (unbounded cardinality).
_METRICS_LOCK = threading.Lock()
_REQUEST_METRICS = defaultdict(lambda: [0, 0.0])


def _metrics_snapshot():
    with _METRICS_LOCK:
        return {route: {"count": value[0], "duration_seconds_sum": round(value[1], 6)}
                for route, value in _REQUEST_METRICS.items()}


from app.api.v1 import endpoints
from app.api.v1.endpoints import get_memory_service
from app.services.background import cleanup_expired_memories

async def _verify_in_background(app: FastAPI, service):
    try:
        await asyncio.to_thread(service.verify_and_recover)
    except Exception:
        logger.exception("Background memory verification failed")
        app.state.verification_status = "failed"
    else:
        app.state.verification_status = "ready"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Lock before MemoryService initializes or repairs the DB/vector index.
    # flock protects processes on one local filesystem only. Shared network
    # volumes and separately replicated copies of one dataset are unsupported.
    config_path = os.getenv("PINAK_CONFIG_PATH", "app/core/config.json")
    if os.path.exists(config_path):
        with open(config_path) as handle:
            config = json.load(handle)
    else:
        config = {}
    data_root = os.getenv("PINAK_DATA_ROOT") or config.get("data_root", "data")
    os.makedirs(data_root, exist_ok=True)
    lock_path = os.path.join(data_root, ".pinak-writer.lock")
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(lock_fd)
        raise RuntimeError(f"Memory data directory already has a writer: {data_root}") from exc
    try:
        service = get_memory_service()
        # Tests may override service construction. In normal operation this
        # resolves from the same config path, so the held lock covers its data.
        if os.path.realpath(service.data_root) != os.path.realpath(data_root):
            raise RuntimeError("Memory service data directory differs from writer lock directory")
        app.state.memory_service = service
        app.state.verification_status = "verifying"
        skip_verify = os.getenv("PINAK_SKIP_VERIFY_ON_STARTUP", "false").lower() in ("1", "true", "yes")
        background_verify = os.getenv("PINAK_VERIFY_IN_BACKGROUND", "false").lower() in ("1", "true", "yes")
        verification_task = None
        if skip_verify:
            # No verification means no proof of readiness.
            app.state.verification_status = "skipped"
        elif background_verify:
            verification_task = asyncio.create_task(_verify_in_background(app, service))
        else:
            service.verify_and_recover()
            app.state.verification_status = "ready"

        cleanup_task = asyncio.create_task(cleanup_expired_memories(service.db, interval_seconds=3600))
        try:
            yield
        finally:
            app.state.verification_status = "stopping"
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
            if verification_task is not None:
                await verification_task
            if getattr(service, "vector_store", None):
                service.vector_store.save()
    finally:
        app.state.memory_service = None
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

app = FastAPI(title="Pinak Memory Service", lifespan=lifespan)

class RequestSizeLimit:
    """ASGI body cap with one response for both Content-Length and streamed bodies."""
    def __init__(self, app, limit=1024 * 1024):
        self.app = app
        self.limit = limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") not in {"POST", "PUT", "PATCH"}:
            return await self.app(scope, receive, send)
        from starlette.responses import JSONResponse
        headers = dict(scope.get("headers", []))
        try:
            if int(headers.get(b"content-length", b"0")) > self.limit:
                return await JSONResponse({"detail": "Request body too large"}, status_code=413)(scope, receive, send)
        except ValueError:
            return await JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)(scope, receive, send)
        # Buffer at most limit + 1 bytes *before* invoking the application,
        # so a chunked overflow cannot leave a half-sent response.
        chunks = []
        size = 0
        while True:
            event = await receive()
            if event["type"] != "http.request":
                return await JSONResponse({"detail": "Request body interrupted"}, status_code=400)(scope, receive, send)
            chunk = event.get("body", b"")
            size += len(chunk)
            if size > self.limit:
                return await JSONResponse({"detail": "Request body too large"}, status_code=413)(scope, receive, send)
            chunks.append(chunk)
            if not event.get("more_body", False):
                break
        body = b"".join(chunks)
        first = True
        async def replay():
            nonlocal first
            if first:
                first = False
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()
        return await self.app(scope, replay, send)

app.add_middleware(RequestSizeLimit)

@app.middleware("http")
async def request_observability(request: Request, call_next):
    # Do not trust a caller-supplied request ID for log correlation.
    request_id = uuid.uuid4().hex
    request.state.request_id = request_id
    start = time.monotonic()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        elapsed = time.monotonic() - start
        route = request.scope.get("route")
        route_name = getattr(route, "path", None) or "unmatched"
        # Auth failures may occur before routing; do not put arbitrary URL
        # segments in logs or metrics labels.
        label = f"{request.method} {route_name} {status_code // 100}xx"
        with _METRICS_LOCK:
            _REQUEST_METRICS[label][0] += 1
            _REQUEST_METRICS[label][1] += elapsed
        logger.info(json.dumps({"event": "http_request", "request_id": request_id,
                                "route": route_name, "method": request.method,
                                "status": status_code, "duration_ms": round(elapsed * 1000, 2)}))


@app.middleware("http")
async def block_memory_until_verified(request: Request, call_next):
    if request.url.path.startswith("/api/v1/memory/") and getattr(request.app.state, "verification_status", "not_started") != "ready":
        status = getattr(request.app.state, "verification_status", "not_started")
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content={"detail": {"status": "not_ready", "verification": status}})
    return await call_next(request)

app.include_router(endpoints.router, prefix="/api/v1/memory", tags=["Memory"])

@app.get("/api/v1/health")
def health_check(request: Request):
    status = getattr(request.app.state, "verification_status", "not_started")
    if status != "ready":
        raise HTTPException(status_code=503, detail={"status": "not_ready", "verification": status})
    service = request.app.state.memory_service
    backend = service.embedding_backend or "configured_model"
    return {"status": "ok", "embedding_backend": backend,
            "vector_enabled": service.vector_enabled,
            "embedding_model": service.config.get("embedding_model") if service.vector_enabled and backend != "dummy" else None}

@app.get("/api/v1/metrics")
def metrics(request: Request):
    """Private operator telemetry; no public per-tenant usage feed."""
    from fastapi.security import HTTPAuthorizationCredentials
    from app.core.security import require_auth_context, require_scope, require_role
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    ctx = require_auth_context(HTTPAuthorizationCredentials(scheme="Bearer", credentials=header[7:]),
                               request=request)
    require_scope(ctx, "memory.admin")
    require_role(ctx, "admin")
    if getattr(request.app.state, "verification_status", "not_started") != "ready":
        raise HTTPException(status_code=503, detail="Memory service not ready")
    service = request.app.state.memory_service
    with service.db.get_cursor() as cursor:
        access_count = cursor.execute("SELECT count(*) FROM logs_access").fetchone()[0]
    return {"verification": request.app.state.verification_status,
            "vectors": service.vector_store.total if service.vector_store else 0,
            "access_rows": access_count, "requests": _metrics_snapshot()}


@app.get("/api/v1/live")
def liveness_check():
    return {"status": "alive"}

@app.get("/")
def read_root():
    return {"status": "Memory service is running"}
