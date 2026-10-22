import base64
import secrets
from datetime import timedelta
from urllib.parse import quote

from fastapi import APIRouter, Request, Response
from sqlalchemy import or_, select, update

from anymob.common import DB, client_ip, config, digest, fail, record
from anymob.db import now, uid
from anymob.models import AuthSession, Challenge, Outbox, Shop, User
from anymob.schemas import ChallengeRequest, ChallengeVerify, Login, MFAVerify, Register, phone_number
from anymob.security import (
    Actor,
    check_origin,
    create_session,
    decode_token,
    encrypt,
    hash_password,
    throttle,
    token_for,
    verify_password,
    verify_totp,
)

router = APIRouter(prefix="/auth", tags=["Authentication"])
DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(32))


def identity(value):
    value = value.lower().strip()
    if "@" not in value:
        try:
            value = phone_number(value)
        except ValueError:
            pass
    return value


async def find_user(db, identifier):
    value = identity(identifier)
    return await db.scalar(select(User).where(or_(User.email == value, User.phone == value)))


async def sign_in(db, user, response):
    if user.status != "active":
        fail(403, "This account is not active")
    if user.mfa_enabled:
        auth = AuthSession(
            id=uid(),
            user_id=user.id,
            family_id=uid(),
            token_hash=digest(secrets.token_urlsafe(48)),
            expires_at=now() + timedelta(minutes=5),
            mfa_verified=False,
        )
        db.add(auth)
        await db.flush()
        return {"mfaRequired": True, "challenge": token_for(user, auth, "mfa", 5)}
    return {"token": await create_session(db, user, response), "user": record(user)}


@router.post("/register", status_code=201)
async def register(data: Register, request: Request, response: Response, db: DB):
    await throttle("register:" + client_ip(request), 5, 3600)
    if await db.scalar(
        select(User.id).where(or_(User.email == data.email.lower(), User.phone == data.phone))
    ):
        fail(409, "An account already uses these contact details")
    user = User(
        id=uid(),
        full_name=data.full_name,
        email=data.email.lower(),
        phone=data.phone,
        password_hash=hash_password(data.password),
        role=data.role,
        city=data.city,
    )
    db.add(user)
    await db.flush()
    if data.role == "vendor":
        import re

        name = data.shop_name or f"{data.full_name}'s store"
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "store"
        db.add(
            Shop(
                owner_id=user.id,
                name=name,
                slug=slug + "-" + user.id[:8],
                city=data.city,
                data={
                    "address": data.shop_address,
                    "phone": data.phone,
                    "paymentMethods": ["cod"],
                    "delivery": {"enabled": False, "fee": 0, "cities": [], "allPakistan": False},
                    "pickup": {"enabled": True, "holdHours": 48},
                    "policies": {"returnDays": 7},
                    "storeSections": [],
                },
            )
        )
    return {"token": await create_session(db, user, response), "user": record(user)}


@router.post("/login")
async def login(data: Login, request: Request, response: Response, db: DB):
    await throttle("login-ip:" + client_ip(request), 30, 300)
    await throttle("login:" + identity(data.identifier), 8, 300)
    user = await find_user(db, data.identifier)
    # Hashing a dummy password provides similar work for unknown identities.
    valid = verify_password(user.password_hash if user else DUMMY_PASSWORD_HASH, data.password)
    if not user or not valid:
        fail(401, "Those details do not match an account")
    return await sign_in(db, user, response)


@router.get("/me")
async def me(actor: Actor, db: DB):
    result = record(actor)
    if actor.role == "admin":
        result["permissions"] = (await config(db, "team"))["permissions"].get(actor.admin_role, {})
    return result


@router.post("/refresh")
async def refresh(request: Request, response: Response, db: DB):
    check_origin(request)
    value = request.cookies.get("anymob_refresh", "")
    auth = await db.scalar(
        select(AuthSession).where(AuthSession.token_hash == digest(value)).with_for_update()
    )
    if not auth or auth.expires_at <= now():
        fail(401, "Session expired")
    if auth.revoked_at:
        await db.execute(
            update(AuthSession).where(AuthSession.family_id == auth.family_id).values(revoked_at=now())
        )
        await db.commit()  # Persist replay revocation even though the response is an error.
        fail(401, "Refresh token was already used; sign in again")
    user = await db.get(User, auth.user_id)
    if user.status != "active":
        fail(403, "Account inactive")
    auth.revoked_at = now()
    return {
        "token": await create_session(db, user, response, family=auth.family_id, mfa=auth.mfa_verified),
        "user": record(user),
    }


