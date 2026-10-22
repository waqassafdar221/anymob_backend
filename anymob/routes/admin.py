import secrets
from datetime import datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import AwareDatetime, EmailStr, Field, ValidationError
from sqlalchemy import or_, select, update

from anymob.common import (
    DB,
    Page,
    advisory_lock,
    audit,
    client_ip,
    config,
    digest,
    fail,
    get,
    paginated,
    record,
)
from anymob.config import settings
from anymob.db import now
from anymob.defaults import DEFAULTS
from anymob.models import (
    AuditLog,
    AuthSession,
    Category,
    Classified,
    InternalNote,
    Invitation,
    Order,
    Outbox,
    Product,
    ReturnRequest,
    Setting,
    Shop,
    User,
)
from anymob.routes.orders import Key
from anymob.routes.returns import apply_return_action
from anymob.schemas import Input, Name, Note, OrderTransition, Rate, ReturnAction, Role
from anymob.security import hash_password, permission, throttle
from anymob.services.admin_orders import order_dossier, order_rows, return_view
from anymob.services.admin_reports import AUDIT_TYPES
from anymob.services.catalog import product_views, shop_view, visible_products, visible_shops
from anymob.services.commerce import TRANSITIONS, notify, transition_order
from anymob.settings_schemas import SETTINGS

router = APIRouter(tags=["Administration"])
Admin = Annotated[object, Depends(permission())]
UsersAdmin = Annotated[object, Depends(permission("manage_users"))]
StoresAdmin = Annotated[object, Depends(permission("verify_stores"))]
CatalogAdmin = Annotated[object, Depends(permission("moderate_catalog"))]
OrdersAdmin = Annotated[object, Depends(permission("manage_orders"))]
DisputesAdmin = Annotated[object, Depends(permission("resolve_disputes"))]
SettingsAdmin = Annotated[object, Depends(permission("edit_settings"))]
FinanceAdmin = Annotated[object, Depends(permission("manage_finance"))]


def super_admin(actor):
    if actor.admin_role != "super_admin":
        fail(403, "Super Admin access required")


@router.get("/settings/public")
async def public_settings(db: DB):
    commerce = await config(db, "commerce")
    return {
        **{key: await config(db, key) for key in ["general", "homepage", "maintenance"]},
        "commerce": {
            key: commerce[key]
            for key in [
                "paymentMethods",
                "codMaximum",
                "minimumOrder",
                "returnWindowDays",
                "maximumDeliveryFee",
            ]
        },
    }


@router.get("/homepage")
async def homepage(db: DB):
    cfg = await config(db, "homepage")
    shops = (
        await db.scalars(
            visible_shops()
            .where(or_(Shop.is_featured.is_(True), Shop.id.in_(cfg["pinnedStoreIds"])))
            .order_by(Shop.created_at.desc())
            .limit(100)
        )
    ).all()
    products = (await db.scalars(visible_products().order_by(Product.created_at.desc()).limit(100))).all()

    def pins(rows, ids):
        rank = {id: index for index, id in enumerate(ids)}
        return sorted(rows, key=lambda r: rank.get(r.id, len(ids)))

    return {
        "settings": cfg,
        "shops": [shop_view(s) for s in pins(shops, cfg["pinnedStoreIds"])[: cfg["featuredStoreCount"]]],
        "products": await product_views(
            db, pins(products, cfg["pinnedProductIds"])[: cfg["featuredProductCount"]]
        ),
    }


@router.get("/admin/settings")
async def all_settings(actor: SettingsAdmin, db: DB):
    return {
        key: {
            "value": await config(db, key),
            "version": (row.version if (row := await db.get(Setting, key)) else 0),
        }
        for key in DEFAULTS
    }


class SettingInput(Input):
    value: dict
    version: int = Field(ge=0)


