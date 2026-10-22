from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field
from sqlalchemy import func, or_, select

from anymob.common import DB, Page, advisory_lock, audit, config, fail, get, owned_shop, paginated, record
from anymob.db import now
from anymob.models import Brand, Category, Classified, Product, Shop, User, Variant
from anymob.schemas import BrandInput, CategoryInput, ClassifiedInput, Input, ProductInput
from anymob.security import Actor, permission, throttle
from anymob.services.catalog import (
    ad_view,
    category_view,
    enrich_shop,
    prepare_listing,
    product_view,
    product_views,
    resolved_schema,
    save_product,
    shop_view,
    visible_ads,
    visible_products,
    visible_shops,
)

router = APIRouter(tags=["Catalog"])
CatalogAdmin = Annotated[object, Depends(permission("moderate_catalog"))]


@router.get("/categories")
async def categories(db: DB):
    rows = (
        await db.scalars(
            select(Category).where(Category.is_active.is_(True)).order_by(Category.sort_order, Category.name)
        )
    ).all()
    return [category_view(c) for c in rows]


@router.get("/categories/{id}/schema")
async def category_schema(id: str, db: DB):
    category = await get(db, Category, id)
    fields, variants = await resolved_schema(db, category)
    return {"fields": fields, "variantAttributes": variants, "version": category.schema_version}


@router.get("/brands")
async def brands(db: DB, category_id: str | None = Query(None, alias="categoryId"), popular: bool = False):
    query = select(Brand).where(Brand.is_active.is_(True)).order_by(Brand.popular_order, Brand.name)
    if category_id:
        query = query.where(Brand.category_ids.contains([category_id]))
    if popular:
        query = query.where(Brand.is_popular.is_(True))
    return [record(b) for b in (await db.scalars(query)).all()]


@router.get("/shops")
async def shops(db: DB, page: Page, search: str = "", city: str | None = None, featured: bool = False):
    query = visible_shops()
    if search:
        query = query.where(Shop.name.ilike(f"%{search[:160]}%"))
    if city:
        query = query.where(Shop.city == city)
    if featured:
        query = query.where(Shop.is_featured.is_(True))
    result = await paginated(db, query.order_by(Shop.created_at.desc()), page, shop_view)
    result["items"] = [await enrich_shop(db, await db.get(Shop, item["id"])) for item in result["items"]]
    return result


@router.get("/shops/{slug}")
async def shop_by_slug(slug: str, db: DB):
    shop = await db.scalar(visible_shops().where(or_(Shop.slug == slug, Shop.id == slug)))
    if not shop:
        fail(404, "Store not found")
    return await enrich_shop(db, shop)


async def descendant_ids(db, root_id):
    root = select(Category.id).where(Category.id == root_id).cte(recursive=True)
    root = root.union_all(select(Category.id).where(Category.parent_id == root.c.id))
    return list((await db.scalars(select(root.c.id))).all())


