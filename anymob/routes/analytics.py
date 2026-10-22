from datetime import UTC, datetime, time, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from sqlalchemy import func, select

from anymob.common import DB, client_ip, config, digest, fail, get, owned_shop
from anymob.db import now
from anymob.models import (
    AnalyticsEvent,
    Classified,
    Order,
    Payment,
    Payout,
    Product,
    Report,
    Shop,
    User,
)
from anymob.schemas import Input
from anymob.security import Actor, permission, throttle
from anymob.services.catalog import visible_shops

router = APIRouter(tags=["Analytics"])
Admin = Annotated[object, Depends(permission())]
FinanceAdmin = Annotated[object, Depends(permission("manage_finance"))]
PKT = ZoneInfo("Asia/Karachi")


class EventInput(Input):
    shop_id: str
    product_id: str | None = None
    kind: Literal["store_view", "product_view", "add_to_cart"]
    visitor_id: str = Field(min_length=16, max_length=100)


@router.post("/events", status_code=202)
async def event(data: EventInput, request: Request, db: DB):
    await throttle("analytics:" + client_ip(request), 120, 60)
    if not await db.scalar(visible_shops().where(Shop.id == data.shop_id)):
        fail(404, "Store not found")
    if data.product_id:
        product = await get(db, Product, data.product_id)
        if product.shop_id != data.shop_id or product.status != "active":
            fail(422, "Product is not available in this store")
    elif data.kind != "store_view":
        fail(422, "A product ID is required")
    from anymob.config import settings

    visitor = digest(settings().secret_key + data.visitor_id + now().date().isoformat())
    # A browser event is a visit estimate, not a financial source of truth.
    if not await db.scalar(
        select(AnalyticsEvent.id)
        .where(
            AnalyticsEvent.visitor_hash == visitor,
            AnalyticsEvent.shop_id == data.shop_id,
            AnalyticsEvent.product_id == data.product_id,
            AnalyticsEvent.kind == data.kind,
            AnalyticsEvent.created_at > now() - timedelta(minutes=5),
        )
        .limit(1)
    ):
        db.add(
            AnalyticsEvent(
                shop_id=data.shop_id, product_id=data.product_id, kind=data.kind, visitor_hash=visitor
            )
        )
    return {"ok": True}


def date_range(start, end):
    today = now().astimezone(PKT).date()
    last = end or today
    first = start or last - timedelta(days=29)
    if first > last or (last - first).days > 365 or last > today:
        fail(422, "Choose a range of 1–366 days ending today or earlier")
    return datetime.combine(first, time.min, PKT).astimezone(UTC), datetime.combine(
        last + timedelta(days=1), time.min, PKT
    ).astimezone(UTC)


@router.get("/vendor/analytics/traffic")
async def vendor_traffic(actor: Actor, db: DB, start: str | None = None, end: str | None = None):
    """Visit and add-to-cart counts per Pakistan-time day and product, plus how long products have been sold out."""
    from datetime import date

    from anymob.models import InventoryMovement, Variant

    try:
        first, last = date_range(
            date.fromisoformat(start) if start else None, date.fromisoformat(end) if end else None
        )
    except ValueError:
        fail(422, "Dates must use YYYY-MM-DD")
    shop = await owned_shop(db, actor)
    day = func.date(func.timezone("Asia/Karachi", AnalyticsEvent.created_at))
    rows = (
        await db.execute(
            select(day, AnalyticsEvent.product_id, AnalyticsEvent.kind, func.count())
            .where(
                AnalyticsEvent.shop_id == shop.id,
                AnalyticsEvent.created_at >= first,
                AnalyticsEvent.created_at < last,
            )
            .group_by(day, AnalyticsEvent.product_id, AnalyticsEvent.kind)
        )
    ).all()
    sold_out = (
        await db.execute(
            select(Variant.product_id, func.max(InventoryMovement.created_at))
            .join(InventoryMovement, InventoryMovement.variant_id == Variant.id)
            .join(Product, Product.id == Variant.product_id)
            .where(
                Product.shop_id == shop.id,
                Variant.is_active.is_(True),
                Variant.stock == 0,
                InventoryMovement.stock_after == 0,
            )
            .group_by(Variant.product_id)
        )
    ).all()
    return {
        "events": [
            {"date": d.isoformat(), "productId": product_id, "kind": kind, "count": count}
            for d, product_id, kind, count in rows
        ],
        "soldOut": [{"productId": product_id, "since": since} for product_id, since in sold_out],
    }