@router.put("/admin/settings/{section}")
async def save_settings(section: str, data: SettingInput, actor: SettingsAdmin, request: Request, db: DB):
    if section not in SETTINGS:
        fail(404, "Settings section not found")
    if section == "team":
        super_admin(actor)
    await advisory_lock(db, "settings:" + section)
    try:
        parsed = SETTINGS[section].model_validate(data.value)
    except ValidationError as exc:
        fail(422, str(exc))
    row = await db.get(Setting, section)
    if data.version != (row.version if row else 0):
        fail(409, "Settings changed; reload before saving")
    before = record(row) if row else None
    if section == "homepage":
        for id in parsed.pinned_store_ids:
            if not await db.scalar(visible_shops().where(Shop.id == id)):
                fail(422, "Pinned stores must be public")
        for id in parsed.pinned_product_ids:
            if not await db.scalar(visible_products().where(Product.id == id)):
                fail(422, "Pinned products must be public")
    row = row or Setting(key=section, version=0)
    row.value, row.version = parsed.model_dump(mode="json", by_alias=True), row.version + 1
    # Monetary JSON values are decimal strings; consumers parse them with Decimal.
    from anymob.common import json_value

    row.value = json_value(parsed.model_dump(by_alias=True))
    db.add(row)
    await audit(db, actor, request, "settings.saved", row, before=before, note=section)
    return record(row)


@router.get("/admin/team")
async def team(actor: SettingsAdmin, db: DB):
    return {
        "admins": [record(u) for u in (await db.scalars(select(User).where(User.role == "admin"))).all()],
        "invitations": [
            record(i)
            for i in (
                await db.scalars(
                    select(Invitation).where(Invitation.accepted.is_(False), Invitation.expires_at > now())
                )
            ).all()
        ],
    }


class Invite(Input):
    email: EmailStr
    role: Role


@router.post("/admin/team/invite", status_code=201)
async def invite(data: Invite, actor: SettingsAdmin, request: Request, db: DB):
    super_admin(actor)
    token = secrets.token_urlsafe(40)
    row = Invitation(
        email=data.email.lower(),
        role=data.role,
        token_hash=digest(token),
        expires_at=now() + timedelta(days=7),
    )
    db.add(row)
    await db.flush()
    db.add(
        Outbox(
            kind="email",
            payload={
                "to": row.email,
                "subject": "Join the AnyMob admin team",
                "body": f"Accept your invitation: {settings().frontend_url}/admin/accept-invite?token={token}",
            },
            event_key="invite:" + row.id,
            available_at=now(),
        )
    )
    await audit(db, actor, request, "admin.invited", row)
    return record(row)


class AcceptInvite(Input):
    token: str = Field(min_length=20, max_length=100)
    full_name: Name
    phone: str
    password: str = Field(min_length=10, max_length=128)


@router.post("/auth/accept-invite")
async def accept_invite(data: AcceptInvite, request: Request, db: DB):
    await throttle("invite:" + client_ip(request), 5, 300)
    row = await db.scalar(
        select(Invitation).where(Invitation.token_hash == digest(data.token)).with_for_update()
    )
    if not row or row.accepted or row.expires_at <= now():
        fail(422, "Invitation is invalid or expired")
    if await db.scalar(select(User.id).where(User.email == row.email)):
        fail(409, "Existing accounts must be promoted by a Super Admin")
    from anymob.schemas import phone_number

    user = User(
        email=row.email,
        phone=phone_number(data.phone),
        full_name=data.full_name,
        password_hash=hash_password(data.password),
        role="admin",
        admin_role=row.role,
        is_verified=True,
    )
    db.add(user)
    row.accepted = True
    await db.flush()
    await audit(db, user, request, "admin.invitation_accepted", row)
    return {"id": user.id, "message": "Sign in and configure MFA"}


class AdminRoleInput(Input):
    role: Role


@router.put("/admin/team/{id}/role")
async def change_role(id: str, data: AdminRoleInput, actor: SettingsAdmin, request: Request, db: DB):
    super_admin(actor)
    if id == actor.id:
        fail(409, "You cannot change your own role")
    row = await get(db, User, id, lock=True)
    before = record(row)
    row.role, row.admin_role = "admin", data.role
    await db.execute(update(AuthSession).where(AuthSession.user_id == id).values(revoked_at=now()))
    await audit(db, actor, request, "admin.role_changed", row, before=before)
    return record(row)


