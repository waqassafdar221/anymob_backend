"""The admin dashboard report: marketplace totals, trends and work queues for the last N days."""

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select

from anymob.db import now
from anymob.models import (
    AuditLog,
    Classified,
    LedgerEntry,
    Order,
    OrderItem,
    Payment,
    Payout,
    Product,
    Report,
    ReturnRequest,
    Review,
    Shop,
    User,
    Variant,
)

PKT = ZoneInfo("Asia/Karachi")
QUEUES = [
    ("orders", "unconfirmed", "orders unconfirmed for over 24 hours"),
    ("returns", "open", "open disputes"),
    ("products", "pending", "products pending review"),
    ("ads", "pending", "classified ads pending review"),
    ("vendors", "pending", "vendors awaiting verification"),
    ("reports", "open", "open reports"),
    ("payouts", "pending", "payouts to process"),
    ("transactions", "failed", "failed payments"),
]


def money(value) -> float:
    return round(float(value or 0), 2)


def pkt_day(moment: datetime) -> str:
    return moment.astimezone(PKT).date().isoformat()


def trend(value, previous):
    if previous == 0:
        return 0 if value == 0 else None
    return round((value - previous) / previous * 100, 2)


async def dashboard_report(db, days: int) -> dict:
    current_time = now()
    today = current_time.astimezone(PKT).date()
    start = datetime.combine(today - timedelta(days=days - 1), datetime.min.time(), PKT)
    end = current_time + timedelta(microseconds=1)
    previous_start = start - timedelta(days=days)
    # Compare the elapsed part of this period with the same elapsed part of the previous one.
    comparison_end = min(start, previous_start + (end - start))

    def merchandise(order):
        return max(Decimal(0), order.subtotal - order.discount)

    def is_sale(order):
        return order.status not in {"cancelled", "returned"}

    async def orders_between(first, last):
        return (
            await db.scalars(select(Order).where(Order.created_at >= first, Order.created_at < last))
        ).all()

    async def totals(first, last):
        orders = await orders_between(first, last)
        sales = [o for o in orders if is_sale(o)]
        gmv = sum((merchandise(o) for o in sales), Decimal(0))
        # Platform commission as booked in the ledger: debits on vendors, less reversals on refunds.
        commission = await db.scalar(
            select(func.coalesce(func.sum(LedgerEntry.amount), 0)).where(
                LedgerEntry.kind.in_(["commission", "commission_reversal"]),
                LedgerEntry.created_at >= first,
                LedgerEntry.created_at < last,
            )
        )
        fees = await db.scalar(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.kind.in_(["subscription", "featured_package"]),
                Payment.status == "paid",
                Payment.created_at >= first,
                Payment.created_at < last,
            )
        )
        in_stock = await db.scalar(
            select(func.count(func.distinct(Product.id)))
            .join(Variant, Variant.product_id == Product.id)
            .where(Product.status == "active", Variant.is_active.is_(True), Variant.stock > 0)
        )
        new_users = await db.scalar(
            select(func.count()).select_from(User).where(User.created_at >= first, User.created_at < last)
        )
        ads = await db.scalar(
            select(func.count())
            .select_from(Classified)
            .where(Classified.status != "draft", Classified.created_at >= first, Classified.created_at < last)
        )
        return {
            "gmv": money(gmv),
            "orders": len(orders),
            "revenue": money(-Decimal(commission) + Decimal(fees)),
            "aov": money(gmv / len(sales)) if sales else 0,
            "activeStores": len({o.shop_id for o in sales}),
            "productsInStock": in_stock,
            "newUsers": new_users,
            "adsPosted": ads,
        }

    current = await totals(start, end)
    previous = await totals(previous_start, comparison_end)
    orders = await orders_between(start, end)
    sales = [o for o in orders if is_sale(o)]
    shops = {s.id: s for s in (await db.scalars(select(Shop))).all()}

    daily = []
    for offset in range(days):
        day = (start + timedelta(days=offset)).date().isoformat()
        rows = [o for o in orders if pkt_day(o.created_at) == day]
        daily.append(
            {
                "date": day,
                "label": (start + timedelta(days=offset)).strftime("%-d %b"),
                "gmv": money(sum((merchandise(o) for o in rows if is_sale(o)), Decimal(0))),
                "orders": len(rows),
            }
        )

    def count_by(key):
        return [{"label": label, "value": value} for label, value in Counter(key(o) for o in orders).items()]

    months = list(dict.fromkeys(d["date"][:7] for d in daily))
    commission_rows = (
        await db.execute(
            select(LedgerEntry.created_at, LedgerEntry.amount).where(
                LedgerEntry.kind.in_(["commission", "commission_reversal"]),
                LedgerEntry.created_at >= start,
                LedgerEntry.created_at < end,
            )
        )
    ).all()
    fee_rows = (
        await db.execute(
            select(Payment.created_at, Payment.kind, Payment.amount).where(
                Payment.kind.in_(["subscription", "featured_package"]),
                Payment.status == "paid",
                Payment.created_at >= start,
                Payment.created_at < end,
            )
        )
    ).all()
    monthly = [
        {
            "month": month,
            "commission": money(
                -sum((a for at, a in commission_rows if pkt_day(at).startswith(month)), Decimal(0))
            ),
            "subscriptions": money(
                sum(
                    (a for at, k, a in fee_rows if k == "subscription" and pkt_day(at).startswith(month)),
                    Decimal(0),
                )
            ),
            "featured": money(
                sum(
                    (a for at, k, a in fee_rows if k == "featured_package" and pkt_day(at).startswith(month)),
                    Decimal(0),
                )
            ),
        }
        for month in months
    ]

    ratings = dict(
        (
            await db.execute(
                select(Review.shop_id, func.avg(Review.rating))
                .where(Review.hidden.is_(False))
                .group_by(Review.shop_id)
            )
        ).all()
    )
    by_store = defaultdict(lambda: {"orders": 0, "gmv": Decimal(0)})
    for order in sales:
        by_store[order.shop_id]["orders"] += 1
        by_store[order.shop_id]["gmv"] += merchandise(order)
    stores = sorted(
        (
            {
                "id": shop_id,
                "name": shops[shop_id].name if shop_id in shops else shop_id,
                "logo": shops[shop_id].data.get("logo", "") if shop_id in shops else "",
                "city": shops[shop_id].city if shop_id in shops else "",
                "orders": row["orders"],
                "gmv": money(row["gmv"]),
                "rating": round(float(ratings.get(shop_id) or 0), 2),
            }
            for shop_id, row in by_store.items()
        ),
        key=lambda row: row["gmv"],
        reverse=True,
    )[:5]

    sale_ids = [o.id for o in sales]
    items = (
        (await db.scalars(select(OrderItem).where(OrderItem.order_id.in_(sale_ids)))).all()
        if sale_ids
        else []
    )
    products = {}
    for item in items:
        row = products.setdefault(
            item.product_id,
            {
                "id": item.product_id,
                "name": item.snapshot.get("name", ""),
                "image": item.snapshot.get("image", ""),
                "units": 0,
                "sales": 0.0,
            },
        )
        row["units"] += item.quantity
        row["sales"] = round(row["sales"] + float(item.unit_price * item.quantity), 2)

    cities = defaultdict(Decimal)
    for order in sales:
        city = (order.address or {}).get("city") or (
            shops[order.shop_id].city if order.shop_id in shops else "Unknown"
        )
        cities[city] += merchandise(order)

    owners = {
        u.id: u for u in (await db.scalars(select(User).where(User.role.in_(["vendor", "admin"])))).all()
    }
    pending_shops = [s for s in shops.values() if s.verification_status == "pending"]
    applications = sorted(
        (
            {
                "id": shop.id,
                "name": shop.name,
                "logo": shop.data.get("logo", ""),
                "city": shop.city,
                "owner": owners[shop.owner_id].full_name if shop.owner_id in owners else "Unknown",
                "phone": shop.data.get("phone", ""),
                "address": shop.data.get("address", ""),
                "submittedAt": shop.data.get("verificationSubmittedAt") or shop.created_at.isoformat(),
                "documents": {
                    kind: {
                        "name": kind,
                        "url": f"/api/v1/media/{upload_id}",
                        "type": "image/webp",
                        "status": "pending",
                    }
                    for kind, upload_id in shop.data.get("verificationDocuments", {}).items()
                },
            }
            for shop in pending_shops
        ),
        key=lambda row: row["submittedAt"],
        reverse=True,
    )[:5]

    day_ago = current_time - timedelta(days=1)
    queue_counts = {
        ("orders", "unconfirmed"): select(func.count())
        .select_from(Order)
        .where(Order.status == "pending", Order.created_at < day_ago),
        ("returns", "open"): select(func.count())
        .select_from(ReturnRequest)
        .where(or_(ReturnRequest.status.not_in(["rejected", "refunded"]), ReturnRequest.disputed.is_(True))),
        ("products", "pending"): select(func.count()).select_from(Product).where(Product.status == "pending"),
        ("ads", "pending"): select(func.count())
        .select_from(Classified)
        .where(Classified.status == "pending"),
        ("vendors", "pending"): select(func.count())
        .select_from(Shop)
        .where(Shop.verification_status == "pending"),
        ("reports", "open"): select(func.count())
        .select_from(Report)
        .where(Report.status.in_(["open", "reviewing"])),
        ("payouts", "pending"): select(func.count())
        .select_from(Payout)
        .where(Payout.status.in_(["pending", "processing"])),
        ("transactions", "failed"): select(func.count())
        .select_from(Payment)
        .where(Payment.status == "failed"),
    }
    attention = [
        {
            "section": section,
            "queue": queue,
            "label": label,
            "count": await db.scalar(queue_counts[(section, queue)]),
            "href": f"/admin/{section}?queue={queue}",
        }
        for section, queue, label in QUEUES
    ]

    buyers = (
        {
            u.id: u.full_name
            for u in (await db.scalars(select(User).where(User.id.in_({o.buyer_id for o in orders})))).all()
        }
        if orders
        else {}
    )
    latest = sorted(orders, key=lambda o: o.created_at, reverse=True)[:8]
    logs = (await db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(6))).all()
    actors = (
        {
            u.id: u.full_name
            for u in (
                await db.scalars(
                    select(User).where(User.id.in_({log.admin_id for log in logs if log.admin_id}))
                )
            ).all()
        }
        if logs
        else {}
    )

    return {
        "today": today.isoformat(),
        "days": days,
        "from": start.date().isoformat(),
        "current": current,
        "previous": previous,
        "trends": {
            key: None if key == "productsInStock" else trend(current[key], previous[key]) for key in current
        },
        "daily": daily,
        "statuses": count_by(lambda o: o.status),
        "fulfillment": count_by(lambda o: o.fulfillment),
        "payments": count_by(lambda o: o.payment_method),
        "monthly": monthly,
        "attention": attention,
        "latestOrders": [
            {
                "id": o.id,
                "orderNumber": o.order_number,
                "shop": shops[o.shop_id].name if o.shop_id in shops else o.shop_id,
                "buyer": buyers.get(o.buyer_id, o.buyer_id),
                "total": money(o.total),
                "status": o.status,
                "createdAt": o.created_at.isoformat(),
            }
            for o in latest
        ],
        "stores": stores,
        "applications": applications,
        "products": sorted(products.values(), key=lambda row: row["units"], reverse=True)[:5],
        "cities": sorted(
            ({"city": city, "sales": money(total)} for city, total in cities.items()),
            key=lambda row: row["sales"],
            reverse=True,
        )[:7],
        "activity": [
            {
                "id": log.id,
                "actorId": log.admin_id or "",
                "actor": actors.get(log.admin_id, "System"),
                "action": log.action,
                "targetType": log.target_type,
                "targetId": log.target_id,
                "summary": log.details or log.action.replace(".", " ").replace("_", " "),
                "createdAt": log.created_at.isoformat(),
                "ip": log.ip,
            }
            for log in logs
        ],
    }
