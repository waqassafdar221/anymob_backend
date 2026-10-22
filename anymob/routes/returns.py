from datetime import timedelta

from fastapi import APIRouter, Request
from sqlalchemy import select

from anymob.common import DB, Page, audit, config, fail, get, owned_shop, paginated, record
from anymob.db import now
from anymob.models import Order, OrderItem, ReturnRequest, Shop, Variant
from anymob.routes.orders import Key
from anymob.schemas import ReturnAction, ReturnInput
from anymob.security import Actor
from anymob.services.admin_orders import return_view
from anymob.services.catalog import validate_media
from anymob.services.commerce import adjust_stock, notify
from anymob.services.finance import refund

router = APIRouter(tags=["Returns and disputes"])


@router.get("/returns")
async def returns(actor: Actor, db: DB, page: Page):
    return await paginated(
        db,
        select(ReturnRequest)
        .where(ReturnRequest.buyer_id == actor.id)
        .order_by(ReturnRequest.created_at.desc()),
        page,
        return_view,
    )


@router.get("/vendor/returns")
async def vendor_returns(actor: Actor, db: DB, page: Page):
    shop = await owned_shop(db, actor)
    return await paginated(
        db,
        select(ReturnRequest)
        .where(ReturnRequest.shop_id == shop.id)
        .order_by(ReturnRequest.created_at.desc()),
        page,
        return_view,
    )


@router.post("/returns", status_code=201)
async def create_return(data: ReturnInput, actor: Actor, db: DB):
    order = await get(db, Order, data.order_id)
    if order.buyer_id != actor.id:
        fail(404, "Order not found")
    await get(db, Shop, order.shop_id, lock=True)
    order = await get(db, Order, data.order_id, lock=True)
    if order.buyer_id != actor.id:
        fail(404, "Order not found")
    if (
        order.status not in {"delivered", "picked_up"}
        or not order.completed_at
        or order.data["returnDays"] == 0
        or now() > order.completed_at + timedelta(days=order.data["returnDays"])
    ):
        fail(409, "This order is outside its return window")
    if data.reason in {"damaged", "defective"} and not data.images:
        fail(422, "Photo evidence is required")
    await validate_media(db, actor.id, data.images)
    originals = {
        i.variant_id: i
        for i in (await db.scalars(select(OrderItem).where(OrderItem.order_id == order.id))).all()
    }
    previous = (await db.scalars(select(ReturnRequest).where(ReturnRequest.order_id == order.id))).all()
    quantities = {}
    for prior in previous:
        if prior.status != "rejected" or prior.disputed:
            for item in prior.items:
                quantities[item["variantId"]] = quantities.get(item["variantId"], 0) + item["quantity"]
    seen = set()
    for item in data.items:
        original = originals.get(item.variant_id)
        if (
            item.variant_id in seen
            or not original
            or item.quantity + quantities.get(item.variant_id, 0) > original.quantity
        ):
            fail(422, "Return quantities exceed the purchased items")
        seen.add(item.variant_id)
    row = ReturnRequest(
        order_id=order.id,
        buyer_id=actor.id,
        shop_id=order.shop_id,
        reason=data.reason,
        details=data.details,
        items=[i.model_dump(by_alias=True) for i in data.items],
        images=data.images,
    )
    db.add(row)
    await db.flush()
    shop = await db.get(Shop, order.shop_id)
    await notify(db, shop.owner_id, "Return requested", data.details, "/vendor/orders/" + order.id)
    return return_view(row)


@router.get("/returns/{id}")
async def return_detail(id: str, actor: Actor, db: DB):
    row = await get(db, ReturnRequest, id)
    if row.buyer_id != actor.id:
        shop = await owned_shop(db, actor)
        if shop.id != row.shop_id:
            fail(404, "Return not found")
    return return_view(row)


