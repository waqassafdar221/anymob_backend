from typing import Literal

from fastapi import APIRouter, Request
from pydantic import Field, model_validator
from sqlalchemy import delete, select, update

from anymob.common import DB, advisory_lock, audit, config, fail, get, owned_shop, record
from anymob.db import now
from anymob.models import Address, Preference, Shop, Upload, User
from anymob.schemas import AddressInput, Input, Method, Money, Name
from anymob.security import Actor, encrypt
from anymob.services.catalog import shop_view, validate_media

router = APIRouter(tags=["Accounts and stores"])


@router.get("/me/addresses")
async def addresses(actor: Actor, db: DB):
    return [
        {**r.data, **record(r, exclude={"data"})}
        for r in (
            await db.scalars(
                select(Address)
                .where(Address.user_id == actor.id)
                .order_by(Address.is_default.desc(), Address.created_at)
            )
        ).all()
    ]


async def write_address(db, actor, data, row=None):
    await advisory_lock(db, "address:" + actor.id)
    if data.is_default:
        await db.execute(update(Address).where(Address.user_id == actor.id).values(is_default=False))
    row = row or Address(user_id=actor.id)
    row.is_default = data.is_default
    row.data = data.model_dump(by_alias=True, exclude={"is_default"})
    db.add(row)
    await db.flush()
    return {**row.data, **record(row, exclude={"data"})}


@router.post("/me/addresses", status_code=201)
async def add_address(data: AddressInput, actor: Actor, db: DB):
    return await write_address(db, actor, data)


@router.put("/me/addresses/{id}")
async def edit_address(id: str, data: AddressInput, actor: Actor, db: DB):
    row = await get(db, Address, id)
    if row.user_id != actor.id:
        fail(404, "Address not found")
    return await write_address(db, actor, data, row)


@router.delete("/me/addresses/{id}")
async def delete_address(id: str, actor: Actor, db: DB):
    row = await get(db, Address, id)
    if row.user_id != actor.id:
        fail(404, "Address not found")
    await db.delete(row)
    return {"id": id}


class Profile(Input):
    full_name: Name
    city: Name
    avatar: str = Field(default="", max_length=1000)


@router.patch("/me")
async def profile(data: Profile, actor: Actor, db: DB):
    if data.avatar:
        await validate_media(db, actor.id, [data.avatar])
    actor.full_name, actor.city, actor.avatar = data.full_name, data.city, data.avatar
    return record(actor)


class Delivery(Input):
    enabled: bool = False
    cities: list[Name] = Field(default_factory=list, max_length=100)
    all_pakistan: bool = False
    fee: Money = 0
    other_city_fee: Money = 0
    free_above_amount: Money | None = None
    estimated_days: dict[str, str] = Field(default={"ownCity": "1–2 days", "elsewhere": "3–5 days"})
    default_courier: str = Field(default="TCS", max_length=100)


class Pickup(Input):
    enabled: bool = True
    hold_hours: int = Field(default=48, ge=1, le=720)
    instructions: str = Field(default="", max_length=2000)


class Policies(Input):
    return_days: int = Field(default=7, ge=0, le=365)
    return_text: str = Field(default="", max_length=4000)
    warranty_text: str = Field(default="", max_length=4000)
    cancellation_text: str = Field(default="", max_length=4000)


class ShopSettings(Input):
    name: Name
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=180)
    city: Name
    address: str = Field(min_length=1, max_length=500)
    phone: str = Field(default="", max_length=30)
    whatsapp: str = Field(default="", max_length=30)
    email: str = Field(default="", max_length=254)
    description: str = Field(default="", max_length=10000)
    tagline: str = Field(default="", max_length=200)
    logo: str = Field(default="", max_length=1000)
    cover_image: str = Field(default="", max_length=1000)
    show_phone_publicly: bool = True
    business_hours: dict[str, dict] = Field(default_factory=dict)
    social_links: dict[str, str] = Field(default_factory=dict)
    specialties: list[Name] = Field(default_factory=list, max_length=30)
    store_sections: list[dict] = Field(default_factory=list, max_length=50)
    promo: dict | None = None
    vacation: dict | None = None
    is_on_vacation: bool = False
    delivery: Delivery = Field(default_factory=Delivery)
    pickup: Pickup = Field(default_factory=Pickup)
    policies: Policies = Field(default_factory=Policies)
    payment_methods: list[Method] = Field(default=["cod"], min_length=1, max_length=5)
    cod_maximum: Money | None = None


@router.get("/vendor/shop")
async def vendor_shop(actor: Actor, db: DB):
    shop = await owned_shop(db, actor)
    return {
        "shop": shop_view(shop),
        "payoutDestination": shop.payout_mask,
        "bankTransferReady": bool(shop.bank_account),
        "verification": {
            "status": shop.verification_status,
            "documents": shop.data.get("verificationDocuments", {}),
            "note": shop.data.get("verificationNote", ""),
        },
    }