@router.get("/products")
async def products(
    db: DB,
    page: Page,
    search: str = "",
    shop_id: str | None = Query(None, alias="shopId"),
    category_id: str | None = Query(None, alias="categoryId"),
    brand_id: str | None = Query(None, alias="brandId"),
    min_price: float | None = Query(None, alias="minPrice", ge=0),
    max_price: float | None = Query(None, alias="maxPrice", ge=0),
    sort: str = "newest",
    attributes: str | None = None,
):
    query = visible_products()
    if shop_id:
        query = query.where(Product.shop_id == shop_id)
    if category_id:
        query = query.where(Product.category_id.in_(await descendant_ids(db, category_id)))
    if brand_id:
        query = query.where(Product.brand_id == brand_id)
    if search:
        query = query.where(
            or_(Product.name.ilike(f"%{search[:200]}%"), Product.description.ilike(f"%{search[:200]}%"))
        )
    variant_query = select(Variant.product_id).where(Variant.is_active.is_(True))
    if min_price is not None:
        variant_query = variant_query.where(Variant.price >= min_price)
    if max_price is not None:
        variant_query = variant_query.where(Variant.price <= max_price)
    query = query.where(Product.id.in_(variant_query))
    if attributes:
        import json

        try:
            facets = json.loads(attributes)
            if not isinstance(facets, dict) or len(facets) > 30:
                raise ValueError()
        except ValueError:
            fail(422, "attributes must be a JSON object")
        # JSON containment uses bound parameters and the JSONB GIN index.
        query = query.where(
            or_(
                Product.attributes.contains(facets),
                Product.id.in_(
                    select(Variant.product_id).where(
                        Variant.attributes.contains(facets), Variant.is_active.is_(True)
                    )
                ),
            )
        )
    price = (
        select(func.min(Variant.price))
        .where(Variant.product_id == Product.id, Variant.is_active.is_(True))
        .scalar_subquery()
    )
    query = query.order_by(
        price.asc()
        if sort == "price_asc"
        else price.desc()
        if sort == "price_desc"
        else Product.created_at.desc(),
        Product.id,
    )
    result = await paginated(db, query, page, lambda row: row)
    result["items"] = await product_views(db, result["items"])
    return result


@router.get("/products/{id}")
async def product(id: str, db: DB, shop_slug: str | None = Query(None, alias="shopSlug")):
    query = visible_products().where(or_(Product.id == id, Product.slug == id))
    if shop_slug:
        query = query.where(Shop.slug == shop_slug)
    rows = (await db.scalars(query.limit(2))).all()
    if not rows:
        fail(404, "Product not found")
    if len(rows) > 1:
        fail(422, "Specify the store slug for this product")
    return await product_view(db, rows[0])


@router.get("/ads")
async def ads(
    db: DB,
    page: Page,
    search: str = "",
    category_id: str | None = Query(None, alias="categoryId"),
    city: str | None = None,
    min_price: float | None = Query(None, alias="minPrice", ge=0),
    max_price: float | None = Query(None, alias="maxPrice", ge=0),
):
    query = visible_ads()
    if search:
        query = query.where(Classified.title.ilike(f"%{search[:200]}%"))
    if category_id:
        query = query.where(Classified.category_id.in_(await descendant_ids(db, category_id)))
    if city:
        query = query.where(Classified.city == city)
    if min_price is not None:
        query = query.where(Classified.price >= min_price)
    if max_price is not None:
        query = query.where(Classified.price <= max_price)
    return await paginated(db, query.order_by(Classified.created_at.desc()), page, ad_view)


@router.get("/ads/{slug}")
async def ad(slug: str, db: DB):
    row = await db.scalar(visible_ads().where(or_(Classified.slug == slug, Classified.id == slug)))
    if not row:
        fail(404, "Ad not found")
    return ad_view(row)


@router.post("/ads/{id}/view", status_code=202)
async def record_ad_view(id: str, request: Request, db: DB):
    from redis.asyncio import Redis
    from redis.exceptions import RedisError

    from anymob.common import client_ip, digest
    from anymob.config import settings

    row = await db.scalar(visible_ads().where(Classified.id == id).with_for_update(of=Classified))
    if not row:
        fail(404, "Ad not found")
    # One view per visitor per ad every 30 minutes; refreshes and bots do not inflate the counter.
    client = Redis.from_url(settings().redis_url)
    try:
        first = await client.set("adview:" + digest(client_ip(request) + ":" + id), 1, nx=True, ex=1800)
    except (RedisError, OSError):
        first = False
    finally:
        await client.aclose()
    if first:
        row.data = {**row.data, "views": int(row.data.get("views", 0)) + 1}
    return {"views": int(row.data.get("views", 0))}


