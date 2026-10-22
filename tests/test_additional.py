import asyncio
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from conftest import checkout_payload
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from test_marketplace import capture, place

from anymob.db import Session, now
from anymob.models import (
    Challenge,
    Coupon,
    Order,
    Outbox,
    Payout,
    ReturnRequest,
    Variant,
)
from anymob.security import totp
from anymob.worker import drain_outbox, lifecycle


async def test_csrf_cookie_and_auth_field_validation(client, market):
    await client.post(
        "/api/v1/auth/login", json={"identifier": "buyer@example.com", "password": "Secure-pass-123"}
    )
    bad = await client.post("/api/v1/auth/logout", headers={"Origin": "https://attacker.example"})
    assert bad.status_code == 403
    assert (await client.get("/api/v1/auth/me")).status_code == 200
    valid = await client.post("/api/v1/auth/logout")
    assert valid.status_code == 200
    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_real_mfa_enrollment_and_replay(client, market):
    headers = market["admin"]["headers"]
    setup = await client.post("/api/v1/auth/mfa/setup", headers=headers)
    assert setup.status_code == 200
    code = totp(setup.json()["secret"])
    enabled = await client.post("/api/v1/auth/mfa/enable", headers=headers, json={"code": code})
    assert enabled.status_code == 200
    login = await client.post(
        "/api/v1/auth/login", json={"identifier": "admin@example.com", "password": "Secure-pass-123"}
    )
    assert login.json()["mfaRequired"] is True
    challenge = login.json()["challenge"]
    replay = await client.post("/api/v1/auth/mfa/verify", json={"challenge": challenge, "code": code})
    assert replay.status_code == 401
    # Next TOTP time step, accepted within the allowed clock-skew window.
    import time

    next_code = totp(setup.json()["secret"], int(time.time()) // 30 + 1)
    verified = await client.post("/api/v1/auth/mfa/verify", json={"challenge": challenge, "code": next_code})
    assert verified.status_code == 200, verified.text
    assert (
        await client.post("/api/v1/auth/mfa/verify", json={"challenge": challenge, "code": next_code})
    ).status_code == 401


async def test_email_code_attempts_persist_and_no_response_secret(client, market):
    sent = await client.post(
        "/api/v1/auth/challenge", json={"identifier": "buyer@example.com", "purpose": "reset"}
    )
    assert sent.status_code == 200
    assert "code" not in sent.json()  # Code is delivered only through the outbox.
    async with Session() as db:
        outbox = await db.scalar(select(Outbox))
        code = outbox.payload["body"].split()[3].rstrip(".")
    incorrect = "999999" if code != "999999" else "999998"
    bad = await client.post(
        "/api/v1/auth/challenge/verify",
        json={
            "identifier": "buyer@example.com",
            "purpose": "reset",
            "code": incorrect,
            "password": "New-secure-pass",
        },
    )
    assert bad.status_code == 401
    async with Session() as db:
        assert (await db.scalar(select(Challenge))).attempts == 1
    good = await client.post(
        "/api/v1/auth/challenge/verify",
        json={
            "identifier": "buyer@example.com",
            "purpose": "reset",
            "code": code,
            "password": "New-secure-pass",
        },
    )
    assert good.status_code == 200, good.text
    assert (await client.get("/api/v1/auth/me", headers=market["buyer"]["headers"])).status_code == 401


async def test_coupon_consumed_once_across_stores_and_retries(client, market):
    async with Session.begin() as db:
        db.add(
            Coupon(
                code="ONCE",
                kind="fixed",
                value=1000,
                minimum_order=1,
                usage_limit=1,
                valid_from=now() - timedelta(days=1),
                valid_to=now() + timedelta(days=1),
            )
        )
    payload = {**checkout_payload(market, both=True), "couponCode": "ONCE"}

    async def request():
        return await client.post(
            "/api/v1/checkout",
            headers={**market["buyer"]["headers"], "Idempotency-Key": "coupon-retry-key"},
            json=payload,
        )

    one, two = await asyncio.gather(request(), request())
    assert one.status_code == two.status_code == 201
    assert one.json()["orderGroup"]["total"] == 19100
    async with Session() as db:
        assert (await db.scalar(select(Coupon))).used_count == 1


async def test_bank_confirmation_is_scoped_and_cash_refund_not_payable(client, market):
    result = await place(client, market, "bank_transfer")
    order_id = result["orders"][0]["id"]
    wrong = await client.post(
        f"/api/v1/vendor/orders/{order_id}/confirm-payment",
        headers=market["vendor2"]["headers"],
        json={"note": "Funds received"},
    )
    assert wrong.status_code == 404
    correct = await client.post(
        f"/api/v1/vendor/orders/{order_id}/confirm-payment",
        headers=market["vendor"]["headers"],
        json={"note": "Funds received"},
    )
    assert correct.status_code == 200, correct.text
    assert correct.json()["paymentStatus"] == "paid"
    async with Session() as db:
        from anymob.models import LedgerEntry

        assert await db.scalar(select(func.sum(LedgerEntry.amount))) == Decimal("-500")


async def test_pending_refunds_reserve_amount_and_block_payout(client, market):
    result = await place(client, market, "card")
    await capture(client, result["payments"][0])
    order_id = result["orders"][0]["id"]
    async with Session.begin() as db:
        order = await db.get(Order, order_id)
        order.status, order.completed_at, order.hold_until = (
            "delivered",
            now() - timedelta(days=10),
            now() - timedelta(days=1),
        )
    first = await client.post(
        f"/api/v1/admin/orders/{order_id}/refund",
        headers={**market["admin"]["headers"], "Idempotency-Key": "pending-refund-one"},
        json={"amount": 7000, "note": "Approved first refund"},
    )
    assert first.status_code == 200
    second = await client.post(
        f"/api/v1/admin/orders/{order_id}/refund",
        headers={**market["admin"]["headers"], "Idempotency-Key": "pending-refund-two"},
        json={"amount": 4000, "note": "Approved second refund"},
    )
    assert second.status_code == 422
    assert (
        await client.post("/api/v1/vendor/payouts", headers=market["vendor"]["headers"])
    ).status_code == 422


async def test_return_quantity_escalation_and_payout_hold(client, market):
    result = await place(client, market, "card")
    await capture(client, result["payments"][0])
    order_id = result["orders"][0]["id"]
    async with Session.begin() as db:
        order = await db.get(Order, order_id)
        order.status, order.completed_at, order.hold_until = "delivered", now(), now() - timedelta(days=1)
    payload = {
        "orderId": order_id,
        "reason": "other",
        "details": "Would like to return this item",
        "items": [{"variantId": market["vendor"]["variant"], "quantity": 1}],
    }
    returned = await client.post("/api/v1/returns", headers=market["buyer"]["headers"], json=payload)
    assert returned.status_code == 201, returned.text
    assert (
        await client.post("/api/v1/returns", headers=market["buyer"]["headers"], json=payload)
    ).status_code == 422
    id = returned.json()["id"]
    headers = {**market["buyer"]["headers"], "Idempotency-Key": "return-escalation"}
    early = await client.post(
        f"/api/v1/returns/{id}/escalate",
        headers=headers,
        json={"action": "escalate", "note": "Please review the evidence"},
    )
    assert early.status_code == 409
    rejected = await client.post(
        f"/api/v1/vendor/returns/{id}/action",
        headers={**market["vendor"]["headers"], "Idempotency-Key": "return-rejection"},
        json={"action": "reject", "note": "Store rejected return"},
    )
    assert rejected.status_code == 200
    escalated = await client.post(
        f"/api/v1/returns/{id}/escalate",
        headers=headers,
        json={"action": "escalate", "note": "Please review the evidence"},
    )
    assert escalated.status_code == 200
    assert escalated.json()["claim"] == "Please review the evidence"
    assert (
        await client.post("/api/v1/vendor/payouts", headers=market["vendor"]["headers"])
    ).status_code == 409
    admin = {**market["admin"]["headers"], "Idempotency-Key": "dispute-ruling"}
    asked = await client.post(
        f"/api/v1/admin/returns/{id}/action",
        headers=admin,
        json={"action": "request_information", "note": "Send the courier receipt", "side": "store"},
    )
    assert asked.json()["informationRequests"][0]["side"] == "store"
    ruled = await client.post(
        f"/api/v1/admin/returns/{id}/action",
        headers=admin,
        json={"action": "resolve_buyer", "note": "Evidence supports the buyer", "amount": 1000},
    )
    assert ruled.status_code == 200, ruled.text
    body = ruled.json()
    assert body["resolution"] == "buyer" and body["status"] == "refund_pending" and body["refundAmount"] == 1000
    again = await client.post(
        f"/api/v1/admin/returns/{id}/action",
        headers=admin,
        json={"action": "resolve_store", "note": "Second ruling"},
    )
    assert again.status_code == 409
    listed = (await client.get("/api/v1/admin/returns", headers=market["admin"]["headers"])).json()
    assert listed["items"][0]["order"]["buyer"]["id"] == market["buyer"]["id"]


async def test_admin_order_override_walks_forward(client, market):
    result = await place(client, market)
    id = result["orders"][0]["id"]
    headers = market["admin"]["headers"]
    path = f"/api/v1/admin/orders/{id}/transition"
    backwards = await client.post(path, headers=headers, json={"status": "confirmed", "note": "x"})
    assert backwards.status_code == 422
    shipped = await client.post(
        path,
        headers=headers,
        json={"status": "shipped", "note": "Courier confirmed", "courier": "TCS", "trackingNumber": "TCS123"},
    )
    assert shipped.status_code == 200, shipped.text
    order = shipped.json()["order"]
    assert order["status"] == "shipped" and order["trackingNumber"] == "TCS123"
    assert [e["status"] for e in order["timeline"]][-3:] == ["confirmed", "packed", "shipped"]
    assert (await client.post(path, headers=headers, json={"status": "packed", "note": "Go back"})).status_code == 409
    noted = await client.post(
        f"/api/v1/admin/orders/{id}/notes", headers=headers, json={"note": "Checked with buyer", "internal": True}
    )
    assert noted.status_code == 201 and noted.json()["notes"][0]["note"] == "Checked with buyer"
    listed = (await client.get("/api/v1/admin/orders", headers=headers)).json()["items"]
    assert listed[0]["buyer"]["id"] == market["buyer"]["id"] and listed[0]["itemCount"] == 1


async def test_bulk_payout_rollback(client, market):
    result = await place(client, market, "card", both=True)
    await capture(client, result["payments"][0])
    async with Session.begin() as db:
        for order in (await db.scalars(select(Order))).all():
            order.status, order.completed_at, order.hold_until = (
                "delivered",
                now() - timedelta(days=10),
                now() - timedelta(days=1),
            )
    payouts = []
    for name in ["vendor", "vendor2"]:
        response = await client.post("/api/v1/vendor/payouts", headers=market[name]["headers"])
        assert response.status_code == 201
        payouts.append(response.json())
    async with Session.begin() as db:
        db.add(
            ReturnRequest(
                order_id=result["orders"][1]["id"],
                buyer_id=market["buyer"]["id"],
                shop_id=market["vendor2"]["shop"],
                reason="other",
                details="A disputed order",
                items=[],
                disputed=True,
            )
        )
    response = await client.post(
        "/api/v1/admin/payouts/bulk-paid",
        headers=market["admin"]["headers"],
        json={
            "payouts": {payouts[0]["id"]: "BANK-FIRST", payouts[1]["id"]: "BANK-SECOND"},
            "note": "Bulk transfer received",
        },
    )
    assert response.status_code == 409
    async with Session() as db:
        assert all(p.status == "pending" for p in (await db.scalars(select(Payout))).all())


async def test_worker_cancel_and_outbox_retry(client, market):
    result = await place(client, market)
    id = result["orders"][0]["id"]
    async with Session.begin() as db:
        (await db.get(Order, id)).expires_at = now() - timedelta(hours=1)
        db.add(
            Outbox(
                kind="email",
                payload={"to": "buyer@example.com", "subject": "Test", "body": "Hello"},
                event_key="test-message",
                available_at=now(),
            )
        )
    await lifecycle()
    await lifecycle()
    async with Session() as db:
        assert (await db.get(Order, id)).status == "cancelled"
        assert (await db.get(Variant, market["vendor"]["variant"])).stock == 10
    with patch("anymob.worker.send_email", side_effect=OSError("SMTP down")):
        await drain_outbox()
    async with Session() as db:
        row = await db.scalar(select(Outbox))
        assert row.attempts == 1 and row.sent_at is None and row.available_at > now()


async def test_audit_immutable_at_database_layer(client, market):
    response = await client.put(
        "/api/v1/admin/settings/maintenance",
        headers=market["admin"]["headers"],
        json={"version": 1, "value": {"enabled": True, "message": "Maintenance"}},
    )
    assert response.status_code == 200
    with pytest.raises(DBAPIError):
        async with Session.begin() as db:
            await db.execute(text("DELETE FROM audit_logs"))


async def test_catalog_csv_is_atomic(client, market):
    csv = f"name,slug,categoryId,sku,price,stock\nValid,valid,{market['category']},NEW-ONE,1000,2\nInvalid,invalid,{market['category']},NEW-TWO,-10,2\n"
    response = await client.post(
        "/api/v1/vendor/products/import-csv", headers=market["vendor"]["headers"], json={"csv": csv}
    )
    assert response.status_code == 422, response.text
    async with Session() as db:
        from anymob.models import Product

        assert not await db.scalar(select(Product.id).where(Product.slug == "valid"))


async def test_media_processing_and_private_documents(client, market):
    import io

    from PIL import Image

    from anymob.db import uid
    from anymob.models import Upload
    from anymob.worker import process_media

    output = io.BytesIO()
    Image.new("RGB", (32, 16), color="blue").save(output, "PNG")
    raw = output.getvalue()
    uploaded = {}

    class Storage:
        def get_object(self, **kwargs):
            return {"Body": io.BytesIO(raw)}

        def put_object(self, **kwargs):
            uploaded.update(kwargs)

        def delete_object(self, **kwargs):
            pass

        def generate_presigned_url(self, *args, **kwargs):
            return "https://storage.example/private-signed-file"

    id = uid()
    async with Session.begin() as db:
        db.add(
            Upload(
                id=id,
                owner_id=market["vendor"]["id"],
                object_key="incoming/test",
                purpose="verification",
                content_type="image/png",
                size=len(raw),
                status="processing",
            )
        )
    with patch("anymob.routes.media.s3", return_value=Storage()):
        async with Session.begin() as db:
            await process_media(db, id)
        assert uploaded["ContentType"] == "image/webp"
        assert uploaded["Body"][:4] == b"RIFF"
        assert (await client.get("/api/v1/media/" + id)).status_code == 401
        assert (
            await client.get("/api/v1/media/" + id, headers=market["buyer"]["headers"])
        ).status_code == 404
        assert (
            await client.get("/api/v1/media/" + id, headers=market["vendor"]["headers"])
        ).status_code == 307


async def test_bad_image_rejected_and_reset_guard(client, market):
    import io

    from anymob.db import uid
    from anymob.models import Upload
    from anymob.worker import process_media

    id = uid()
    async with Session.begin() as db:
        db.add(
            Upload(
                id=id,
                owner_id=market["vendor"]["id"],
                object_key="incoming/bad",
                purpose="catalog",
                content_type="image/png",
                size=7,
                status="processing",
            )
        )

    class Storage:
        def get_object(self, **kwargs):
            return {"Body": io.BytesIO(b"not-png")}

    with patch("anymob.routes.media.s3", return_value=Storage()):
        async with Session.begin() as db:
            await process_media(db, id)
    async with Session() as db:
        assert (await db.get(Upload, id)).status == "rejected"
    response = await client.post(
        "/api/v1/admin/demo/reset",
        headers=market["admin"]["headers"],
        json={"confirmation": "RESET DEMO DATA"},
    )
    assert response.status_code == 403


async def test_return_refunds_cannot_exceed_selected_quantity(client, market):
    result = await place(client, market, "card", quantity=2)
    await capture(client, result["payments"][0])
    order_id = result["orders"][0]["id"]
    async with Session.begin() as db:
        order = await db.get(Order, order_id)
        order.status, order.completed_at = "delivered", now()
    returned = await client.post(
        "/api/v1/returns",
        headers=market["buyer"]["headers"],
        json={
            "orderId": order_id,
            "reason": "other",
            "details": "Return one of the two purchased phones",
            "items": [{"variantId": market["vendor"]["variant"], "quantity": 1}],
        },
    )
    id = returned.json()["id"]
    for action in ["approve", "received"]:
        response = await client.post(
            f"/api/v1/vendor/returns/{id}/action",
            headers={**market["vendor"]["headers"], "Idempotency-Key": "return-action-" + action},
            json={"action": action, "note": "Vendor checked return"},
        )
        assert response.status_code == 200
    for key, expected in [("first-partial-refund", 200), ("second-partial-refund", 422)]:
        response = await client.post(
            f"/api/v1/vendor/returns/{id}/action",
            headers={**market["vendor"]["headers"], "Idempotency-Key": key},
            json={"action": "refund", "amount": 8000, "note": "Partial returned item refund"},
        )
        assert response.status_code == expected, response.text


async def test_archived_parent_hides_catalog_and_search(client, market):
    response = await client.get("/api/v1/search?q=Test")
    assert response.status_code == 200, response.text
    assert len(response.json()["products"]) == 2
    async with Session.begin() as db:
        from anymob.models import Category

        (await db.get(Category, market["category"])).is_active = False
    assert (await client.get("/api/v1/products")).json()["total"] == 0
    assert (await client.get("/api/v1/search?q=Test")).json()["products"] == []
