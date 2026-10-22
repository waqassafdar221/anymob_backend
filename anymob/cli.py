import argparse
import asyncio
import getpass

from sqlalchemy import select

from anymob.config import settings
from anymob.db import Session, engine, uid
from anymob.defaults import DEFAULTS
from anymob.models import Category, Plan, Setting, User
from anymob.security import hash_password


async def seed():
    if settings().environment == "production":
        raise SystemExit("Demo seed is disabled in production")
    async with Session.begin() as db:
        await seed_into(db)
    print("Development settings, categories and starter plan seeded. No default passwords were created.")


async def seed_into(db):
    for key, value in DEFAULTS.items():
        if not await db.get(Setting, key):
            db.add(Setting(key=key, value=value))
    for name, slug in [
        ("Mobile Phones", "mobile-phones"),
        ("Accessories", "accessories"),
        ("Tablets", "tablets"),
        ("Smart Watches", "smart-watches"),
    ]:
        if not await db.scalar(select(Category.id).where(Category.slug == slug)):
            parent = Category(id=uid(), name=name, slug=slug, attributes_schema=[], variant_attributes=[])
            db.add(parent)
            await db.flush()
            if slug == "mobile-phones":
                db.add(
                    Category(
                        name="Smartphones",
                        slug="smartphones",
                        parent_id=parent.id,
                        attributes_schema=[
                            {
                                "name": "storage",
                                "label": "Storage",
                                "type": "select",
                                "options": ["64GB", "128GB", "256GB", "512GB"],
                                "required": True,
                                "filterable": True,
                                "group": "Performance",
                            },
                            {
                                "name": "color",
                                "label": "Color",
                                "type": "select",
                                "options": ["Black", "White", "Blue"],
                                "required": True,
                                "filterable": True,
                                "group": "Design",
                            },
                        ],
                        variant_attributes=["storage", "color"],
                    )
                )
    if not await db.scalar(select(Plan.id).where(Plan.slug == "starter")):
        db.add(
            Plan(
                name="Starter",
                slug="starter",
                price=0,
                yearly_price=0,
                product_limit=20,
                featured_slots=0,
                commission_rate="0.05",
                features=["20 products", "Storefront", "Order management"],
            )
        )


async def create_admin(email, name, phone, password):
    from anymob.schemas import phone_number

    async with Session.begin() as db:
        if await db.scalar(select(User.id).where(User.email == email.lower())):
            raise SystemExit("An account with this email already exists")
        row = User(
            full_name=name,
            email=email.lower(),
            phone=phone_number(phone),
            password_hash=hash_password(password),
            role="admin",
            admin_role="super_admin",
            is_verified=True,
        )
        db.add(row)
    print("Super Admin created. Sign in and enroll MFA before production administration.")


async def execute(args):
    try:
        if args.command == "seed":
            await seed()
        elif args.command == "create-admin":
            password = getpass.getpass("Admin password (10+ characters): ")
            if len(password) < 10:
                raise SystemExit("Password must contain at least 10 characters")
            await create_admin(args.email, args.name, args.phone, password)
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description="AnyMob backend management")
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("seed", help="Seed development catalog and settings")
    admin = subs.add_parser("create-admin", help="Create a Super Admin with an interactive password")
    admin.add_argument("--email", required=True)
    admin.add_argument("--name", required=True)
    admin.add_argument("--phone", required=True)
    args = parser.parse_args()
    asyncio.run(execute(args))


if __name__ == "__main__":
    main()
