import secrets
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import delete, select

from anymob.common import advisory_lock, config, digest, fail, fingerprint, get, money, record
from anymob.config import settings
from anymob.db import now, uid
from anymob.models import (
    Address,
    CartItem,
    Category,
    Coupon,
    InventoryMovement,
    LedgerEntry,
    Notification,
    Order,
    OrderGroup,
    OrderItem,
    Payment,
    PaymentAllocation,
    Plan,
    Product,
    Shop,
    User,
    Variant,
)


async def commission_rate(db, shop, category_id):
    if shop.commission_override is not None:
        return shop.commission_override
    category = await db.get(Category, category_id)
    while category:
        if category.commission_override is not None:
            return category.commission_override
        category = await db.get(Category, category.parent_id) if category.parent_id else None
    plan = await db.get(Plan, shop.plan_id) if shop.plan_id else None
    if plan and plan.commission_rate is not None:
        return plan.commission_rate
    return Decimal(str((await config(db, "revenue"))["defaultCommission"]))


async def order_view(db, order, *, buyer=False):
    return (await order_views(db, [order], buyer=buyer))[0]


async def order_views(db, orders, *, buyer=False) -> list[dict]:
    """Orders with their items, loaded in one query for the whole list."""
    orders = list(orders)
    if not orders:
        return []
    items: dict[str, list] = {}
    for item in (
        await db.scalars(select(OrderItem).where(OrderItem.order_id.in_({o.id for o in orders})))
    ).all():
        items.setdefault(item.order_id, []).append(item)
    views = []
    for order in orders:
        data = {k: v for k, v in order.data.items() if k not in {"internalNote", "pickupCode"}}
        if buyer and "pickupCode" in order.data:
            data["pickupCode"] = order.data["pickupCode"]
        views.append(
            {
                **data,
                **record(order, exclude={"data"}),
                "items": [{**i.snapshot, **record(i, exclude={"snapshot"})} for i in items.get(order.id, [])],
            }
        )
    return views


async def notify(db, user_id, title, body, href="", *, event=None, variables=None):
    from anymob.models import Outbox

    if event:
        import re

        templates = (await config(db, "notifications"))["templates"]
        template = templates.get(event)
        user = await db.get(User, user_id)
        values = {
            "site_name": (await config(db, "general"))["siteName"],
            "customer_name": user.full_name,
            "reason": body,
            **(variables or {}),
        }

        def render(value):
            return re.sub(r"\{\{\s*([a-z_]+)\s*\}\}", lambda match: str(values.get(match[1], "")), value)

        if template:
            title, body = render(template["emailSubject"]), render(template["emailBody"])
            if user.is_verified:
                db.add(
                    Outbox(
                        kind="email",
                        payload={"to": user.email, "subject": title, "body": body},
                        event_key="notification-email:" + uid(),
                        available_at=now(),
                    )
                )
                if settings().sms_provider != "disabled":
                    db.add(
                        Outbox(
                            kind="sms",
                            payload={"to": user.phone, "body": render(template["sms"])},
                            event_key="notification-sms:" + uid(),
                            available_at=now(),
                        )
                    )
    db.add(Notification(user_id=user_id, title=title, body=body, href=href))


async def adjust_stock(db, variant, quantity, reason, order_id=None):
    if variant.stock + quantity < 0:
        fail(409, "Insufficient stock")
    variant.stock += quantity
    db.add(
        InventoryMovement(
            variant_id=variant.id, order_id=order_id, delta=quantity, stock_after=variant.stock, reason=reason
        )
    )


