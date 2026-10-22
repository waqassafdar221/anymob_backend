import csv
import hashlib
import hmac
import io
from datetime import timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse
from pydantic import AwareDatetime, Field
from sqlalchemy import func, select, update

from anymob.common import DB, Page, advisory_lock, audit, fail, get, owned_shop, paginated, record
from anymob.config import settings
from anymob.db import now
from anymob.models import (
    Coupon,
    FeaturedPackage,
    Order,
    Payment,
    PaymentAllocation,
    Payout,
    Placement,
    Plan,
    Shop,
    Subscription,
    WebhookEvent,
)
from anymob.routes.orders import Key
from anymob.schemas import CouponInput, Input, Money, Name, Note, PayoutAction, PlanInput, Rate, RefundInput
from anymob.security import Actor, permission
from anymob.services.admin_revenue import payout_detail as payout_dossier
from anymob.services.admin_revenue import payout_rows, transaction_rows
from anymob.services.commerce import capture_payment
from anymob.services.finance import earnings, payout_action, refund, request_payout, settle_refund

router = APIRouter(tags=["Revenue and payouts"])
FinanceAdmin = Annotated[object, Depends(permission("manage_finance"))]
PayoutAdmin = Annotated[object, Depends(permission("process_payouts"))]


@router.get("/plans")
async def plans(db: DB):
    return [
        record(r)
        for r in (await db.scalars(select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.price))).all()
    ]


@router.get("/featured-packages")
async def packages(db: DB):
    return [
        record(r)
        for r in (await db.scalars(select(FeaturedPackage).where(FeaturedPackage.is_active.is_(True)))).all()
    ]


@router.get("/vendor/earnings")
async def vendor_earnings(actor: Actor, db: DB):
    return await earnings(db, await owned_shop(db, actor))


@router.post("/vendor/payouts", status_code=201)
async def payout_request(actor: Actor, db: DB):
    payout = await request_payout(db, await owned_shop(db, actor, lock=True))
    return record(payout)


@router.get("/vendor/subscription")
async def vendor_subscription(actor: Actor, db: DB):
    shop = await owned_shop(db, actor)
    active = await db.scalar(
        select(Subscription)
        .where(Subscription.shop_id == shop.id, Subscription.status == "active")
        .order_by(Subscription.created_at.desc())
        .limit(1)
    )
    cadence = "monthly"
    if active:
        payment = await db.get(Payment, active.payment_id)
        cadence = (payment.data or {}).get("cadence", "monthly") if payment else "monthly"
    return {
        "planId": shop.plan_id,
        "cadence": cadence,
        "renewalDate": active.expires_at if active else None,
    }


@router.get("/vendor/purchases")
async def vendor_purchases(actor: Actor, db: DB, page: Page):
    shop = await owned_shop(db, actor)
    return await paginated(
        db,
        select(Payment)
        .where(Payment.shop_id == shop.id, Payment.kind.in_(["subscription", "featured_package"]))
        .order_by(Payment.created_at.desc()),
        page,
    )


class Purchase(Input):
    kind: Literal["subscription", "featured_package"]
    plan_id: str | None = None
    package_id: str | None = None
    product_id: str | None = None
    cadence: Literal["monthly", "yearly"] = "monthly"


@router.post("/vendor/purchases", status_code=201)
async def purchase(data: Purchase, key: Key, actor: Actor, db: DB):
    shop = await owned_shop(db, actor, lock=True)
    await advisory_lock(db, "purchase:" + actor.id + key)
    from anymob.common import fingerprint

    old = await db.scalar(select(Payment).where(Payment.idempotency_key == "purchase:" + actor.id + key))
    request_hash = fingerprint(data.model_dump())
    if old:
        if old.data["requestHash"] != request_hash:
            fail(409, "Idempotency key already used")
        return record(old)
    if data.kind == "subscription":
        plan = await get(db, Plan, data.plan_id or "")
        if not plan.is_active:
            fail(422, "Plan inactive")
        amount = plan.yearly_price if data.cadence == "yearly" else plan.price
    else:
        from anymob.models import Product

        package = await get(db, FeaturedPackage, data.package_id or "")
        product = await get(db, Product, data.product_id or "")
        if not package.is_active or product.shop_id != shop.id or product.status != "active":
            fail(422, "Package or product unavailable")
        amount = package.price
    row = Payment(
        user_id=actor.id,
        shop_id=shop.id,
        kind=data.kind,
        amount=amount,
        method="bank_transfer",
        idempotency_key="purchase:" + actor.id + key,
        data={**data.model_dump(by_alias=True), "requestHash": request_hash},
    )
    db.add(row)
    await db.flush()
    if amount == 0:
        # Free plans need no transfer to confirm.
        row.status, row.paid_at = "paid", now()
        await activate_purchase(db, row)
    return record(row)