@router.post("/logout")
async def logout(request: Request, response: Response, db: DB):
    check_origin(request)
    refresh = request.cookies.get("anymob_refresh", "")
    auth = await db.scalar(select(AuthSession).where(AuthSession.token_hash == digest(refresh)))
    if auth:
        await db.execute(
            update(AuthSession).where(AuthSession.family_id == auth.family_id).values(revoked_at=now())
        )
    response.delete_cookie("anymob_refresh", path="/api/v1/auth")
    response.delete_cookie("anymob_access", path="/")
    return {"ok": True}


@router.post("/challenge")
async def request_challenge(data: ChallengeRequest, request: Request, db: DB):
    await throttle("challenge-ip:" + client_ip(request), 20, 3600)
    await throttle("challenge:" + identity(data.identifier), 3, 300)
    from anymob.config import settings

    sms = "@" not in data.identifier
    if sms and settings().sms_provider == "disabled":
        fail(503, "SMS delivery has not been configured; use email")
    user = await find_user(db, data.identifier)
    if user and user.status == "active":
        code = str(secrets.randbelow(1000000)).zfill(6)
        await db.execute(
            update(Challenge)
            .where(Challenge.identifier == identity(data.identifier), Challenge.purpose == data.purpose)
            .values(consumed=True)
        )
        challenge = Challenge(
            identifier=identity(data.identifier),
            purpose=data.purpose,
            token_hash=digest(code + settings().secret_key),
            expires_at=now() + timedelta(minutes=10),
        )
        db.add(challenge)
        db.add(
            Outbox(
                kind="sms" if sms else "email",
                payload={
                    "to": identity(data.identifier),
                    "subject": "Your AnyMob verification code",
                    "body": f"Your code is {code}. It expires in 10 minutes.",
                },
                event_key="challenge:" + uid(),
                available_at=now(),
            )
        )
    return {"message": "If an active account matches, a code will be sent.", "expiresInSeconds": 600}


@router.post("/challenge/verify")
async def verify_challenge(data: ChallengeVerify, request: Request, response: Response, db: DB):
    from anymob.config import settings

    await throttle("verify:" + identity(data.identifier), 5, 600)
    challenge = await db.scalar(
        select(Challenge)
        .where(
            Challenge.identifier == identity(data.identifier),
            Challenge.purpose == data.purpose,
            Challenge.consumed.is_(False),
        )
        .order_by(Challenge.created_at.desc())
        .limit(1)
        .with_for_update()
    )
    if not challenge or challenge.expires_at <= now() or challenge.attempts >= 5:
        fail(401, "Invalid or expired verification code")
    challenge.attempts += 1
    if not secrets.compare_digest(challenge.token_hash, digest(data.code + settings().secret_key)):
        await db.commit()
        fail(401, "Invalid or expired verification code")
    user = await find_user(db, data.identifier)
    if not user or user.status != "active":
        fail(401, "Invalid or expired verification code")
    if data.purpose == "reset":
        if not data.password:
            fail(422, "A new password is required")
        user.password_hash = hash_password(data.password)
        await db.execute(update(AuthSession).where(AuthSession.user_id == user.id).values(revoked_at=now()))
    user.is_verified = True
    challenge.consumed = True
    return await sign_in(db, user, response)


@router.post("/mfa/setup")
async def setup_mfa(actor: Actor, db: DB):
    if actor.mfa_enabled:
        fail(409, "MFA is already configured")
    secret = base64.b32encode(secrets.token_bytes(20)).decode()
    actor.mfa_secret = encrypt(secret)
    return {
        "secret": secret,
        "uri": f"otpauth://totp/AnyMob:{quote(actor.email)}?secret={secret}&issuer=AnyMob",
    }


@router.post("/mfa/enable")
async def enable_mfa(data: MFAVerify, actor: Actor, request: Request, db: DB):
    await throttle("mfa:" + actor.id, 5, 300)
    if not actor.mfa_secret or actor.mfa_enabled:
        fail(409, "Start MFA setup first")
    await verify_totp(db, actor, data.code)
    actor.mfa_enabled = True
    request.state.session.mfa_verified = True
    await db.execute(
        update(AuthSession)
        .where(AuthSession.user_id == actor.id, AuthSession.id != request.state.session.id)
        .values(revoked_at=now())
    )
    return {"enabled": True}


@router.post("/mfa/verify")
async def complete_mfa(data: MFAVerify, response: Response, db: DB):
    claims = decode_token(data.challenge or "", "mfa")
    await throttle("mfa:" + claims["sub"], 5, 300)
    auth = await db.scalar(select(AuthSession).where(AuthSession.id == claims["sid"]).with_for_update())
    if not auth or auth.revoked_at or auth.expires_at <= now() or auth.user_id != claims["sub"]:
        fail(401, "Challenge expired")
    user = await db.get(User, auth.user_id)
    if user.status != "active" or not user.mfa_enabled:
        fail(403, "Account inactive")
    await verify_totp(db, user, data.code)
    auth.revoked_at = now()
    return {"token": await create_session(db, user, response, mfa=True), "user": record(user)}