async def apply_return_action(db, row, data, actor, request, key):
    # Lock order is store -> order -> return across refund/payout operations.
    order = await get(db, Order, row.order_id, lock=True)
    row = await get(db, ReturnRequest, row.id, lock=True)
    shop_owner = (await db.get(Shop, row.shop_id)).owner_id
    before = record(row)
    if data.action == "escalate":
        if row.buyer_id != actor.id:
            fail(403, "Only the buyer may escalate a return")
        hours = (await config(db, "commerce"))["disputeEscalationHours"]
        if row.disputed or row.status == "refunded" or row.resolution:
            fail(409, "This return cannot be escalated")
        if row.status != "rejected" and now() < row.created_at + timedelta(hours=hours):
            fail(409, "Allow the store's response window before escalating")
        row.disputed = True
        row.data = {**row.data, "dispute": {"claim": data.note, "escalatedAt": now().isoformat()}}
        await notify(db, shop_owner, "Return escalated to AnyMob", data.note, "/vendor/orders/" + order.id)
    elif data.action in {"resolve_buyer", "resolve_store", "request_information"}:
        if not row.disputed or row.resolution:
            fail(409, "This dispute is not open")
        if data.action == "request_information":
            recipient = row.buyer_id if data.side == "buyer" else shop_owner
            href = "/orders/" + order.id if data.side == "buyer" else "/vendor/orders/" + order.id
            row.data = {
                **row.data,
                "informationRequests": [
                    *row.data.get("informationRequests", []),
                    {"side": data.side, "note": data.note, "at": now().isoformat()},
                ],
            }
            await notify(db, recipient, "Dispute information requested", data.note, href)
        else:
            outcome = "buyer" if data.action == "resolve_buyer" else "store"
            row.resolution = outcome
            row.data = {
                **row.data,
                "dispute": {
                    **row.data.get("dispute", {}),
                    "resolutionNote": data.note,
                    "resolvedAt": now().isoformat(),
                },
            }
            if outcome == "buyer" and data.amount:
                # The refund settles later; funds stay held while the return is disputed.
                payment = await refund(db, order, data.amount, data.note, "dispute:" + row.id + ":" + key)
                payment.data = {**payment.data, "returnId": row.id}
                row.status = "refund_pending"
                row.data = {
                    **row.data,
                    "refundAmount": float(row.data.get("refundAmount", 0)) + float(data.amount),
                }
            elif outcome == "buyer":
                row.status = "approved"
            else:
                if row.status not in {"received", "refunded"}:
                    row.status = "rejected"
                row.disputed = False
            for recipient, href in ((row.buyer_id, "/orders/"), (shop_owner, "/vendor/orders/")):
                await notify(db, recipient, f"Dispute resolved for the {outcome}", data.note, href + order.id)
    else:
        if row.disputed and actor.role != "admin":
            fail(409, "This return is under admin review")
        if data.action == "approve" and row.status == "requested":
            row.status = "approved"
        elif data.action == "reject" and row.status == "requested":
            row.status = "rejected"
        elif data.action == "received" and row.status == "approved":
            row.status = "received"
            if data.restock and not row.restocked:
                for item in sorted(row.items, key=lambda i: i["variantId"]):
                    variant = await get(db, Variant, item["variantId"], lock=True)
                    await adjust_stock(db, variant, item["quantity"], "return_received", order.id)
                row.restocked = True
        elif data.action == "refund" and row.status in {"received", "refund_pending"}:
            if not data.amount or data.amount <= 0:
                fail(422, "Enter a refund amount")
            items = (await db.scalars(select(OrderItem).where(OrderItem.order_id == order.id))).all()
            quantities = {i["variantId"]: i["quantity"] for i in row.items}
            basis = sum(i.unit_price * quantities.get(i.variant_id, 0) for i in items)
            from anymob.common import money

            limit = (
                money(basis * (order.subtotal - order.discount) / order.subtotal)
                if order.subtotal
                else money(0)
            )
            from sqlalchemy import func

            from anymob.models import Payment

            request_key = "return:" + row.id + ":" + key
            prior_refunds = await db.scalar(
                select(func.coalesce(func.sum(-Payment.amount), 0)).where(
                    Payment.kind == "refund",
                    Payment.status.in_(["pending", "paid"]),
                    Payment.data["returnId"].astext == row.id,
                    Payment.idempotency_key != request_key,
                )
            )
            if data.amount + prior_refunds > limit:
                fail(422, "Refund exceeds the returned items' discounted value")
            payment = await refund(db, order, data.amount, data.note, "return:" + row.id + ":" + key)
            payment.data = {**payment.data, "returnId": row.id}
            row.status = "refund_pending"
            row.data = {
                **row.data,
                "refundAmount": float(row.data.get("refundAmount", 0)) + float(data.amount),
            }
        else:
            fail(409, "Invalid return status transition")
    row.data = {
        **row.data,
        "notes": [
            *row.data.get("notes", []),
            {"authorId": actor.id, "note": data.note, "at": now().isoformat(), "action": data.action},
        ],
    }
    await audit(db, actor, request, "return." + data.action, row, before=before, note=data.note)
    return return_view(row)


@router.post("/returns/{id}/escalate")
async def escalate(id: str, data: ReturnAction, key: Key, actor: Actor, request: Request, db: DB):
    if data.action != "escalate":
        fail(422, "Use the escalation action")
    row = await get(db, ReturnRequest, id)
    if row.buyer_id != actor.id:
        fail(404, "Return not found")
    await get(db, Shop, row.shop_id, lock=True)
    return await apply_return_action(db, row, data, actor, request, key)


@router.post("/vendor/returns/{id}/action")
async def vendor_action(id: str, data: ReturnAction, key: Key, actor: Actor, request: Request, db: DB):
    shop = await owned_shop(db, actor, lock=True)
    row = await get(db, ReturnRequest, id)
    if row.shop_id != shop.id:
        fail(404, "Return not found")
    if data.action not in {"approve", "reject", "received", "refund"}:
        fail(403, "This action requires admin access")
    return await apply_return_action(db, row, data, actor, request, key)