async def lock_payment_shops(db, payment):
    shop_ids = set(
        (
            await db.scalars(
                select(Order.shop_id)
                .join(PaymentAllocation, PaymentAllocation.order_id == Order.id)
                .where(PaymentAllocation.payment_id == payment.id)
            )
        ).all()
    )
    if payment.shop_id:
        shop_ids.add(payment.shop_id)
    for shop_id in sorted(shop_ids):
        await get(db, Shop, shop_id, lock=True)


async def activate_purchase(db, payment):
    if payment.kind == "subscription":
        shop = await get(db, Shop, payment.shop_id, lock=True)
        plan = await get(db, Plan, payment.data["planId"])
        await db.execute(
            update(Subscription)
            .where(Subscription.shop_id == shop.id, Subscription.status == "active")
            .values(status="replaced")
        )
        shop.plan_id = plan.id
        db.add(
            Subscription(
                shop_id=shop.id,
                plan_id=plan.id,
                payment_id=payment.id,
                expires_at=now() + timedelta(days=366 if payment.data["cadence"] == "yearly" else 31),
            )
        )
    elif payment.kind == "featured_package":
        package = await get(db, FeaturedPackage, payment.data["packageId"])
        db.add(
            Placement(
                shop_id=payment.shop_id,
                package_id=package.id,
                product_id=payment.data["productId"],
                payment_id=payment.id,
                expires_at=now() + timedelta(days=package.duration_days),
            )
        )


@router.get("/admin/plans")
async def all_plans(actor: FinanceAdmin, db: DB):
    result = []
    for p in (await db.scalars(select(Plan).order_by(Plan.price))).all():
        count = await db.scalar(
            select(func.count())
            .select_from(Subscription)
            .where(
                Subscription.plan_id == p.id, Subscription.status == "active", Subscription.expires_at > now()
            )
        )
        result.append({**record(p), "activeSubscribers": count})
    return result


async def write_plan(db, actor, request, data, row=None):
    await advisory_lock(db, "plans")
    if data.is_popular:
        await db.execute(update(Plan).where(Plan.is_popular.is_(True)).values(is_popular=False))
    before = record(row) if row else None
    row = row or Plan()
    for key, value in data.model_dump().items():
        setattr(row, key, value)
    db.add(row)
    await db.flush()
    await audit(db, actor, request, "plan.saved", row, before=before)
    return record(row)


@router.post("/admin/plans", status_code=201)
async def add_plan(data: PlanInput, actor: FinanceAdmin, request: Request, db: DB):
    return await write_plan(db, actor, request, data)


@router.put("/admin/plans/{id}")
async def edit_plan(id: str, data: PlanInput, actor: FinanceAdmin, request: Request, db: DB):
    return await write_plan(db, actor, request, data, await get(db, Plan, id))


class PackageInput(Input):
    name: Name
    duration_days: int = Field(ge=1, le=365)
    price: Money
    placement: Literal["homepage_featured", "category_top", "search_top"]
    is_active: bool = True


@router.get("/admin/featured-packages")
async def all_packages(actor: FinanceAdmin, db: DB):
    return [
        {
            **record(p),
            "timesPurchased": await db.scalar(
                select(func.count()).select_from(Placement).where(Placement.package_id == p.id)
            ),
        }
        for p in (await db.scalars(select(FeaturedPackage))).all()
    ]


