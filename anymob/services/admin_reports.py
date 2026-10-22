"""The admin report queue: reports grouped by target, target previews, owner history and allowed actions."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, tuple_

from anymob.common import record
from anymob.db import now
from anymob.models import (
    AuditLog,
    Classified,
    InternalNote,
    Message,
    Order,
    Product,
    Report,
    ReturnRequest,
    Review,
    Shop,
    User,
    Variant,
)
from anymob.security import has_permission

PKT = ZoneInfo("Asia/Karachi")
ACTIVE = {"open", "reviewing"}
AUDIT_TYPES = {
    "users": "user",
    "shops": "shop",
    "products": "product",
    "classifieds": "ad",
    "orders": "order",
    "returns": "return",
    "reports": "report",
    "reviews": "review",
    "messages": "message",
    "plans": "plan",
    "categories": "category",
    "brands": "brand",
    "coupons": "coupon",
    "featured_packages": "package",
    "payments": "transaction",
    "payouts": "payout",
    "settings": "settings",
}


def is_active(report: Report) -> bool:
    return report.status in ACTIVE


def priority(reports) -> str:
    """Urgency uses unresolved reports only; historical dismissals do not inflate it."""
    active = [r for r in reports if is_active(r)]
    basis = active or reports
    reasons = {r.reason for r in basis}
    if len(active) >= 5 or reasons & {"scam", "counterfeit", "prohibited_item"}:
        return "high"
    if len(active) >= 2 or reasons & {"not_as_described", "offensive_content", "wrong_price"}:
        return "medium"
    return "low"


def filter_reason(reason: str) -> str:
    if reason == "duplicate":
        return "spam"
    return "other" if reason in {"wrong_category", "prohibited_item"} else reason


def report_record(report: Report) -> dict:
    return {
        **record(report, exclude={"data"}),
        "resolvedBy": report.data.get("resolvedBy"),
        "resolutionNote": report.data.get("resolutionNote", ""),
        "resolutionAction": report.data.get("resolutionAction", ""),
    }


def rupees(value) -> str:
    return f"Rs {float(value or 0):,.0f}"


async def preview(db, kind: str, id: str, report_id: str = "") -> dict:
    result = {
        "id": id,
        "type": kind,
        "name": f"Unavailable {kind}",
        "description": "This content no longer exists.",
        "image": "",
        "images": [],
        "href": None,
        "ownerId": None,
        "shopId": None,
        "status": "removed",
        "exists": False,
        "attributes": {},
    }
    if kind == "shop" and (shop := await db.get(Shop, id)):
        rating = await db.scalar(
            select(func.avg(Review.rating)).where(Review.shop_id == id, Review.hidden.is_(False))
        )
        products = await db.scalar(select(func.count()).select_from(Product).where(Product.shop_id == id))
        status = (
            "removed" if shop.is_deleted else "suspended" if shop.is_suspended else shop.verification_status
        )
        result |= {
            "name": shop.name,
            "description": shop.data.get("description", ""),
            "image": shop.data.get("logo", ""),
            "images": [shop.data["coverImage"]] if shop.data.get("coverImage") else [],
            "href": f"/shop/{shop.slug}",
            "ownerId": shop.owner_id,
            "shopId": shop.id,
            "status": status,
            "exists": not shop.is_deleted,
            "attributes": {
                "City": shop.city,
                "Rating": f"{float(rating or 0):.1f} / 5",
                "Products": str(products),
            },
        }
    elif kind == "product" and (product := await db.get(Product, id)):
        shop = await db.get(Shop, product.shop_id)
        price = await db.scalar(
            select(func.min(Variant.price)).where(Variant.product_id == id, Variant.is_active.is_(True))
        )
        result |= {
            "name": product.name,
            "description": product.description,
            "image": (product.images or [""])[0],
            "images": product.images,
            "href": f"/shop/{shop.slug}/p/{product.slug}",
            "ownerId": shop.owner_id,
            "shopId": shop.id,
            "status": product.status,
            "exists": product.status != "archived",
            "attributes": {
                "Price": rupees(price),
                "Condition": product.data.get("condition", "new"),
                "Store": shop.name,
            },
        }
    elif kind == "ad" and (ad := await db.get(Classified, id)):
        result |= {
            "name": ad.title,
            "description": ad.description,
            "image": (ad.images or [""])[0],
            "images": ad.images,
            "href": f"/ads/{ad.slug}",
            "ownerId": ad.seller_id,
            "status": ad.status,
            "exists": ad.status != "archived",
            "attributes": {
                "Price": rupees(ad.price),
                "City": ad.city,
                "Condition": ad.data.get("condition", "used"),
            },
        }
    elif kind == "user" and (user := await db.get(User, id)):
        role = {"vendor": "Vendor", "admin": "Administrator"}.get(user.role, "Member")
        result |= {
            "name": user.full_name,
            "description": f"{role} in {user.city}",
            "image": user.avatar,
            "href": f"/admin/users?selected={user.id}",
            "ownerId": user.id,
            "status": user.status,
            "exists": True,
            "attributes": {
                "Role": user.role,
                "Joined": user.created_at.isoformat(),
                "Verified": "Yes" if user.is_verified else "No",
            },
        }
    elif kind == "review" and (review := await db.get(Review, id)):
        shop = await db.get(Shop, review.shop_id)
        author = await db.get(User, review.author_id)
        result |= {
            "name": review.title,
            "description": review.body,
            "image": author.avatar if author else "",
            "href": f"/shop/{shop.slug}?tab=reviews" if shop else None,
            "ownerId": review.author_id,
            "status": "removed" if review.hidden else "published",
            "exists": not review.hidden,
            "attributes": {
                "Rating": f"{review.rating} / 5",
                "Store": shop.name if shop else "Unavailable",
                "Verified purchase": "Yes",
            },
        }
    elif kind == "message" and (message := await db.get(Message, id)):
        sender = await db.get(User, message.sender_id)
        result |= {
            "name": f"Message from {sender.full_name if sender else 'Unavailable user'}",
            "description": message.body,
            "image": sender.avatar if sender else "",
            "images": message.attachments,
            # Private messages are inspected here; their threads keep participant-only access.
            "href": f"/admin/reports?selected={report_id}" if report_id else None,
            "ownerId": message.sender_id,
            "status": "removed" if message.hidden else "sent",
            "exists": not message.hidden,
            "attributes": {"Sent": message.created_at.isoformat()},
        }
    return result


async def names(db, ids) -> dict[str, User]:
    ids = {i for i in ids if i}
    return {u.id: u for u in (await db.scalars(select(User).where(User.id.in_(ids)))).all()} if ids else {}


async def report_rows(db, reports) -> list[dict]:
    """Each report with its target preview, the target's report group, reporter and assignee."""
    keys = {(r.target_type, r.target_id) for r in reports}
    groups: dict[tuple[str, str], list[Report]] = {}
    if keys:
        for row in (
            await db.scalars(select(Report).where(tuple_(Report.target_type, Report.target_id).in_(keys)))
        ).all():
            groups.setdefault((row.target_type, row.target_id), []).append(row)
    previews = {}
    for report in reports:
        key = (report.target_type, report.target_id)
        if key not in previews:
            previews[key] = await preview(db, *key, report_id=report.id)
    people = await names(db, [r.reporter_id for r in reports] + [r.assigned_admin_id for r in reports])
    rows = []
    for report in reports:
        group = groups.get((report.target_type, report.target_id), [report])
        reporter, admin = people.get(report.reporter_id), people.get(report.assigned_admin_id)
        rows.append(
            {
                **report_record(report),
                "target": previews[(report.target_type, report.target_id)],
                "priority": priority(group),
                "reportCount": len(group),
                "activeReportCount": sum(is_active(r) for r in group),
                "reporter": {
                    "id": report.reporter_id,
                    "name": reporter.full_name if reporter else "Unavailable user",
                    "avatar": reporter.avatar if reporter else "",
                },
                "assignedAdmin": {"id": admin.id, "name": admin.full_name} if admin else None,
                "filterReason": filter_reason(report.reason),
            }
        )
    return rows