@router.put("/vendor/shop")
async def update_shop(data: ShopSettings, actor: Actor, db: DB):
    shop = await owned_shop(db, actor, lock=True)
    rules = await config(db, "commerce")
    if max(data.delivery.fee, data.delivery.other_city_fee) > rules["maximumDeliveryFee"]:
        fail(422, "Delivery fee exceeds the platform maximum")
    if not data.delivery.enabled and not data.pickup.enabled:
        fail(422, "Enable delivery or store pickup")
    # Only newly chosen images must be the vendor's own uploads; unchanged ones are kept as they are.
    current = {shop.data.get("logo"), shop.data.get("coverImage")}
    await validate_media(db, actor.id, [v for v in [data.logo, data.cover_image] if v and v not in current])
    for key in ["name", "slug", "city", "is_on_vacation"]:
        setattr(shop, key, getattr(data, key))
    shop.data = {
        **shop.data,
        **data.model_dump(mode="json", by_alias=True, exclude={"name", "slug", "city", "is_on_vacation"}),
    }
    return shop_view(shop)


class PayoutAccount(Input):
    method: Literal["bank_transfer", "jazzcash", "easypaisa"]
    title: Name
    bank_name: str = Field(default="", max_length=120)
    iban: str = Field(default="", max_length=34)
    wallet: str = Field(default="", max_length=20)

    @model_validator(mode="after")
    def valid_account(self):
        import re

        from anymob.schemas import phone_number

        if self.method == "bank_transfer":
            self.iban = self.iban.replace(" ", "").upper()
            if not self.bank_name or not re.fullmatch(r"PK\d{2}[A-Z]{4}[A-Z0-9]{16}", self.iban):
                raise ValueError("A bank name and Pakistani IBAN are required")
            expanded = "".join(str(ord(c) - 55) if c.isalpha() else c for c in self.iban[4:] + self.iban[:4])
            if int(expanded) % 97 != 1:
                raise ValueError("IBAN checksum is invalid")
        else:
            self.wallet = phone_number(self.wallet)
        return self


@router.put("/vendor/payout-account")
async def payout_account(data: PayoutAccount, actor: Actor, db: DB, request: Request):
    shop = await owned_shop(db, actor, lock=True)
    shop.payout_account = encrypt(data.model_dump_json())
    shop.payout_mask = f"{data.method}: •••• {(data.iban or data.wallet)[-4:]}"
    await audit(
        db,
        actor,
        request,
        "vendor.payout_account_changed",
        shop,
        note="Payout destination changed; full account omitted from audit",
    )
    return {"destination": shop.payout_mask}


class Verification(Input):
    cnic_front: str
    cnic_back: str
    shop_photo: str
    business_proof: str


@router.post("/vendor/verification")
async def submit_verification(data: Verification, actor: Actor, db: DB):
    shop = await owned_shop(db, actor, lock=True)
    if shop.verification_status == "pending":
        fail(409, "Verification is already under review")
    for id in data.model_dump().values():
        media = await get(db, Upload, id)
        if media.owner_id != actor.id or media.purpose != "verification" or media.status != "ready":
            fail(422, "Verification documents must be completed private uploads belonging to you")
    shop.data = {
        **shop.data,
        "verificationDocuments": data.model_dump(by_alias=True),
        "verificationSubmittedAt": now().isoformat(),
        "verificationNote": "",
    }
    shop.verification_status = "pending"
    return {"status": "pending"}


@router.get("/me/preferences")
async def preferences(actor: Actor, db: DB):
    return [
        record(r) for r in (await db.scalars(select(Preference).where(Preference.user_id == actor.id))).all()
    ]


@router.put("/me/preferences/{kind}/{target_id}")
async def set_preference(
    kind: Literal["favorite_product", "favorite_ad", "follow_shop", "block_user"],
    target_id: str,
    actor: Actor,
    db: DB,
):
    from anymob.models import Classified, Product

    model = {"favorite_product": Product, "favorite_ad": Classified, "follow_shop": Shop, "block_user": User}[
        kind
    ]
    await get(db, model, target_id)
    if kind == "block_user" and target_id == actor.id:
        fail(422, "You cannot block yourself")
    await advisory_lock(db, f"preference:{actor.id}:{kind}:{target_id}")
    if not await db.scalar(
        select(Preference.id).where(
            Preference.user_id == actor.id, Preference.kind == kind, Preference.target_id == target_id
        )
    ):
        db.add(Preference(user_id=actor.id, kind=kind, target_id=target_id))
    return {"ok": True}


@router.delete("/me/preferences/{kind}/{target_id}")
async def remove_preference(kind: str, target_id: str, actor: Actor, db: DB):
    await db.execute(
        delete(Preference).where(
            Preference.user_id == actor.id, Preference.kind == kind, Preference.target_id == target_id
        )
    )
    return {"ok": True}


@router.put("/vendor/bank-transfer-account")
async def bank_transfer_account(data: PayoutAccount, actor: Actor, db: DB, request: Request):
    if data.method != "bank_transfer":
        fail(422, "A bank account is required for incoming transfers")
    shop = await owned_shop(db, actor, lock=True)
    shop.bank_account = encrypt(data.model_dump_json(by_alias=True))
    await audit(
        db, actor, request, "vendor.bank_account_changed", shop, note="Incoming bank transfer account changed"
    )
    return {"destination": "•••• " + data.iban[-4:]}


@router.get("/users/{id}/profile")
async def public_profile(id: str, db: DB):
    user = await get(db, User, id)
    if user.status != "active":
        fail(404, "Profile not found")
    return {
        "id": user.id,
        "fullName": user.full_name,
        "avatar": user.avatar,
        "city": user.city,
        "isVerified": user.is_verified,
        "createdAt": user.created_at,
    }