async def checkout(db, actor, data, key):
    await advisory_lock(db, f"checkout:{actor.id}:{key}")
    request_hash = fingerprint(data.model_dump(mode="json"))
    previous = await db.scalar(
        select(OrderGroup).where(OrderGroup.buyer_id == actor.id, OrderGroup.idempotency_key == key)
    )
    if previous:
        if previous.request_hash != request_hash:
            fail(409, "This idempotency key was used for a different checkout")
        return previous
    if (await config(db, "maintenance"))["enabled"]:
        fail(503, "Checkout is temporarily unavailable during maintenance")
    rules = await config(db, "commerce")
    if data.payment_method not in rules["paymentMethods"]:
        fail(422, "This payment method is disabled")
    online = data.payment_method not in {"cod", "bank_transfer"}
    if online and settings().payment_provider == "disabled":
        fail(503, "Online payments have not been configured")
    if len({s.shop_id for s in data.shops}) != len(data.shops):
        fail(422, "Each store can appear only once")
    address = None
    if any(s.fulfillment == "delivery" for s in data.shops):
        address = await db.get(Address, data.address_id) if data.address_id else None
        if not address or address.user_id != actor.id:
            fail(422, "Choose one of your saved delivery addresses")
    # Stable shop then variant lock ordering avoids races between checkout and vendor edits.
    shops = {
        s.id: s
        for s in (
            await db.scalars(
                select(Shop)
                .where(Shop.id.in_([s.shop_id for s in data.shops]))
                .order_by(Shop.id)
                .with_for_update()
            )
        ).all()
    }
    ids = [i.variant_id for s in data.shops for i in s.items]
    if len(ids) != len(set(ids)):
        fail(422, "Combine duplicate cart lines")
    variants = {
        v.id: v
        for v in (
            await db.scalars(
                select(Variant).where(Variant.id.in_(ids)).order_by(Variant.id).with_for_update()
            )
        ).all()
    }
    planned = []
    for selection in data.shops:
        shop = shops.get(selection.shop_id)
        owner = await db.get(User, shop.owner_id) if shop else None
        if (
            not shop
            or shop.verification_status != "verified"
            or shop.is_suspended
            or shop.is_deleted
            or shop.is_on_vacation
            or owner.status != "active"
        ):
            fail(409, "A store is unavailable")
        if shop.owner_id == actor.id:
            fail(422, "You cannot purchase from your own store")
        if data.payment_method not in shop.data.get("paymentMethods", ["cod"]):
            fail(422, "Choose a payment method accepted by every store")
        if data.payment_method == "bank_transfer" and not shop.bank_account:
            fail(422, "A store has not configured its bank transfer account")
        delivery = shop.data.get("delivery", {})
        pickup = shop.data.get("pickup", {})
        if selection.fulfillment == "delivery":
            if not delivery.get("enabled") or not (
                delivery.get("allPakistan") or address.data["city"] in delivery.get("cities", [])
            ):
                fail(422, f"{shop.name} does not deliver to this city")
        elif not pickup.get("enabled"):
            fail(422, f"{shop.name} does not offer pickup")
        subtotal, lines = Decimal(0), []
        for line in selection.items:
            variant = variants.get(line.variant_id)
            product = await db.get(Product, variant.product_id) if variant else None
            if (
                not variant
                or not variant.is_active
                or not product
                or product.shop_id != shop.id
                or product.status != "active"
            ):
                fail(409, "An item is unavailable")
            if variant.stock < line.quantity:
                fail(409, f"Only {variant.stock} left of {product.name}")
            subtotal += variant.price * line.quantity
            lines.append(
                (product, variant, line.quantity, await commission_rate(db, shop, product.category_id))
            )
        fee = Decimal(0)
        if selection.fulfillment == "delivery":
            fee = money(
                delivery.get("fee", 0)
                if address.data["city"] == shop.city
                else delivery.get("otherCityFee", delivery.get("fee", 0))
            )
            fee = min(fee, money(rules["maximumDeliveryFee"]))
            free_above = delivery.get("freeAboveAmount")
            if free_above is not None and subtotal >= Decimal(str(free_above)):
                fee = Decimal(0)
        planned.append(
            {
                "shop": shop,
                "selection": selection,
                "subtotal": subtotal,
                "delivery": fee,
                "lines": lines,
                "discount": Decimal(0),
            }
        )
    coupon = None
    if data.coupon_code:
        coupon = await db.scalar(
            select(Coupon).where(Coupon.code == data.coupon_code.upper()).with_for_update()
        )
        if (
            not coupon
            or not coupon.is_active
            or not coupon.valid_from <= now() <= coupon.valid_to
            or (coupon.usage_limit is not None and coupon.used_count >= coupon.usage_limit)
        ):
            fail(422, "Coupon is invalid, expired or exhausted")
        eligible = [p for p in planned if coupon.shop_id is None or p["shop"].id == coupon.shop_id]
        basis = sum((p["subtotal"] for p in eligible), Decimal(0))
        if not eligible or basis < coupon.minimum_order or basis <= 0:
            fail(422, "Coupon minimum has not been met")
        discount = min(basis, money(basis * coupon.value / 100) if coupon.kind == "percent" else coupon.value)
        if coupon.max_discount is not None:
            discount = min(discount, coupon.max_discount)
        remaining = discount
        for index, part in enumerate(eligible):
            share = (
                remaining
                if index == len(eligible) - 1
                else min(remaining, part["subtotal"], money(discount * part["subtotal"] / basis))
            )
            part["discount"] = share
            remaining -= share
        coupon.used_count += 1
    for part in planned:
        subtotal = part["subtotal"] - part["discount"]
        if subtotal < money(rules["minimumOrder"]):
            fail(422, "Minimum order amount has not been met")
        part["total"] = subtotal + part["delivery"]
        if data.payment_method == "cod":
            maximum = min(
                money(rules["codMaximum"]), money(part["shop"].data.get("codMaximum") or rules["codMaximum"])
            )
            if part["total"] > maximum:
                fail(422, "Order exceeds the cash payment maximum")
    group = OrderGroup(
        id=uid(),
        buyer_id=actor.id,
        idempotency_key=key,
        request_hash=request_hash,
        payment_method=data.payment_method,
        total=sum((p["total"] for p in planned), Decimal(0)),
    )
    db.add(group)
    await db.flush()
    combined = None
    if online:
        combined = Payment(
            id=uid(),
            user_id=actor.id,
            order_group_id=group.id,
            kind="order_payment",
            amount=group.total,
            method=data.payment_method,
            idempotency_key="checkout:" + group.id,
            data={"provider": settings().payment_provider},
        )
        db.add(combined)
        await db.flush()
    for part in planned:
        shop = part["shop"]
        raw_commission = sum(
            (v.price * quantity * rate for _, v, quantity, rate in part["lines"]), Decimal(0)
        )
        commission = (
            money(raw_commission * (part["subtotal"] - part["discount"]) / part["subtotal"])
            if part["subtotal"]
            else Decimal(0)
        )
        order = Order(
            id=uid(),
            order_number="AM-" + secrets.token_hex(6).upper(),
            order_group_id=group.id,
            buyer_id=actor.id,
            shop_id=shop.id,
            subtotal=part["subtotal"],
            discount=part["discount"],
            delivery_fee=part["delivery"],
            total=part["total"],
            commission=commission,
            fulfillment=part["selection"].fulfillment,
            address=address.data if part["selection"].fulfillment == "delivery" else None,
            payment_method=data.payment_method,
            collector="platform" if online else "vendor",
            expires_at=now() + timedelta(hours=rules["autoCancelHours"]),
            timeline=[{"status": "pending", "at": now().isoformat(), "note": "Order placed"}],
            data={
                "returnDays": max(
                    shop.data.get("policies", {}).get("returnDays", 0), rules["returnWindowDays"]
                ),
                "holdingDays": rules["holdingPeriodDays"],
                "couponCode": coupon.code if coupon else None,
                "pickupHoldHours": shop.data.get("pickup", {}).get("holdHours", 48),
                "shopName": shop.name,
            },
        )
        if data.payment_method == "bank_transfer":
            import json

            from anymob.security import decrypt

            order.data = {**order.data, "bankTransferAccount": json.loads(decrypt(shop.bank_account))}
        db.add(order)
        await db.flush()
        for product, variant, quantity, rate in part["lines"]:
            db.add(
                OrderItem(
                    order_id=order.id,
                    product_id=product.id,
                    variant_id=variant.id,
                    quantity=quantity,
                    unit_price=variant.price,
                    commission_rate=rate,
                    snapshot={
                        "name": product.name,
                        "sku": variant.sku,
                        "variantLabel": " / ".join(str(v) for v in variant.attributes.values()),
                        "image": variant.image or next(iter(product.images), ""),
                        "attributes": variant.attributes,
                    },
                )
            )
            await adjust_stock(db, variant, -quantity, "checkout_reservation", order.id)
        payment = combined
        if payment is None:
            payment = Payment(
                id=uid(),
                user_id=actor.id,
                shop_id=shop.id,
                order_group_id=group.id,
                kind="order_payment",
                amount=order.total,
                method=data.payment_method,
                idempotency_key="order:" + order.id,
                data={"collector": "vendor"},
            )
            db.add(payment)
            await db.flush()
        db.add(PaymentAllocation(payment_id=payment.id, order_id=order.id, amount=order.total))
        await notify(
            db,
            shop.owner_id,
            "New order",
            order.order_number,
            "/vendor/orders/" + order.id,
            event="order_placed",
            variables={
                "order_number": order.order_number,
                "store_name": shop.name,
                "amount": str(order.total),
            },
        )
    await db.execute(
        delete(CartItem).where(
            CartItem.user_id == actor.id, CartItem.variant_id.in_(ids), CartItem.saved.is_(False)
        )
    )
    return group


