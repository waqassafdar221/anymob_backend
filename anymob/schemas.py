import re
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from anymob.common import camel


class Input(BaseModel):
    model_config = ConfigDict(
        alias_generator=camel,
        populate_by_name=True,
        extra="forbid",
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )


Name = Annotated[str, Field(min_length=1, max_length=160)]
Slug = Annotated[str, Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=220)]
Money = Annotated[Decimal, Field(ge=0, max_digits=16, decimal_places=2)]
Rate = Annotated[Decimal, Field(ge=0, le=1, max_digits=7, decimal_places=6)]
Method = Literal["cod", "bank_transfer", "card", "jazzcash", "easypaisa"]
Role = Literal["super_admin", "moderator", "support", "finance"]


def phone_number(value):
    value = re.sub(r"[\s()-]", "", value)
    if value.startswith("03"):
        value = "+92" + value[1:]
    if not re.fullmatch(r"\+923\d{9}", value):
        raise ValueError("Enter a Pakistani mobile number")
    return value


class Register(Input):
    full_name: Name
    email: EmailStr
    phone: str
    password: str = Field(min_length=10, max_length=128)
    city: Name = "Lahore"
    role: Literal["user", "vendor"] = "user"
    shop_name: Name | None = None
    shop_address: str = Field(default="", max_length=500)
    _phone = field_validator("phone")(phone_number)


class Login(Input):
    identifier: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class ChallengeRequest(Input):
    identifier: str = Field(min_length=1, max_length=254)
    purpose: Literal["login", "verify", "reset"] = "login"


class ChallengeVerify(ChallengeRequest):
    code: str = Field(min_length=6, max_length=128)
    password: str | None = Field(default=None, min_length=10, max_length=128)


class MFAVerify(Input):
    code: str = Field(pattern=r"^\d{6}$")
    challenge: str | None = None


class MFAResend(Input):
    challenge: str = Field(min_length=10, max_length=2000)


class AddressInput(Input):
    label: Name = "Home"
    full_name: Name
    phone: str
    line1: str = Field(min_length=1, max_length=300)
    line2: str = Field(default="", max_length=300)
    city: Name
    area: str = Field(default="", max_length=160)
    postal_code: str = Field(default="", max_length=20)
    is_default: bool = False
    _phone = field_validator("phone")(phone_number)


class FieldSchema(Input):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=80)
    label: Name
    type: Literal["text", "number", "select", "multiselect", "boolean"]
    required: bool = False
    options: list[Name] = Field(default_factory=list, max_length=100)
    filterable: bool = False
    group: str = Field(default="General", max_length=100)
    unit: str = Field(default="", max_length=30)
    hint: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def options_valid(self):
        if self.type in {"select", "multiselect"} and not self.options:
            raise ValueError("Select fields need options")
        if len(set(self.options)) != len(self.options):
            raise ValueError("Options must be unique")
        return self


class CategoryInput(Input):
    name: Name
    slug: Slug
    parent_id: str | None = None
    order: int = Field(default=0, ge=0)
    is_active: bool = True
    available_for: list[Literal["store", "classified"]] = Field(default=["store", "classified"], min_length=1)
    attributes_schema: list[FieldSchema] = Field(default_factory=list, max_length=100)
    variant_attributes: list[str] = Field(default_factory=list, max_length=10)
    icon: str = Field(default="", max_length=1000)
    unit: str = Field(default="items", max_length=50)
    blurb: str = Field(default="", max_length=500)
    seo_title: str = Field(default="", max_length=160)
    seo_description: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def fields_valid(self):
        names = [f.name for f in self.attributes_schema]
        if len(set(names)) != len(names) or len(set(self.variant_attributes)) != len(self.variant_attributes):
            raise ValueError("Field keys must be unique")
        return self


class BrandInput(Input):
    name: Name
    slug: Slug
    logo: str = Field(default="", max_length=1000)
    kind: Literal["phone", "accessory"] = "phone"
    category_ids: list[str] = Field(default_factory=list, max_length=100)
    is_active: bool = True
    is_popular: bool = False
    popular_order: int = Field(default=0, ge=0)


class VariantInput(Input):
    id: str | None = None
    sku: str = Field(min_length=1, max_length=100)
    attributes: dict[str, Any] = Field(default_factory=dict)
    price: Money
    compare_at_price: Money | None = None
    stock: int = Field(ge=0, le=1000000)
    low_stock_threshold: int = Field(default=5, ge=0)
    image: str | None = Field(default=None, max_length=1000)


