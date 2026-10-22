"""Admin revenue views: payments as transactions with payer and reference, and payouts with their ledger breakdown."""

import json
from decimal import Decimal

from cryptography.fernet import InvalidToken
from sqlalchemy import func, select

from anymob.models import (
    FeaturedPackage,
    LedgerEntry,
    Order,
    Payment,
    PaymentAllocation,
    Payout,
    PayoutEntry,
    Plan,
    ReturnRequest,
    Shop,
    User,
)
from anymob.security import decrypt

KINDS = {"featured_package": "featured_ad"}


async def transaction_rows(db, payments) -> list[dict]:
    if not payments:
        return []
    ids = [p.id for p in payments]
    allocations: dict[str, list[str]] = {}
    for payment_id, order_id in (
        await db.execute(
            select(PaymentAllocation.payment_id, PaymentAllocation.order_id).where(
                PaymentAllocation.payment_id.in_(ids)
            )
        )
    ).all():
        allocations.setdefault(payment_id, []).append(order_id)
    # Refunds point at their order through their data.
    order_ids = {o for orders in allocations.values() for o in orders} | {
        p.data["orderId"] for p in payments if p.data.get("orderId")
    }
    orders = {o.id: o for o in (await db.scalars(select(Order).where(Order.id.in_(order_ids)))).all()}
    users = {
        u.id: u.full_name
        for u in (await db.scalars(select(User).where(User.id.in_({p.user_id for p in payments})))).all()
    }
    shop_ids = {p.shop_id for p in payments if p.shop_id} | {o.shop_id for o in orders.values()}
    shops = {s.id: s.name for s in (await db.scalars(select(Shop).where(Shop.id.in_(shop_ids)))).all()}
    plans = {p.id: p.name for p in (await db.scalars(select(Plan))).all()}
    packages = {p.id: p.name for p in (await db.scalars(select(FeaturedPackage))).all()}
    # Refunds requested but not yet settled already count against what remains.
    reserved: dict[str, Decimal] = {}
    for data, amount in (
        await db.execute(
            select(Payment.data, Payment.amount).where(Payment.kind == "refund", Payment.status == "pending")
        )
    ).all():
        if data.get("orderId") in orders:
            reserved[data["orderId"]] = reserved.get(data["orderId"], Decimal(0)) - amount
    rows = []
    for p in payments:
        linked = allocations.get(p.id) or ([p.data["orderId"]] if p.data.get("orderId") else [])
        order = orders.get(linked[0]) if len(linked) == 1 else None
        shop_id = p.shop_id or (order.shop_id if order else None)
        refundable = Decimal(0)
        if p.kind == "order_payment" and order and order.payment_status in {"paid", "partially_refunded"}:
            refundable = max(
                Decimal(0), order.total - order.refunded_amount - Decimal(reserved.get(order.id) or 0)
            )
        status = p.status
        if p.kind == "order_payment" and order and order.payment_status == "refunded":
            status = "refunded"
        reference_name = (
            ", ".join(orders[o].order_number for o in linked if o in orders)
            or plans.get(p.data.get("planId"))
            or packages.get(p.data.get("packageId"))
            or p.provider_reference
            or p.id
        )
        rows.append(
            {
                "id": p.id,
                "userId": p.user_id,
                "shopId": shop_id,
                "planId": p.data.get("planId"),
                "packageId": p.data.get("packageId"),
                "orderId": order.id if order else None,
                "orderIds": linked,
                "parentTransactionId": p.parent_id,
                "reason": p.data.get("reason"),
                "type": KINDS.get(p.kind, p.kind),
                "amount": float(p.amount),
                "currency": "PKR",
                "status": status,
                "method": p.method,
                "reference": p.provider_reference or "",
                "createdAt": p.created_at.isoformat(),
                "paidAt": p.paid_at.isoformat() if p.paid_at else None,
                "payer": users.get(p.user_id, "Deleted user"),
                "storeName": shops.get(shop_id, "—") if shop_id else "—",
                "referenceName": reference_name,
                "referenceHref": f"/admin/orders/{order.id}"
                if order
                else "/admin/plans"
                if p.data.get("planId")
                else None,
                "refundable": float(refundable),
                # Pending bank transfers are confirmed by hand; so are pending refunds once sent.
                "canMarkPaid": p.status == "pending"
                and (p.kind == "refund" or p.method == "bank_transfer")
                and not (order and order.status == "cancelled" and p.kind != "refund"),
            }
        )
    return rows