async def settle_order(db, order, payment):
    if order.payment_status == "paid":
        return
    if order.status == "cancelled":
        fail(409, "Cancelled orders cannot accept payments; reconcile the payment with a refund")
    order.payment_status = "paid"
    # Only platform-collected funds become a vendor payable; direct cash/bank produce a receivable.
    if order.collector == "platform":
        db.add(
            LedgerEntry(
                shop_id=order.shop_id,
                order_id=order.id,
                payment_id=payment.id,
                kind="sale",
                amount=order.total - order.delivery_fee,
                event_key="sale:" + order.id,
            )
        )
    db.add(
        LedgerEntry(
            shop_id=order.shop_id,
            order_id=order.id,
            payment_id=payment.id,
            kind="commission",
            amount=-order.commission,
            event_key="commission:" + order.id,
        )
    )


async def capture_payment(db, payment):
    if payment.status == "paid":
        return
    if payment.status != "pending":
        fail(409, "Payment cannot be captured in its current state")
    allocations = (
        await db.scalars(
            select(PaymentAllocation)
            .where(PaymentAllocation.payment_id == payment.id)
            .order_by(PaymentAllocation.order_id)
        )
    ).all()
    for allocation in allocations:
        order = await get(db, Order, allocation.order_id, lock=True)
        await settle_order(db, order, payment)
    payment.status, payment.paid_at = "paid", now()


