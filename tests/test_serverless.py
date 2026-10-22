from unittest.mock import patch

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from starlette.requests import Request

from anymob.common import client_ip
from anymob.config import settings
from anymob.db import Session, now
from anymob.main import app
from anymob.models import Outbox
from anymob.serverless import InlineOutbox


@pytest_asyncio.fixture
async def inline_client():
    async with AsyncClient(
        transport=ASGITransport(app=InlineOutbox(app)),
        base_url="http://testserver",
        headers={"Origin": "http://localhost:3000"},
    ) as client:
        yield client


async def test_inline_outbox_delivers_code_before_responding(inline_client, market):
    with patch("anymob.worker.send_email") as send:
        sent = await inline_client.post(
            "/api/v1/auth/challenge", json={"identifier": "buyer@example.com", "purpose": "reset"}
        )
        assert sent.status_code == 200, sent.text
        send.assert_called_once()
        assert send.call_args.args[0]["to"] == "buyer@example.com"
    async with Session() as db:
        assert (await db.scalar(select(Outbox))).sent_at is not None


async def test_inline_outbox_ignores_requests_without_events(inline_client, market):
    with patch("anymob.worker.drain_outbox") as drain:
        assert (await inline_client.get("/api/v1/categories")).status_code == 200
        drain.assert_not_called()


async def test_daily_cron_requires_secret(client, market, monkeypatch):
    path = "/api/v1/internal/cron/daily"
    monkeypatch.setattr(settings(), "cron_secret", "")
    assert (await client.get(path, headers={"Authorization": "Bearer "})).status_code == 404
    monkeypatch.setattr(settings(), "cron_secret", "test-cron-secret")
    assert (await client.get(path)).status_code == 404
    assert (await client.get(path, headers={"Authorization": "Bearer wrong"})).status_code == 404
    async with Session.begin() as db:
        db.add(
            Outbox(
                kind="email",
                payload={"to": "buyer@example.com", "subject": "Test", "body": "Hello"},
                event_key="cron-test",
                available_at=now(),
            )
        )
    with patch("anymob.worker.send_email") as send:
        ran = await client.get(path, headers={"Authorization": "Bearer test-cron-secret"})
    assert ran.status_code == 200 and ran.json()["outboxProcessed"] == 1
    send.assert_called_once()


def test_client_ip_trusts_header_only_when_configured(monkeypatch):
    request = Request(
        {"type": "http", "headers": [(b"x-real-ip", b"203.0.113.9, 10.0.0.1")], "client": ("10.0.0.1", 1234)}
    )
    monkeypatch.setattr(settings(), "trusted_ip_header", "")
    assert client_ip(request) == "10.0.0.1"
    monkeypatch.setattr(settings(), "trusted_ip_header", "x-real-ip")
    assert client_ip(request) == "203.0.113.9"


def test_client_ip_trusts_proxy_header_only_with_matching_key(monkeypatch):
    def request(key):
        headers = [(b"x-anymob-client-ip", b"198.51.100.7"), (b"x-vercel-forwarded-for", b"203.0.113.9")]
        if key:
            headers.append((b"x-anymob-proxy-key", key))
        return Request({"type": "http", "headers": headers, "client": ("10.0.0.1", 1234)})

    monkeypatch.setattr(settings(), "trusted_ip_header", "x-vercel-forwarded-for")
    monkeypatch.setattr(settings(), "proxy_secret", "")
    assert client_ip(request(b"anything")) == "203.0.113.9"
    monkeypatch.setattr(settings(), "proxy_secret", "frontend-proxy-secret")
    assert client_ip(request(b"wrong")) == "203.0.113.9"
    assert client_ip(request(None)) == "203.0.113.9"
    assert client_ip(request(b"frontend-proxy-secret")) == "198.51.100.7"
