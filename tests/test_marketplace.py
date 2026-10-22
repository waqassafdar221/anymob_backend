import asyncio
import hashlib
import hmac
import json
import time
from datetime import timedelta
from decimal import Decimal

from conftest import checkout_payload
from sqlalchemy import func, select

from anymob.db import Session, now
from anymob.models import (
    AuditLog,
    Category,
    LedgerEntry,
    Order,
    Shop,
    Variant,
)


async def place(client, market, method="cod", both=False, key="checkout-key-001", quantity=1):
    response = await client.post(
        "/api/v1/checkout",
        headers={**market["buyer"]["headers"], "Idempotency-Key": key},
        json=checkout_payload(market, method, both, quantity),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def capture(client, payment):
    payload = {
        "eventId": "event-" + payment["id"],
        "paymentId": payment["id"],
        "amount": payment["amount"],
        "currency": "PKR",
        "reference": "gateway-" + payment["id"],
        "status": "paid",
    }
    raw = json.dumps(payload).encode()
    timestamp = int(time.time())
    signature = hmac.new(
        b"test-webhook-secret", str(timestamp).encode() + b"." + raw, hashlib.sha256
    ).hexdigest()
    return await client.post(
        "/api/v1/payments/webhooks/sandbox",
        content=raw,
        headers={"Content-Type": "application/json", "X-Signature": signature, "X-Timestamp": str(timestamp)},
    )


async def test_real_login_no_password_leak(client, market):
    bad = await client.post(
        "/api/v1/auth/login", json={"identifier": "buyer@example.com", "password": "incorrect"}
    )
    assert bad.status_code == 401
    response = await client.post(
        "/api/v1/auth/login", json={"identifier": "buyer@example.com", "password": "Secure-pass-123"}
    )
    assert response.status_code == 200, response.text
    assert "password" not in response.text
    assert "HttpOnly" in response.headers["set-cookie"]
    assert (await client.get("/api/v1/auth/me")).json()["id"] == market["buyer"]["id"]
    user = await client.post(
        "/api/v1/auth/register",
        json={
            "fullName": "Admin attempt",
            "email": "bad@example.com",
            "phone": "03001112222",
            "password": "Secure-pass-123",
            "role": "admin",
        },
    )
    assert user.status_code == 422


async def test_refresh_replay_revokes_family(client, market):
    await client.post(
        "/api/v1/auth/login", json={"identifier": "buyer@example.com", "password": "Secure-pass-123"}
    )
    old_refresh = client.cookies.get("anymob_refresh")
    rotated = await client.post("/api/v1/auth/refresh")
    assert rotated.status_code == 200
    token = rotated.json()["token"]
    replay = await client.post("/api/v1/auth/refresh", headers={"Cookie": "anymob_refresh=" + old_refresh})
    assert replay.status_code == 401
    assert (
        await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer " + token})
    ).status_code == 401


async def test_forged_role_and_cross_vendor_access(client, market):
    assert (
        await client.get("/api/v1/admin/users", headers={"Cookie": "anymob_role=admin; anymob_token=fake"})
    ).status_code == 401
    assert (
        await client.get("/api/v1/admin/users", headers=market["moderator"]["headers"])
    ).status_code == 403
    group = await place(client, market)
    id = group["orders"][0]["id"]
    assert (
        await client.get("/api/v1/vendor/orders/" + id, headers=market["vendor2"]["headers"])
    ).status_code == 404
    assert (await client.get("/api/v1/orders/" + id, headers=market["vendor"]["headers"])).status_code == 404


async def test_multi_store_checkout_and_retry(client, market):
    first = await place(client, market, both=True)
    second = await place(client, market, both=True)
    assert first["orderGroup"]["id"] == second["orderGroup"]["id"]
    assert len(first["orders"]) == 2
    assert first["orderGroup"]["total"] == 20100
    async with Session() as db:
        assert await db.scalar(select(func.count()).select_from(Order)) == 2
        assert (await db.get(Variant, market["vendor"]["variant"])).stock == 9
    changed = await client.post(
        "/api/v1/checkout",
        headers={**market["buyer"]["headers"], "Idempotency-Key": "checkout-key-001"},
        json=checkout_payload(market, quantity=2),
    )
    assert changed.status_code == 409


async def test_checkout_rollback_and_stock_race(client, market):
    async with Session.begin() as db:
        (await db.get(Variant, market["vendor"]["variant"])).stock = 1
        (await db.get(Variant, market["vendor2"]["variant"])).stock = 0
    invalid = await client.post(
        "/api/v1/checkout",
        headers={**market["buyer"]["headers"], "Idempotency-Key": "rollback-001"},
        json=checkout_payload(market, both=True),
    )
    assert invalid.status_code == 409
    async with Session() as db:
        assert (await db.get(Variant, market["vendor"]["variant"])).stock == 1
        assert await db.scalar(select(func.count()).select_from(Order)) == 0

    async def buy(key):
        return await client.post(
            "/api/v1/checkout",
            headers={**market["buyer"]["headers"], "Idempotency-Key": key},
            json=checkout_payload(market),
        )

    results = await asyncio.gather(buy("concurrent-001"), buy("concurrent-002"))
    assert sorted(r.status_code for r in results) == [201, 409]


async def test_cancellation_is_idempotent_and_releases_once(client, market):
    order = (await place(client, market))["orders"][0]
    for _ in range(2):
        response = await client.post(
            f"/api/v1/orders/{order['id']}/cancel",
            headers=market["buyer"]["headers"],
            json={"note": "Changed my mind"},
        )
        assert response.status_code == 200
    async with Session() as db:
        assert (await db.get(Variant, market["vendor"]["variant"])).stock == 10


async def test_combined_payment_webhook_replay_and_tampering(client, market):
    result = await place(client, market, "card", both=True)
    assert len(result["payments"]) == 1
    payment = result["payments"][0]
    assert (await capture(client, payment)).status_code == 200
    assert (await capture(client, payment)).status_code == 200
    async with Session() as db:
        rows = (await db.scalars(select(Order))).all()
        assert all(o.payment_status == "paid" for o in rows)
        assert await db.scalar(select(func.count()).select_from(LedgerEntry)) == 4
    bad = await client.post(
        "/api/v1/payments/webhooks/sandbox",
        json={},
        headers={"X-Signature": "fake", "X-Timestamp": str(int(time.time()))},
    )
    assert bad.status_code == 401


async def test_vendor_fulfillment_and_cash_not_platform_payable(client, market):
    order = (await place(client, market))["orders"][0]
    path = f"/api/v1/vendor/orders/{order['id']}/transition"
    headers = market["vendor"]["headers"]
    assert (await client.post(path, headers=headers, json={"status": "shipped"})).status_code == 409
    for status in ["confirmed", "packed", "shipped", "delivered"]:
        response = await client.post(
            path, headers=headers, json={"status": status, "courier": "TCS", "trackingNumber": "TRACK-123"}
        )
        assert response.status_code == 200, response.text
    buyer = await client.get("/api/v1/orders/" + order["id"], headers=market["buyer"]["headers"])
    assert buyer.json()["status"] == "delivered"
    async with Session() as db:
        assert await db.scalar(select(func.sum(LedgerEntry.amount))) == Decimal("-500.00")
    report = await client.get("/api/v1/vendor/earnings", headers=headers)
    assert report.json()["available"] == -500


async def test_payout_holding_window_and_once_only(client, market):
    result = await place(client, market, "card")
    assert (await capture(client, result["payments"][0])).status_code == 200
    id = result["orders"][0]["id"]
    assert (
        await client.post("/api/v1/vendor/payouts", headers=market["vendor"]["headers"])
    ).status_code == 422
    async with Session.begin() as db:
        order = await db.get(Order, id)
        order.status, order.completed_at, order.hold_until = (
            "delivered",
            now() - timedelta(days=10),
            now() - timedelta(days=1),
        )
    response = await client.post("/api/v1/vendor/payouts", headers=market["vendor"]["headers"])
    assert response.status_code == 201, response.text
    assert response.json()["netAmount"] == 9500
    assert (
        await client.post("/api/v1/vendor/payouts", headers=market["vendor"]["headers"])
    ).status_code == 422
    paid = await client.post(
        f"/api/v1/admin/payouts/{response.json()['id']}/action",
        headers=market["admin"]["headers"],
        json={"action": "paid", "reference": "BANK-001", "note": "Transfer confirmed"},
    )
    assert paid.status_code == 200, paid.text
    admin = market["admin"]["headers"]
    payout = (await client.get(f"/api/v1/admin/payouts/{response.json()['id']}", headers=admin)).json()
    assert payout["payout"]["grossSales"] == 10000 and payout["payout"]["orderIds"] == [id]
    assert payout["account"]["destination"] == "test" and payout["orders"][0]["id"] == id
    rows = (await client.get("/api/v1/admin/transactions", headers=admin)).json()["items"]
    assert rows[0]["orderId"] == id and rows[0]["payer"] and rows[0]["refundable"] == 10100
    detail = (await client.get(f"/api/v1/admin/transactions/{rows[0]['id']}", headers=admin)).json()
    assert detail["order"]["id"] == id and detail["transaction"]["type"] == "order_payment"


async def test_revenue_overview_and_commission_rules(client, market):
    admin = market["admin"]["headers"]
    overview = (await client.get("/api/v1/admin/revenue/overview", headers=admin)).json()
    category = overview["categories"][0]["id"]
    saved = await client.put(
        "/api/v1/admin/revenue/commission",
        headers=admin,
        json={"defaultRate": 0.07, "categoryOverrides": [{"categoryId": category, "rate": 0.03}]},
    )
    assert saved.status_code == 200, saved.text
    overview = (await client.get("/api/v1/admin/revenue/overview", headers=admin)).json()
    assert overview["commission"] == {
        "defaultRate": 0.07,
        "categoryOverrides": [{"categoryId": category, "rate": 0.03}],
    }
    assert (
        await client.put(
            "/api/v1/admin/revenue/commission",
            headers=market["moderator"]["headers"],
            json={"defaultRate": 0.5},
        )
    ).status_code == 403


async def test_refund_requires_settlement_and_preserves_ledger(client, market):
    result = await place(client, market, "card")
    await capture(client, result["payments"][0])
    order_id = result["orders"][0]["id"]
    headers = {**market["admin"]["headers"], "Idempotency-Key": "refund-request-001"}
    response = await client.post(
        f"/api/v1/admin/orders/{order_id}/refund",
        headers=headers,
        json={"amount": 5050, "note": "Partial refund approved"},
    )
    assert response.status_code == 200, response.text
    async with Session() as db:
        assert (await db.get(Order, order_id)).refunded_amount == 0
    payment = response.json()
    settled = await client.post(
        f"/api/v1/admin/refunds/{payment['id']}/settle",
        headers=market["admin"]["headers"],
        json={"reference": "REF-001", "note": "Transfer returned to buyer"},
    )
    assert settled.status_code == 200, settled.text
    async with Session() as db:
        assert (await db.get(Order, order_id)).refunded_amount == 5050
        assert await db.scalar(select(func.sum(LedgerEntry.amount))) == Decimal("4750.00")


async def test_category_cycles_and_usage_guard(client, market):
    headers = market["admin"]["headers"]
    child = await client.post(
        "/api/v1/admin/categories",
        headers=headers,
        json={"name": "Child", "slug": "child", "parentId": market["category"]},
    )
    assert child.status_code == 201, child.text
    cycle = await client.put(
        "/api/v1/admin/categories/" + market["category"],
        headers=headers,
        json={"name": "Phones", "slug": "phones", "parentId": child.json()["id"]},
    )
    assert cycle.status_code == 422
    used = await client.delete("/api/v1/admin/categories/" + market["category"], headers=headers)
    assert used.status_code == 409


async def test_schema_validation_and_private_store_documents(client, market):
    async with Session.begin() as db:
        category = await db.get(Category, market["category"])
        category.attributes_schema = [
            {
                "name": "capacity",
                "label": "Capacity",
                "type": "select",
                "required": True,
                "options": ["128GB"],
            }
        ]
        shop = await db.get(Shop, market["vendor"]["shop"])
        shop.data = {**shop.data, "verificationDocuments": {"cnicFront": "private-document-id"}}
    public = await client.get("/api/v1/shops/vendor")
    assert "private-document-id" not in public.text
    response = await client.post(
        "/api/v1/vendor/products",
        headers=market["vendor"]["headers"],
        json={
            "name": "Invalid",
            "slug": "invalid",
            "categoryId": market["category"],
            "status": "pending",
            "variants": [{"sku": "INVALID", "price": 500, "stock": 1}],
        },
    )
    assert response.status_code == 422


async def test_settings_conflict_and_maintenance_checkout(client, market):
    headers = market["admin"]["headers"]
    response = await client.put(
        "/api/v1/admin/settings/maintenance",
        headers=headers,
        json={"version": 1, "value": {"enabled": True, "message": "Upgrading"}},
    )
    assert response.status_code == 200, response.text
    stale = await client.put(
        "/api/v1/admin/settings/maintenance",
        headers=headers,
        json={"version": 1, "value": {"enabled": False, "message": ""}},
    )
    assert stale.status_code == 409
    checkout = await client.post(
        "/api/v1/checkout",
        headers={**market["buyer"]["headers"], "Idempotency-Key": "maintenance-test"},
        json=checkout_payload(market),
    )
    assert checkout.status_code == 503


async def test_reports_group_and_audit_resolution(client, market):
    target = market["vendor"]["product"]
    report = await client.post(
        "/api/v1/reports",
        headers=market["buyer"]["headers"],
        json={
            "targetType": "product",
            "targetId": target,
            "reason": "scam",
            "details": "Reported product appears fraudulent",
        },
    )
    assert report.status_code == 201
    id = report.json()["id"]
    headers = market["moderator"]["headers"]
    assert (await client.post(f"/api/v1/admin/reports/{id}/assign", headers=headers)).status_code == 200
    result = await client.post(
        f"/api/v1/admin/reports/{id}/resolve",
        headers=headers,
        json={"action": "remove", "note": "Evidence confirms prohibited listing"},
    )
    assert result.status_code == 200, result.text
    assert result.json()["capabilities"]["remove"] is False
    assert (await client.get("/api/v1/products/" + target)).status_code == 404
    listed = (await client.get("/api/v1/admin/reports", headers=headers)).json()
    row = next(r for r in listed["rows"] if r["id"] == id)
    assert row["status"] == "resolved" and row["resolutionAction"] == "remove"
    assert row["target"]["type"] == "product" and row["target"]["exists"] is False
    assert listed["counts"]["resolved"] >= 1 and listed["stats"]["resolved"] >= 1
    again = await client.post(
        f"/api/v1/admin/reports/{id}/resolve", headers=headers, json={"action": "dismiss", "note": "Duplicate"}
    )
    assert again.status_code == 409
    async with Session() as db:
        assert await db.scalar(select(func.count()).select_from(AuditLog)) >= 3


async def test_messages_participant_and_review_eligibility(client, market):
    opened = await client.post(
        "/api/v1/conversations",
        headers=market["buyer"]["headers"],
        json={"subjectType": "product", "subjectId": market["vendor"]["product"]},
    )
    assert opened.status_code == 201, opened.text
    id = opened.json()["id"]
    assert (
        await client.get(f"/api/v1/conversations/{id}/messages", headers=market["vendor2"]["headers"])
    ).status_code == 404
    sent = await client.post(
        f"/api/v1/conversations/{id}/messages",
        headers=market["buyer"]["headers"],
        json={"body": "Is it available?"},
    )
    assert sent.status_code == 201
    order = (await place(client, market))["orders"][0]
    review = await client.post(
        "/api/v1/reviews",
        headers=market["buyer"]["headers"],
        json={"orderId": order["id"], "rating": 5, "title": "Great", "body": "A great purchase"},
    )
    assert review.status_code == 403


async def test_vendor_approval_becomes_public(client, market):
    async with Session.begin() as db:
        shop = await db.get(Shop, market["vendor"]["shop"])
        shop.verification_status = "pending"
    assert (await client.get("/api/v1/shops/vendor")).status_code == 404
    approved = await client.post(
        f"/api/v1/admin/vendors/{market['vendor']['shop']}/action",
        headers=market["admin"]["headers"],
        json={"action": "approve", "note": "Documents have been verified"},
    )
    assert approved.status_code == 200
    assert (await client.get("/api/v1/shops/vendor")).status_code == 200
