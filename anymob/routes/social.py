import asyncio
from typing import Literal

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import Field
from sqlalchemy import and_, func, or_, select, update

from anymob.common import DB, Page, advisory_lock, fail, get, owned_shop, paginated, record
from anymob.db import Session, now
from anymob.models import (
    Conversation,
    Message,
    Notification,
    Order,
    OrderItem,
    Preference,
    Product,
    Question,
    Review,
    Shop,
    User,
)
from anymob.schemas import Input, Name
from anymob.security import Actor, throttle
from anymob.services.catalog import validate_media, visible_ads, visible_products
from anymob.services.commerce import notify

router = APIRouter(tags=["Messaging and reviews"])


class ConversationInput(Input):
    subject_type: Literal["ad", "product", "order"]
    subject_id: str


async def participant(db, actor, id):
    row = await get(db, Conversation, id)
    if actor.id not in {row.buyer_id, row.seller_id}:
        fail(404, "Conversation not found")
    return row


async def conversation_view(db, row, actor):
    last = await db.scalar(
        select(Message).where(Message.conversation_id == row.id).order_by(Message.created_at.desc()).limit(1)
    )
    unread = await db.scalar(
        select(func.count())
        .select_from(Message)
        .where(Message.conversation_id == row.id, Message.sender_id != actor.id, Message.read_at.is_(None))
    )
    return {
        **row.data,
        **record(row, exclude={"data"}),
        "lastMessagePreview": last.body if last and not last.hidden else "",
        "lastMessageAt": last.created_at if last else row.created_at,
        "unreadCount": {actor.id: unread},
    }


@router.post("/conversations", status_code=201)
async def open_conversation(data: ConversationInput, actor: Actor, db: DB):
    from anymob.models import Classified

    shop_id = None
    if data.subject_type == "order":
        order = await get(db, Order, data.subject_id)
        if order.buyer_id != actor.id:
            fail(403, "Only the buyer can open this order conversation")
        shop_id = order.shop_id
        seller_id = (await get(db, Shop, shop_id)).owner_id
    elif data.subject_type == "product":
        product = await db.scalar(visible_products().where(Product.id == data.subject_id))
        if not product:
            fail(404, "Product not found")
        shop_id = product.shop_id
        seller_id = (await get(db, Shop, shop_id)).owner_id
    else:
        ad = await db.scalar(visible_ads().where(Classified.id == data.subject_id))
        if not ad:
            fail(404, "Ad not found")
        seller_id = ad.seller_id
    if actor.id == seller_id:
        fail(422, "You cannot message yourself")
    await advisory_lock(db, f"conversation:{actor.id}:{data.subject_type}:{data.subject_id}")
    row = await db.scalar(
        select(Conversation).where(
            Conversation.buyer_id == actor.id,
            Conversation.seller_id == seller_id,
            Conversation.subject_type == data.subject_type,
            Conversation.subject_id == data.subject_id,
        )
    )
    if not row:
        row = Conversation(**data.model_dump(), buyer_id=actor.id, seller_id=seller_id, shop_id=shop_id)
        db.add(row)
        await db.flush()
    return await conversation_view(db, row, actor)


@router.get("/conversations")
async def conversations(actor: Actor, db: DB, page: Page):
    result = await paginated(
        db,
        select(Conversation)
        .where(or_(Conversation.buyer_id == actor.id, Conversation.seller_id == actor.id))
        .order_by(Conversation.created_at.desc()),
        page,
    )
    result["items"] = [
        await conversation_view(db, await db.get(Conversation, row["id"]), actor) for row in result["items"]
    ]
    return result


@router.get("/conversations/{id}/messages")
async def messages(id: str, actor: Actor, db: DB, page: Page):
    await participant(db, actor, id)
    return await paginated(
        db,
        select(Message).where(Message.conversation_id == id).order_by(Message.created_at.desc(), Message.id),
        page,
        lambda m: {
            **record(m),
            "body": "[Removed by moderation]" if m.hidden else m.body,
            "attachments": [] if m.hidden else m.attachments,
            "sentAt": m.created_at.isoformat(),
        },
    )


class MessageInput(Input):
    body: str = Field(min_length=1, max_length=5000)
    attachments: list[str] = Field(default_factory=list, max_length=5)


