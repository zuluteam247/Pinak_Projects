
from fastapi import FastAPI, HTTPException, Request
from contextlib import asynccontextmanager
import asyncio
import logging
import sys
import os

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

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
    service = get_memory_service()
    app.state.verification_status = "verifying"
    skip_verify = os.getenv("PINAK_SKIP_VERIFY_ON_STARTUP", "false").lower() in ("1", "true", "yes")
    background_verify = os.getenv("PINAK_VERIFY_IN_BACKGROUND", "false").lower() in ("1", "true", "yes")
    verification_task = None
    if skip_verify:
        # Explicit bypass leaves readiness off: no verification means no proof of readiness.
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
        # asyncio.to_thread cannot safely interrupt an in-progress rebuild.
        # Wait before saving the vector snapshot or closing the service.
        if verification_task is not None:
            await verification_task
        if getattr(service, "vector_store", None):
            service.vector_store.save()

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
    return {"status": "ok"}

@app.get("/api/v1/live")
def liveness_check():
    return {"status": "alive"}

@app.get("/")
def read_root():
    return {"status": "Memory service is running"}