@router.post("/ads/{id}/contact")
async def reveal_ad_contact(id: str, actor: Actor, db: DB):
    # Signed-in and rate limited, so seller numbers cannot be harvested in bulk.
    await throttle("ad-contact:" + actor.id, 30, 3600)
    row = await db.scalar(visible_ads().where(Classified.id == id).with_for_update(of=Classified))
    if not row:
        fail(404, "Ad not found")
    seller = await db.get(User, row.seller_id)
    if row.seller_id != actor.id:
        row.data = {**row.data, "inquiries": int(row.data.get("inquiries", 0)) + 1}
    return {"phone": seller.phone}


@router.get("/me/ads")
async def my_ads(actor: Actor, db: DB, page: Page):
    return await paginated(
        db,
        select(Classified)
        .where(Classified.seller_id == actor.id, Classified.status != "archived")
        .order_by(Classified.created_at.desc()),
        page,
        ad_view,
    )


def ad_data(data: ClassifiedInput, flags, previous=None):
    price_type = data.price_type or ("negotiable" if data.negotiable else "fixed")
    # Counters are owned by the platform, so an edit keeps them.
    counters = {k: v for k, v in (previous or {}).items() if k in {"views", "favorites", "inquiries"}}
    return {
        **counters,
        "negotiable": price_type == "negotiable",
        "priceType": price_type,
        "condition": data.condition,
        "moderationFlags": flags,
    }


@router.post("/me/ads", status_code=201)
async def create_ad(data: ClassifiedInput, actor: Actor, db: DB):
    await throttle("ad-create:" + actor.id, 20, 3600)
    fields, _, status, flags = await prepare_listing(db, actor, data)
    rules = await config(db, "catalog")
    row = Classified(
        **data.model_dump(exclude={"negotiable", "price_type", "condition", "status"}),
        seller_id=actor.id,
        status=status,
        schema_snapshot=fields,
        expires_at=now() + timedelta(days=rules["adExpiryDays"]),
        data=ad_data(data, flags),
    )
    db.add(row)
    await db.flush()
    return ad_view(row)


@router.put("/me/ads/{id}")
async def update_ad(id: str, data: ClassifiedInput, actor: Actor, db: DB):
    row = await get(db, Classified, id, lock=True)
    if row.seller_id != actor.id:
        fail(404, "Ad not found")
    fields, _, status, flags = await prepare_listing(db, actor, data, existing_images=row.images)
    for key, value in data.model_dump(exclude={"negotiable", "price_type", "condition", "status"}).items():
        setattr(row, key, value)
    row.status, row.schema_snapshot = status, fields
    row.data = ad_data(data, flags, row.data)
    return ad_view(row)


@router.delete("/me/ads/{id}")
async def delete_ad(id: str, actor: Actor, db: DB):
    row = await get(db, Classified, id, lock=True)
    if row.seller_id != actor.id:
        fail(404, "Ad not found")
    row.status = "archived"
    return {"id": id}


@router.get("/vendor/products")
async def vendor_products(actor: Actor, db: DB, page: Page, status: str | None = None, search: str = ""):
    shop = await owned_shop(db, actor)
    query = select(Product).where(Product.shop_id == shop.id)
    if status:
        query = query.where(Product.status == status)
    if search:
        query = query.where(Product.name.ilike(f"%{search[:200]}%"))
    result = await paginated(db, query.order_by(Product.created_at.desc()), page, lambda row: row)
    result["items"] = await product_views(db, result["items"])
    return result


@router.get("/vendor/products/{id}")
async def vendor_product(id: str, actor: Actor, db: DB):
    shop = await owned_shop(db, actor)
    row = await get(db, Product, id)
    if row.shop_id != shop.id:
        fail(404, "Product not found")
    return await product_view(db, row)


@router.post("/vendor/products", status_code=201)
async def create_product(data: ProductInput, actor: Actor, db: DB):
    shop = await owned_shop(db, actor, lock=True)
    row = await save_product(db, actor, shop, data)
    await db.flush()
    return await product_view(db, row)


@router.put("/vendor/products/{id}")
async def update_product(id: str, data: ProductInput, actor: Actor, db: DB):
    shop = await owned_shop(db, actor, lock=True)
    row = await get(db, Product, id, lock=True)
    if row.shop_id != shop.id:
        fail(404, "Product not found")
    row = await save_product(db, actor, shop, data, row)
    await db.flush()
    return await product_view(db, row)


