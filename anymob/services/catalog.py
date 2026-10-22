from datetime import timedelta

from sqlalchemy import func, select

from anymob.common import config, fail, get, record
from anymob.db import now
from anymob.models import Brand, Category, Classified, Product, Shop, Upload, User, Variant


def category_view(category):
    value = {**category.data, **record(category, exclude={"data"})}
    value["order"] = value.pop("sortOrder")
    value["adCount"] = 0
    return value


def shop_view(shop):
    return {
        "logo": "",
        "coverImage": "",
        "tagline": "",
        "description": "",
        "address": "",
        "phone": "",
        "whatsapp": "",
        "lat": 0,
        "lng": 0,
        "rating": 0,
        "reviewCount": 0,
        "followers": 0,
        "totalProducts": 0,
        "sinceYear": shop.created_at.year,
        "socialLinks": {},
        "specialties": [],
        "businessHours": {},
        "storeSections": [],
        "promo": None,
        **{
            k: v
            for k, v in shop.data.items()
            if k
            not in {
                "verificationDocuments",
                "verificationNote",
                "verificationSubmittedAt",
                "bankTransfer",
                "verificationDraft",
            }
        },
        **record(shop, exclude={"data", "payout_mask", "commission_override"}),
    }


def ad_view(ad):
    return {
        "priceType": "negotiable" if ad.data.get("negotiable") else "fixed",
        "condition": "used",
        "views": 0,
        "favorites": 0,
        "inquiries": 0,
        **{k: v for k, v in ad.data.items() if k != "moderationFlags"},
        **record(ad, exclude={"schema_snapshot", "data"}),
    }


def visible_shops():
    return (
        select(Shop)
        .join(User, Shop.owner_id == User.id)
        .where(
            Shop.verification_status == "verified",
            Shop.is_suspended.is_(False),
            Shop.is_deleted.is_(False),
            User.status == "active",
        )
    )


def active_category_ids(channel):
    roots = (
        select(Category.id)
        .where(
            Category.parent_id.is_(None),
            Category.is_active.is_(True),
            Category.available_for.contains([channel]),
        )
        .cte(name="active_" + channel + "_categories", recursive=True)
    )
    return roots.union_all(
        select(Category.id).where(
            Category.parent_id == roots.c.id,
            Category.is_active.is_(True),
            Category.available_for.contains([channel]),
        )
    )


def visible_products():
    return (
        select(Product)
        .join(Shop)
        .join(User, Shop.owner_id == User.id)
        .where(
            Product.status == "active",
            Product.category_id.in_(select(active_category_ids("store").c.id)),
            Shop.verification_status == "verified",
            Shop.is_suspended.is_(False),
            Shop.is_deleted.is_(False),
            User.status == "active",
        )
    )


def visible_ads():
    return (
        select(Classified)
        .join(User, Classified.seller_id == User.id)
        .where(
            Classified.status == "active",
            Classified.expires_at > now(),
            User.status == "active",
            Classified.category_id.in_(select(active_category_ids("classified").c.id)),
        )
    )


def product_record(product, variants):
    return {
        "condition": "new",
        "ptaStatus": "not_applicable",
        "rating": 0,
        "reviewCount": 0,
        "isFeatured": False,
        "soldCount": 0,
        **product.data,
        **record(product, exclude={"data", "schema_snapshot"}),
        "variants": [record(v) for v in variants],
    }


async def product_view(db, product):
    return (await product_views(db, [product]))[0]


async def product_views(db, products) -> list[dict]:
    """Products with their active variants, loaded in one query for the whole list."""
    products = list(products)
    if not products:
        return []
    variants: dict[str, list] = {}
    for variant in (
        await db.scalars(
            select(Variant)
            .where(Variant.product_id.in_({p.id for p in products}), Variant.is_active.is_(True))
            .order_by(Variant.created_at)
        )
    ).all():
        variants.setdefault(variant.product_id, []).append(variant)
    return [product_record(p, variants.get(p.id, [])) for p in products]


