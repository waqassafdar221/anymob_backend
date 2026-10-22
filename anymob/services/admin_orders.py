"""Admin views of orders: list rows with buyer and store, and the full order dossier."""

from decimal import Decimal

from sqlalchemy import func, select

from anymob.common import record
from anymob.models import (
    Conversation,
    InternalNote,
    LedgerEntry,
    Message,
    Order,
    OrderItem,
    ReturnRequest,
    Shop,
    User,
)
from anymob.services.catalog import shop_view


def return_view(row: ReturnRequest) -> dict:
    """A return with its dispute history flattened for display."""
    notes = row.data.get("notes", [])
    response = next(
        (
            n["note"]
            for n in reversed(notes)
            if n.get("action") in {"approve", "reject", "received", "refund"} and n.get("note")
        ),
        "",
    )
    dispute = row.data.get("dispute", {})
    return {
        **record(row, exclude={"data"}),
        "resolution": (row.resolution or "").removeprefix("resolve_") or None,
        "notes": response,
        "claim": dispute.get("claim", ""),
        "escalatedAt": dispute.get("escalatedAt"),
        "resolutionNote": dispute.get("resolutionNote", ""),
        "resolvedAt": dispute.get("resolvedAt"),
        "informationRequests": row.data.get("informationRequests", []),
        "refundAmount": row.data.get("refundAmount"),
    }


async def order_rows(db, orders) -> list[dict]:
    """Orders with items, buyer, store and named timeline actors, in a fixed number of queries."""
    if not orders:
        return []
    ids = [o.id for o in orders]
    items: dict[str, list] = {}
    for item in (await db.scalars(select(OrderItem).where(OrderItem.order_id.in_(ids)))).all():
        items.setdefault(item.order_id, []).append({**item.snapshot, **record(item, exclude={"snapshot"})})
    shops = {
        s.id: s
        for s in (await db.scalars(select(Shop).where(Shop.id.in_({o.shop_id for o in orders})))).all()
    }
    people = {o.buyer_id for o in orders} | {
        entry.get("actor") for o in orders for entry in o.timeline if entry.get("actor")
    }
    users = {u.id: u for u in (await db.scalars(select(User).where(User.id.in_(people)))).all()}
    rows = []
    for order in orders:
        shop, buyer = shops.get(order.shop_id), users.get(order.buyer_id)
        lines = items.get(order.id, [])
        rows.append(
            {
                **{k: v for k, v in order.data.items() if k != "internalNote"},
                **record(order, exclude={"data"}),
                "items": lines,
                "timeline": [
                    {**entry, "actor": users[entry["actor"]].full_name}
                    if entry.get("actor") in users
                    else entry
                    for entry in order.timeline
                ],
                "buyer": record(buyer) if buyer else None,
                "shop": shop_view(shop) if shop else None,
                "itemCount": sum(line["quantity"] for line in lines),
                "city": (order.address or {}).get("city") or (shop.city if shop else ""),
            }
        )
    return rows


async def order_dossier(db, order: Order) -> dict:
    commission_entries = await db.scalar(
        select(func.count())
        .select_from(LedgerEntry)
        .where(LedgerEntry.order_id == order.id, LedgerEntry.kind == "commission")
    )
    booked = await db.scalar(
        select(func.coalesce(func.sum(LedgerEntry.amount), 0)).where(
            LedgerEntry.order_id == order.id, LedgerEntry.kind.in_(["commission", "commission_reversal"])
        )
    )
    # Commission is booked when the order is paid; before that it is the agreed rate on the order.
    commission = -Decimal(booked) if commission_entries else order.commission
    merchandise = order.total - order.delivery_fee
    refunded = order.refunded_amount * merchandise / order.total if order.total else Decimal(0)
    earnings = (
        Decimal(0) if order.status == "cancelled" else max(Decimal(0), merchandise - refunded - commission)
    )

    notes = (
        await db.scalars(
            select(InternalNote)
            .where(InternalNote.target_type == "order", InternalNote.target_id == order.id)
            .order_by(InternalNote.created_at)
        )
    ).all()
    conversations = select(Conversation.id).where(
        Conversation.subject_type == "order", Conversation.subject_id == order.id
    )
    messages = (
        await db.scalars(
            select(Message).where(Message.conversation_id.in_(conversations)).order_by(Message.created_at)
        )
    ).all()
    names = {
        u.id: u.full_name
        for u in (
            await db.scalars(
                select(User).where(
                    User.id.in_({n.author_id for n in notes} | {m.sender_id for m in messages})
                )
            )
        ).all()
    }
    returns = (
        await db.scalars(
            select(ReturnRequest).where(ReturnRequest.order_id == order.id).order_by(ReturnRequest.created_at)
        )
    ).all()
    return {
        "order": (await order_rows(db, [order]))[0],
        "commission": float(round(commission, 2)),
        "earnings": float(round(earnings, 2)),
        "returns": [return_view(r) for r in returns],
        "notes": [
            {"at": n.created_at.isoformat(), "actor": names.get(n.author_id, "Admin"), "note": n.body}
            for n in notes
        ],
        "messages": [
            {
                **record(m),
                "sentAt": m.created_at.isoformat(),
                "sender": names.get(m.sender_id, m.sender_id),
            }
            for m in messages
        ],
    }