@router.get("/admin/audit")
async def audit_log(
    actor: SettingsAdmin,
    db: DB,
    page: Page,
    admin_id: str | None = None,
    action: str | None = None,
    start: AwareDatetime | None = None,
    end: AwareDatetime | None = None,
):
    query = select(AuditLog)
    if admin_id:
        query = query.where(AuditLog.admin_id == admin_id)
    if action:
        query = query.where(AuditLog.action == action)
    if start:
        query = query.where(AuditLog.created_at >= start)
    if end:
        query = query.where(AuditLog.created_at <= end)
    result = await paginated(db, query.order_by(AuditLog.created_at.desc()), page, lambda row: row)
    return {**result, "items": await audit_rows(db, result["items"])}


async def audit_rows(db, logs) -> list[dict]:
    """Audit entries with the admin's name and a readable name for what they changed."""
    names = {
        u.id: u.full_name
        for u in (
            await db.scalars(select(User).where(User.id.in_({log.admin_id for log in logs if log.admin_id})))
        ).all()
    }

    def label(log):
        snapshot = log.after or log.before or {}
        for key in ("name", "fullName", "title", "orderNumber", "code", "key"):
            if snapshot.get(key):
                return str(snapshot[key])
        return log.target_id

    return [
        {
            **record(log),
            "actorId": log.admin_id or "system",
            "adminName": names.get(log.admin_id, "System"),
            "targetType": AUDIT_TYPES.get(log.target_type, log.target_type),
            "targetName": label(log),
            "summary": log.details or log.action.replace(".", " ").replace("_", " "),
        }
        for log in logs
    ]


@router.get("/admin/users")
async def users(
    actor: UsersAdmin,
    db: DB,
    page: Page,
    search: str = "",
    status: str | None = None,
    role: str | None = None,
):
    query = select(User)
    if search:
        query = query.where(
            or_(
                User.full_name.ilike(f"%{search[:160]}%"),
                User.email.ilike(f"%{search[:160]}%"),
                User.phone.ilike(f"%{search[:30]}%"),
            )
        )
    if status:
        query = query.where(User.status == status)
    if role:
        query = query.where(User.role == role)
    result = await paginated(db, query.order_by(User.created_at.desc()), page)
    from anymob.services.admin_users import user_rows

    rows = (await db.scalars(select(User).where(User.id.in_([r["id"] for r in result["items"]])))).all()
    by_id = {row["id"]: row for row in await user_rows(db, rows)}
    result["items"] = [by_id[r["id"]] for r in result["items"]]
    return result


@router.get("/admin/users/{id}")
async def user_dossier(id: str, actor: UsersAdmin, db: DB):
    from anymob.services.admin_users import user_detail

    return await user_detail(db, await get(db, User, id))


class AdminUserInput(Input):
    full_name: Name
    email: EmailStr
    phone: str
    city: Name
    role: Literal["user", "vendor"]
    password: str | None = Field(default=None, min_length=10, max_length=128)


async def contact_taken(db, email, phone, exclude=None):
    query = select(User.id).where(or_(User.email == email, User.phone == phone))
    if exclude:
        query = query.where(User.id != exclude)
    return await db.scalar(query.limit(1))


@router.post("/admin/users", status_code=201)
async def create_user(data: AdminUserInput, actor: UsersAdmin, request: Request, db: DB):
    from anymob.schemas import phone_number
    from anymob.services.admin_users import user_rows

    if not data.password:
        fail(422, "Set an initial password")
    phone, email = phone_number(data.phone), data.email.lower()
    if await contact_taken(db, email, phone):
        fail(409, "Email or phone already belongs to another user")
    user = User(
        full_name=data.full_name,
        email=email,
        phone=phone,
        city=data.city,
        role=data.role,
        password_hash=hash_password(data.password),
    )
    db.add(user)
    await db.flush()
    if data.role == "vendor":
        # A vendor account always has a store, which starts unverified.
        db.add(
            Shop(
                owner_id=user.id,
                name=f"{data.full_name}'s store",
                slug=f"store-{user.id[:8]}",
                city=data.city,
                data={
                    "phone": phone,
                    "paymentMethods": ["cod"],
                    "pickup": {"enabled": True, "holdHours": 48},
                    "storeSections": [],
                },
            )
        )
    await audit(db, actor, request, "user.created", user, note=f"Created {data.role} account")
    return (await user_rows(db, [user]))[0]


