import logging
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException

from anymob import serverless
from anymob.config import settings
from anymob.db import Session, engine
from anymob.routes import (
    accounts,
    admin,
    analytics,
    auth,
    bulk,
    catalog,
    finance,
    media,
    orders,
    reports,
    returns,
    social,
)

logger = logging.getLogger("anymob")


@asynccontextmanager
async def lifespan(app):
    yield
    await engine.dispose()


app = FastAPI(
    title="AnyMob API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings().environment != "production" else None,
    redoc_url=None,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings().cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
    expose_headers=["X-Request-ID"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request.state.request_id = str(uuid4())
    length = request.headers.get("content-length")
    if length and (not length.isdigit() or int(length) > 2 * 1024 * 1024):
        return JSONResponse({"error": {"code": 413, "message": "Request body too large"}}, status_code=413)
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(HTTPException)
async def http_error(request, exc):
    return JSONResponse(
        {
            "error": {
                "code": exc.status_code,
                "message": exc.detail,
                "requestId": getattr(request.state, "request_id", ""),
            }
        },
        status_code=exc.status_code,
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    # Never echo input: validation failures may include passwords or verification codes.
    return JSONResponse(
        {
            "error": {
                "code": 422,
                "message": "Check the submitted fields",
                "fields": [{"path": list(e["loc"]), "message": e["msg"]} for e in exc.errors()],
            }
        },
        status_code=422,
    )


@app.exception_handler(IntegrityError)
async def integrity_error(request, exc):
    return JSONResponse(
        {
            "error": {
                "code": 409,
                "message": "This change conflicts with an existing record or a database rule",
            }
        },
        status_code=409,
    )


@app.get("/health/live", tags=["Health"])
async def live():
    return {"status": "ok"}


@app.get("/health/ready", tags=["Health"])
async def ready():
    from redis.asyncio import Redis

    client = Redis.from_url(settings().redis_url)
    try:
        async with Session() as db:
            await db.execute(text("SELECT 1"))
            await db.execute(text("SELECT version_num FROM alembic_version"))
        await client.ping()
    except Exception:
        return JSONResponse({"status": "unavailable"}, status_code=503)
    finally:
        await client.aclose()
    return {"status": "ready"}


for router in [
    auth.router,
    bulk.router,
    accounts.router,
    catalog.router,
    orders.router,
    returns.router,
    finance.router,
    admin.router,
    reports.router,
    social.router,
    media.router,
    analytics.router,
    serverless.router,
]:
    app.include_router(router, prefix="/api/v1")

if settings().outbox_inline:
    app.add_middleware(serverless.InlineOutbox)
