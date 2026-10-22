from typing import Annotated

from fastapi import APIRouter, Header, Request
from sqlalchemy import select

from anymob.common import DB, Page, advisory_lock, audit, fail, get, owned_shop, paginated, record
from anymob.models import CartItem, Order, OrderGroup, Payment, PaymentAllocation, Product, Variant
from anymob.schemas import CartLine, Checkout, Note, OrderTransition
from anymob.security import Actor, throttle
from anymob.services.catalog import product_view, visible_products
from anymob.services.commerce import (
    cancel_order,
    capture_payment,
    checkout,
    order_view,
    order_views,
    transition_order,
)

router = APIRouter(tags=["Cart and orders"])
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=100)]


@router.get("/cart")
async def cart(actor: Actor, db: DB):
    rows = (await db.scalars(select(CartItem).where(CartItem.user_id == actor.id))).all()
    result = []
    for row in rows:
        variant = await db.get(Variant, row.variant_id)
        product = await db.scalar(visible_products().where(Product.id == variant.product_id))
        result.append(
            {
                **record(row),
                "available": bool(product and variant.is_active and variant.stock >= row.quantity),
                "variant": record(variant),
                "product": await product_view(db, product) if product else None,
            }
        )
    return result


@router.put("/cart/items")
async def cart_item(data: CartLine, actor: Actor, db: DB):
    await advisory_lock(db, "cart:" + actor.id)
    variant = await get(db, Variant, data.variant_id)
    product = await db.scalar(visible_products().where(Product.id == variant.product_id))
    if not product or not variant.is_active:
        fail(404, "Product not available")
    row = await db.scalar(
        select(CartItem).where(CartItem.user_id == actor.id, CartItem.variant_id == data.variant_id)
    )
    if row is None:
        row = CartItem(user_id=actor.id, variant_id=data.variant_id)
        db.add(row)
    row.quantity, row.saved = data.quantity, data.saved
    await db.flush()
    return record(row)


@router.delete("/cart/items/{variant_id}")
async def delete_cart_item(variant_id: str, actor: Actor, db: DB):
    row = await db.scalar(
        select(CartItem).where(CartItem.user_id == actor.id, CartItem.variant_id == variant_id)
    )
    if row:
        await db.delete(row)
    return {"ok": True}


@router.post("/checkout", status_code=201)
async def place_order(data: Checkout, key: Key, actor: Actor, db: DB):
    await throttle("checkout:" + actor.id, 20, 60)
    group = await checkout(db, actor, data, key)
    await db.flush()
    rows = (
        await db.scalars(select(Order).where(Order.order_group_id == group.id).order_by(Order.created_at))
    ).all()
    payments = (await db.scalars(select(Payment).where(Payment.order_group_id == group.id))).all()
    return {
        "orderGroup": record(group),
        "orders": await order_views(db, rows, buyer=True),
        "payments": [record(p) for p in payments],
    }


@router.get("/orders")
async def orders(actor: Actor, db: DB, page: Page, status: str | None = None):
    query = select(Order).where(Order.buyer_id == actor.id)
    if status:
        query = query.where(Order.status == status)
    result = await paginated(db, query.order_by(Order.created_at.desc()), page, lambda row: row)
    result["items"] = await order_views(db, result["items"], buyer=True)
    return result


@router.get("/order-groups/{id}")
async def order_group(id: str, actor: Actor, db: DB):
    group = await get(db, OrderGroup, id)
    if group.buyer_id != actor.id:
        fail(404, "Order group not found")
    rows = (await db.scalars(select(Order).where(Order.order_group_id == id))).all()
    return {"orderGroup": record(group), "orders": await order_views(db, rows, buyer=True)}


@router.get("/orders/{id}")
async def buyer_order(id: str, actor: Actor, db: DB):
    row = await get(db, Order, id)
    if row.buyer_id != actor.id:
        fail(404, "Order not found")
    return await order_view(db, row, buyer=True)


@router.post("/orders/{id}/cancel")
async def buyer_cancel(id: str, data: Note, actor: Actor, db: DB):
    order = await get(db, Order, id)
    if order.buyer_id != actor.id:
        fail(404, "Order not found")
    from anymob.models import Shop

    await get(db, Shop, order.shop_id, lock=True)
    order = await get(db, Order, id, lock=True)
    if order.buyer_id != actor.id:
        fail(404, "Order not found")
    await cancel_order(db, order, actor.id, data.note)
    return await order_view(db, order, buyer=True)


@router.get("/vendor/orders")
async def vendor_orders(actor: Actor, db: DB, page: Page, status: str | None = None, search: str = ""):
    shop = await owned_shop(db, actor)
    query = select(Order).where(Order.shop_id == shop.id)
    if status:
        query = query.where(Order.status == status)
    if search:
        query = query.where(Order.order_number.ilike(f"%{search[:100]}%"))
    result = await paginated(db, query.order_by(Order.created_at.desc()), page, lambda row: row)
    result["items"] = await order_views(db, result["items"])
    return result


@router.get("/vendor/orders/{id}")
async def vendor_order(id: str, actor: Actor, db: DB):
    shop = await owned_shop(db, actor)
    order = await get(db, Order, id)
    if order.shop_id != shop.id:
        fail(404, "Order not found")
    return {**await order_view(db, order), "internalNote": order.data.get("internalNote", "")}


@router.post("/vendor/orders/{id}/transition")
async def vendor_transition(id: str, data: OrderTransition, actor: Actor, db: DB, request: Request):
    shop = await owned_shop(db, actor, lock=True)
    order = await get(db, Order, id, lock=True)
    if order.shop_id != shop.id:
        fail(404, "Order not found")
    if data.status == "picked_up":
        await throttle("pickup:" + id, 5, 300)
    before = record(order)
    await transition_order(db, order, data, actor.id)
    await audit(db, actor, request, "order.transition", order, before=before, note=data.note)
    return await order_view(db, order)


@router.post("/vendor/orders/{id}/confirm-payment")
async def confirm_bank_transfer(id: str, data: Note, actor: Actor, db: DB, request: Request):
    shop = await owned_shop(db, actor, lock=True)
    order = await get(db, Order, id, lock=True)
    if order.shop_id != shop.id:
        fail(404, "Order not found")
    if order.payment_method != "bank_transfer":
        fail(409, "Only bank transfers may be manually confirmed")
    payment = await db.scalar(
        select(Payment)
        .join(PaymentAllocation, PaymentAllocation.payment_id == Payment.id)
        .where(PaymentAllocation.order_id == id, Payment.kind == "order_payment")
        .with_for_update(of=Payment)
    )
    before = record(payment)
    await capture_payment(db, payment)
    await audit(db, actor, request, "payment.bank_confirmed", payment, before=before, note=data.note)
    return await order_view(db, order)


@router.put("/vendor/orders/{id}/note")
async def order_note(id: str, data: Note, actor: Actor, db: DB):
    shop = await owned_shop(db, actor)
    order = await get(db, Order, id, lock=True)
    if order.shop_id != shop.id:
        fail(404, "Order not found")
    order.data = {**order.data, "internalNote": data.note}
    return {"note": data.note}
