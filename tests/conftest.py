import os
from copy import deepcopy
from datetime import timedelta

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text

os.environ.setdefault("SECRET_KEY", "test-only-secret-key-at-least-32-characters")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://waqassafdar@127.0.0.1:55432/anymob_test")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:56379/15")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("PAYMENT_PROVIDER", "sandbox")
os.environ.setdefault("PAYMENT_WEBHOOK_SECRET", "test-webhook-secret")
# Keep tests off any real mail server, bucket or proxy configured in a developer's .env.
for key, value in {
    "OUTBOX_INLINE": "false",
    "PROXY_SECRET": "",
    "SMTP_HOST": "localhost",
    "SMTP_PORT": "1025",
    "SMTP_USERNAME": "",
    "SMTP_PASSWORD": "",
    "SMTP_STARTTLS": "false",
    "S3_ENDPOINT": "http://localhost:9000",
    "S3_PUBLIC_ENDPOINT": "http://localhost:9000",
    "S3_ACCESS_KEY": "anymob",
    "S3_SECRET_KEY": "anymob-local-storage",
    "S3_BUCKET": "anymob",
    "S3_REGION": "us-east-1",
}.items():
    os.environ.setdefault(key, value)

from anymob.config import settings
from anymob.db import Base, Session, engine, now, uid
from anymob.defaults import DEFAULTS
from anymob.main import app
from anymob.models import Address, AuthSession, Category, Product, Setting, Shop, User, Variant
from anymob.security import encrypt, hash_password, token_for


@pytest_asyncio.fixture(autouse=True)
async def clean_database():
    if settings().environment != "test" or not engine.url.database.endswith("_test"):
        raise RuntimeError("Tests require a dedicated database ending in _test")
    async with engine.begin() as conn:
        tables = ", ".join('"' + t.name + '"' for t in Base.metadata.sorted_tables)
        await conn.execute(text("TRUNCATE " + tables + " RESTART IDENTITY CASCADE"))
    client = Redis.from_url(settings().redis_url)
    # Only remove this suite's rate keys, never FLUSHALL a shared Redis.
    async for key in client.scan_iter(match="limit:*"):
        await client.delete(key)
    await client.aclose()
    yield
    await engine.dispose()


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Origin": "http://localhost:3000"},
    ) as client:
        yield client


@pytest_asyncio.fixture
async def market():
    result = {}
    async with Session.begin() as db:
        for key, value in DEFAULTS.items():
            data = deepcopy(value)
            if key == "commerce":
                data["paymentMethods"] = ["cod", "bank_transfer", "card"]
                data["minimumPayout"] = 1
            db.add(Setting(key=key, value=data))
        for index, (name, role, admin_role) in enumerate(
            [
                ("buyer", "user", None),
                ("vendor", "vendor", None),
                ("vendor2", "vendor", None),
                ("admin", "admin", "super_admin"),
                ("moderator", "admin", "moderator"),
            ]
        ):
            user = User(
                id=uid(),
                email=name + "@example.com",
                phone=f"+92300123456{index}",
                full_name=name,
                role=role,
                admin_role=admin_role,
                password_hash=hash_password("Secure-pass-123"),
                is_verified=True,
            )
            db.add(user)
            await db.flush()
            session = AuthSession(
                id=uid(),
                user_id=user.id,
                token_hash=uid(),
                family_id=uid(),
                expires_at=now() + timedelta(days=1),
                mfa_verified=True,
            )
            db.add(session)
            await db.flush()
            result[name] = {"id": user.id, "headers": {"Authorization": "Bearer " + token_for(user, session)}}
        category = Category(
            id=uid(), name="Phones", slug="phones", attributes_schema=[], variant_attributes=[]
        )
        db.add(category)
        await db.flush()
        result["category"] = category.id
        for index, name in enumerate(["vendor", "vendor2"]):
            shop = Shop(
                id=uid(),
                owner_id=result[name]["id"],
                name=name,
                slug=name,
                city="Lahore",
                verification_status="verified",
                bank_account=encrypt('{"bankName":"Test Bank","iban":"PK00TEST0000000000000000"}'),
                payout_account=encrypt('{"iban":"test"}'),
                payout_mask="bank •••• 1234",
                data={
                    "paymentMethods": ["cod", "bank_transfer", "card"],
                    "delivery": {"enabled": True, "cities": ["Lahore"], "fee": 100},
                    "pickup": {"enabled": True, "holdHours": 48},
                    "policies": {"returnDays": 7},
                },
            )
            db.add(shop)
            await db.flush()
            product = Product(
                id=uid(),
                shop_id=shop.id,
                category_id=category.id,
                name="Test Phone",
                slug="test-phone",
                status="active",
            )
            db.add(product)
            await db.flush()
            variant = Variant(
                id=uid(), product_id=product.id, sku="TEST-" + name, price=10000, stock=10, attributes={}
            )
            db.add(variant)
            await db.flush()
            result[name].update(shop=shop.id, product=product.id, variant=variant.id)
        address = Address(
            id=uid(),
            user_id=result["buyer"]["id"],
            data={"fullName": "Buyer", "phone": "03001234567", "line1": "1 Test Road", "city": "Lahore"},
            is_default=True,
        )
        db.add(address)
        result["address"] = address.id
    return result


def checkout_payload(market, method="cod", both=False, quantity=1):
    return {
        "paymentMethod": method,
        "addressId": market["address"],
        "shops": [
            {
                "shopId": market[name]["shop"],
                "fulfillment": "delivery" if name == "vendor" else "pickup",
                "items": [{"variantId": market[name]["variant"], "quantity": quantity}],
            }
            for name in (["vendor", "vendor2"] if both else ["vendor"])
        ],
    }