class ProductInput(Input):
    name: str = Field(min_length=2, max_length=200)
    slug: Slug
    category_id: str
    brand_id: str | None = None
    description: str = Field(default="", max_length=20000)
    attributes: dict[str, Any] = Field(default_factory=dict)
    images: list[str] = Field(default_factory=list, max_length=20)
    variants: list[VariantInput] = Field(min_length=1, max_length=100)
    status: Literal["draft", "pending", "archived"] = "draft"
    condition: Literal["new", "used", "refurbished"] = "new"
    pta_status: Literal["approved", "non_pta", "not_applicable"] = "not_applicable"
    warranty: str = Field(default="", max_length=1000)
    section_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ClassifiedInput(Input):
    title: str = Field(min_length=2, max_length=200)
    slug: Slug
    category_id: str
    brand_id: str | None = None
    description: str = Field(min_length=10, max_length=20000)
    price: Money
    city: Name
    attributes: dict[str, Any] = Field(default_factory=dict)
    images: list[str] = Field(default_factory=list, max_length=20)
    status: Literal["draft", "pending", "sold"] = "pending"
    negotiable: bool = False
    price_type: Literal["fixed", "negotiable", "exchange_possible", "on_call"] | None = None
    condition: Literal["new", "used", "refurbished"] = "used"


class CartLine(Input):
    variant_id: str
    quantity: int = Field(ge=1, le=100)
    saved: bool = False


class ShopCheckout(Input):
    shop_id: str
    fulfillment: Literal["delivery", "pickup"]
    items: list[CartLine] = Field(min_length=1, max_length=100)


class Checkout(Input):
    shops: list[ShopCheckout] = Field(min_length=1, max_length=20)
    address_id: str | None = None
    payment_method: Method
    coupon_code: str | None = Field(default=None, max_length=40)


class Note(Input):
    note: str = Field(min_length=3, max_length=4000)


class OrderTransition(Input):
    status: Literal[
        "confirmed", "packed", "shipped", "delivered", "ready_for_pickup", "picked_up", "cancelled"
    ]
    note: str = Field(default="", max_length=4000)
    courier: str | None = Field(default=None, max_length=100)
    tracking_number: str | None = Field(default=None, max_length=200)
    pickup_code: str | None = Field(default=None, max_length=6)


class ReturnItem(Input):
    variant_id: str
    quantity: int = Field(ge=1, le=100)


class ReturnInput(Input):
    order_id: str
    reason: Literal["damaged", "defective", "wrong_item", "not_as_described", "other"]
    details: str = Field(min_length=10, max_length=4000)
    items: list[ReturnItem] = Field(min_length=1, max_length=100)
    images: list[str] = Field(default_factory=list, max_length=10)


class RefundInput(Note):
    amount: Money


class ReturnAction(Note):
    action: Literal[
        "approve",
        "reject",
        "received",
        "refund",
        "escalate",
        "resolve_buyer",
        "resolve_store",
        "request_information",
    ]
    restock: bool = False
    amount: Money | None = None
    # Who an admin asks for more information on a dispute.
    side: Literal["buyer", "store"] = "buyer"


class PayoutAction(Note):
    action: Literal["paid", "hold", "reject", "schedule"]
    reference: str | None = Field(default=None, min_length=3, max_length=160)


class PlanInput(Input):
    name: Name
    slug: Slug
    price: Money
    yearly_price: Money
    product_limit: int | None = Field(default=None, ge=0)
    featured_slots: int = Field(default=0, ge=0)
    commission_rate: Rate | None = None
    features: list[Name] = Field(default_factory=list, max_length=50)
    perks: dict[Literal["storePromo", "advancedAnalytics", "csvImport", "prioritySupport"], bool] = Field(
        default_factory=dict
    )
    is_active: bool = True
    is_popular: bool = False


class CouponInput(Input):
    code: str = Field(pattern=r"^[A-Z0-9_-]{2,40}$")
    kind: Literal["percent", "fixed"]
    value: Money
    max_discount: Money | None = None
    minimum_order: Money = Decimal(0)
    usage_limit: int | None = Field(default=None, ge=1)
    valid_from: datetime
    valid_to: datetime
    is_active: bool = True

    @model_validator(mode="after")
    def validate_dates(self):
        if self.valid_from.tzinfo is None or self.valid_to.tzinfo is None or self.valid_from >= self.valid_to:
            raise ValueError("Valid dates require time zones and end after start")
        if self.kind == "percent" and self.value > 100:
            raise ValueError("Percent cannot exceed 100")
        return self
