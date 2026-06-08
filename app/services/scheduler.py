from __future__ import annotations

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app.database import session_scope
from app.models import Draft, ScheduledPost
from app.services.autopilot import AutopilotService
from app.services.publisher import Publisher

logger = logging.getLogger(__name__)


def create_scheduler(publisher: Publisher, autopilot: AutopilotService, timezone_name: str) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=timezone_name)
    scheduler.add_job(_publish_due_posts, "interval", seconds=30, args=[publisher], max_instances=1)
    scheduler.add_job(_run_autopilot, "interval", seconds=60, args=[autopilot], max_instances=1)
    return scheduler


async def _publish_due_posts(publisher: Publisher) -> None:
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(ScheduledPost)
                .where(ScheduledPost.status == "pending", ScheduledPost.run_at <= datetime.now(timezone.utc).replace(tzinfo=None))
                .order_by(ScheduledPost.run_at.asc())
                .limit(10)
            )
        ).scalars().all()

        for scheduled in rows:
            draft = await session.get(Draft, scheduled.draft_id)
            if draft is None or draft.status not in {"draft", "scheduled"}:
                scheduled.status = "cancelled"
                continue
            try:
                await publisher.publish_draft(session, draft)
                scheduled.status = "published"
            except Exception as exc:
                logger.exception("Scheduled post failed: %s", exc)
                scheduled.status = "failed"
                scheduled.error = str(exc)


async def _run_autopilot(autopilot: AutopilotService) -> None:
    async with session_scope() as session:
        await autopilot.tick(session)
