import asyncio
import logging
from datetime import datetime, timedelta, timezone
import os
from app.core.database import DatabaseManager

logger = logging.getLogger(__name__)

async def cleanup_expired_memories(db: DatabaseManager, interval_seconds: int = 3600):
    """
    Background task: Delete expired session/working memories.

    Args:
        db: DatabaseManager instance
        interval_seconds: How often to run (default: 1 hour)
    """
    logger.info(f"Expiration cleanup task started (interval: {interval_seconds}s)")

    while True:
        try:
            await asyncio.sleep(interval_seconds)

            now = datetime.utcnow().isoformat()

            # Delete expired session memories
            with db.get_cursor() as cur:
                cur.execute("DELETE FROM logs_session WHERE expires_at IS NOT NULL AND expires_at < ?", (now,))
                session_deleted = cur.rowcount

                # Delete expired working memories
                cur.execute("DELETE FROM working_memory WHERE expires_at IS NOT NULL AND expires_at < ?", (now,))
                working_deleted = cur.rowcount

            # Keep the immutable audit chain intact. Only the access index
            # (which may be rebuilt independently) has a finite retention.
            retention_days = int(os.getenv("PINAK_ACCESS_LOG_RETENTION_DAYS", "90"))
            if retention_days < 1:
                raise ValueError("PINAK_ACCESS_LOG_RETENTION_DAYS must be positive")
            access_deleted = await asyncio.to_thread(
                db.prune_access_events,
                datetime.now(timezone.utc) - timedelta(days=retention_days),
            )
            revoked_deleted = await asyncio.to_thread(db.prune_expired_jti)
            if access_deleted or revoked_deleted:
                logger.info("Pruned %d access rows and %d expired token ids", access_deleted, revoked_deleted)
            total = session_deleted + working_deleted

            if total > 0:
                logger.info(f"Expired {total} memories (session: {session_deleted}, working: {working_deleted})")

        except asyncio.CancelledError:
            logger.info("Cleanup task cancelled")
            break
        except Exception as e:
            logger.error(f"Cleanup task failed: {e}", exc_info=True)
            # Continue even if failed
