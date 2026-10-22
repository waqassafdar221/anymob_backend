from typing import Literal

import boto3
from botocore.config import Config
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from pydantic import Field
from sqlalchemy import or_, select
from starlette.concurrency import run_in_threadpool

from anymob.common import DB, config, fail, get, record
from anymob.config import settings
from anymob.db import now, uid
from anymob.models import Conversation, Message, Outbox, Upload
from anymob.schemas import Input
from anymob.security import Actor, current_user, throttle

router = APIRouter(tags=["Media"])


def s3(public=False):
    cfg = settings()
    return boto3.client(
        "s3",
        endpoint_url=cfg.s3_public_endpoint if public else cfg.s3_endpoint,
        aws_access_key_id=cfg.s3_access_key,
        aws_secret_access_key=cfg.s3_secret_key,
        region_name=cfg.s3_region,
        # Optional checksums are off because S3-compatible stores such as R2 support only a subset of them.
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


class UploadInput(Input):
    purpose: Literal["catalog", "verification", "message"]
    content_type: Literal["image/jpeg", "image/png", "image/webp", "application/pdf"]
    size: int = Field(ge=1, le=10 * 1024 * 1024)


@router.post("/uploads", status_code=201)
async def initiate_upload(data: UploadInput, actor: Actor, db: DB):
    await throttle("upload:" + actor.id, 60, 3600)
    if data.purpose != "verification" and data.content_type == "application/pdf":
        fail(422, "Only verification documents may be PDF files")
    if data.purpose == "verification" and actor.role != "vendor":
        fail(403, "Vendor access required")
    row = Upload(id=uid(), owner_id=actor.id, object_key=f"incoming/{actor.id}/{uid()}", **data.model_dump())
    db.add(row)
    await db.flush()
    client = s3(public=True)
    # Presigned PUT rather than POST: R2 does not support form uploads. Content-Type and Content-Length are
    # signed, so storage rejects a different type or size; the browser sets Content-Length from the body.
    url = await run_in_threadpool(
        client.generate_presigned_url,
        "put_object",
        Params={
            "Bucket": settings().s3_bucket,
            "Key": row.object_key,
            "ContentType": data.content_type,
            "ContentLength": data.size,
        },
        ExpiresIn=600,
    )
    return {"upload": record(row), "put": {"url": url, "headers": {"Content-Type": data.content_type}}}


@router.post("/uploads/{id}/complete")
async def complete_upload(id: str, actor: Actor, db: DB):
    row = await get(db, Upload, id, lock=True)
    if row.owner_id != actor.id:
        fail(404, "Upload not found")
    if row.status == "pending":
        row.status = "processing"
        db.add(Outbox(kind="media", payload={"uploadId": id}, event_key="media:" + id, available_at=now()))
    return record(row)


@router.get("/uploads/{id}")
async def upload_status(id: str, actor: Actor, db: DB):
    row = await get(db, Upload, id)
    if row.owner_id != actor.id:
        fail(404, "Upload not found")
    return {**record(row), "url": "/api/v1/media/" + row.id if row.status == "ready" else None}


@router.get("/media/{id}")
async def media(id: str, request: Request, db: DB):
    row = await get(db, Upload, id)
    if row.status != "ready":
        fail(404, "Media is not ready")
    if row.purpose != "catalog":
        actor = await current_user(request, db)
        allowed = actor.id == row.owner_id
        if row.purpose == "verification" and actor.role == "admin":
            allowed = (
                (await config(db, "team"))["permissions"]
                .get(actor.admin_role, {})
                .get("verify_stores", False)
            )
        if row.purpose == "message" and not allowed:
            allowed = bool(
                await db.scalar(
                    select(Message.id)
                    .join(Conversation)
                    .where(
                        Message.attachments.contains(["/api/v1/media/" + id]),
                        Message.hidden.is_(False),
                        or_(Conversation.buyer_id == actor.id, Conversation.seller_id == actor.id),
                    )
                    .limit(1)
                )
            )
        if not allowed:
            fail(404, "Media not found")
    client = s3(public=True)
    url = await run_in_threadpool(
        client.generate_presigned_url,
        "get_object",
        Params={"Bucket": settings().s3_bucket, "Key": row.object_key},
        ExpiresIn=120,
    )
    return RedirectResponse(
        url,
        headers={
            "Cache-Control": "private, no-store" if row.purpose != "catalog" else "public, max-age=60",
            "X-Content-Type-Options": "nosniff",
        },
    )