@router.post("/admin/featured-packages", status_code=201)
async def add_package(data: PackageInput, actor: FinanceAdmin, request: Request, db: DB):
    row = FeaturedPackage(**data.model_dump())
    db.add(row)
    await db.flush()
    await audit(db, actor, request, "package.created", row)
    return record(row)


@router.put("/admin/featured-packages/{id}")
async def edit_package(id: str, data: PackageInput, actor: FinanceAdmin, request: Request, db: DB):
    row = await get(db, FeaturedPackage, id, lock=True)
    before = record(row)
    for key, value in data.model_dump().items():
        setattr(row, key, value)
    await audit(db, actor, request, "package.updated", row, before=before)
    return record(row)


@router.get("/admin/revenue/overview")
async def revenue_overview(actor: FinanceAdmin, db: DB):
    """Plans with live subscriber counts, commission rules, featured packages and platform coupons."""
    from anymob.common import config
    from anymob.models import Category

    categories = (await db.scalars(select(Category).order_by(Category.sort_order, Category.name))).all()
    coupons = (
        await db.scalars(select(Coupon).where(Coupon.shop_id.is_(None)).order_by(Coupon.created_at.desc()))
    ).all()
    return {
        "plans": await all_plans(actor, db),
        "commission": {
            "defaultRate": float((await config(db, "revenue"))["defaultCommission"]),
            "categoryOverrides": [
                {"categoryId": c.id, "rate": float(c.commission_override)}
                for c in categories
                if c.commission_override is not None
            ],
        },
        "packages": await all_packages(actor, db),
        "coupons": [record(c) for c in coupons],
        "categories": [record(c) for c in categories],
    }


class CategoryRate(Input):
    category_id: str
    rate: Rate


class CommissionRules(Input):
    default_rate: Rate
    category_overrides: list[CategoryRate] = Field(default_factory=list, max_length=500)


@router.put("/admin/revenue/commission")
async def save_commission(data: CommissionRules, actor: FinanceAdmin, request: Request, db: DB):
    from anymob.models import Category, Setting

    if len({o.category_id for o in data.category_overrides}) != len(data.category_overrides):
        fail(422, "Each category can have only one override")
    await advisory_lock(db, "settings:revenue")
    row = await db.get(Setting, "revenue", with_for_update=True)
    before = record(row) if row else None
    value = {**(row.value if row else {}), "defaultCommission": float(data.default_rate)}
    if row:
        row.value, row.version = value, row.version + 1
    else:
        row = Setting(key="revenue", value=value, version=1)
        db.add(row)
    await db.flush()
    await audit(db, actor, request, "settings.revenue", row, before=before)
    rates = {o.category_id: o.rate for o in data.category_overrides}
    for category in (await db.scalars(select(Category).with_for_update())).all():
        rate = rates.pop(category.id, None)
        if category.commission_override != rate:
            previous = record(category)
            category.commission_override = rate
            await audit(db, actor, request, "commission.updated", category, before=previous)
    if rates:
        fail(422, "Choose existing categories")
    return {"saved": True}


class CouponCheck(Input):
    code: str = Field(min_length=1, max_length=40)
    shop_id: str | None = None
    subtotal: Money


@router.post("/coupons/check")
async def check_coupon(data: CouponCheck, request: Request, db: DB):
    """Previews a coupon for the checkout summary; checkout applies the same rules authoritatively."""
    from decimal import Decimal

    from anymob.common import client_ip
    from anymob.security import throttle

    # Rate limited so codes cannot be enumerated.
    await throttle("coupon-check:" + client_ip(request), 30, 300)
    coupon = await db.scalar(select(Coupon).where(Coupon.code == data.code.strip().upper()))

    def refuse(reason):
        return {"valid": False, "reason": reason, "discount": 0, "coupon": None}

    if not coupon or not coupon.is_active:
        return refuse("That code was not recognised.")
    if now() < coupon.valid_from:
        return refuse("That code is not live yet.")
    if now() > coupon.valid_to:
        return refuse("That code has expired.")
    if coupon.usage_limit is not None and coupon.used_count >= coupon.usage_limit:
        return refuse("That code has been fully claimed.")
    if coupon.shop_id and data.shop_id and coupon.shop_id != data.shop_id:
        return refuse("That code does not apply to this shop.")
    subtotal = Decimal(str(data.subtotal))
    if subtotal <= 0 or subtotal < coupon.minimum_order:
        return refuse(f"Spend Rs. {coupon.minimum_order:,.0f} to use this code.")
    discount = subtotal * coupon.value / 100 if coupon.kind == "percent" else coupon.value
    discount = min(subtotal, discount.quantize(Decimal("0.01")))
    if coupon.max_discount is not None:
        discount = min(discount, coupon.max_discount)
    return {
        "valid": True,
        "reason": None,
        "discount": discount,
        "coupon": {"code": coupon.code, "kind": coupon.kind, "value": coupon.value, "shopId": coupon.shop_id},
    }


