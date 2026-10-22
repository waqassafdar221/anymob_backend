"""Load the frontend's demo catalog (scripts/demo_catalog.json) into a development database.

Wipes every table first, so it refuses to run unless ENVIRONMENT=development and the database name ends in
`_dev`. All imported accounts share one password, printed at the end. Images stay as the demo's placeholder
URLs, which bypass the upload pipeline that real listings go through.

    .venv/bin/python scripts/import_demo_catalog.py
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text

from anymob.cli import seed_into
from anymob.config import settings
from anymob.db import Base, Session, engine, uid
from anymob.models import Brand, Category, Classified, Plan, Product, Question, Shop, User, Variant
from anymob.schemas import phone_number
from anymob.security import hash_password

DEMO_PASSWORD = "AnyMob-demo-2026"
FIXTURE = Path(__file__).with_name("demo_catalog.json")
PRODUCT_STATUS = {"pending_review": "pending"}
PTA_STATUS = {True: "approved", False: "non_pta", None: "not_applicable"}


def when(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def resolved_fields(categories, category_id):
    chain, current = [], categories.get(category_id)
    while current:
        chain.append(current)
        current = categories.get(current["parentId"])
    fields = {}
    for node in reversed(chain):
        for field in node.get("attributesSchema", []):
            fields[field["name"]] = field
    return list(fields.values())


async def main():
    cfg = settings()
    if cfg.environment != "development" or not engine.url.database.endswith("_dev"):
        raise SystemExit("Refusing: needs ENVIRONMENT=development and a database whose name ends in _dev")
    demo = json.loads(FIXTURE.read_text())
    categories = {c["id"]: c for c in demo["categories"]}

    async with engine.begin() as conn:
        tables = ", ".join('"' + t.name + '"' for t in Base.metadata.sorted_tables)
        await conn.execute(text("TRUNCATE " + tables + " RESTART IDENTITY CASCADE"))

    password_hash = hash_password(DEMO_PASSWORD)
    async with Session.begin() as db:
        await seed_into(db)
        await db.flush()
        # The demo categories and plans replace the generic seed rows.
        await db.execute(text("DELETE FROM categories"))
        await db.execute(text("DELETE FROM plans"))

        for plan in demo["plans"]:
            db.add(
                Plan(
                    id=plan["id"],
                    name=plan["name"],
                    slug=plan["slug"],
                    price=plan["price"],
                    yearly_price=plan["price"] * 10,
                    product_limit=plan.get("adLimit"),
                    featured_slots=plan.get("featuredAdsPerMonth", 0),
                    commission_rate=plan.get("commissionRate"),
                    features=plan.get("features", []),
                    perks={
                        k: bool(plan.get(k))
                        for k in ("storePromo", "advancedAnalytics", "csvImport", "prioritySupport")
                    },
                    is_popular=plan.get("isPopular", False),
                )
            )
        # Parents before children, for the self-referencing foreign key.
        for category in sorted(demo["categories"], key=lambda c: c["parentId"] is not None):
            db.add(
                Category(
                    id=category["id"],
                    name=category["name"],
                    slug=category["slug"],
                    parent_id=category["parentId"],
                    sort_order=category.get("order", 0),
                    attributes_schema=category.get("attributesSchema", []),
                    variant_attributes=category.get("variantAttributes", []),
                    data={k: category[k] for k in ("icon", "unit", "blurb") if k in category},
                )
            )
            await db.flush()
        for brand in demo["brands"]:
            db.add(
                Brand(
                    id=brand["id"],
                    name=brand["name"],
                    slug=brand["slug"],
                    logo=brand.get("logo", ""),
                    kind=brand.get("kind", "phone"),
                    category_ids=brand.get("categoryIds", []),
                    is_active=brand.get("isActive", True),
                    is_popular=brand.get("isPopular", False),
                    popular_order=brand.get("popularOrder") or 0,
                )
            )
        for user in demo["users"]:
            db.add(
                User(
                    id=user["id"],
                    email=user["email"].lower(),
                    phone=phone_number(user["phone"]),
                    password_hash=password_hash,
                    full_name=user["fullName"],
                    role=user["role"],
                    admin_role="super_admin" if user["role"] == "admin" else None,
                    status=user["status"],
                    suspended_until=when(user.get("suspendedUntil")),
                    is_verified=user.get("isVerified", False),
                    city=user.get("city", "Lahore"),
                    avatar=user.get("avatar", ""),
                    created_at=when(user.get("createdAt")),
                )
            )
        await db.flush()

        record_keys = {
            "id",
            "slug",
            "ownerId",
            "name",
            "city",
            "verificationStatus",
            "isFeatured",
            "isOnVacation",
            "planId",
        }
        for shop in demo["shops"]:
            db.add(
                Shop(
                    id=shop["id"],
                    owner_id=shop["ownerId"],
                    name=shop["name"],
                    slug=shop["slug"],
                    city=shop["city"],
                    verification_status=shop.get("verificationStatus", "verified"),
                    is_featured=shop.get("isFeatured", False),
                    is_on_vacation=shop.get("isOnVacation", False),
                    plan_id=shop.get("planId"),
                    data={k: v for k, v in shop.items() if k not in record_keys},
                )
            )
        await db.flush()

        skus = set()
        for product in demo["store_products"]:
            details = {
                k: product[k]
                for k in (
                    "tags",
                    "sectionIds",
                    "batteryHealth",
                    "conditionGrade",
                    "conditionReport",
                    "rejectionReason",
                )
                if product.get(k) is not None
            }
            db.add(
                Product(
                    id=product["id"],
                    shop_id=product["shopId"],
                    category_id=product["categoryId"],
                    brand_id=product.get("brandId"),
                    name=product["name"],
                    slug=product["slug"],
                    status=PRODUCT_STATUS.get(product["status"], product["status"]),
                    description=product.get("description", ""),
                    attributes=product.get("specs", {}),
                    schema_snapshot=resolved_fields(categories, product["categoryId"]),
                    images=product.get("images", []),
                    data={
                        "condition": product.get("condition", "new"),
                        "ptaStatus": PTA_STATUS[product.get("ptaApproved")],
                        "warranty": product.get("warranty", ""),
                        "sectionId": (product.get("sectionIds") or [None])[0],
                        "details": details,
                        "moderationFlags": [],
                        "isFeatured": product.get("isFeatured", False),
                        "rating": product.get("rating", 0),
                        "reviewCount": product.get("reviewCount", 0),
                        "soldCount": product.get("soldCount", 0),
                    },
                    created_at=when(product.get("createdAt")),
                )
            )
            for index, variant in enumerate(product["variants"]):
                sku = variant["sku"]
                while sku in skus:
                    sku = f"{variant['sku']}-{product['id']}-{index}"
                skus.add(sku)
                db.add(
                    Variant(
                        id=uid(),
                        product_id=product["id"],
                        sku=sku,
                        attributes=variant.get("attributes", {}),
                        price=Decimal(str(variant["price"])),
                        compare_at_price=variant.get("compareAtPrice"),
                        stock=variant.get("stock", 0),
                        low_stock_threshold=variant.get("lowStockThreshold", 5),
                    )
                )
        await db.flush()

        now = datetime.now(UTC)
        for ad in demo["classified_ads"]:
            # The fixture's dates are frozen; keep active ads live for a month from today.
            expires = now + timedelta(days=30) if ad["status"] == "active" else when(ad.get("expiresAt"))
            db.add(
                Classified(
                    id=ad["id"],
                    seller_id=ad["sellerId"],
                    category_id=ad["categoryId"],
                    brand_id=ad.get("brandId"),
                    title=ad["title"],
                    slug=ad["slug"],
                    description=ad["description"],
                    price=ad["price"],
                    city=ad["city"],
                    status=PRODUCT_STATUS.get(ad["status"], ad["status"]),
                    attributes=ad.get("attributes", {}),
                    schema_snapshot=resolved_fields(categories, ad["categoryId"]),
                    images=ad.get("images", []),
                    data={
                        k: ad[k]
                        for k in ("priceType", "condition", "views", "favorites", "inquiries")
                        if k in ad
                    },
                    expires_at=expires,
                    created_at=when(ad.get("createdAt")),
                )
            )
        for question in demo["questions"]:
            answer = question.get("answer") or {}
            db.add(
                Question(
                    id=question["id"],
                    product_id=question["productId"],
                    author_id=question["askedBy"],
                    question=question["question"],
                    answer=answer.get("body"),
                    answered_at=when(answer.get("answeredAt")),
                    created_at=when(question.get("askedAt")),
                )
            )

    await engine.dispose()
    counts = {k: len(v) for k, v in demo.items()}
    print(f"Imported into {engine.url.database}: {counts}")
    print(f"Every imported account uses the password: {DEMO_PASSWORD}")


if __name__ == "__main__":
    asyncio.run(main())
