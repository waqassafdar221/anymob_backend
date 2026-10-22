from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field
from sqlalchemy import select, update

from anymob.common import DB, advisory_lock, audit, fail, get, record
from anymob.db import now
from anymob.models import (
    AuthSession,
    Classified,
    InternalNote,
    Message,
    Product,
    Report,
    Review,
    Shop,
    User,
)
from anymob.schemas import Input, Note
from anymob.security import Actor, has_permission, permission, throttle
from anymob.services.admin_reports import day_bounds, matches, report_detail, report_rows, report_stats
from anymob.services.commerce import notify

router = APIRouter(tags=["Reports"])
Moderator = Annotated[object, Depends(permission("moderate_catalog"))]
TARGETS = {
    "ad": Classified,
    "product": Product,
    "shop": Shop,
    "user": User,
    "review": Review,
    "message": Message,
}


async def target_owner(db, kind, id):
    obj = await get(db, TARGETS[kind], id)
    if kind == "product":
        return obj, (await get(db, Shop, obj.shop_id)).owner_id
    owner_field = {
        "ad": "seller_id",
        "shop": "owner_id",
        "user": "id",
        "review": "author_id",
        "message": "sender_id",
    }[kind]
    return obj, getattr(obj, owner_field)


class ReportInput(Input):
    target_type: Literal["ad", "product", "shop", "user", "review", "message"]
    target_id: str
    reason: Literal[
        "scam", "counterfeit", "wrong_price", "not_as_described", "offensive_content", "spam", "other"
    ]
    details: str = Field(min_length=5, max_length=4000)


@router.post("/reports", status_code=201)
async def create_report(data: ReportInput, actor: Actor, db: DB):
    await throttle("report:" + actor.id, 10, 3600)
    target, owner_id = await target_owner(db, data.target_type, data.target_id)
    if data.target_type == "message":
        from anymob.models import Conversation

        conv = await db.get(Conversation, target.conversation_id)
        if actor.id not in {conv.buyer_id, conv.seller_id}:
            fail(404, "Message not found")
    await advisory_lock(db, f"report:{actor.id}:{data.target_type}:{data.target_id}")
    if await db.scalar(
        select(Report.id).where(
            Report.reporter_id == actor.id,
            Report.target_type == data.target_type,
            Report.target_id == data.target_id,
            Report.status.in_(["open", "reviewing"]),
        )
    ):
        fail(409, "You have already reported this target")
    row = Report(**data.model_dump(), reporter_id=actor.id, owner_id=owner_id)
    db.add(row)
    await db.flush()
    return record(row)


@router.get("/admin/reports")
async def reports(
    actor: Moderator,
    db: DB,
    status: Literal["open", "reviewing", "resolved", "dismissed"] | None = None,
    target_type: str | None = Query(None, alias="targetType"),
    reason: str | None = None,
    priority: Literal["low", "medium", "high"] | None = None,
    start: date | None = Query(None, alias="from"),
    end: date | None = Query(None, alias="to"),
    search: str = "",
):
    if start and end and start > end:
        fail(422, "Start date must be on or before the end date.")
    first, last = day_bounds(start and start.isoformat(), end and end.isoformat())
    latest = (await db.scalars(select(Report).order_by(Report.created_at.desc()).limit(2000))).all()
    rows = [
        row
        for row in await report_rows(db, latest)
        if matches(
            row,
            status=None,
            target_type=target_type,
            reason=reason,
            priority_filter=priority,
            start=first,
            end=last,
            search=search.strip().lower(),
        )
    ]
    rank = {"high": 0, "medium": 1, "low": 2}
    rows.sort(key=lambda row: row["createdAt"], reverse=True)
    rows.sort(key=lambda row: rank[row["priority"]])
    counts = {
        name: sum(row["status"] == name for row in rows)
        for name in ("open", "reviewing", "resolved", "dismissed")
    }
    if status:
        rows = [row for row in rows if row["status"] == status]
    return {"rows": rows, "stats": await report_stats(db), "counts": counts}


@router.get("/admin/reports/stats")
async def stats(actor: Moderator, db: DB):
    return await report_stats(db)


@router.get("/admin/reports/{id}")
async def detail(id: str, actor: Moderator, db: DB):
    return await report_detail(db, await get(db, Report, id), actor)


@router.post("/admin/reports/{id}/assign")
async def assign(id: str, actor: Moderator, request: Request, db: DB):
    row = await get(db, Report, id)
    await advisory_lock(db, f"reports:{row.target_type}:{row.target_id}")
    group = (
        await db.scalars(
            select(Report)
            .where(
                Report.target_type == row.target_type,
                Report.target_id == row.target_id,
                Report.status.in_(["open", "reviewing"]),
            )
            .with_for_update()
        )
    ).all()
    if not group:
        fail(409, "This target has no open reports to assign.")
    for item in group:
        before = record(item)
        item.assigned_admin_id, item.status = actor.id, "reviewing"
        await audit(db, actor, request, "report.assigned", item, before=before)
    await db.flush()
    return await report_detail(db, row, actor)