@router.get("/admin/coupons")
async def coupons(actor: FinanceAdmin, db: DB, page: Page):
    return await paginated(db, select(Coupon).order_by(Coupon.created_at.desc()), page)


@router.post("/admin/coupons", status_code=201)
async def add_coupon(data: CouponInput, actor: FinanceAdmin, request: Request, db: DB):
    row = Coupon(**data.model_dump())
    db.add(row)
    await db.flush()
    await audit(db, actor, request, "coupon.created", row)
    return record(row)


@router.put("/admin/coupons/{id}")
async def edit_coupon(id: str, data: CouponInput, actor: FinanceAdmin, request: Request, db: DB):
    row = await get(db, Coupon, id, lock=True)
    if data.usage_limit is not None and data.usage_limit < row.used_count:
        fail(422, "Usage limit cannot be below the used count")
    before = record(row)
    for key, value in data.model_dump().items():
        setattr(row, key, value)
    await audit(db, actor, request, "coupon.updated", row, before=before)
    return record(row)


def transaction_query(search="", kind=None, method=None, status=None, start=None, end=None):
    query = select(Payment)
    if search:
        from anymob.models import User

        needle = f"%{search[:160]}%"
        from sqlalchemy import or_

        query = query.where(
            or_(
                Payment.id.ilike(needle),
                Payment.provider_reference.ilike(needle),
                Payment.user_id.in_(select(User.id).where(User.full_name.ilike(needle))),
                Payment.shop_id.in_(select(Shop.id).where(Shop.name.ilike(needle))),
                Payment.order_group_id.in_(
                    select(Order.order_group_id).where(Order.order_number.ilike(needle))
                ),
            )
        )
    for column, value in [(Payment.kind, kind), (Payment.method, method), (Payment.status, status)]:
        if value:
            query = query.where(column == value)
    if start:
        query = query.where(Payment.created_at >= start)
    if end:
        query = query.where(Payment.created_at <= end)
    return query.order_by(Payment.created_at.desc())


@router.get("/admin/transactions")
async def transactions(
    actor: FinanceAdmin,
    db: DB,
    page: Page,
    search: str = "",
    kind: str | None = None,
    method: str | None = None,
    status: str | None = None,
    start: AwareDatetime | None = None,
    end: AwareDatetime | None = None,
):
    if kind == "featured_ad":
        kind = "featured_package"
    result = await paginated(
        db, transaction_query(search, kind, method, status, start, end), page, lambda row: row
    )
    return {**result, "items": await transaction_rows(db, result["items"])}


@router.get("/admin/transactions/export")
async def export_transactions(
    actor: FinanceAdmin,
    db: DB,
    search: str = "",
    kind: str | None = None,
    method: str | None = None,
    status: str | None = None,
    start: AwareDatetime | None = None,
    end: AwareDatetime | None = None,
):
    rows = (await db.scalars(transaction_query(search, kind, method, status, start, end).limit(100000))).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Type", "Amount PKR", "Method", "Status", "Date", "Reference"])

    def cell(value):
        value = str(value or "")
        return "'" + value if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value

    for row in rows:
        writer.writerow(
            [
                cell(row.id),
                cell(row.kind),
                row.amount,
                cell(row.method),
                cell(row.status),
                row.created_at.isoformat(),
                cell(row.provider_reference),
            ]
        )
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=transactions.csv"},
    )