@router.get("/vendor/analytics")
async def vendor_analytics(actor: Actor, db: DB, start: str | None = None, end: str | None = None):
    from datetime import date

    try:
        first, last = date_range(
            date.fromisoformat(start) if start else None, date.fromisoformat(end) if end else None
        )
    except ValueError:
        fail(422, "Dates must use YYYY-MM-DD")
    shop = await owned_shop(db, actor)
    duration = last - first

    async def period(begin, finish):
        orders = (
            await db.scalars(
                select(Order).where(
                    Order.shop_id == shop.id, Order.created_at >= begin, Order.created_at < finish
                )
            )
        ).all()
        events = (
            await db.scalars(
                select(AnalyticsEvent).where(
                    AnalyticsEvent.shop_id == shop.id,
                    AnalyticsEvent.created_at >= begin,
                    AnalyticsEvent.created_at < finish,
                )
            )
        ).all()
        paid = [o for o in orders if o.payment_status in {"paid", "partially_refunded", "refunded"}]
        revenue = sum(o.total - o.refunded_amount for o in paid)
        views = sum(e.kind in {"store_view", "product_view"} for e in events)
        daily = {}
        cursor = begin.astimezone(PKT).date()
        while cursor < finish.astimezone(PKT).date():
            daily[cursor.isoformat()] = {
                "date": cursor.isoformat(),
                "revenue": 0,
                "orders": 0,
                "views": 0,
                "addToCarts": 0,
            }
            cursor += timedelta(days=1)
        for order in orders:
            bucket = daily[order.created_at.astimezone(PKT).date().isoformat()]
            bucket["orders"] += 1
            if order in paid:
                bucket["revenue"] += float(order.total - order.refunded_amount)
        for event in events:
            bucket = daily[event.created_at.astimezone(PKT).date().isoformat()]
            bucket["addToCarts" if event.kind == "add_to_cart" else "views"] += 1
        return {
            "revenue": float(revenue),
            "orders": len(orders),
            "views": views,
            "conversion": len(orders) / views if views else 0,
            "averageOrder": float(revenue / len(paid)) if paid else 0,
            "daily": list(daily.values()),
        }

    current, previous = await period(first, last), await period(first - duration, first)
    return {"current": current, "previous": previous, "timezone": "Asia/Karachi"}


@router.get("/admin/dashboard")
async def dashboard(actor: Admin, db: DB):
    permissions = (await config(db, "team"))["permissions"].get(actor.admin_role, {})
    result = {"permissions": permissions}
    specs = [
        ("users", User, None, "manage_users"),
        ("pendingVendors", Shop, Shop.verification_status == "pending", "verify_stores"),
        ("pendingProducts", Product, Product.status == "pending", "moderate_catalog"),
        ("pendingAds", Classified, Classified.status == "pending", "moderate_catalog"),
        ("openReports", Report, Report.status.in_(["open", "reviewing"]), "moderate_catalog"),
        ("orders", Order, None, "manage_orders"),
        ("pendingPayouts", Payout, Payout.status == "pending", "process_payouts"),
    ]
    for key, model, condition, perm in specs:
        if permissions.get(perm):
            query = select(func.count()).select_from(model)
            if condition is not None:
                query = query.where(condition)
            result[key] = await db.scalar(query)
    return result


@router.get("/admin/dashboard/report")
async def dashboard_report_view(actor: Admin, db: DB, days: int = 30):
    from anymob.services.dashboard import dashboard_report

    if days not in {1, 7, 30, 90}:
        fail(422, "Choose a valid dashboard date range")
    return await dashboard_report(db, days)


@router.get("/admin/transactions/stats/volume")
async def transaction_volume(actor: FinanceAdmin, db: DB, start: str | None = None, end: str | None = None):
    from datetime import date

    try:
        first, last = date_range(
            date.fromisoformat(start) if start else None, date.fromisoformat(end) if end else None
        )
    except ValueError:
        fail(422, "Dates must use YYYY-MM-DD")
    rows = (
        await db.scalars(select(Payment).where(Payment.created_at >= first, Payment.created_at < last))
    ).all()
    stats = {"totalProcessed": 0, "orderPayments": 0, "subscriptionPayments": 0, "refunds": 0, "failed": 0}
    daily = {}
    for row in rows:
        key = row.created_at.astimezone(PKT).date().isoformat()
        daily.setdefault(key, {"date": key, "volume": 0, "refunds": 0})
        if row.status == "failed":
            stats["failed"] += 1
        if row.status != "paid":
            continue
        if row.kind == "refund":
            stats["refunds"] += float(-row.amount)
            daily[key]["refunds"] += float(-row.amount)
        else:
            stats["totalProcessed"] += float(row.amount)
            daily[key]["volume"] += float(row.amount)
            if row.kind == "order_payment":
                stats["orderPayments"] += float(row.amount)
            if row.kind == "subscription":
                stats["subscriptionPayments"] += float(row.amount)
    return {
        "stats": stats,
        "daily": sorted(daily.values(), key=lambda d: d["date"]),
        "timezone": "Asia/Karachi",
    }


PayoutAdmin = Annotated[object, Depends(permission("process_payouts"))]


@router.get("/admin/payout-statistics")
async def payout_statistics(actor: PayoutAdmin, db: DB):
    from anymob.models import ReturnRequest

    today = now().astimezone(PKT)
    week_end = datetime.combine(today.date() + timedelta(days=7 - today.weekday()), time.min, PKT)
    month_start = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    async def total(*conditions):
        count, amount = (
            await db.execute(
                select(func.count(), func.coalesce(func.sum(Payout.net_amount), 0)).where(*conditions)
            )
        ).one()
        return {"count": count, "amount": float(amount)}

    held_stores = select(ReturnRequest.shop_id).where(ReturnRequest.disputed.is_(True))
    return {
        "pending": await total(Payout.status == "pending"),
        "scheduledThisWeek": await total(
            Payout.status == "processing", Payout.scheduled_at >= now(), Payout.scheduled_at < week_end
        ),
        "paidThisMonth": await total(Payout.status == "paid", Payout.paid_at >= month_start),
        "held": await total(
            Payout.status.not_in(["paid", "failed"]),
            (Payout.status == "on_hold") | Payout.shop_id.in_(held_stores),
        ),
    }