@router.put("/admin/users/{id}")
async def edit_user(id: str, data: AdminUserInput, actor: UsersAdmin, request: Request, db: DB):
    from anymob.schemas import phone_number
    from anymob.services.admin_users import user_rows

    user = await get(db, User, id, lock=True)
    if user.role == "admin":
        fail(403, "Administrator accounts are managed under Settings → Team")
    if await db.scalar(select(Shop.id).where(Shop.owner_id == id)) and data.role != "vendor":
        fail(422, "A store owner must keep the vendor role")
    phone, email = phone_number(data.phone), data.email.lower()
    if await contact_taken(db, email, phone, exclude=id):
        fail(409, "Email or phone already belongs to another user")
    before = record(user)
    if email != user.email or phone != user.phone:
        user.is_verified = False
    user.full_name, user.email, user.phone, user.city, user.role = (
        data.full_name,
        email,
        phone,
        data.city,
        data.role,
    )
    if data.password:
        user.password_hash = hash_password(data.password)
        await db.execute(update(AuthSession).where(AuthSession.user_id == id).values(revoked_at=now()))
    await audit(db, actor, request, "user.updated", user, before=before)
    return (await user_rows(db, [user]))[0]


@router.post("/admin/users/{id}/reset-password")
async def reset_user_password(id: str, actor: UsersAdmin, request: Request, db: DB):
    """Signs the member out everywhere and emails a reset code; administrators never see passwords."""
    from anymob.common import digest as hash_digest
    from anymob.models import Challenge

    user = await get(db, User, id, lock=True)
    if user.role == "admin":
        fail(403, "Administrator accounts are protected from this action")
    code = str(secrets.randbelow(1000000)).zfill(6)
    db.add(
        Challenge(
            identifier=user.email,
            purpose="reset",
            token_hash=hash_digest(code + settings().secret_key),
            expires_at=now() + timedelta(hours=24),
        )
    )
    db.add(
        Outbox(
            kind="email",
            payload={
                "to": user.email,
                "subject": "Reset your AnyMob password",
                "body": f"An administrator started a password reset for your account. Your code is {code}. "
                f"Enter it at {settings().frontend_url}/forgot-password within 24 hours.",
            },
            event_key="admin-reset:" + secrets.token_hex(8),
            available_at=now(),
        )
    )
    await db.execute(update(AuthSession).where(AuthSession.user_id == id).values(revoked_at=now()))
    await audit(db, actor, request, "user.password_reset", user, note="Reset code emailed")
    return {"sent": True}


class UserStatus(Note):
    status: Literal["active", "suspended", "banned"]
    suspended_until: datetime | None = None


@router.put("/admin/users/{id}/status")
async def user_status(id: str, data: UserStatus, actor: UsersAdmin, request: Request, db: DB):
    row = await get(db, User, id, lock=True)
    if id == actor.id:
        fail(409, "You cannot deactivate your own account")
    if row.role == "admin":
        super_admin(actor)
    if data.suspended_until and (data.suspended_until.tzinfo is None or data.suspended_until <= now()):
        fail(422, "Suspension expiry must be a future date with timezone")
    before = record(row)
    row.status, row.suspended_until = (
        data.status,
        data.suspended_until if data.status == "suspended" else None,
    )
    if data.status != "active":
        await db.execute(update(AuthSession).where(AuthSession.user_id == id).values(revoked_at=now()))
    await audit(db, actor, request, "user.status_changed", row, before=before, note=data.note)
    return record(row)


@router.get("/admin/vendors")
async def vendors(actor: StoresAdmin, db: DB, page: Page, status: str | None = None, search: str = ""):
    query = select(Shop)
    if status:
        query = query.where(Shop.verification_status == status)
    if search:
        query = query.where(Shop.name.ilike(f"%{search[:160]}%"))
    result = await paginated(db, query.order_by(Shop.created_at.desc()), page, lambda row: row)
    from anymob.services.admin_vendors import vendor_rows

    result["items"] = await vendor_rows(db, result["items"])
    return result


@router.get("/admin/vendors/{id}")
async def vendor_dossier(id: str, actor: StoresAdmin, db: DB):
    from anymob.services.admin_vendors import vendor_detail

    return await vendor_detail(db, await get(db, Shop, id))


class AdminNotes(Input):
    notes: str = Field(default="", max_length=8000)