async def cancel_order(db, order, actor_id, note):
    if order.status == "cancelled":
        return
    allowed = (
        {"pending", "confirmed"}
        if actor_id == order.buyer_id
        else {"pending", "confirmed", "packed", "ready_for_pickup"}
    )
    if order.status not in allowed:
        fail(409, "This order can no longer be cancelled")
    if len(note.strip()) < 3:
        fail(422, "A cancellation reason is required")
    items = (
        await db.scalars(
            select(OrderItem).where(OrderItem.order_id == order.id).order_by(OrderItem.variant_id)
        )
    ).all()
    for item in items:
        variant = await get(db, Variant, item.variant_id, lock=True)
        await adjust_stock(db, variant, item.quantity, "cancellation_release", order.id)
    order.status = "cancelled"
    order.data = {**order.data, "cancelReason": note, "refundPending": order.payment_status == "paid"}
    order.timeline = [
        *order.timeline,
        {"status": "cancelled", "at": now().isoformat(), "note": note, "actor": actor_id},
    ]
    await notify(db, order.buyer_id, "Order cancelled", note, "/orders/" + order.id)


TRANSITIONS = {
    "pending": ["confirmed"],
    "confirmed": ["packed"],
    "packed": ["shipped", "ready_for_pickup"],
    "shipped": ["delivered"],
    "ready_for_pickup": ["picked_up"],
}


async def transition_order(db, order, data, actor_id, *, verify_pickup=True):
    if data.status == "cancelled":
        return await cancel_order(db, order, actor_id, data.note)
    if data.status not in TRANSITIONS.get(order.status, []):
        fail(409, "Invalid order status transition")
    if (data.status in {"shipped", "delivered"} and order.fulfillment != "delivery") or (
        data.status in {"ready_for_pickup", "picked_up"} and order.fulfillment != "pickup"
    ):
        fail(409, "Status does not match fulfillment method")
    if data.status == "confirmed" and order.payment_method != "cod" and order.payment_status != "paid":
        fail(409, "Payment must be confirmed first")
    if data.status == "shipped":
        rules = await config(db, "commerce")
        couriers = {c["name"] for c in rules["couriers"] if c["enabled"]} | {"Own rider"}
        if data.courier not in couriers or not data.tracking_number:
            fail(422, "An enabled courier and tracking number are required")
        order.data = {**order.data, "courier": data.courier, "trackingNumber": data.tracking_number}
    if data.status == "ready_for_pickup":
        code = str(secrets.randbelow(1000000)).zfill(6)
        order.pickup_code_hash = digest(code)
        order.data = {**order.data, "pickupCode": code}
        order.expires_at = now() + timedelta(hours=order.data["pickupHoldHours"])
    if (
        data.status == "picked_up"
        and verify_pickup
        and not secrets.compare_digest(order.pickup_code_hash or "", digest(data.pickup_code or ""))
    ):
        fail(422, "Pickup code does not match")
    order.status = data.status
    order.timeline = [
        *order.timeline,
        {"status": data.status, "at": now().isoformat(), "note": data.note, "actor": actor_id},
    ]
    if data.status in {"delivered", "picked_up"}:
        order.completed_at = now()
        order.hold_until = now() + timedelta(days=max(order.data["returnDays"], order.data["holdingDays"]))
        if order.payment_method == "cod":
            payment = await db.scalar(
                select(Payment)
                .join(PaymentAllocation, PaymentAllocation.payment_id == Payment.id)
                .where(PaymentAllocation.order_id == order.id, Payment.kind == "order_payment")
                .with_for_update(of=Payment)
            )
            await capture_payment(db, payment)
    event = {
        "confirmed": "order_confirmed",
        "shipped": "order_shipped",
        "ready_for_pickup": "ready_for_pickup",
        "delivered": "order_delivered",
        "picked_up": "order_delivered",
    }.get(data.status)
    await notify(
        db,
        order.buyer_id,
        "Order " + data.status.replace("_", " "),
        order.order_number,
        "/orders/" + order.id,
        event=event,
        variables={
            "order_number": order.order_number,
            "tracking_number": order.data.get("trackingNumber", ""),
            "pickup_code": order.data.get("pickupCode", ""),
            "store_name": order.data.get("shopName", ""),
        },
    )