@router.post("/conversations/{id}/messages", status_code=201)
async def send_message(id: str, data: MessageInput, actor: Actor, db: DB):
    await throttle("message:" + actor.id, 60, 60)
    conv = await participant(db, actor, id)
    other = conv.seller_id if actor.id == conv.buyer_id else conv.buyer_id
    blocked = await db.scalar(
        select(Preference.id).where(
            Preference.kind == "block_user",
            or_(
                and_(Preference.user_id == actor.id, Preference.target_id == other),
                and_(Preference.user_id == other, Preference.target_id == actor.id),
            ),
        )
    )
    if blocked or (await get(db, User, other)).status != "active":
        fail(403, "Messaging is unavailable for this conversation")
    await validate_media(db, actor.id, data.attachments, "message")
    row = Message(conversation_id=id, sender_id=actor.id, **data.model_dump())
    db.add(row)
    await db.flush()
    await notify(db, other, "New message", data.body[:200], "/messages?conversation=" + id)
    return {**record(row), "sentAt": row.created_at.isoformat()}


@router.post("/conversations/{id}/read")
async def mark_read(id: str, actor: Actor, db: DB):
    await participant(db, actor, id)
    await db.execute(
        update(Message)
        .where(Message.conversation_id == id, Message.sender_id != actor.id, Message.read_at.is_(None))
        .values(read_at=now())
    )
    return {"ok": True}


class ThreadState(Input):
    archived: bool
    starred: bool


@router.put("/conversations/{id}/state")
async def thread_state(id: str, data: ThreadState, actor: Actor, db: DB):
    row = await participant(db, actor, id)
    row = await get(db, Conversation, row.id, lock=True)
    row.data = {
        **row.data,
        "archivedBy": {**row.data.get("archivedBy", {}), actor.id: data.archived},
        "starredBy": {**row.data.get("starredBy", {}), actor.id: data.starred},
    }
    return await conversation_view(db, row, actor)


@router.websocket("/ws/notifications")
async def notification_socket(websocket: WebSocket):
    from anymob.config import settings
    from anymob.models import AuthSession
    from anymob.security import decode_token

    if websocket.headers.get("origin") not in settings().cors_origins:
        await websocket.close(code=1008)
        return
    try:
        claims = decode_token(websocket.cookies.get("anymob_access", ""))
    except Exception:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    cursor = now()
    try:
        while True:
            # Recheck revocation and account status on every poll; persisted events support reconnection.
            async with Session() as db:
                auth = await db.get(AuthSession, claims["sid"])
                user = await db.get(User, claims["sub"])
                if (
                    not auth
                    or auth.user_id != claims["sub"]
                    or auth.revoked_at
                    or auth.expires_at <= now()
                    or not user
                    or user.status != "active"
                    or now().timestamp() >= claims["exp"]
                ):
                    await websocket.close(code=1008)
                    return
                rows = (
                    await db.scalars(
                        select(Notification)
                        .where(Notification.user_id == user.id, Notification.created_at > cursor)
                        .order_by(Notification.created_at)
                        .limit(100)
                    )
                ).all()
                if rows:
                    cursor = rows[-1].created_at
                    await websocket.send_json({"type": "notifications", "items": [record(r) for r in rows]})
            await asyncio.sleep(2)
    except (WebSocketDisconnect, RuntimeError):
        return


@router.get("/notifications")
async def notifications(actor: Actor, db: DB, page: Page):
    return await paginated(
        db,
        select(Notification).where(Notification.user_id == actor.id).order_by(Notification.created_at.desc()),
        page,
    )


@router.post("/notifications/read-all")
async def all_read(actor: Actor, db: DB):
    await db.execute(update(Notification).where(Notification.user_id == actor.id).values(is_read=True))
    return {"ok": True}


@router.post("/notifications/{id}/read")
async def notification_read(id: str, actor: Actor, db: DB):
    row = await get(db, Notification, id)
    if row.user_id != actor.id:
        fail(404, "Notification not found")
    row.is_read = True
    return record(row)


class ReviewInput(Input):
    order_id: str
    product_id: str | None = None
    rating: int = Field(ge=1, le=5)
    title: Name
    body: str = Field(min_length=5, max_length=4000)