@router.post("/admin/reports/{id}/notes", status_code=201)
async def report_note(id: str, data: Note, actor: Moderator, request: Request, db: DB):
    row = await get(db, Report, id)
    note = InternalNote(
        author_id=actor.id, target_type=row.target_type, target_id=row.target_id, body=data.note.strip()
    )
    db.add(note)
    await db.flush()
    await audit(db, actor, request, "report.note_added", note, note=data.note.strip())
    return await report_detail(db, row, actor)


class ResolveReport(Note):
    # "suspend" is the older name for "suspend_user".
    action: Literal["dismiss", "remove", "warn", "suspend", "suspend_user", "suspend_store", "ban"]
    message: str = Field(default="", max_length=4000)


CAPABILITY = {
    "remove": "remove",
    "warn": "warn",
    "suspend_user": "suspendUser",
    "suspend_store": "suspendStore",
    "ban": "ban",
}
OWNER_PAGES = {"product": "/vendor/products", "shop": "/vendor/store-settings", "ad": "/my-ads"}


@router.post("/admin/reports/{id}/resolve")
async def resolve_report(id: str, data: ResolveReport, actor: Moderator, request: Request, db: DB):
    row = await get(db, Report, id)
    action = "suspend_user" if data.action == "suspend" else data.action
    note, message = data.note.strip(), data.message.strip()
    if action in {"suspend_user", "ban"} and not await has_permission(db, actor, "manage_users"):
        fail(403, "Your role does not permit suspending or banning members")
    if (
        action == "suspend_store" or action == "remove" and row.target_type == "shop"
    ) and not await has_permission(db, actor, "verify_stores"):
        fail(403, "Your role does not permit store enforcement")
    if action == "warn" and not message:
        fail(422, "Write a warning message of 1–4,000 characters.")
    await advisory_lock(db, f"reports:{row.target_type}:{row.target_id}")
    group = (
        await db.scalars(
            select(Report)
            .where(
                Report.target_type == row.target_type,
                Report.target_id == row.target_id,
                Report.status.in_(["open", "reviewing"]),
            )
            .with_for_update()
        )
    ).all()
    if not group:
        fail(409, "These reports have already been closed.")
    current = await report_detail(db, row, actor)
    if action != "dismiss" and not current["capabilities"][CAPABILITY[action]]:
        fail(409, "This action is unavailable for the reported content or owner.")
    # Every check happens before any write.
    target, owner_id = await target_owner(db, row.target_type, row.target_id)
    if action == "remove":
        before = record(target)
        if row.target_type in {"product", "ad"}:
            target.status = "archived"
        elif row.target_type in {"review", "message"}:
            target.hidden = True
        else:
            # Soft delete: the store leaves public view; orders, payouts and audit history remain.
            target.is_deleted, target.is_featured = True, False
        await audit(db, actor, request, "content.removed", target, before=before, note=note)
    elif action in {"suspend_user", "ban"}:
        owner = await get(db, User, owner_id, lock=True)
        before = record(owner)
        owner.status, owner.suspended_until = ("banned" if action == "ban" else "suspended"), None
        await db.execute(update(AuthSession).where(AuthSession.user_id == owner.id).values(revoked_at=now()))
        await audit(
            db,
            actor,
            request,
            "user.banned" if action == "ban" else "user.suspended",
            owner,
            before=before,
            note=note,
        )
    elif action == "suspend_store":
        shop = await get(db, Shop, current["target"]["shopId"], lock=True)
        before = record(shop)
        shop.is_suspended = True
        await audit(db, actor, request, "store.suspend", shop, before=before, note=note)
    if action != "dismiss":
        await notify(
            db,
            owner_id,
            "Content policy warning" if action == "warn" else "Moderation decision",
            message if action == "warn" else note,
            OWNER_PAGES.get(row.target_type, ""),
        )
    for item in group:
        before = record(item)
        item.status = "dismissed" if action == "dismiss" else "resolved"
        item.resolved_at = now()
        item.assigned_admin_id = item.assigned_admin_id or actor.id
        item.data = {**item.data, "resolutionAction": action, "resolutionNote": note, "resolvedBy": actor.id}
        await audit(
            db,
            actor,
            request,
            "report.dismissed" if action == "dismiss" else "report.resolved",
            item,
            before=before,
            note=note,
        )
    await db.flush()
    return await report_detail(db, row, actor)
