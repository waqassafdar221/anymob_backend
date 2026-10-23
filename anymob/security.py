import base64
import hashlib
import hmac
import secrets
import struct
import time
from datetime import timedelta
from typing import Annotated

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from cryptography.fernet import Fernet
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select

from anymob.common import DB, config, digest, fail
from anymob.config import settings
from anymob.db import now, uid
from anymob.models import AuthSession, User

hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return hasher.hash(password)


def verify_password(stored: str, password: str) -> bool:
    try:
        return hasher.verify(stored, password)
    except VerificationError:
        return False


def encryption() -> Fernet:
    key = settings().encryption_key
    if not key:
        # Only permitted in development. Production configuration requires a dedicated key.
        key = base64.urlsafe_b64encode(hashlib.sha256(settings().secret_key.encode()).digest())
    return Fernet(key)


def encrypt(value: str) -> str:
    return encryption().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    return encryption().decrypt(value.encode()).decode()


def token_for(user, session, kind="access", minutes=None):
    return jwt.encode(
        {
            "sub": user.id,
            "sid": session.id,
            "type": kind,
            "iss": "anymob",
            "aud": "anymob-api",
            "iat": now(),
            "exp": now() + timedelta(minutes=minutes or settings().access_minutes),
        },
        settings().secret_key,
        algorithm="HS256",
    )


def decode_token(token: str, kind="access"):
    try:
        claims = jwt.decode(
            token,
            settings().secret_key,
            algorithms=["HS256"],
            issuer="anymob",
            audience="anymob-api",
            options={"require": ["exp", "sub", "sid", "type"]},
        )
        if claims["type"] != kind:
            fail(401, "Invalid session")
        return claims
    except jwt.PyJWTError:
        fail(401, "Invalid or expired session")


async def create_session(db, user, response, *, family=None, mfa=False):
    refresh = secrets.token_urlsafe(48)
    session = AuthSession(
        id=uid(),
        user_id=user.id,
        token_hash=digest(refresh),
        family_id=family or uid(),
        expires_at=now() + timedelta(days=settings().refresh_days),
        mfa_verified=mfa,
    )
    db.add(session)
    await db.flush()
    access = token_for(user, session)
    response.set_cookie(
        "anymob_refresh",
        refresh,
        httponly=True,
        secure=settings().cookie_secure,
        samesite="lax",
        max_age=settings().refresh_days * 86400,
        path="/api/v1/auth",
    )
    response.set_cookie(
        "anymob_access",
        access,
        httponly=True,
        secure=settings().cookie_secure,
        samesite="lax",
        max_age=settings().access_minutes * 60,
        path="/",
    )
    return access


def check_origin(request: Request):
    origin = request.headers.get("origin")
    if not origin or origin not in settings().cors_origins:
        fail(403, "A trusted Origin header is required for cookie-authenticated changes")


async def current_user(
    request: Request,
    db: DB,
    credentials: HTTPAuthorizationCredentials | None = Depends(HTTPBearer(auto_error=False)),
):
    bearer = request.headers.get("authorization", "")
    token = bearer[7:] if bearer.startswith("Bearer ") else request.cookies.get("anymob_access")
    if not token:
        fail(401, "Sign in to continue")
    if not bearer and request.method not in {"GET", "HEAD", "OPTIONS"}:
        check_origin(request)
    claims = decode_token(token)
    auth = await db.get(AuthSession, claims["sid"])
    if not auth or auth.user_id != claims["sub"] or auth.revoked_at or auth.expires_at <= now():
        fail(401, "Session expired")
    user = await db.get(User, auth.user_id)
    if not user:
        fail(401, "Invalid session")
    if user.status == "suspended" and user.suspended_until and user.suspended_until <= now():
        user.status, user.suspended_until = "active", None
    if user.status != "active":
        fail(403, "This account is suspended or banned")
    # Admin sessions count only once the emailed sign-in code has been entered.
    if user.role == "admin" and not auth.mfa_verified:
        fail(403, "Admin second-factor verification is required")
    request.state.user, request.state.session = user, auth
    return user


Actor = Annotated[User, Depends(current_user)]


async def has_permission(db, actor, name: str) -> bool:
    permissions = (await config(db, "team"))["permissions"].get(actor.admin_role, {})
    return actor.role == "admin" and bool(permissions.get(name, False))


def permission(name: str | None = None):
    async def check(actor: Actor, db: DB):
        if actor.role != "admin":
            fail(403, "Admin access required")
        if name and not await has_permission(db, actor, name):
            fail(403, "Your role does not permit this action")
        return actor

    return check


async def throttle(key: str, limit=10, seconds=60):
    client = Redis.from_url(settings().redis_url)
    try:
        count = await client.eval(
            "local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],ARGV[1]) end; return n",
            1,
            "limit:" + digest(key),
            seconds,
        )
        if count > limit:
            fail(429, "Too many requests; try again later")
    except (RedisError, OSError):
        fail(503, "Rate-limit service unavailable")
    finally:
        await client.aclose()


def totp(secret: str, step: int | None = None) -> str:
    value = hmac.new(
        base64.b32decode(secret),
        struct.pack(">Q", step if step is not None else int(time.time()) // 30),
        hashlib.sha1,
    ).digest()
    offset = value[-1] & 15
    return str((struct.unpack(">I", value[offset : offset + 4])[0] & 0x7FFFFFFF) % 1000000).zfill(6)


async def verify_totp(db, user, code: str):
    locked = await db.scalar(select(User).where(User.id == user.id).with_for_update())
    step = int(time.time()) // 30
    for candidate in [step - 1, step, step + 1]:
        if candidate > locked.last_totp_step and hmac.compare_digest(
            totp(decrypt(locked.mfa_secret), candidate), code
        ):
            locked.last_totp_step = candidate
            return
    fail(401, "Invalid or already used verification code")
