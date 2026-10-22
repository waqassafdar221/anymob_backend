from decimal import Decimal

from sqlalchemy import delete, func, or_, select

from anymob.common import advisory_lock, config, fail, get, money, record
from anymob.db import now, uid
from anymob.models import (
    LedgerEntry,
    Order,
    Payment,
    PaymentAllocation,
    Payout,
    PayoutEntry,
    ReturnRequest,
    Shop,
)


async def refund(db, order, amount, reason, key):
    await advisory_lock(db, "refund:" + key)
    previous = await db.scalar(select(Payment).where(Payment.idempotency_key == key))
    if previous:
        if (
            previous.amount != -amount
            or previous.data.get("orderId") != order.id
            or previous.data.get("reason") != reason
        ):
            fail(409, "Refund idempotency key was used with different details")
        return previous
    reserved = await db.scalar(
        select(func.coalesce(func.sum(-Payment.amount), 0)).where(
            Payment.kind == "refund", Payment.status == "pending", Payment.data["orderId"].astext == order.id
        )
    )
    if (
        amount <= 0
        or order.payment_status not in {"paid", "partially_refunded"}
        or amount > order.total - order.refunded_amount - reserved
    ):
        fail(422, "Refund exceeds the remaining paid amount")
    parent = await db.scalar(
        select(Payment)
        .join(PaymentAllocation, PaymentAllocation.payment_id == Payment.id)
        .where(PaymentAllocation.order_id == order.id, Payment.kind == "order_payment")
    )
    if not parent or parent.status != "paid":
        fail(409, "No captured payment is available to refund")
    row = Payment(
        id=uid(),
        user_id=order.buyer_id,
        shop_id=order.shop_id,
        order_group_id=order.order_group_id,
        parent_id=parent.id,
        kind="refund",
        amount=-amount,
        method=order.payment_method,
        idempotency_key=key,
        data={"orderId": order.id, "reason": reason, "collector": order.collector},
    )
    db.add(row)
    await db.flush()
    # A request is not a completed transfer. Settlement requires an acknowledged provider/manual reference.
    return row


async def settle_refund(db, payment, reference):
    if payment.status == "paid":
        return
    if payment.status != "pending" or payment.kind != "refund":
        fail(409, "Refund cannot be settled")
    order = await get(db, Order, payment.data["orderId"], lock=True)
    amount = -payment.amount
    if amount > order.total - order.refunded_amount:
        fail(409, "Other refunds have exhausted this order's remaining balance")
    # Commission reversal is proportional, with exact final reversal to avoid rounding drift.
    old_reversal = (
        money(order.commission * order.refunded_amount / order.total) if order.total else Decimal(0)
    )
    order.refunded_amount += amount
    reversal = (
        money(order.commission * order.refunded_amount / order.total) - old_reversal
        if order.total
        else Decimal(0)
    )
    order.payment_status = "refunded" if order.refunded_amount == order.total else "partially_refunded"
    order.data = {**order.data, "refundPending": False}
    payment.status, payment.paid_at, payment.provider_reference = "paid", now(), reference
    if order.collector == "platform":
        # Delivery deductions are apportioned too; vendor earnings exclude the delivery fee.
        merchandise_refund = (
            (
                money(order.refunded_amount * (order.total - order.delivery_fee) / order.total)
                - money((order.refunded_amount - amount) * (order.total - order.delivery_fee) / order.total)
            )
            if order.total
            else Decimal(0)
        )
        db.add(
            LedgerEntry(
                shop_id=order.shop_id,
                order_id=order.id,
                payment_id=payment.id,
                kind="refund",
                amount=-merchandise_refund,
                event_key="refund:" + payment.id,
            )
        )
    db.add(
        LedgerEntry(
            shop_id=order.shop_id,
            order_id=order.id,
            payment_id=payment.id,
            kind="commission_reversal",
            amount=reversal,
            event_key="reversal:" + payment.id,
        )
    )
    return_id = payment.data.get("returnId")
    if return_id:
        returned = await db.get(ReturnRequest, return_id)
        returned.status = "refunded"
        returned.disputed = False


async def eligible_entries(db, shop):
    held = await db.scalar(
        select(ReturnRequest.id)
        .where(ReturnRequest.shop_id == shop.id, ReturnRequest.disputed.is_(True))
        .limit(1)
    )
    entries = (
        await db.scalars(
            select(LedgerEntry)
            .outerjoin(PayoutEntry, PayoutEntry.ledger_id == LedgerEntry.id)
            .where(LedgerEntry.shop_id == shop.id, PayoutEntry.ledger_id.is_(None))
            .order_by(LedgerEntry.id)
        )
    ).all()
    eligible, pending = [], []
    for entry in entries:
        order = await db.get(Order, entry.order_id) if entry.order_id else None
        open_return = (
            await db.scalar(
                select(ReturnRequest.id)
                .where(
                    ReturnRequest.order_id == entry.order_id,
                    or_(
                        ReturnRequest.status.not_in(["rejected", "refunded"]),
                        ReturnRequest.disputed.is_(True),
                    ),
                )
                .limit(1)
            )
            if order
            else None
        )
        pending_refund = (
            await db.scalar(
                select(Payment.id)
                .where(
                    Payment.kind == "refund",
                    Payment.status == "pending",
                    Payment.data["orderId"].astext == entry.order_id,
                )
                .limit(1)
            )
            if order
            else None
        )
        # Debits are always applied; credits wait for delivery, expiry of return windows and disputes.
        ready = (
            (order is not None and order.payment_status == "refunded")
            or entry.amount <= 0
            or (
                not held
                and order
                and order.status in {"delivered", "picked_up", "returned"}
                and order.hold_until
                and order.hold_until <= now()
                and not open_return
                and not pending_refund
            )
        )
        (eligible if ready else pending).append(entry)
    return eligible, pending, bool(held)