@router.get("/admin/transactions/{id}")
async def transaction(id: str, actor: FinanceAdmin, db: DB):
    from anymob.models import FeaturedPackage, User
    from anymob.services.admin_orders import order_rows
    from anymob.services.catalog import shop_view

    row = await get(db, Payment, id)
    allocations = (
        await db.scalars(select(PaymentAllocation).where(PaymentAllocation.payment_id == id))
    ).all()
    view = (await transaction_rows(db, [row]))[0]
    order = await db.get(Order, view["orderId"]) if view["orderId"] else None
    refunds = (
        await db.scalars(
            select(Payment)
            .where(
                Payment.kind == "refund",
                (Payment.parent_id == row.id)
                | (Payment.data["orderId"].astext.in_(view["orderIds"]) if view["orderIds"] else False),
            )
            .order_by(Payment.created_at.desc())
        )
    ).all()
    plan = await db.get(Plan, view["planId"]) if view["planId"] else None
    package = await db.get(FeaturedPackage, view["packageId"]) if view["packageId"] else None
    user = await db.get(User, row.user_id)
    shop = await db.get(Shop, view["shopId"]) if view["shopId"] else None
    return {
        "transaction": view,
        "order": (await order_rows(db, [order]))[0] if order else None,
        "plan": record(plan) if plan else None,
        "package": record(package) if package else None,
        "user": record(user) if user else None,
        "shop": shop_view(shop) if shop else None,
        "refunds": await transaction_rows(db, [r for r in refunds if r.id != row.id]),
        "allocations": [record(a) for a in allocations],
        "receipt": {
            "number": row.id,
            "currency": "PKR",
            "amount": float(row.amount),
            "paidAt": row.paid_at,
            "reference": row.provider_reference,
        },
    }


class Paid(Note):
    reference: str = Field(min_length=3, max_length=160)


@router.post("/admin/transactions/{id}/mark-paid")
async def mark_paid(id: str, data: Paid, actor: FinanceAdmin, request: Request, db: DB):
    row = await get(db, Payment, id)
    await lock_payment_shops(db, row)
    row = await get(db, Payment, id, lock=True)
    if row.method != "bank_transfer" or row.kind == "refund":
        fail(409, "Only incoming bank transfers may be marked paid")
    before = record(row)
    if row.status == "paid":
        if row.provider_reference != data.reference:
            fail(409, "Payment already has a different reference")
        return record(row)
    await capture_payment(db, row)
    row.provider_reference = data.reference
    await activate_purchase(db, row)
    await audit(db, actor, request, "transaction.marked_paid", row, before=before, note=data.note)
    return record(row)


@router.post("/admin/orders/{id}/refund")
async def request_refund(id: str, data: RefundInput, key: Key, actor: FinanceAdmin, request: Request, db: DB):
    order = await get(db, Order, id)
    await get(db, Shop, order.shop_id, lock=True)
    order = await get(db, Order, id, lock=True)
    row = await refund(db, order, data.amount, data.note, "admin:" + key)
    await audit(db, actor, request, "refund.requested", row, note=data.note)
    return record(row)


@router.post("/admin/refunds/{id}/settle")
async def confirm_refund(id: str, data: Paid, actor: FinanceAdmin, request: Request, db: DB):
    row = await get(db, Payment, id)
    if row.kind != "refund":
        fail(422, "This is not a refund")
    await get(db, Shop, row.shop_id, lock=True)
    row = await get(db, Payment, id, lock=True)
    if row.status == "paid":
        if row.provider_reference != data.reference:
            fail(409, "Refund already has a different reference")
        return record(row)
    before = record(row)
    await settle_refund(db, row, data.reference)
    await audit(db, actor, request, "refund.settled", row, before=before, note=data.note)
    return record(row)


@router.get("/admin/payouts")
async def payouts(actor: PayoutAdmin, db: DB, page: Page, status: str | None = None):
    query = select(Payout)
    if status:
        query = query.where(Payout.status == status)
    result = await paginated(db, query.order_by(Payout.created_at.desc()), page, lambda row: row)
    return {**result, "items": await payout_rows(db, result["items"])}