async def resolved_schema(db, category):
    fields, variants, seen = {}, [], set()
    chain = []
    current = category
    while current:
        if current.id in seen:
            fail(409, "Category hierarchy contains a cycle")
        seen.add(current.id)
        chain.append(current)
        current = await db.get(Category, current.parent_id) if current.parent_id else None
    for node in reversed(chain):
        for field in node.attributes_schema:
            fields[field["name"]] = field
        if node.variant_attributes:
            variants = node.variant_attributes
    return list(fields.values()), variants


def validate_attributes(fields, attrs, *, skip=()):
    known = {f["name"]: f for f in fields}
    if any(key not in known for key in attrs):
        fail(422, "Unknown specification field")
    for name, field in known.items():
        value = attrs.get(name)
        if name in skip:
            continue
        if value is None or value == "" or value == []:
            if field.get("required"):
                fail(422, f"{field['label']} is required")
            continue
        kind = field["type"]
        valid = (
            (kind == "text" and isinstance(value, str) and len(value) <= 2000)
            or (kind == "number" and type(value) in (int, float) and abs(value) < 1e15)
            or (kind == "boolean" and isinstance(value, bool))
            or (kind == "select" and isinstance(value, str) and value in field.get("options", []))
            or (
                kind == "multiselect"
                and isinstance(value, list)
                and all(isinstance(v, str) and v in field.get("options", []) for v in value)
            )
        )
        if not valid:
            fail(422, f"Invalid value for {field['label']}")


async def validate_media(db, actor_id, urls, purpose="catalog"):
    # API-owned URLs only. No arbitrary URL fetches or client data URLs.
    for url in urls:
        prefix = "/api/v1/media/"
        if not isinstance(url, str) or not url.startswith(prefix):
            fail(422, "Upload images through the media API first")
        media = await db.get(Upload, url[len(prefix) :])
        if not media or media.owner_id != actor_id or media.status != "ready" or media.purpose != purpose:
            fail(422, "An image is unavailable or belongs to another account")


async def prepare_listing(db, actor, data, *, shop=None, existing_images=()):
    category = await get(db, Category, data.category_id)
    channel = "store" if shop else "classified"
    current = category
    seen = set()
    while current:
        if current.id in seen or not current.is_active or channel not in current.available_for:
            fail(422, "Category is unavailable for this listing")
        seen.add(current.id)
        current = await db.get(Category, current.parent_id) if current.parent_id else None
    if data.brand_id:
        brand = await get(db, Brand, data.brand_id)
        if not brand.is_active or (brand.category_ids and not set(brand.category_ids).intersection(seen)):
            fail(422, "Brand is unavailable in this category")
    fields, variant_keys = await resolved_schema(db, category)
    validate_attributes(fields, data.attributes, skip=variant_keys if shop else ())
    rules = await config(db, "catalog")
    if len(data.images) > rules["maxProductImages" if shop else "maxAdImages"]:
        fail(422, "Too many images")
    # Images already on the listing were accepted before; only new ones must be the seller's uploads.
    await validate_media(db, actor.id, [url for url in data.images if url not in set(existing_images)])
    status = data.status
    text = ((data.name if shop else data.title) + " " + data.description).lower()
    flags = ["banned_words"] if any(word.lower() in text for word in rules["bannedWords"]) else []
    auto = (
        rules["autoApproveVerifiedProducts"] and shop.verification_status == "verified"
        if shop
        else rules["autoApproveAds"]
    )
    if (
        status == "pending"
        and auto
        and not flags
        and actor.is_verified
        and actor.created_at <= now() - timedelta(days=rules["reviewUnderDays"])
    ):
        status = "active"
    return fields, variant_keys, status, flags