async def open_disputes(db, shop_ids) -> dict[str, int]:
    if not shop_ids:
        return {}
    return dict(
        (
            await db.execute(
                select(ReturnRequest.shop_id, func.count(func.distinct(ReturnRequest.order_id)))
                .where(ReturnRequest.shop_id.in_(shop_ids), ReturnRequest.disputed.is_(True))
                .group_by(ReturnRequest.shop_id)
            )
        ).all()
    )


async def payout_rows(db, payouts) -> list[dict]:
    if not payouts:
        return []
    ids = [p.id for p in payouts]
    entries: dict[str, list[LedgerEntry]] = {}
    for payout_id, entry in (
        await db.execute(
            select(PayoutEntry.payout_id, LedgerEntry)
            .join(LedgerEntry, LedgerEntry.id == PayoutEntry.ledger_id)
            .where(PayoutEntry.payout_id.in_(ids))
        )
    ).all():
        entries.setdefault(payout_id, []).append(entry)
    shops = {
        s.id: s
        for s in (await db.scalars(select(Shop).where(Shop.id.in_({p.shop_id for p in payouts})))).all()
    }
    owners = {
        u.id: u.full_name
        for u in (
            await db.scalars(select(User).where(User.id.in_({s.owner_id for s in shops.values()})))
        ).all()
    }
    disputes = await open_disputes(db, list(shops))
    rows = []
    for p in payouts:
        lines = entries.get(p.id, [])

        def total(*kinds, lines=lines):
            return float(sum((e.amount for e in lines if e.kind in kinds), Decimal(0)))

        shop = shops.get(p.shop_id)
        count = disputes.get(p.shop_id, 0)
        created = p.created_at.isoformat()
        dates = sorted(e.created_at for e in lines)
        rows.append(
            {
                "id": p.id,
                "shopId": p.shop_id,
                "periodStart": dates[0].isoformat() if dates else created,
                "periodEnd": dates[-1].isoformat() if dates else created,
                "grossSales": total("sale"),
                "commission": -total("commission", "commission_reversal"),
                "refunds": -total("refund"),
                "netAmount": float(p.net_amount),
                "status": p.status,
                "method": (p.destination.split(":")[0] or "bank_transfer")
                if p.destination
                else "bank_transfer",
                "destination": p.destination,
                "reference": p.reference or "",
                "createdAt": created,
                "paidAt": p.paid_at.isoformat() if p.paid_at else None,
                "scheduledAt": p.scheduled_at.isoformat() if p.scheduled_at else None,
                "holdReason": p.note if p.status == "on_hold" else None,
                "rejectionReason": p.note if p.status == "failed" else None,
                "orderIds": sorted({e.order_id for e in lines if e.order_id}),
                "storeName": shop.name if shop else "Deleted store",
                "ownerName": owners.get(shop.owner_id, "—") if shop else "—",
                "accountLabel": p.destination.split(":", 1)[-1].strip()
                if p.destination
                else "Not configured",
                "displayStatus": "on_hold"
                if count and p.status in {"pending", "processing", "on_hold"}
                else p.status,
                "openDisputes": count,
                "orderCount": len({e.order_id for e in lines if e.order_id}),
            }
        )
    return rows


def account(payout: Payout) -> dict:
    """The full destination the payout was requested to, for the admin sending the transfer."""
    try:
        details = json.loads(decrypt(payout.account_snapshot))
    except (InvalidToken, ValueError):
        return {"bankName": "", "title": "", "destination": "Unreadable account snapshot"}
    return {
        "bankName": details.get("bank_name", ""),
        "title": details.get("title", ""),
        "destination": details.get("iban") or details.get("wallet") or "",
    }


async def payout_detail(db, payout: Payout) -> dict:
    from anymob.services.admin_orders import order_rows
    from anymob.services.catalog import shop_view

    row = (await payout_rows(db, [payout]))[0]
    shop = await db.get(Shop, payout.shop_id)
    orders = (
        (await db.scalars(select(Order).where(Order.id.in_(row["orderIds"])))).all()
        if row["orderIds"]
        else []
    )
    return {
        "payout": row,
        "shop": shop_view(shop),
        "orders": await order_rows(db, orders),
        "account": account(payout),
        "deductions": row["refunds"],
    }
