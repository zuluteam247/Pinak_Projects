
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