@router.post("/reviews", status_code=201)
async def create_review(data: ReviewInput, actor: Actor, db: DB):
    order = await get(db, Order, data.order_id)
    if order.buyer_id != actor.id or order.status not in {"delivered", "picked_up", "returned"}:
        fail(403, "Only completed purchases can be reviewed")
    if data.product_id and not await db.scalar(
        select(OrderItem.id)
        .where(OrderItem.order_id == order.id, OrderItem.product_id == data.product_id)
        .limit(1)
    ):
        fail(422, "Product was not included in the order")
    row = Review(
        **data.model_dump(), author_id=actor.id, shop_id=order.shop_id, target_key=data.product_id or "shop"
    )
    db.add(row)
    await db.flush()
    return {**record(row), "isVerifiedPurchase": True}


@router.get("/reviews")
async def reviews(db: DB, page: Page, shop_id: str | None = None, product_id: str | None = None):
    query = select(Review).where(Review.hidden.is_(False))
    if shop_id:
        query = query.where(Review.shop_id == shop_id)
    if product_id:
        query = query.where(Review.product_id == product_id)
    return await paginated(
        db,
        query.order_by(Review.created_at.desc()),
        page,
        lambda r: {**record(r), "isVerifiedPurchase": True},
    )


class TextInput(Input):
    body: str = Field(min_length=1, max_length=4000)


@router.put("/vendor/reviews/{id}/reply")
async def reply_review(id: str, data: TextInput, actor: Actor, db: DB):
    shop = await owned_shop(db, actor)
    row = await get(db, Review, id, lock=True)
    if row.shop_id != shop.id:
        fail(404, "Review not found")
    row.seller_reply = {"body": data.body, "createdAt": now().isoformat()}
    return record(row)


@router.delete("/vendor/reviews/{id}/reply")
async def remove_review_reply(id: str, actor: Actor, db: DB):
    shop = await owned_shop(db, actor)
    row = await get(db, Review, id, lock=True)
    if row.shop_id != shop.id:
        fail(404, "Review not found")
    row.seller_reply = None
    return record(row)


@router.get("/products/{id}/questions")
async def questions(id: str, db: DB, page: Page):
    product = await db.scalar(visible_products().where(Product.id == id))
    if not product:
        fail(404, "Product not found")
    return await paginated(
        db,
        select(Question).where(Question.product_id == id).order_by(Question.created_at.desc()),
        page,
        lambda q: {**record(q), "shopId": product.shop_id},
    )


@router.get("/vendor/questions")
async def vendor_questions(actor: Actor, db: DB, page: Page, unanswered: bool = False):
    shop = await owned_shop(db, actor)
    query = select(Question, Product.name).join(Product, Product.id == Question.product_id)
    query = query.where(Product.shop_id == shop.id)
    if unanswered:
        query = query.where(Question.answer.is_(None))
    total = await db.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    rows = (
        await db.execute(
            query.order_by(Question.created_at.desc()).offset((page.page - 1) * page.size).limit(page.size)
        )
    ).all()
    return {
        "items": [{**record(q), "shopId": shop.id, "productName": name} for q, name in rows],
        "total": total,
        "page": page.page,
        "pageSize": page.size,
        "pageCount": max(1, (total + page.size - 1) // page.size),
        "hasMore": page.page * page.size < total,
    }


@router.post("/products/{id}/questions", status_code=201)
async def ask_question(id: str, data: TextInput, actor: Actor, db: DB):
    await throttle("question:" + actor.id, 10, 3600)
    if not await db.scalar(visible_products().where(Product.id == id)):
        fail(404, "Product not found")
    row = Question(product_id=id, author_id=actor.id, question=data.body)
    db.add(row)
    await db.flush()
    return record(row)


@router.put("/vendor/questions/{id}/answer")
async def answer_question(id: str, data: TextInput, actor: Actor, db: DB):
    shop = await owned_shop(db, actor)
    row = await get(db, Question, id, lock=True)
    product = await get(db, Product, row.product_id)
    if product.shop_id != shop.id:
        fail(404, "Question not found")
    row.answer, row.answered_at = data.body, now()
    return record(row)