def week_start() -> datetime:
    """Monday 00:00 in Pakistan; all user-facing report dates use Asia/Karachi."""
    today = now().astimezone(PKT).date()
    return datetime.combine(today - timedelta(days=today.weekday()), datetime.min.time(), PKT)


async def report_stats(db) -> dict:
    counts = dict((await db.execute(select(Report.status, func.count()).group_by(Report.status))).all())
    resolved = await db.scalar(
        select(func.count())
        .select_from(Report)
        .where(Report.status == "resolved", Report.resolved_at >= week_start())
    )
    average = await db.scalar(
        select(func.avg(func.extract("epoch", Report.resolved_at - Report.created_at))).where(
            Report.status.in_(["resolved", "dismissed"]), Report.resolved_at.is_not(None)
        )
    )
    return {
        "open": counts.get("open", 0),
        "reviewing": counts.get("reviewing", 0),
        "resolved": counts.get("resolved", 0),
        "dismissed": counts.get("dismissed", 0),
        "resolvedThisWeek": resolved,
        "averageResolutionHours": float(average) / 3600 if average is not None else None,
    }


async def report_detail(db, report: Report, actor) -> dict:
    group = (
        await db.scalars(
            select(Report)
            .where(Report.target_type == report.target_type, Report.target_id == report.target_id)
            .order_by(Report.created_at.desc())
        )
    ).all()
    target = await preview(db, report.target_type, report.target_id, report.id)
    owner = await db.get(User, target["ownerId"]) if target["ownerId"] else None
    shop = await db.get(Shop, target["shopId"]) if target["shopId"] else None
    if shop and shop.is_deleted:
        shop = None
    owned = (
        (
            await db.scalars(
                select(Report)
                .where(Report.owner_id == owner.id)
                .order_by(Report.created_at.desc())
                .limit(200)
            )
        ).all()
        if owner
        else []
    )
    previous = [r for r in owned if (r.target_type, r.target_id) != (report.target_type, report.target_id)]

    # The owner's moderation history: their account, stores and reported content, plus those reports.
    ids = {report.target_id, *(r.target_id for r in owned), *(r.id for r in owned), *(r.id for r in group)}
    if owner:
        ids |= {owner.id, *(await db.scalars(select(Shop.id).where(Shop.owner_id == owner.id))).all()}
    logs = (
        await db.scalars(
            select(AuditLog)
            .where(AuditLog.target_id.in_(ids))
            .order_by(AuditLog.created_at.desc())
            .limit(100)
        )
    ).all()
    notes = (
        await db.scalars(
            select(InternalNote)
            .where(InternalNote.target_type == report.target_type, InternalNote.target_id == report.target_id)
            .order_by(InternalNote.created_at)
        )
    ).all()
    people = await names(db, [log.admin_id for log in logs] + [note.author_id for note in notes])

    store_rates = None
    if shop:
        orders = (
            await db.scalars(
                select(Order).where(Order.shop_id == shop.id, Order.created_at >= now() - timedelta(days=30))
            )
        ).all()
        order_ids = [o.id for o in orders]
        disputes = (
            await db.scalar(
                select(func.count(func.distinct(ReturnRequest.order_id))).where(
                    ReturnRequest.order_id.in_(order_ids), ReturnRequest.disputed.is_(True)
                )
            )
            if order_ids
            else 0
        )
        cancellations = sum(o.status == "cancelled" for o in orders)
        store_rates = {
            "orders": len(orders),
            "cancellations": cancellations,
            "disputes": disputes,
            "cancellationRate": cancellations / len(orders) * 100 if orders else 0,
            "disputeRate": disputes / len(orders) * 100 if orders else 0,
        }

    admin_owner = bool(owner and owner.role == "admin")
    users_allowed = await has_permission(db, actor, "manage_users")
    stores_allowed = await has_permission(db, actor, "verify_stores")
    return {
        "target": target,
        "reports": await report_rows(db, group),
        "priority": priority(group),
        "activeReportCount": sum(is_active(r) for r in group),
        "owner": {
            "id": owner.id,
            "name": owner.full_name,
            "avatar": owner.avatar,
            "status": owner.status,
            "href": f"/admin/users?selected={owner.id}",
            "protected": admin_owner,
        }
        if owner
        else None,
        "previousReports": await report_rows(db, previous),
        "history": [
            {
                "id": log.id,
                "actorId": log.admin_id or "",
                "actorName": people[log.admin_id].full_name if log.admin_id in people else "System",
                "action": log.action,
                "targetType": AUDIT_TYPES.get(log.target_type, log.target_type),
                "targetId": log.target_id,
                "summary": log.details or log.action.replace(".", " ").replace("_", " "),
                "createdAt": log.created_at.isoformat(),
                "ip": log.ip,
            }
            for log in logs
        ],
        "storeRates": store_rates,
        "notes": [
            {
                "id": note.id,
                "actorId": note.author_id,
                "actorName": people[note.author_id].full_name
                if note.author_id in people
                else "Unavailable admin",
                "body": note.body,
                "createdAt": note.created_at.isoformat(),
            }
            for note in notes
        ],
        "capabilities": {
            "remove": target["exists"]
            and report.target_type != "user"
            and not admin_owner
            and (report.target_type != "shop" or stores_allowed),
            "warn": bool(owner) and not admin_owner,
            "suspendUser": bool(owner) and not admin_owner and owner.status == "active" and users_allowed,
            "suspendStore": bool(shop) and not shop.is_suspended and not admin_owner and stores_allowed,
            "ban": bool(owner) and not admin_owner and owner.status != "banned" and users_allowed,
        },
    }


def matches(row: dict, *, status, target_type, reason, priority_filter, start, end, search) -> bool:
    created = datetime.fromisoformat(row["createdAt"])
    text = f"{row['target']['name']} {row['details']} {row['reporter']['name']} {row['id']}".lower()
    return (
        (not status or row["status"] == status)
        and (not target_type or row["targetType"] == target_type)
        and (not reason or row["filterReason"] == reason)
        and (not priority_filter or row["priority"] == priority_filter)
        and (start is None or created >= start)
        and (end is None or created < end)
        and (not search or search in text)
    )


def day_bounds(start: str | None, end: str | None):
    """Inclusive Pakistan-time calendar days to an aware [start, end) range."""
    first = (
        datetime.combine(datetime.fromisoformat(start).date(), datetime.min.time(), PKT) if start else None
    )
    last = (
        datetime.combine(datetime.fromisoformat(end).date() + timedelta(days=1), datetime.min.time(), PKT)
        if end
        else None
    )
    return first, last