async def save_product(db, actor, shop, data, existing=None):
    fields, variant_keys, status, flags = await prepare_listing(
        db, actor, data, shop=shop, existing_images=existing.images if existing else ()
    )
    if len({v.sku for v in data.variants}) != len(data.variants):
        fail(422, "Variant SKUs must be unique")
    combinations = set()
    for variant in data.variants:
        if set(variant.attributes) != set(variant_keys):
            fail(422, "Each variant must contain exactly the category's variant fields")
        validate_attributes([f for f in fields if f["name"] in variant_keys], variant.attributes)
        signature = str(sorted(variant.attributes.items()))
        if signature in combinations:
            fail(422, "Duplicate variant combination")
        combinations.add(signature)
        if variant.image:
            await validate_media(db, actor.id, [variant.image])
    if not existing:
        from anymob.models import Plan

        plan = (
            await db.get(Plan, shop.plan_id)
            if shop.plan_id
            else await db.scalar(
                select(Plan)
                .where(Plan.is_active.is_(True), Plan.price == 0)
                .order_by(Plan.created_at)
                .limit(1)
            )
        )
        count = await db.scalar(
            select(func.count())
            .select_from(Product)
            .where(Product.shop_id == shop.id, Product.status != "archived")
        )
        limit = plan.product_limit if plan else 20
        if limit is not None and count >= limit:
            fail(409, "Your subscription product limit has been reached")
    product = existing or Product(shop_id=shop.id)
    for key in ["name", "slug", "category_id", "brand_id", "description", "attributes", "images"]:
        setattr(product, key, getattr(data, key))
    product.schema_snapshot = fields
    product.status = status
    # Ratings, sales and featuring are platform-owned, so a vendor edit keeps them.
    kept = {
        k: v
        for k, v in (product.data or {}).items()
        if k in {"rating", "reviewCount", "soldCount", "isFeatured"}
    }
    product.data = {
        **kept,
        "condition": data.condition,
        "ptaStatus": data.pta_status,
        "warranty": data.warranty,
        "sectionId": data.section_id,
        "details": data.details,
        "moderationFlags": flags,
    }
    db.add(product)
    await db.flush()
    old = {
        v.id: v
        for v in (
            await db.scalars(
                select(Variant).where(Variant.product_id == product.id).order_by(Variant.id).with_for_update()
            )
        ).all()
    }
    for variant in data.variants:
        row = old.pop(variant.id, None) if variant.id else None
        if variant.id and row is None:
            fail(422, "Variant does not belong to this product")
        if row is None:
            row = Variant(product_id=product.id)
        previous_stock = row.stock or 0
        for key, value in variant.model_dump(exclude={"id"}).items():
            setattr(row, key, value)
        row.is_active = True
        db.add(row)
        await db.flush()
        if previous_stock != row.stock:
            from anymob.models import InventoryMovement

            db.add(
                InventoryMovement(
                    variant_id=row.id,
                    delta=row.stock - previous_stock,
                    reason="vendor_adjustment",
                    stock_after=row.stock,
                )
            )
    for row in old.values():
        row.is_active = False  # Historical order items retain a valid FK.
    return product


async def enrich_shop(db, shop):
    from anymob.models import Preference, Review

    avg, count = (
        await db.execute(
            select(func.avg(Review.rating), func.count(Review.id)).where(
                Review.shop_id == shop.id, Review.hidden.is_(False)
            )
        )
    ).one()
    followers = await db.scalar(
        select(func.count())
        .select_from(Preference)
        .where(Preference.kind == "follow_shop", Preference.target_id == shop.id)
    )
    products = await db.scalar(
        select(func.count())
        .select_from(Product)
        .where(Product.shop_id == shop.id, Product.status == "active")
    )
    result = {
        **shop_view(shop),
        "rating": round(float(avg or 0), 2),
        "reviewCount": count,
        "followers": followers,
        "totalProducts": products,
    }
    if not shop.data.get("showPhonePublicly", True):
        result["phone"], result["whatsapp"] = "", ""
    return result
