"""Admin views of member accounts: list rows with purchase totals, and the account dossier."""

from decimal import Decimal

from sqlalchemy import func, or_, select

from anymob.common import record
from anymob.models import AuditLog, Classified, Order, Report, ReturnRequest, Review, Shop, User
from anymob.services.catalog import ad_view
from anymob.services.commerce import order_views


async def user_rows(db, users) -> list[dict]:
    ids = [u.id for u in users]
    if not ids:
        return []
    orders = dict(
        (
            await db.execute(
                select(Order.buyer_id, func.count()).where(Order.buyer_id.in_(ids)).group_by(Order.buyer_id)
            )
        ).all()
    )
    spent = dict(
        (
            await db.execute(
                select(Order.buyer_id, func.coalesce(func.sum(Order.total - Order.refunded_amount), 0))
                .where(Order.buyer_id.in_(ids), Order.payment_status == "paid", Order.status != "cancelled")
                .group_by(Order.buyer_id)
            )
        ).all()
    )
    ads = dict(
        (
            await db.execute(
                select(Classified.seller_id, func.count())
                .where(Classified.seller_id.in_(ids))
                .group_by(Classified.seller_id)
            )
        ).all()
    )
    return [
        {
            **record(u),
            "ordersCount": orders.get(u.id, 0),
            "spent": float(spent.get(u.id, Decimal(0))),
            "adsCount": ads.get(u.id, 0),
        }
        for u in users
    ]


async def user_detail(db, user: User) -> dict:
    orders = (
        await db.scalars(
            select(Order).where(Order.buyer_id == user.id).order_by(Order.created_at.desc()).limit(100)
        )
    ).all()
    ads = (
        await db.scalars(
            select(Classified).where(Classified.seller_id == user.id).order_by(Classified.created_at.desc())
        )
    ).all()
    returns = (await db.scalars(select(ReturnRequest).where(ReturnRequest.buyer_id == user.id))).all()
    shop_ids = select(Shop.id).where(Shop.owner_id == user.id)
    ad_ids = select(Classified.id).where(Classified.seller_id == user.id)
    review_ids = select(Review.id).where(Review.author_id == user.id)
    reports = (
        await db.scalars(
            select(Report).where(
                or_(
                    Report.reporter_id == user.id,
                    Report.owner_id == user.id,
                    (Report.target_type == "user") & (Report.target_id == user.id),
                    (Report.target_type == "ad") & Report.target_id.in_(ad_ids),
                    (Report.target_type == "shop") & Report.target_id.in_(shop_ids),
                    (Report.target_type == "review") & Report.target_id.in_(review_ids),
                )
            )
        )
    ).all()
    logs = (
        await db.scalars(
            select(AuditLog)
            .where(
                or_(
                    AuditLog.admin_id == user.id,
                    (AuditLog.target_type == "users") & (AuditLog.target_id == user.id),
                )
            )
            .order_by(AuditLog.created_at.desc())
            .limit(100)
        )
    ).all()
    return {
        "user": (await user_rows(db, [user]))[0],
        "orders": await order_views(db, orders),
        "ads": [ad_view(a) for a in ads],
        "returns": [record(r) for r in returns],
        "reports": [record(r) for r in reports],
        "activity": [
            {
                "at": log.created_at.isoformat(),
                "action": log.details or log.action.replace(".", " ").replace("_", " "),
            }
            for log in logs
        ],
    }
