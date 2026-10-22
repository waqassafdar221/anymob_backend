"""Durable outbox delivery and scheduled lifecycle work; no browser request required."""

import asyncio
import hashlib
import io
import logging
import smtplib
from datetime import timedelta
from email.message import EmailMessage

from celery import Celery
from PIL import Image, ImageOps
from sqlalchemy import select, update

from anymob.config import settings
from anymob.db import Session, engine, now
from anymob.models import AuditLog, Classified, Order, Outbox, Shop, Subscription, Upload, User

logger = logging.getLogger("anymob.worker")
celery = Celery("anymob", broker=settings().redis_url, backend=settings().redis_url)
celery.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_reject_on_worker_lost=True,
    broker_connection_retry_on_startup=True,
    beat_schedule={
        "outbox": {"task": "anymob.outbox", "schedule": 10.0},
        "lifecycle": {"task": "anymob.lifecycle", "schedule": 60.0},
    },
)


async def process_media(db, id):
    from anymob.routes.media import s3

    upload = await db.get(Upload, id)
    if not upload or upload.status == "ready":
        return
    client = s3()
    cfg = settings()
    response = await asyncio.to_thread(client.get_object, Bucket=cfg.s3_bucket, Key=upload.object_key)
    raw = await asyncio.to_thread(response["Body"].read, 10 * 1024 * 1024 + 1)
    if len(raw) != upload.size or len(raw) > 10 * 1024 * 1024:
        upload.status = "rejected"
        return
    content_type = upload.content_type
    try:
        if content_type == "application/pdf":
            if upload.purpose != "verification" or not raw.startswith(b"%PDF-"):
                raise ValueError("Invalid PDF")
            # Private downloads use attachment disposition, never inline active content.
            extension = "pdf"
        else:
            Image.MAX_IMAGE_PIXELS = 30_000_000
            with Image.open(io.BytesIO(raw)) as source:
                source.verify()
            with Image.open(io.BytesIO(raw)) as source:
                image = ImageOps.exif_transpose(source).convert("RGB")
                image.thumbnail((2400, 2400))
                output = io.BytesIO()
                image.save(output, "WEBP", quality=88)
                raw = output.getvalue()
                upload.data = {"width": image.width, "height": image.height}
            content_type, extension = "image/webp", "webp"
    except (ValueError, OSError, Image.DecompressionBombError):
        upload.status = "rejected"
        return
    key = f"processed/{upload.purpose}/{upload.id}.{extension}"
    await asyncio.to_thread(
        client.put_object,
        Bucket=cfg.s3_bucket,
        Key=key,
        Body=raw,
        ContentType=content_type,
        ContentDisposition="attachment" if extension == "pdf" else "inline",
    )
    old = upload.object_key
    upload.object_key, upload.status, upload.checksum = key, "ready", hashlib.sha256(raw).hexdigest()
    await asyncio.to_thread(client.delete_object, Bucket=cfg.s3_bucket, Key=old)


def send_email(payload, event_key):
    cfg = settings()
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = cfg.smtp_from, payload["to"], payload["subject"]
    message["Message-ID"] = f"<{hashlib.sha256(event_key.encode()).hexdigest()}@anymob>"
    message.set_content(payload["body"])
    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=15) as smtp:
        if cfg.smtp_starttls:
            smtp.starttls()
        if cfg.smtp_username:
            smtp.login(cfg.smtp_username, cfg.smtp_password)
        smtp.send_message(message)