@router.delete("/vendor/products/{id}")
async def archive_product(id: str, actor: Actor, db: DB):
    shop = await owned_shop(db, actor)
    row = await get(db, Product, id, lock=True)
    if row.shop_id != shop.id:
        fail(404, "Product not found")
    row.status = "archived"
    return {"id": id}


@router.get("/admin/categories")
async def admin_categories(actor: CatalogAdmin, db: DB):
    rows = (await db.scalars(select(Category).order_by(Category.sort_order))).all()

    async def counts(model, column):
        return dict((await db.execute(select(column, func.count()).group_by(column))).all())

    children = await counts(Category, Category.parent_id)
    products = await counts(Product, Product.category_id)
    ads = await counts(Classified, Classified.category_id)
    # Usage backs the delete guard: a category in use cannot be removed.
    return [
        {
            **category_view(c),
            "usage": {
                "children": children.get(c.id, 0),
                "products": products.get(c.id, 0),
                "ads": ads.get(c.id, 0),
            },
        }
        for c in rows
    ]


async def write_category(db, actor, request, data, row=None):
    await advisory_lock(db, "catalog:categories")
    parent = await get(db, Category, data.parent_id) if data.parent_id else None
    if row and data.parent_id in await descendant_ids(db, row.id):
        fail(422, "A category cannot become its own descendant")
    fields = data.attributes_schema
    inherited, _ = await resolved_schema(db, parent) if parent else ([], [])
    available = {f["name"] for f in inherited} | {f.name for f in fields}
    if not set(data.variant_attributes) <= available:
        fail(422, "Variant fields must exist in the category schema")
    before = category_view(row) if row else None
    row = row or Category()
    for key in ["name", "slug", "parent_id", "is_active", "available_for", "variant_attributes"]:
        setattr(row, key, getattr(data, key))
    row.sort_order = data.order
    row.attributes_schema = [f.model_dump(by_alias=True) for f in fields]
    row.schema_version = (row.schema_version or 0) + 1
    row.data = data.model_dump(
        by_alias=True, include={"icon", "unit", "blurb", "seo_title", "seo_description"}
    )
    db.add(row)
    await db.flush()
    await audit(db, actor, request, "category.saved", row, before=before)
    return category_view(row)


@router.post("/admin/categories", status_code=201)
async def add_category(data: CategoryInput, request: Request, actor: CatalogAdmin, db: DB):
    return await write_category(db, actor, request, data)


@router.put("/admin/categories/{id}")
async def edit_category(id: str, data: CategoryInput, request: Request, actor: CatalogAdmin, db: DB):
    return await write_category(db, actor, request, data, await get(db, Category, id))


class Reorder(Input):
    ids: list[str] = Field(min_length=1, max_length=500)
    parent_id: str | None = None


@router.post("/admin/categories/reorder")
async def reorder_categories(data: Reorder, actor: CatalogAdmin, request: Request, db: DB):
    await advisory_lock(db, "catalog:categories")
    rows = (await db.scalars(select(Category).where(Category.parent_id == data.parent_id))).all()
    if len(data.ids) != len(set(data.ids)) or set(data.ids) != {r.id for r in rows}:
        fail(422, "Include every sibling exactly once")
    for row in rows:
        before = record(row)
        row.sort_order = data.ids.index(row.id)
        await audit(db, actor, request, "category.reordered", row, before=before)
    return {"ok": True}