async def earnings(db, shop):
    eligible, pending, held = await eligible_entries(db, shop)
    payouts = (
        await db.scalars(select(Payout).where(Payout.shop_id == shop.id).order_by(Payout.created_at.desc()))
    ).all()
    paid = sum((p.net_amount for p in payouts if p.status == "paid"), Decimal(0))
    return {
        "available": float(sum((e.amount for e in eligible), Decimal(0))),
        "pending": float(sum((e.amount for e in pending), Decimal(0))),
        "paid": float(paid),
        "onHold": held,
        "accountReady": bool(shop.payout_account),
        "destination": shop.payout_mask,
        # `eligible` marks entries that can be paid out now; the rest are still in their holding window.
        "rows": [{**record(e), "eligible": True} for e in eligible]
        + [{**record(e), "eligible": False} for e in pending],
        "payouts": [record(p) for p in payouts],
    }


async def request_payout(db, shop):
    # Caller must hold the shop row lock; all payout/refund settlements use that same lock.
    if not shop.payout_account:
        fail(422, "Save a payout account first")
    entries, _, held = await eligible_entries(db, shop)
    if held:
        fail(409, "Resolve open disputes before requesting a payout")
    amount = sum((e.amount for e in entries), Decimal(0))
    rules = await config(db, "commerce")
    if amount <= 0 or amount < money(rules["minimumPayout"]):
        fail(422, "Available balance is below the payout minimum")
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    next_week = now().astimezone(ZoneInfo("Asia/Karachi"))
    next_week = (next_week + timedelta(days=7 - next_week.weekday())).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    payout = Payout(
        scheduled_at=next_week if rules["payoutSchedule"] == "weekly" else None,
        id=uid(),
        shop_id=shop.id,
        net_amount=amount,
        destination=shop.payout_mask,
        account_snapshot=shop.payout_account,
        status="processing" if rules["payoutSchedule"] == "weekly" else "pending",
    )
    db.add(payout)
    await db.flush()
    for entry in entries:
        db.add(PayoutEntry(ledger_id=entry.id, payout_id=payout.id))
    return payout


async def payout_action(db, payout, data):
    if payout.status in {"paid", "failed"}:
        fail(409, "Payout has already been finalized")
    if data.action == "paid":
        if not data.reference:
            fail(422, "A transfer reference is required")
        shop = await db.get(Shop, payout.shop_id)
        if shop.is_suspended or shop.is_deleted:
            fail(409, "Store is suspended")
        if await db.scalar(
            select(ReturnRequest.id)
            .where(ReturnRequest.shop_id == payout.shop_id, ReturnRequest.disputed.is_(True))
            .limit(1)
        ):
            fail(409, "Store has an open dispute")
        linked_orders = (
            select(LedgerEntry.order_id)
            .join(PayoutEntry, PayoutEntry.ledger_id == LedgerEntry.id)
            .where(PayoutEntry.payout_id == payout.id)
        )
        if await db.scalar(
            select(ReturnRequest.id)
            .where(
                ReturnRequest.order_id.in_(linked_orders),
                ReturnRequest.status.not_in(["rejected", "refunded"]),
            )
            .limit(1)
        ):
            fail(409, "A payout order now has an open return")
        if await db.scalar(
            select(Payment.id)
            .where(
                Payment.kind == "refund",
                Payment.status == "pending",
                Payment.data["orderId"].astext.in_(linked_orders),
            )
            .limit(1)
        ):
            fail(409, "A payout order has an unsettled refund")
        entries, _, _ = await eligible_entries(db, shop)
        if sum((e.amount for e in entries), Decimal(0)) < 0:
            fail(409, "New refund deductions require rejecting and recalculating this payout")
        payout.status, payout.reference, payout.paid_at = "paid", data.reference, now()
    elif data.action == "reject":
        payout.status = "failed"
        await db.execute(delete(PayoutEntry).where(PayoutEntry.payout_id == payout.id))
    elif data.action == "hold":
        payout.status = "on_hold"
    elif data.action == "schedule":
        payout.status, payout.scheduled_at = "processing", now()
    payout.note = data.note
    return payout
