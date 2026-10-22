"""Bounded atomic imports, bulk catalog actions, and schema-driven facets."""

import csv
import io
import json
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import Field, ValidationError
from sqlalchemy import select

from anymob.common import DB, audit, fail, get, owned_shop, record
from anymob.models import Category, InventoryMovement, Product, Variant
from anymob.schemas import Input, Money, ProductInput
from anymob.security import Actor
from anymob.services.catalog import resolved_schema, save_product, visible_products

router = APIRouter(tags=["Catalog bulk tools"])


class CSVInput(Input):
    csv: str = Field(min_length=1, max_length=1000000)


@router.post("/vendor/products/import-csv")
async def import_csv(data: CSVInput, actor: Actor, request: Request, db: DB):
    shop = await owned_shop(db, actor, lock=True)
    reader = csv.DictReader(io.StringIO(data.csv))
    required = {"name", "slug", "categoryId", "sku", "price", "stock"}
    if not reader.fieldnames or not required <= set(reader.fieldnames):
        fail(422, "CSV requires name, slug, categoryId, sku, price and stock columns")
    drafts = {}
    for line, row in enumerate(reader, 2):
        if line > 501:
            fail(422, "Import at most 500 variant rows at a time")
        try:
            variant = {
                "sku": row["sku"],
                "price": row["price"],
                "stock": int(row["stock"]),
                "attributes": json.loads(row.get("variantAttributes") or "{}"),
            }
            if row["slug"] not in drafts:
                drafts[row["slug"]] = {
                    "name": row["name"],
                    "slug": row["slug"],
                    "categoryId": row["categoryId"],
                    "brandId": row.get("brandId") or None,
                    "description": row.get("description") or "",
                    "attributes": json.loads(row.get("attributes") or "{}"),
                    "images": json.loads(row.get("images") or "[]"),
                    "status": "draft",
                    "variants": [],
                }
            drafts[row["slug"]]["variants"].append(variant)
        except (ValueError, TypeError):
            fail(422, f"Invalid values at CSV row {line}")
    if not drafts:
        fail(422, "CSV contains no products")
    result = []
    for draft in drafts.values():
        try:
            parsed = ProductInput.model_validate(draft)
        except ValidationError as exc:
            fail(422, f"Invalid product {draft['slug']}: {exc}")
        row = await save_product(db, actor, shop, parsed)
        result.append(row.id)
        await audit(db, actor, request, "product.imported", row)
    return {"ids": result, "count": len(result)}


@router.get("/vendor/catalog-export")
async def export_csv(actor: Actor, db: DB):
    shop = await owned_shop(db, actor)
    rows = (
        await db.execute(
            select(Product, Variant)
            .join(Variant, Variant.product_id == Product.id)
            .where(Product.shop_id == shop.id, Variant.is_active.is_(True))
            .order_by(Product.name, Variant.sku)
            .limit(100000)
        )
    ).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "name",
            "slug",
            "categoryId",
            "brandId",
            "description",
            "sku",
            "price",
            "stock",
            "attributes",
            "variantAttributes",
            "images",
        ]
    )

    def safe(value):
        text = str(value or "")
        return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text

    for product, variant in rows:
        writer.writerow(
            [
                safe(product.name),
                product.slug,
                product.category_id,
                product.brand_id or "",
                safe(product.description),
                safe(variant.sku),
                variant.price,
                variant.stock,
                json.dumps(product.attributes),
                json.dumps(variant.attributes),
                json.dumps(product.images),
            ]
        )
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=catalog.csv"},
    )


class BulkProduct(Input):
    ids: list[str] = Field(min_length=1, max_length=100)
    action: Literal["archive", "submit", "price", "stock"]
    amount: Money | None = None
    stock: int | None = Field(default=None, ge=0, le=1000000)


@router.post("/vendor/products/bulk")
async def bulk_product(data: BulkProduct, actor: Actor, request: Request, db: DB):
    shop = await owned_shop(db, actor, lock=True)
    rows = (
        await db.scalars(
            select(Product)
            .where(Product.shop_id == shop.id, Product.id.in_(data.ids))
            .order_by(Product.id)
            .with_for_update()
        )
    ).all()
    if len(rows) != len(set(data.ids)):
        fail(404, "A selected product does not belong to this store")
    if data.action == "price" and data.amount is None or data.action == "stock" and data.stock is None:
        fail(422, "Enter the new value")
    for product in rows:
        before = record(product)
        if data.action == "archive":
            product.status = "archived"
        elif data.action == "submit":
            # Validate the current schema before re-submission, including all variants.
            from anymob.services.catalog import validate_attributes

            category = await get(db, Category, product.category_id)
            fields, variant_keys = await resolved_schema(db, category)
            validate_attributes(fields, product.attributes, skip=variant_keys)
            if not category.is_active or "store" not in category.available_for:
                fail(422, "A category is inactive")
            product.status = "pending"
        else:
            variants = (
                await db.scalars(
                    select(Variant)
                    .where(Variant.product_id == product.id, Variant.is_active.is_(True))
                    .order_by(Variant.id)
                    .with_for_update()
                )
            ).all()
            for variant in variants:
                if data.action == "price":
                    variant.price = data.amount
                    product.status = "pending"
                else:
                    delta = data.stock - variant.stock
                    variant.stock = data.stock
                    db.add(
                        InventoryMovement(
                            variant_id=variant.id,
                            delta=delta,
                            reason="bulk_adjustment",
                            stock_after=variant.stock,
                        )
                    )
        await audit(db, actor, request, "product.bulk_" + data.action, product, before=before)
    return {"count": len(rows)}


@router.get("/catalog/facets/{category_id}")
async def facets(category_id: str, db: DB, shop_id: str | None = None):
    from anymob.routes.catalog import descendant_ids

    category = await get(db, Category, category_id)
    fields, variant_keys = await resolved_schema(db, category)
    query = visible_products().where(Product.category_id.in_(await descendant_ids(db, category_id)))
    if shop_id:
        query = query.where(Product.shop_id == shop_id)
    rows = (await db.scalars(query.limit(10000))).all()
    variants = (
        await db.scalars(
            select(Variant).where(Variant.product_id.in_([p.id for p in rows]), Variant.is_active.is_(True))
        )
    ).all()
    buckets = []
    for field in fields:
        if not field.get("filterable"):
            continue
        counts = {}
        sources = variants if field["name"] in variant_keys else rows
        for row in sources:
            value = row.attributes.get(field["name"])
            for option in value if isinstance(value, list) else [value]:
                if option is not None:
                    key = str(option)
                    counts[key] = counts.get(key, 0) + 1
        buckets.append(
            {
                "field": field,
                "options": [{"value": key, "count": count} for key, count in sorted(counts.items())],
            }
        )
    return {
        "fields": buckets,
        "price": {
            "min": float(min((v.price for v in variants), default=0)),
            "max": float(max((v.price for v in variants), default=0)),
        },
    }