@router.delete("/admin/categories/{id}")
async def remove_category(id: str, actor: CatalogAdmin, request: Request, db: DB):
    await advisory_lock(db, "catalog:categories")
    row = await get(db, Category, id)
    usage = {}
    for model in [Product, Classified]:
        usage[model.__tablename__] = await db.scalar(
            select(func.count()).select_from(model).where(model.category_id == id)
        )
    if any(usage.values()) or await db.scalar(select(Category.id).where(Category.parent_id == id).limit(1)):
        fail(409, "Move this category's products, ads and subcategories before deleting it")
    if await db.scalar(select(Brand.id).where(Brand.category_ids.contains([id])).limit(1)):
        fail(409, "Unlink brands from this category before deleting it")
    await audit(db, actor, request, "category.deleted", row, before=record(row))
    await db.delete(row)
    return {"id": id}


@router.get("/admin/brands")
async def admin_brands(actor: CatalogAdmin, db: DB):
    result = []
    for brand in (await db.scalars(select(Brand).order_by(Brand.popular_order, Brand.name))).all():
        result.append(
            {
                **record(brand),
                "productCount": await db.scalar(
                    select(func.count()).select_from(Product).where(Product.brand_id == brand.id)
                ),
            }
        )
    return result


async def write_brand(db, actor, request, data, row=None):
    for id in data.category_ids:
        await get(db, Category, id)
    before = record(row) if row else None
    row = row or Brand()
    for key, value in data.model_dump().items():
        setattr(row, key, value)
    db.add(row)
    await db.flush()
    await audit(db, actor, request, "brand.saved", row, before=before)
    return record(row)


@router.post("/admin/brands", status_code=201)
async def add_brand(data: BrandInput, actor: CatalogAdmin, request: Request, db: DB):
    return await write_brand(db, actor, request, data)


@router.put("/admin/brands/{id}")
async def edit_brand(id: str, data: BrandInput, actor: CatalogAdmin, request: Request, db: DB):
    return await write_brand(db, actor, request, data, await get(db, Brand, id, lock=True))


@router.post("/admin/brands/reorder")
async def reorder_brands(data: Reorder, actor: CatalogAdmin, request: Request, db: DB):
    await advisory_lock(db, "catalog:brands")
    rows = (await db.scalars(select(Brand).where(Brand.is_popular.is_(True)))).all()
    if len(data.ids) != len(set(data.ids)) or set(data.ids) != {r.id for r in rows}:
        fail(422, "Include all homepage brands exactly once")
    for row in rows:
        before = record(row)
        row.popular_order = data.ids.index(row.id)
        await audit(db, actor, request, "brand.reordered", row, before=before)
    return {"ok": True}


@router.delete("/admin/brands/{id}")
async def remove_brand(id: str, actor: CatalogAdmin, request: Request, db: DB):
    row = await get(db, Brand, id, lock=True)
    for model in [Product, Classified]:
        if await db.scalar(select(model.id).where(model.brand_id == id).limit(1)):
            fail(409, "Reassign listings before deleting this brand")
    await audit(db, actor, request, "brand.deleted", row, before=record(row))
    await db.delete(row)
    return {"id": id}


@router.get("/search")
async def search_catalog(
    db: DB, q: str = Query(min_length=2, max_length=200), limit: int = Query(10, ge=1, le=30)
):
    from sqlalchemy import literal_column

    simple = literal_column("'simple'::regconfig")
    term = func.plainto_tsquery(simple, q)
    products_query = visible_products().where(
        or_(
            func.to_tsvector(simple, Product.name + " " + Product.description).op("@@")(term),
            Product.name.ilike(f"%{q}%"),
        )
    )
    ads_query = visible_ads().where(
        or_(
            func.to_tsvector(simple, Classified.title + " " + Classified.description).op("@@")(term),
            Classified.title.ilike(f"%{q}%"),
        )
    )
    shops_query = visible_shops().where(Shop.name.ilike(f"%{q}%"))
    return {
        "products": await product_views(db, (await db.scalars(products_query.limit(limit))).all()),
        "ads": [
            record(a, exclude={"schema_snapshot"}) for a in (await db.scalars(ads_query.limit(limit))).all()
        ],
        "shops": [await enrich_shop(db, s) for s in (await db.scalars(shops_query.limit(limit))).all()],
    }