async def drain_outbox():
    # Row locks with SKIP LOCKED allow multiple workers. A crash retries unsent events.
    async with Session.begin() as db:
        rows = (
            await db.scalars(
                select(Outbox)
                .where(Outbox.sent_at.is_(None), Outbox.available_at <= now(), Outbox.attempts < 10)
                .order_by(Outbox.created_at)
                .limit(20)
                .with_for_update(skip_locked=True)
            )
        ).all()
        for row in rows:
            row.attempts += 1
            try:
                if row.kind == "email":
                    await asyncio.to_thread(send_email, row.payload, row.event_key)
                elif row.kind == "media":
                    await process_media(db, row.payload["uploadId"])
                elif row.kind == "sms":
                    if settings().sms_provider != "development" or settings().environment == "production":
                        raise RuntimeError("SMS adapter not configured")
                    # Development SMS is delivered to the local mail inbox, never logged with its code.
                    await asyncio.to_thread(
                        send_email,
                        {"to": "sms@anymob.local", "subject": "Development SMS", "body": row.payload["body"]},
                        row.event_key,
                    )
                else:
                    raise RuntimeError("Unsupported outbox event")
                row.sent_at, row.last_error = now(), None
            except Exception as exc:
                row.last_error = type(exc).__name__
                row.available_at = now() + timedelta(seconds=min(3600, 2**row.attempts * 10))
                logger.warning(
                    "Outbox delivery failed: id=%s attempt=%s error=%s", row.id, row.attempts, row.last_error
                )
    return len(rows)


async def lifecycle():
    from anymob.common import config, record
    from anymob.schemas import OrderTransition
    from anymob.services.commerce import cancel_order, transition_order

    async with Session.begin() as db:
        rules = await config(db, "commerce")
        await db.execute(
            update(Classified)
            .where(Classified.status == "active", Classified.expires_at < now())
            .values(status="expired")
        )
        await db.execute(
            update(User)
            .where(User.status == "suspended", User.suspended_until <= now())
            .values(status="active", suspended_until=None)
        )
        # Single lifecycle lock makes beat retries deterministic and avoids lock order conflicts.
        from anymob.common import advisory_lock

        await advisory_lock(db, "worker:lifecycle")
        ids = (
            await db.execute(
                select(Order.id, Order.shop_id)
                .where(Order.status.in_(["pending", "ready_for_pickup", "shipped"]))
                .order_by(Order.shop_id, Order.id)
                .limit(1000)
            )
        ).all()
        for id, shop_id in ids:
            await db.scalar(select(Shop).where(Shop.id == shop_id).with_for_update())
            order = await db.scalar(select(Order).where(Order.id == id).with_for_update())
            before = record(order)
            action = None
            if order.status in {"pending", "ready_for_pickup"} and order.expires_at <= now():
                await cancel_order(
                    db, order, "system", "Automatic cancellation: confirmation or collection window expired"
                )
                action = "order.auto_cancelled"
            elif order.status == "shipped":
                shipped = next(
                    (event for event in reversed(order.timeline) if event["status"] == "shipped"), None
                )
                from datetime import datetime

                if shipped and datetime.fromisoformat(shipped["at"]) <= now() - timedelta(
                    days=rules["autoCompleteDays"]
                ):
                    await transition_order(
                        db,
                        order,
                        OrderTransition(
                            status="delivered", note="Automatically completed after the shipping window"
                        ),
                        "system",
                    )
                    action = "order.auto_completed"
            if action:
                db.add(
                    AuditLog(
                        action=action,
                        target_type="orders",
                        target_id=id,
                        before=before,
                        after=record(order),
                        details="Scheduled lifecycle worker",
                    )
                )
        for sub in (
            await db.scalars(
                select(Subscription)
                .where(Subscription.status == "active", Subscription.expires_at <= now())
                .with_for_update()
            )
        ).all():
            sub.status = "expired"
            shop = await db.get(Shop, sub.shop_id)
            if shop.plan_id == sub.plan_id:
                shop.plan_id = None


async def run_and_dispose(function):
    try:
        await function()
    finally:
        # Celery invokes a fresh event loop per task; don't carry asyncpg pooled connections between loops.
        await engine.dispose()


@celery.task(name="anymob.outbox")
def outbox_task():
    asyncio.run(run_and_dispose(drain_outbox))


@celery.task(name="anymob.lifecycle")
def lifecycle_task():
    asyncio.run(run_and_dispose(lifecycle))