@router.get("/admin/payouts/{id}")
async def payout_detail(id: str, actor: PayoutAdmin, db: DB):
    from anymob.models import LedgerEntry, PayoutEntry

    row = await get(db, Payout, id)
    entries = (
        await db.scalars(
            select(LedgerEntry)
            .join(PayoutEntry, PayoutEntry.ledger_id == LedgerEntry.id)
            .where(PayoutEntry.payout_id == id)
        )
    ).all()
    return {**await payout_dossier(db, row), "entries": [record(e) for e in entries]}


@router.post("/admin/payouts/{id}/action")
async def act_on_payout(id: str, data: PayoutAction, actor: PayoutAdmin, request: Request, db: DB):
    row = await get(db, Payout, id)
    await get(db, Shop, row.shop_id, lock=True)
    row = await get(db, Payout, id, lock=True)
    before = record(row)
    await payout_action(db, row, data)
    await audit(db, actor, request, "payout." + data.action, row, before=before, note=data.note)
    return record(row)


class BulkPaid(Input):
    payouts: dict[str, str] = Field(min_length=1, max_length=100)
    note: str = Field(min_length=3, max_length=4000)


@router.post("/admin/payouts/bulk-paid")
async def bulk_paid(data: BulkPaid, actor: PayoutAdmin, request: Request, db: DB):
    rows = (await db.scalars(select(Payout).where(Payout.id.in_(data.payouts)))).all()
    if len(rows) != len(data.payouts) or len(set(data.payouts.values())) != len(rows):
        fail(422, "Choose valid payouts and a unique reference for each")
    for shop_id in sorted({r.shop_id for r in rows}):
        await get(db, Shop, shop_id, lock=True)
    for row in sorted(rows, key=lambda p: p.id):
        row = await get(db, Payout, row.id, lock=True)
        before = record(row)
        await payout_action(
            db, row, PayoutAction(action="paid", reference=data.payouts[row.id], note=data.note)
        )
        await audit(db, actor, request, "payout.paid", row, before=before, note=data.note)
    return {"count": len(rows)}


class Webhook(Input):
    event_id: str = Field(min_length=1, max_length=180)
    payment_id: str
    amount: Money
    currency: Literal["PKR"]
    reference: str = Field(min_length=1, max_length=180)
    status: Literal["paid", "failed"]


@router.post("/payments/webhooks/sandbox")
async def sandbox_webhook(
    request: Request, db: DB, x_signature: Annotated[str, Header()], x_timestamp: Annotated[int, Header()]
):
    if (
        settings().environment == "production"
        or settings().payment_provider != "sandbox"
        or not settings().payment_webhook_secret
    ):
        fail(404, "Provider unavailable")
    import time

    if abs(int(time.time()) - x_timestamp) > 300:
        fail(401, "Expired webhook")
    raw = await request.body()
    expected = hmac.new(
        settings().payment_webhook_secret.encode(), str(x_timestamp).encode() + b"." + raw, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, x_signature):
        fail(401, "Invalid webhook signature")
    from pydantic import ValidationError

    try:
        data = Webhook.model_validate_json(raw)
    except ValidationError:
        fail(422, "Invalid provider event payload")
    await advisory_lock(db, "webhook:" + data.event_id)
    old = await db.scalar(
        select(WebhookEvent).where(WebhookEvent.provider == "sandbox", WebhookEvent.event_id == data.event_id)
    )
    payload_hash = hashlib.sha256(raw).hexdigest()
    if old:
        if old.payload_hash != payload_hash:
            fail(409, "Event ID reused with a different payload")
        return {"received": True}
    payment = await get(db, Payment, data.payment_id)
    await lock_payment_shops(db, payment)
    payment = await get(db, Payment, data.payment_id, lock=True)
    if (
        payment.data.get("provider") != "sandbox"
        or payment.amount != data.amount
        or payment.method in {"cod", "bank_transfer"}
    ):
        fail(422, "Payment does not match the provider event")
    if data.status == "paid":
        await capture_payment(db, payment)
        payment.provider_reference = data.reference
    elif payment.status == "pending":
        payment.status = "failed"
    db.add(WebhookEvent(provider="sandbox", event_id=data.event_id, payload_hash=payload_hash))
    return {"received": True}
