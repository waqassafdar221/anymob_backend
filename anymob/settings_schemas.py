import re
from typing import Literal

from pydantic import EmailStr, Field, model_validator

from anymob.defaults import EVENTS, PERMISSIONS, ROLES
from anymob.schemas import Input, Money, Name, Rate


class General(Input):
    site_name: Name
    tagline: str = Field(max_length=200)
    logo: str = Field(max_length=1000)
    favicon: str = Field(max_length=1000)
    contact_email: EmailStr
    support_phone: str = Field(max_length=30)
    default_city: Name
    currency_format: Literal["symbol", "code", "compact"]


class Section(Input):
    id: Literal["popular_brands", "featured_phones", "featured_shops", "top_deals", "trust_strip"]
    enabled: bool


class Badge(Input):
    title: Name
    description: str = Field(max_length=250)


class Homepage(Input):
    hero_title: Name
    hero_subtitle: Name
    hero_description: str = Field(max_length=600)
    trust_badges: list[Name] = Field(min_length=3, max_length=3)
    trust_strip: list[Badge] = Field(max_length=4)
    featured_product_count: int = Field(ge=0, le=50)
    featured_store_count: int = Field(ge=0, le=30)
    pinned_store_ids: list[str] = Field(max_length=30)
    pinned_product_ids: list[str] = Field(max_length=50)
    sections: list[Section] = Field(min_length=5, max_length=5)

    @model_validator(mode="after")
    def ordered_sections(self):
        if len({s.id for s in self.sections}) != 5:
            raise ValueError("Include each homepage section once")
        if len(set(self.pinned_store_ids)) != len(self.pinned_store_ids) or len(
            set(self.pinned_product_ids)
        ) != len(self.pinned_product_ids):
            raise ValueError("Pins must be unique")
        if (
            len(self.pinned_store_ids) > self.featured_store_count
            or len(self.pinned_product_ids) > self.featured_product_count
        ):
            raise ValueError("Featured counts must accommodate pins")
        return self


class Courier(Input):
    name: Literal["TCS", "Leopards", "M&P", "Trax"]
    enabled: bool


class Commerce(Input):
    payment_methods: list[Literal["cod", "bank_transfer", "card", "jazzcash", "easypaisa"]] = Field(
        min_length=1, max_length=5
    )
    cod_maximum: Money
    minimum_order: Money
    auto_cancel_hours: int = Field(ge=1, le=720)
    auto_complete_days: int = Field(ge=1, le=90)
    return_window_days: int = Field(ge=0, le=365)
    dispute_escalation_hours: int = Field(ge=1, le=720)
    payout_schedule: Literal["weekly", "on_request"]
    minimum_payout: Money
    holding_period_days: int = Field(ge=0, le=365)
    couriers: list[Courier] = Field(min_length=4, max_length=4)
    maximum_delivery_fee: Money

    @model_validator(mode="after")
    def limits(self):
        if self.holding_period_days < self.return_window_days or self.minimum_payout <= 0:
            raise ValueError("Holding period must cover returns; payout minimum must be positive")
        if len({c.name for c in self.couriers}) != 4 or len(set(self.payment_methods)) != len(
            self.payment_methods
        ):
            raise ValueError("Duplicate couriers or payment methods")
        if "cod" in self.payment_methods and self.cod_maximum < self.minimum_order:
            raise ValueError("COD maximum is below the minimum order")
        return self


class Catalog(Input):
    auto_approve_verified_products: bool
    auto_approve_ads: bool
    review_under_days: int = Field(ge=0, le=365)
    ad_expiry_days: int = Field(ge=1, le=365)
    max_product_images: int = Field(ge=1, le=20)
    max_ad_images: int = Field(ge=1, le=20)
    banned_words: list[Name] = Field(max_length=100)
    price_warning_percent: int = Field(ge=1, le=99)


VARIABLES = {
    "site_name",
    "customer_name",
    "store_name",
    "order_number",
    "tracking_number",
    "pickup_code",
    "amount",
    "payout_id",
    "reference",
    "reason",
    "product_name",
    "reset_link",
    "code",
    "expires_minutes",
}


def validate_template(value):
    if any(key not in VARIABLES for key in re.findall(r"\{\{\s*([a-z_]+)\s*\}\}", value)):
        raise ValueError("Unknown template variable")
    if re.search(r"[{}]", re.sub(r"\{\{\s*[a-z_]+\s*\}\}", "", value)):
        raise ValueError("Variables must use {{variable_name}} syntax")


class Template(Input):
    email_subject: str = Field(min_length=1, max_length=300)
    email_body: str = Field(min_length=1, max_length=8000)
    sms: str = Field(min_length=1, max_length=1600)

    @model_validator(mode="after")
    def templates(self):
        for value in [self.email_subject, self.email_body, self.sms]:
            validate_template(value)
        return self


class Notifications(Input):
    templates: dict[str, Template]
    otp_sms: str = Field(min_length=1, max_length=1600)

    @model_validator(mode="after")
    def complete(self):
        if set(self.templates) != set(EVENTS):
            raise ValueError("Include every notification event")
        validate_template(self.otp_sms)
        if not re.search(r"\{\{\s*code\s*\}\}", self.otp_sms):
            raise ValueError("OTP text must contain the code variable")
        return self


class Team(Input):
    permissions: dict[str, dict[str, bool]]

    @model_validator(mode="after")
    def permissions_valid(self):
        if set(self.permissions) != set(ROLES) or any(
            set(v) != set(PERMISSIONS) for v in self.permissions.values()
        ):
            raise ValueError("Include all roles and permissions")
        if not all(self.permissions["super_admin"].values()):
            raise ValueError("Super Admin retains every permission")
        return self


class Maintenance(Input):
    enabled: bool
    message: str = Field(max_length=600)

    @model_validator(mode="after")
    def required_message(self):
        if self.enabled and not self.message:
            raise ValueError("Maintenance message is required")
        return self


class Revenue(Input):
    default_commission: Rate


SETTINGS = {
    "general": General,
    "homepage": Homepage,
    "commerce": Commerce,
    "catalog": Catalog,
    "notifications": Notifications,
    "team": Team,
    "maintenance": Maintenance,
    "revenue": Revenue,
}