@router.put("/admin/vendors/{id}/notes")
async def vendor_notes(id: str, data: AdminNotes, actor: StoresAdmin, db: DB):
    shop = await get(db, Shop, id, lock=True)
    shop.data = {**shop.data, "adminNotes": data.notes}
    return {"notes": data.notes}


class DocumentReview(Input):
    status: Literal["good", "problem"]


@router.post("/admin/vendors/{id}/documents/{kind}")
async def review_document(
    id: str,
    kind: Literal["cnicFront", "cnicBack", "shopPhoto", "businessProof"],
    data: DocumentReview,
    actor: StoresAdmin,
    request: Request,
    db: DB,
):
    shop = await get(db, Shop, id, lock=True)
    if kind not in shop.data.get("verificationDocuments", {}):
        fail(404, "No uploaded document")
    before = record(shop)
    shop.data = {**shop.data, "documentReviews": {**shop.data.get("documentReviews", {}), kind: data.status}}
    await audit(
        db,
        actor,
        request,
        "store.document_reviewed",
        shop,
        before=before,
        note=f"{kind} marked {data.status}",
    )
    return {"saved": True}


@router.post("/admin/vendors/{id}/request-info")
async def request_vendor_info(id: str, data: Note, actor: StoresAdmin, request: Request, db: DB):
    shop = await get(db, Shop, id)
    await notify(db, shop.owner_id, "More verification information needed", data.note, "/vendor/verification")
    await audit(db, actor, request, "store.info_requested", shop, note=data.note)
    return {"sent": True}


class VerificationAction(Note):
    action: Literal["approve", "reject", "suspend", "reactivate", "feature", "unfeature", "delete"]


@router.post("/admin/vendors/{id}/action")
async def verify_vendor(id: str, data: VerificationAction, actor: StoresAdmin, request: Request, db: DB):
    shop = await get(db, Shop, id, lock=True)
    before = record(shop)
    if data.action in {"approve", "reject"}:
        if shop.verification_status != "pending":
            fail(409, "Store is not awaiting verification")
        shop.verification_status = "verified" if data.action == "approve" else "rejected"
        # The vendor's verification page shows why a submission was turned down.
        shop.data = {**shop.data, "verificationNote": data.note if data.action == "reject" else ""}
    elif data.action in {"suspend", "reactivate"}:
        shop.is_suspended = data.action == "suspend"
    elif data.action == "delete":
        # Soft delete: the store leaves public view; orders, payouts and audit history remain.
        shop.is_deleted, shop.is_featured = True, False
    else:
        shop.is_featured = data.action == "feature"
    await audit(db, actor, request, "store." + data.action, shop, before=before, note=data.note)
    await notify(db, shop.owner_id, "Store review updated", data.note, "/vendor/store-settings")
    return record(shop)


class Commission(Input):
    rate: Rate | None


@router.put("/admin/commissions/{kind}/{id}")
async def commission(
    kind: Literal["shop", "category"],
    id: str,
    data: Commission,
    actor: FinanceAdmin,
    request: Request,
    db: DB,
):
    row = await get(db, Shop if kind == "shop" else Category, id, lock=True)
    before = record(row)
    row.commission_override = data.rate
    await audit(db, actor, request, "commission.updated", row, before=before)
    return record(row)


@router.get("/admin/catalog/{kind}")
async def catalog_queue(
    kind: Literal["products", "ads"],
    actor: CatalogAdmin,
    db: DB,
    page: Page,
    status: str = "pending",
    search: str = "",
):
    from anymob.services.catalog import ad_view

    model = Product if kind == "products" else Classified
    query = select(model) if status == "all" else select(model).where(model.status == status)
    if search:
        query = query.where((model.name if model is Product else model.title).ilike(f"%{search[:160]}%"))
    result = await paginated(db, query.order_by(model.created_at.desc()), page, lambda row: row)
    rows = result["items"]
    shops = (
        {
            s.id: s
            for s in (await db.scalars(select(Shop).where(Shop.id.in_({r.shop_id for r in rows})))).all()
        }
        if model is Product
        else {}
    )
    owner_ids = {shops[r.shop_id].owner_id if model is Product else r.seller_id for r in rows}
    owners = {u.id: u for u in (await db.scalars(select(User).where(User.id.in_(owner_ids)))).all()}
    views = await product_views(db, rows) if model is Product else [ad_view(r) for r in rows]
    items = []
    for row, view in zip(rows, views, strict=True):
        shop = shops.get(row.shop_id) if model is Product else None
        owner = owners.get(shop.owner_id if shop else row.seller_id)
        items.append(
            {
                **view,
                # Moderators see who is behind a listing and how established they are.
                "shop": {
                    "id": shop.id,
                    "name": shop.name,
                    "city": shop.city,
                    "verificationStatus": shop.verification_status,
                    "isSuspended": shop.is_suspended,
                    "isDeleted": shop.is_deleted,
                }
                if shop
                else None,
                "owner": {"id": owner.id, "fullName": owner.full_name, "createdAt": owner.created_at}
                if owner
                else None,
            }
        )
    result["items"] = items
    return result


