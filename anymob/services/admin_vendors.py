"""Admin views of stores: list rows with 30-day performance, and the full store dossier."""

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select

from anymob.common import config, record
from anymob.db import now
from anymob.models import AuditLog, Order, Payout, Plan, Product, Report, ReturnRequest, Review, Shop, User
from anymob.services.catalog import product_views, shop_view
from anymob.services.commerce import order_views

DOCUMENT_KINDS = ["cnicFront", "cnicBack", "shopPhoto", "businessProof"]


def merchandise(order) -> Decimal:
    return max(Decimal(0), order.subtotal - order.discount)


def commission_for(shop, plan, default) -> float:
    """The store's own rate, then its plan's, then the platform default."""
    if shop.commission_override is not None:
        return float(shop.commission_override)
    if plan and plan.commission_rate is not None:
        return float(plan.commission_rate)
    return float(default)


async def vendor_row(db, shop: Shop) -> dict:
    return (await vendor_rows(db, [shop]))[0]


async def vendor_rows(db, shops) -> list[dict]:
    """Stores with owner, plan and 30-day performance, in a fixed number of queries."""
    shops = list(shops)
    if not shops:
        return []
    ids = [s.id for s in shops]
    since = now() - timedelta(days=30)
    owners = {
        u.id: u
        for u in (await db.scalars(select(User).where(User.id.in_({s.owner_id for s in shops})))).all()
    }
    plans = {p.id: p for p in (await db.scalars(select(Plan))).all()}
    recent: dict[str, list] = {}
    for order in (
        await db.scalars(select(Order).where(Order.shop_id.in_(ids), Order.created_at >= since))
    ).all():
        recent.setdefault(order.shop_id, []).append(order)
    products = dict(
        (
            await db.execute(
                select(Product.shop_id, func.count())
                .where(Product.shop_id.in_(ids))
                .group_by(Product.shop_id)
            )
        ).all()
    )
    default = (await config(db, "revenue"))["defaultCommission"]
    rows = []
    for shop in shops:
        orders = recent.get(shop.id, [])
        sales = [o for o in orders if o.status not in {"cancelled", "returned"}]
        owner, plan = owners.get(shop.owner_id), plans.get(shop.plan_id)
        rows.append(
            {
                **shop_view(shop),
                "commissionOverride": float(shop.commission_override)
                if shop.commission_override is not None
                else None,
                "owner": record(owner) if owner else None,
                "plan": record(plan) if plan else None,
                "commission": commission_for(shop, plan, default),
                "productsCount": products.get(shop.id, 0),
                "ordersCount": len(orders),
                "gmv": float(sum((merchandise(o) for o in sales), Decimal(0))),
                "cancellationRate": (sum(o.status == "cancelled" for o in orders) / len(orders) * 100)
                if orders
                else 0,
                "onVacation": shop.is_on_vacation,
                "submittedAt": shop.data.get("verificationSubmittedAt"),
            }
        )
    return rows


def documents(shop: Shop) -> dict:
    status = {"verified": "verified", "rejected": "rejected"}.get(shop.verification_status, "pending")
    return {
        kind: {"name": kind, "url": f"/api/v1/media/{upload_id}", "type": "image/webp", "status": status}
        for kind, upload_id in shop.data.get("verificationDocuments", {}).items()
    }


async def vendor_detail(db, shop: Shop) -> dict:
    since = now() - timedelta(days=30)
    orders = (
        await db.scalars(
            select(Order).where(Order.shop_id == shop.id).order_by(Order.created_at.desc()).limit(200)
        )
    ).all()
    recent = [o for o in orders if o.created_at >= since]
    confirmations = []
    for order in recent:
        confirmed = next((e for e in order.timeline if e.get("status") == "confirmed"), None)
        if confirmed:
            from datetime import datetime

            at = datetime.fromisoformat(confirmed["at"].replace("Z", "+00:00"))
            confirmations.append(max(0.0, (at - order.created_at).total_seconds() / 3600))
    returned = set(
        (await db.scalars(select(ReturnRequest.order_id).where(ReturnRequest.shop_id == shop.id))).all()
    )
    daily = []
    for offset in range(30):
        day = (now() - timedelta(days=29 - offset) + timedelta(hours=5)).date().isoformat()
        daily.append(
            {
                "date": day,
                "gmv": float(
                    sum(
                        (
                            merchandise(o)
                            for o in recent
                            if (o.created_at + timedelta(hours=5)).date().isoformat() == day
                            and o.status not in {"cancelled", "returned"}
                        ),
                        Decimal(0),
                    )
                ),
            }
        )
    products = (
        await db.scalars(
            select(Product).where(Product.shop_id == shop.id).order_by(Product.created_at.desc()).limit(200)
        )
    ).all()
    logs = (
        await db.scalars(
            select(AuditLog)
            .where(AuditLog.target_type.in_(["shops", "shop"]), AuditLog.target_id == shop.id)
            .order_by(AuditLog.created_at.desc())
            .limit(50)
        )
    ).all()
    method = (shop.payout_mask.split(":")[0] or "bank_transfer") if shop.payout_mask else "bank_transfer"
    return {
        "shop": await vendor_row(db, shop),
        "documents": documents(shop),
        "rejectionReason": shop.data.get("verificationNote", ""),
        "documentReviews": shop.data.get("documentReviews", {}),
        "notes": shop.data.get("adminNotes", ""),
        "payout": {
            "method": method,
            "bankName": "",
            "title": "",
            "account": shop.payout_mask.split(":", 1)[-1].strip() if shop.payout_mask else "Not configured",
        },
        "products": await product_views(db, products),
        "orders": await order_views(db, orders),
        "payouts": [
            record(p, exclude={"account_snapshot"})
            for p in (
                await db.scalars(
                    select(Payout).where(Payout.shop_id == shop.id).order_by(Payout.created_at.desc())
                )
            ).all()
        ],
        "reviews": [
            {**record(r), "isVerifiedPurchase": True}
            for r in (await db.scalars(select(Review).where(Review.shop_id == shop.id))).all()
        ],
        "reports": [
            record(r)
            for r in (
                await db.scalars(
                    select(Report).where(Report.target_type == "shop", Report.target_id == shop.id)
                )
            ).all()
        ],
        "activity": [
            {
                "id": log.id,
                "actorId": log.admin_id or "",
                "action": log.action,
                "targetType": "shop",
                "targetId": log.target_id,
                "summary": log.details or log.action.replace(".", " ").replace("_", " "),
                "createdAt": log.created_at.isoformat(),
                "ip": log.ip,
            }
            for log in logs
        ],
        "performance": {
            "confirmationHours": sum(confirmations) / len(confirmations) if confirmations else None,
            "returnRate": (sum(o.id in returned for o in recent) / len(recent) * 100) if recent else 0,
            "daily": daily,
        },
    }
