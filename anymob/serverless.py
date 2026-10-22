"""Background work for hosts without a Celery worker or beat, such as Vercel Functions."""

import hmac
import logging
import time
from contextvars import ContextVar

from fastapi import APIRouter, Request
from sqlalchemy import event

from anymob.common import fail
from anymob.config import settings
from anymob.models import Outbox

logger = logging.getLogger("anymob.serverless")
router = APIRouter(include_in_schema=False)
queued: ContextVar[list | None] = ContextVar("outbox_queued", default=None)


@event.listens_for(Outbox, "after_insert")
def note_outbox_insert(mapper, connection, target):
    rows = queued.get()
    if rows is not None:
        rows.append(target.id)


class InlineOutbox:
    """Holds each HTTP response until the request has finished, including the transaction commit that FastAPI
    runs after the response is sent, then delivers any outbox events the request queued before responding.
    A response is never released when the request fails after sending it, so a failed commit becomes a 500."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        messages, rows = [], []
        token = queued.set(rows)

        async def hold(message):
            messages.append(message)

        try:
            await self.app(scope, receive, hold)
        finally:
            queued.reset(token)
        if rows:
            from anymob.worker import drain_outbox

            try:
                await drain_outbox()
            except Exception:
                # Undelivered rows stay in the outbox for the scheduled run.
                logger.exception("Inline outbox delivery failed")
        for message in messages:
            await send(message)


@router.get("/internal/cron/daily")
async def daily_jobs(request: Request):
    secret = settings().cron_secret
    supplied = request.headers.get("authorization", "")
    if not secret or not hmac.compare_digest(supplied.encode(), f"Bearer {secret}".encode()):
        fail(404, "Not found")
    from anymob.worker import drain_outbox, lifecycle

    await lifecycle()
    # Retry anything inline delivery missed; failing rows are rescheduled into the future, so this ends.
    deadline = time.monotonic() + 120
    delivered = 0
    while time.monotonic() < deadline and (batch := await drain_outbox()):
        delivered += batch
    return {"status": "ok", "outboxProcessed": delivered}