class ModerationAction(Note):
    status: Literal["active", "rejected", "archived", "pending"]


class FeatureInput(Input):
    featured: bool


@router.post("/admin/catalog/{kind}/{id}/feature")
async def feature_listing(
    kind: Literal["products", "ads"],
    id: str,
    data: FeatureInput,
    actor: CatalogAdmin,
    request: Request,
    db: DB,
):
    row = await get(db, Product if kind == "products" else Classified, id, lock=True)
    before = record(row)
    row.data = {**row.data, "isFeatured": data.featured}
    await audit(
        db, actor, request, "catalog.featured" if data.featured else "catalog.unfeatured", row, before=before
    )
    return {"id": id, "isFeatured": data.featured}


@router.post("/admin/catalog/{kind}/{id}")
async def moderate(
    kind: Literal["products", "ads"],
    id: str,
    data: ModerationAction,
    actor: CatalogAdmin,
    request: Request,
    db: DB,
):
    row = await get(db, Product if kind == "products" else Classified, id, lock=True)
    before = record(row)
    if data.status == "active" and row.status not in {"pending", "rejected"}:
        fail(409, "Only submitted listings can be approved")
    row.status = data.status
    row.data = {**row.data, "moderationNote": data.note}
    await audit(db, actor, request, "catalog.moderated", row, before=before, note=data.note)
    if data.status in {"active", "rejected", "archived"}:
        # The seller hears about the decision on their listing.
        owner_id = (await db.get(Shop, row.shop_id)).owner_id if kind == "products" else row.seller_id
        title = row.name if kind == "products" else row.title
        outcome = {"active": "approved", "rejected": "rejected", "archived": "removed"}[data.status]
        await notify(
            db,
            owner_id,
            f"Listing {outcome}: {title}"[:200],
            data.note,
            "/vendor/products" if kind == "products" else "/my-ads",
        )
    return record(row)


@router.get("/admin/orders")
async def orders(actor: OrdersAdmin, db: DB, page: Page, status: str | None = None, search: str = ""):
    query = select(Order)
    if status:
        query = query.where(Order.status == status)
    if search:
        query = query.where(Order.order_number.ilike(f"%{search[:100]}%"))
    result = await paginated(db, query.order_by(Order.created_at.desc()), page, lambda row: row)
    return {**result, "items": await order_rows(db, result["items"])}


@router.get("/admin/orders/{id}")
async def order_detail(id: str, actor: OrdersAdmin, db: DB):
    return await order_dossier(db, await get(db, Order, id))


def forward_path(start: str, target: str) -> list[str] | None:
    """The shortest run of normal transitions from one status to another, if there is one."""
    queue = [[start]]
    while queue:
        path = queue.pop(0)
        for step in TRANSITIONS.get(path[-1], []):
            if step == target:
                return [*path[1:], step]
            queue.append([*path, step])
    return None


