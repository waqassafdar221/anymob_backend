"""Relational financial/ownership boundaries; JSONB for versioned catalog and UI settings."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from anymob.db import Base, Record

MONEY = Numeric(16, 2)
RATE = Numeric(7, 6)


class User(Record, Base):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(String(254), unique=True)
    phone: Mapped[str] = mapped_column(String(20), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    full_name: Mapped[str] = mapped_column(String(160))
    role: Mapped[str] = mapped_column(String(20), default="user")
    admin_role: Mapped[str | None] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20), default="active")
    suspended_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    city: Mapped[str] = mapped_column(String(80), default="Lahore")
    avatar: Mapped[str] = mapped_column(Text, default="")
    mfa_secret: Mapped[str | None] = mapped_column(Text)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    last_totp_step: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (
        CheckConstraint("role IN ('user', 'vendor', 'admin')", name="role"),
        CheckConstraint("status IN ('active', 'suspended', 'banned')", name="status"),
    )


class AuthSession(Record, Base):
    __tablename__ = "auth_sessions"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    family_id: Mapped[str] = mapped_column(String(36), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mfa_verified: Mapped[bool] = mapped_column(Boolean, default=False)


class Challenge(Record, Base):
    __tablename__ = "auth_challenges"
    identifier: Mapped[str] = mapped_column(String(254), index=True)
    purpose: Mapped[str] = mapped_column(String(30))
    token_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)


class Address(Record, Base):
    __tablename__ = "addresses"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (
        Index("uq_address_default", "user_id", unique=True, postgresql_where=text("is_default = true")),
    )


class Plan(Record, Base):
    __tablename__ = "plans"
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(160), unique=True)
    price: Mapped[Decimal] = mapped_column(MONEY, default=0)
    yearly_price: Mapped[Decimal] = mapped_column(MONEY, default=0)
    product_limit: Mapped[int | None] = mapped_column(Integer)
    featured_slots: Mapped[int] = mapped_column(Integer, default=0)
    commission_rate: Mapped[Decimal | None] = mapped_column(RATE)
    features: Mapped[list] = mapped_column(JSONB, default=list)
    # Plan perks shown to vendors: storePromo, advancedAnalytics, csvImport, prioritySupport.
    perks: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_popular: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (
        CheckConstraint("price >= 0 AND yearly_price >= 0 AND featured_slots >= 0", name="amounts"),
        CheckConstraint("commission_rate BETWEEN 0 AND 1", name="commission"),
        Index("uq_popular_plan", "is_popular", unique=True, postgresql_where=text("is_popular = true")),
    )


class Shop(Record, Base):
    __tablename__ = "shops"
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), unique=True)
    name: Mapped[str] = mapped_column(String(160))
    slug: Mapped[str] = mapped_column(String(180), unique=True)
    city: Mapped[str] = mapped_column(String(80), index=True)
    verification_status: Mapped[str] = mapped_column(String(30), default="not_submitted", index=True)
    is_suspended: Mapped[bool] = mapped_column(Boolean, default=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    is_featured: Mapped[bool] = mapped_column(Boolean, default=False)
    is_on_vacation: Mapped[bool] = mapped_column(Boolean, default=False)
    plan_id: Mapped[str | None] = mapped_column(ForeignKey("plans.id"))
    commission_override: Mapped[Decimal | None] = mapped_column(RATE)
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    bank_account: Mapped[str | None] = mapped_column(Text)
    payout_account: Mapped[str | None] = mapped_column(Text)
    payout_mask: Mapped[str] = mapped_column(String(100), default="")
    __table_args__ = (CheckConstraint("commission_override BETWEEN 0 AND 1", name="commission"),)


class Category(Record, Base):
    __tablename__ = "categories"
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(160), unique=True)
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"), index=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    attributes_schema: Mapped[list] = mapped_column(JSONB, default=list)
    variant_attributes: Mapped[list] = mapped_column(JSONB, default=list)
    available_for: Mapped[list] = mapped_column(JSONB, default=lambda: ["store", "classified"])
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    commission_override: Mapped[Decimal | None] = mapped_column(RATE)
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    __table_args__ = (CheckConstraint("parent_id IS NULL OR parent_id <> id", name="not_own_parent"),)


class Brand(Record, Base):
    __tablename__ = "brands"
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(160), unique=True)
    logo: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(20), default="phone")
    category_ids: Mapped[list] = mapped_column(JSONB, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_popular: Mapped[bool] = mapped_column(Boolean, default=False)
    popular_order: Mapped[int] = mapped_column(Integer, default=0)


class Product(Record, Base):
    __tablename__ = "products"
    shop_id: Mapped[str] = mapped_column(ForeignKey("shops.id"), index=True)
    category_id: Mapped[str] = mapped_column(ForeignKey("categories.id"), index=True)
    brand_id: Mapped[str | None] = mapped_column(ForeignKey("brands.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(220))
    status: Mapped[str] = mapped_column(String(25), default="draft", index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    attributes: Mapped[dict] = mapped_column(JSONB, default=dict)
    schema_snapshot: Mapped[list] = mapped_column(JSONB, default=list)
    images: Mapped[list] = mapped_column(JSONB, default=list)
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    __table_args__ = (
        UniqueConstraint("shop_id", "slug"),
        Index("ix_product_attributes", "attributes", postgresql_using="gin"),
    )


class Variant(Record, Base):
    __tablename__ = "variants"
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), index=True)
    sku: Mapped[str] = mapped_column(String(100), unique=True)
    attributes: Mapped[dict] = mapped_column(JSONB, default=dict)
    price: Mapped[Decimal] = mapped_column(MONEY)
    compare_at_price: Mapped[Decimal | None] = mapped_column(MONEY)
    stock: Mapped[int] = mapped_column(Integer, default=0)
    low_stock_threshold: Mapped[int] = mapped_column(Integer, default=5)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    image: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        CheckConstraint("stock >= 0", name="nonnegative_stock"),
        CheckConstraint("price >= 0", name="nonnegative_price"),
    )


class Classified(Record, Base):
    __tablename__ = "classifieds"
    seller_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    category_id: Mapped[str] = mapped_column(ForeignKey("categories.id"), index=True)
    brand_id: Mapped[str | None] = mapped_column(ForeignKey("brands.id"))
    title: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(240), unique=True)
    description: Mapped[str] = mapped_column(Text)
    price: Mapped[Decimal] = mapped_column(MONEY)
    city: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(25), default="pending", index=True)
    attributes: Mapped[dict] = mapped_column(JSONB, default=dict)
    schema_snapshot: Mapped[list] = mapped_column(JSONB, default=list)
    images: Mapped[list] = mapped_column(JSONB, default=list)
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    __table_args__ = (CheckConstraint("price >= 0", name="nonnegative_price"),)


class Upload(Record, Base):
    __tablename__ = "uploads"
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    object_key: Mapped[str] = mapped_column(Text, unique=True)
    purpose: Mapped[str] = mapped_column(String(30))
    content_type: Mapped[str] = mapped_column(String(100))
    size: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    checksum: Mapped[str | None] = mapped_column(String(64), index=True)
    data: Mapped[dict] = mapped_column(JSONB, default=dict)


class CartItem(Record, Base):
    __tablename__ = "cart_items"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    variant_id: Mapped[str] = mapped_column(ForeignKey("variants.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    saved: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (
        UniqueConstraint("user_id", "variant_id"),
        CheckConstraint("quantity BETWEEN 1 AND 100", name="quantity"),
    )


class Coupon(Record, Base):
    __tablename__ = "coupons"
    code: Mapped[str] = mapped_column(String(40), unique=True)
    shop_id: Mapped[str | None] = mapped_column(ForeignKey("shops.id"))
    kind: Mapped[str] = mapped_column(String(20))
    value: Mapped[Decimal] = mapped_column(MONEY)
    max_discount: Mapped[Decimal | None] = mapped_column(MONEY)
    minimum_order: Mapped[Decimal] = mapped_column(MONEY, default=0)
    usage_limit: Mapped[int | None] = mapped_column(Integer)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (
        CheckConstraint("value >= 0 AND minimum_order >= 0", name="amounts"),
        CheckConstraint("valid_to > valid_from", name="dates"),
    )


class OrderGroup(Record, Base):
    __tablename__ = "order_groups"
    buyer_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))
    payment_method: Mapped[str] = mapped_column(String(30))
    total: Mapped[Decimal] = mapped_column(MONEY)
    __table_args__ = (UniqueConstraint("buyer_id", "idempotency_key"),)


class Order(Record, Base):
    __tablename__ = "orders"
    order_number: Mapped[str] = mapped_column(String(40), unique=True)
    order_group_id: Mapped[str] = mapped_column(ForeignKey("order_groups.id"), index=True)
    buyer_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    shop_id: Mapped[str] = mapped_column(ForeignKey("shops.id"), index=True)
    subtotal: Mapped[Decimal] = mapped_column(MONEY)
    discount: Mapped[Decimal] = mapped_column(MONEY, default=0)
    delivery_fee: Mapped[Decimal] = mapped_column(MONEY, default=0)
    total: Mapped[Decimal] = mapped_column(MONEY)
    commission: Mapped[Decimal] = mapped_column(MONEY)
    refunded_amount: Mapped[Decimal] = mapped_column(MONEY, default=0)
    fulfillment: Mapped[str] = mapped_column(String(20))
    address: Mapped[dict | None] = mapped_column(JSONB)
    payment_method: Mapped[str] = mapped_column(String(30))
    payment_status: Mapped[str] = mapped_column(String(30), default="pending")
    collector: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    timeline: Mapped[list] = mapped_column(JSONB, default=list)
    pickup_code_hash: Mapped[str | None] = mapped_column(String(64))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hold_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    __table_args__ = (
        CheckConstraint("total >= 0 AND refunded_amount >= 0 AND refunded_amount <= total", name="amounts"),
    )


class OrderItem(Record, Base):
    __tablename__ = "order_items"
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"))
    variant_id: Mapped[str] = mapped_column(ForeignKey("variants.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[Decimal] = mapped_column(MONEY)
    commission_rate: Mapped[Decimal] = mapped_column(RATE)
    snapshot: Mapped[dict] = mapped_column(JSONB)


class InventoryMovement(Record, Base):
    __tablename__ = "inventory_movements"
    variant_id: Mapped[str] = mapped_column(ForeignKey("variants.id"), index=True)
    order_id: Mapped[str | None] = mapped_column(ForeignKey("orders.id"))
    delta: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(100))
    stock_after: Mapped[int] = mapped_column(Integer)


class Payment(Record, Base):
    __tablename__ = "payments"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    order_group_id: Mapped[str | None] = mapped_column(ForeignKey("order_groups.id"), index=True)
    shop_id: Mapped[str | None] = mapped_column(ForeignKey("shops.id"))
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("payments.id"))
    kind: Mapped[str] = mapped_column(String(30))
    amount: Mapped[Decimal] = mapped_column(MONEY)
    method: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    provider_reference: Mapped[str | None] = mapped_column(String(180), unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(180), unique=True)
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PaymentAllocation(Record, Base):
    __tablename__ = "payment_allocations"
    payment_id: Mapped[str] = mapped_column(ForeignKey("payments.id"), index=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(MONEY)
    __table_args__ = (UniqueConstraint("payment_id", "order_id"),)


class WebhookEvent(Record, Base):
    __tablename__ = "webhook_events"
    provider: Mapped[str] = mapped_column(String(40))
    event_id: Mapped[str] = mapped_column(String(180))
    payload_hash: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("provider", "event_id"),)


class ReturnRequest(Record, Base):
    __tablename__ = "returns"
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    buyer_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    shop_id: Mapped[str] = mapped_column(ForeignKey("shops.id"), index=True)
    reason: Mapped[str] = mapped_column(String(80))
    details: Mapped[str] = mapped_column(Text)
    items: Mapped[list] = mapped_column(JSONB)
    images: Mapped[list] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(30), default="requested")
    disputed: Mapped[bool] = mapped_column(Boolean, default=False)
    resolution: Mapped[str | None] = mapped_column(String(40))
    restocked: Mapped[bool] = mapped_column(Boolean, default=False)
    data: Mapped[dict] = mapped_column(JSONB, default=dict)


class LedgerEntry(Record, Base):
    __tablename__ = "ledger_entries"
    shop_id: Mapped[str] = mapped_column(ForeignKey("shops.id"), index=True)
    order_id: Mapped[str | None] = mapped_column(ForeignKey("orders.id"), index=True)
    payment_id: Mapped[str | None] = mapped_column(ForeignKey("payments.id"))
    kind: Mapped[str] = mapped_column(String(40))
    amount: Mapped[Decimal] = mapped_column(MONEY)
    event_key: Mapped[str] = mapped_column(String(180), unique=True)


class Payout(Record, Base):
    __tablename__ = "payouts"
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    shop_id: Mapped[str] = mapped_column(ForeignKey("shops.id"), index=True)
    net_amount: Mapped[Decimal] = mapped_column(MONEY)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    destination: Mapped[str] = mapped_column(String(100))
    account_snapshot: Mapped[str] = mapped_column(Text)
    reference: Mapped[str | None] = mapped_column(String(180), unique=True)
    note: Mapped[str] = mapped_column(Text, default="")
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PayoutEntry(Base):
    __tablename__ = "payout_entries"
    ledger_id: Mapped[str] = mapped_column(ForeignKey("ledger_entries.id"), primary_key=True)
    payout_id: Mapped[str] = mapped_column(ForeignKey("payouts.id"), index=True)


class FeaturedPackage(Record, Base):
    __tablename__ = "featured_packages"
    name: Mapped[str] = mapped_column(String(120))
    duration_days: Mapped[int] = mapped_column(Integer)
    price: Mapped[Decimal] = mapped_column(MONEY)
    placement: Mapped[str] = mapped_column(String(40))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Subscription(Record, Base):
    __tablename__ = "subscriptions"
    shop_id: Mapped[str] = mapped_column(ForeignKey("shops.id"), index=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"))
    payment_id: Mapped[str] = mapped_column(ForeignKey("payments.id"), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="active")


class Placement(Record, Base):
    __tablename__ = "placements"
    shop_id: Mapped[str] = mapped_column(ForeignKey("shops.id"), index=True)
    package_id: Mapped[str] = mapped_column(ForeignKey("featured_packages.id"))
    payment_id: Mapped[str] = mapped_column(ForeignKey("payments.id"), unique=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Conversation(Record, Base):
    __tablename__ = "conversations"
    buyer_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    seller_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    shop_id: Mapped[str | None] = mapped_column(ForeignKey("shops.id"))
    subject_type: Mapped[str] = mapped_column(String(20))
    subject_id: Mapped[str] = mapped_column(String(36))
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    __table_args__ = (UniqueConstraint("buyer_id", "seller_id", "subject_type", "subject_id"),)


class Message(Record, Base):
    __tablename__ = "messages"
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    sender_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(Text)
    attachments: Mapped[list] = mapped_column(JSONB, default=list)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)


class Review(Record, Base):
    __tablename__ = "reviews"
    author_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    shop_id: Mapped[str] = mapped_column(ForeignKey("shops.id"), index=True)
    product_id: Mapped[str | None] = mapped_column(ForeignKey("products.id"), index=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"))
    target_key: Mapped[str] = mapped_column(String(80))
    rating: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(160))
    body: Mapped[str] = mapped_column(Text)
    seller_reply: Mapped[dict | None] = mapped_column(JSONB)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (
        UniqueConstraint("author_id", "order_id", "target_key"),
        CheckConstraint("rating BETWEEN 1 AND 5", name="rating"),
    )


class Question(Record, Base):
    __tablename__ = "questions"
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), index=True)
    author_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str | None] = mapped_column(Text)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Report(Record, Base):
    __tablename__ = "reports"
    reporter_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    target_type: Mapped[str] = mapped_column(String(25))
    target_id: Mapped[str] = mapped_column(String(36))
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    reason: Mapped[str] = mapped_column(String(40))
    details: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    assigned_admin_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    __table_args__ = (Index("ix_report_target", "target_type", "target_id"),)


class InternalNote(Record, Base):
    __tablename__ = "internal_notes"
    author_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    target_type: Mapped[str] = mapped_column(String(30))
    target_id: Mapped[str] = mapped_column(String(80))
    body: Mapped[str] = mapped_column(Text)


class Notification(Record, Base):
    __tablename__ = "notifications"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    href: Mapped[str] = mapped_column(Text, default="")
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)


class Preference(Record, Base):
    __tablename__ = "preferences"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    target_id: Mapped[str] = mapped_column(String(36))
    __table_args__ = (UniqueConstraint("user_id", "kind", "target_id"),)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB)
    version: Mapped[int] = mapped_column(Integer, default=1)


class AuditLog(Record, Base):
    __tablename__ = "audit_logs"
    admin_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), index=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    target_type: Mapped[str] = mapped_column(String(50))
    target_id: Mapped[str] = mapped_column(String(80))
    details: Mapped[str] = mapped_column(Text, default="")
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)
    ip: Mapped[str] = mapped_column(String(64), default="")


class Invitation(Record, Base):
    __tablename__ = "admin_invitations"
    email: Mapped[str] = mapped_column(String(254))
    role: Mapped[str] = mapped_column(String(30))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted: Mapped[bool] = mapped_column(Boolean, default=False)


class Outbox(Record, Base):
    __tablename__ = "outbox"
    kind: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSONB)
    event_key: Mapped[str] = mapped_column(String(180), unique=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class AnalyticsEvent(Record, Base):
    __tablename__ = "analytics_events"
    shop_id: Mapped[str] = mapped_column(ForeignKey("shops.id"), index=True)
    product_id: Mapped[str | None] = mapped_column(ForeignKey("products.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    visitor_hash: Mapped[str] = mapped_column(String(64))


# Explicit PostgreSQL indexes share the same names as the reviewed search migration.
Index("ix_products_name_trgm", Product.name, postgresql_using="gin", postgresql_ops={"name": "gin_trgm_ops"})
Index(
    "ix_classifieds_title_trgm",
    Classified.title,
    postgresql_using="gin",
    postgresql_ops={"title": "gin_trgm_ops"},
)
Index("ix_shops_name_trgm", Shop.name, postgresql_using="gin", postgresql_ops={"name": "gin_trgm_ops"})
Index(
    "ix_products_search",
    text("to_tsvector('simple'::regconfig, (name::text || ' '::text) || description)"),
    _table=Product.__table__,
    postgresql_using="gin",
)
Index(
    "ix_classifieds_search",
    text("to_tsvector('simple'::regconfig, (title::text || ' '::text) || description)"),
    _table=Classified.__table__,
    postgresql_using="gin",
)

Index("ix_classifieds_attributes", Classified.attributes, postgresql_using="gin")
Index("ix_variants_attributes", Variant.attributes, postgresql_using="gin")
