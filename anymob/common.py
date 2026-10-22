import hashlib
import hmac
import json
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated

from fastapi import Depends, HTTPException, Query, Request
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from anymob.config import settings
from anymob.db import session
from anymob.models import AuditLog, Setting, Shop

DB = Annotated[AsyncSession, Depends(session)]


def fail(status: int, message: str):
    raise HTTPException(status, message)


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def camel(value: str) -> str:
    first, *rest = value.split("_")
    return first + "".join(word.title() for word in rest)


def json_value(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    return value


PRIVATE = {
    "password_hash",
    "mfa_secret",
    "last_totp_step",
    "token_hash",
    "family_id",
    "payout_account",
    "bank_account",
    "account_snapshot",
    "pickup_code_hash",
    "request_hash",
}


def record(obj, *, exclude=()):
    return {
        camel(column.key): json_value(getattr(obj, column.key))
        for column in obj.__table__.columns
        if column.key not in PRIVATE and column.key not in exclude
    }


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def fingerprint(value) -> str:
    return digest(json.dumps(json_value(value), sort_keys=True, separators=(",", ":")))


async def advisory_lock(db, key: str):
    # Transaction-scoped and shared by API workers; serializes retries before an insert exists.
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key})


async def get(db, model, id: str, *, lock=False):
    query = select(model).where(model.id == id)
    obj = await db.scalar(
        query.with_for_update().execution_options(populate_existing=True) if lock else query
    )
    if obj is None:
        fail(404, f"{model.__name__} not found")
    return obj


async def owned_shop(db, actor, shop_id: str | None = None, *, lock=False):
    query = select(Shop).where(Shop.owner_id == actor.id, Shop.is_deleted.is_(False))
    if shop_id:
        query = query.where(Shop.id == shop_id)
    shop = await db.scalar(
        query.with_for_update().execution_options(populate_existing=True) if lock else query
    )
    if not shop or actor.role != "vendor":
        fail(403, "Vendor access to this store is required")
    if shop.is_suspended:
        fail(403, "This store is suspended")
    return shop


async def config(db, key: str):
    from anymob.defaults import DEFAULTS

    row = await db.get(Setting, key)
    return row.value if row else DEFAULTS.get(key, {})


async def audit(db, actor, request: Request | None, action: str, obj, *, before=None, note=""):
    db.add(
        AuditLog(
            admin_id=actor.id if actor else None,
            action=action,
            target_type=obj.__tablename__,
            target_id=str(getattr(obj, "id", getattr(obj, "key", ""))),
            before=before,
            after=record(obj),
            details=note,
            ip=client_ip(request) if request else "",
        )
    )


def client_ip(request: Request) -> str:
    secret = settings().proxy_secret
    supplied = request.headers.get("x-anymob-proxy-key", "")
    if secret and supplied and hmac.compare_digest(supplied.encode(), secret.encode()):
        proxied = request.headers.get("x-anymob-client-ip", "").strip()
        if proxied:
            return proxied
    header = settings().trusted_ip_header
    forwarded = request.headers.get(header, "") if header else ""
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


class Pagination:
    def __init__(
        self, page: int = Query(1, ge=1), page_size: int = Query(20, alias="pageSize", ge=1, le=100)
    ):
        self.page = page
        self.size = page_size


Page = Annotated[Pagination, Depends()]


async def paginated(db, query, page, serializer=record):
    total = await db.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    rows = (await db.scalars(query.offset((page.page - 1) * page.size).limit(page.size))).all()
    return {
        "items": [serializer(row) for row in rows],
        "total": total,
        "page": page.page,
        "pageSize": page.size,
        "pageCount": max(1, (total + page.size - 1) // page.size),
        "hasMore": page.page * page.size < total,
    }