@router.post("/admin/orders/{id}/transition")
async def order_transition(id: str, data: OrderTransition, actor: OrdersAdmin, request: Request, db: DB):
    row = await get(db, Order, id)
    await get(db, Shop, row.shop_id, lock=True)
    row = await get(db, Order, id, lock=True)
    if len(data.note.strip()) < 3:
        fail(422, "An admin note is required")
    before = record(row)
    if data.status == "cancelled":
        await transition_order(db, row, data, actor.id)
    else:
        # An override may skip intermediate steps but only moves forward; each step keeps its side effects.
        path = forward_path(row.status, data.status)
        if not path:
            fail(409, "This status change is not supported. Use cancellation or the return workflow.")
        previous = row.status
        for step in path:
            note = f"Status override ({previous} → {data.status}): {data.note.strip()}"
            await transition_order(
                db, row, data.model_copy(update={"status": step, "note": note}), actor.id, verify_pickup=False
            )
        shop = await db.get(Shop, row.shop_id)
        await notify(
            db,
            shop.owner_id,
            row.order_number,
            f"Status changed to {data.status}: {data.note}",
            "/vendor/orders/" + row.id,
        )
    await audit(db, actor, request, "order.admin_transition", row, before=before, note=data.note)
    return await order_dossier(db, row)


class OrderNote(Note):
    internal: bool = False


@router.post("/admin/orders/{id}/notes", status_code=201)
async def order_note(id: str, data: OrderNote, actor: OrdersAdmin, request: Request, db: DB):
    row = await get(db, Order, id, lock=True)
    note = data.note.strip()
    if data.internal:
        db.add(InternalNote(author_id=actor.id, target_type="order", target_id=row.id, body=note))
    else:
        row.timeline = [
            *row.timeline,
            {"status": row.status, "at": now().isoformat(), "note": note, "actor": actor.id},
        ]
        shop = await db.get(Shop, row.shop_id)
        await notify(db, row.buyer_id, row.order_number, note, "/orders/" + row.id)
        await notify(db, shop.owner_id, row.order_number, note, "/vendor/orders/" + row.id)
    await audit(db, actor, request, "order.noted", row, note="Internal note added" if data.internal else note)
    await db.flush()
    return await order_dossier(db, row)


@router.get("/admin/returns")
async def all_returns(actor: DisputesAdmin, db: DB, page: Page, disputed: bool = False):
    query = select(ReturnRequest)
    if disputed:
        query = query.where(ReturnRequest.disputed.is_(True))
    result = await paginated(db, query.order_by(ReturnRequest.created_at.desc()), page, lambda row: row)
    orders = {
        o["id"]: o
        for o in await order_rows(
            db,
            (
                await db.scalars(select(Order).where(Order.id.in_({r.order_id for r in result["items"]})))
            ).all(),
        )
    }
    return {**result, "items": [{**return_view(r), "order": orders.get(r.order_id)} for r in result["items"]]}


@router.post("/admin/returns/{id}/action")
async def resolve_return(
    id: str, data: ReturnAction, key: Key, actor: DisputesAdmin, request: Request, db: DB
):
    row = await get(db, ReturnRequest, id)
    await get(db, Shop, row.shop_id, lock=True)
    return await apply_return_action(db, row, data, actor, request, key)


class DemoReset(Input):
    confirmation: Literal["RESET DEMO DATA"]


@router.post("/admin/demo/reset")
async def reset_demo(data: DemoReset, actor: SettingsAdmin, db: DB):
    """Explicitly opt-in development databases only; browser cart clearing is a client concern."""

    from sqlalchemy import text

    from anymob.db import Base, engine

    super_admin(actor)
    if (
        settings().environment != "development"
        or not settings().demo_reset_enabled
        or not engine.url.database.endswith("_demo")
    ):
        fail(
            403,
            "Demo reset requires development mode, DEMO_RESET_ENABLED=true, and a database ending in _demo",
        )
    await advisory_lock(db, "reset-demo")
    # Preserve only the bootstrap admin identity; sessions, carts and marketplace records are reset.
    admin_values = {column.key: getattr(actor, column.key) for column in User.__table__.columns}
    names = ", ".join('"' + table.name + '"' for table in Base.metadata.sorted_tables)
    await db.execute(text("TRUNCATE " + names + " RESTART IDENTITY CASCADE"))
    db.expunge_all()
    db.add(User(**admin_values))
    from anymob.cli import seed_into

    await seed_into(db)
    await db.flush()
    db.add(
        AuditLog(
            admin_id=admin_values["id"],
            action="demo.reset",
            target_type="settings",
            target_id="demo",
            details="Development data reset; bootstrap admin retained, all sessions revoked",
        )
    )
    return {"reset": True, "signInRequired": True, "clearBrowserCart": True}
