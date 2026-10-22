# AnyMob backend

Independent FastAPI application for the marketplace described in the project PDF. All Python dependencies, configuration, database migrations, workers and tests live here. The Next.js frontend remains in the repository root; this package does not import or require it.

**Integration status:** the existing Next.js screens still use their mock API modules. This implementation supplies the persistent API; replacing those modules and completing the buyer cart/checkout/account screens remains a separate integration stage. The backend must not be treated as production-ready until that integration and provider-specific payment/SMS work are completed.

## Run with Docker

Requirements: Python 3.12+ and Docker Compose. Run commands from `backend/`.

```sh
python3 -m venv .venv
.venv/bin/pip install -c requirements.lock -e '.[dev]'
.venv/bin/python scripts/configure.py
docker compose up --build -d
docker compose exec api anymob seed
docker compose exec api anymob create-admin --email admin@example.com --name Administrator --phone 03001234567
```

`configure.py` creates a private `.env` with independently generated signing/encryption keys. It never overwrites an existing environment. A local `.env` has already been generated in this workspace; skip this command when it exists. Password entry for `create-admin` is interactive; there are no default administrator credentials.

- API: `http://localhost:8000/api/v1`
- Swagger: `http://localhost:8000/docs`
- OpenAPI: `http://localhost:8000/openapi.json`
- Liveness/readiness: `/health/live`, `/health/ready`
- Development email inbox: `http://localhost:8025`
- Object storage console: `http://localhost:9001`

Compose starts PostgreSQL, Redis, private MinIO storage, Mailpit, a migration job, the API, a Celery worker and Celery Beat. Local infrastructure ports bind to loopback. The Docker CLI was unavailable in the implementation environment, so the Compose stack has not been executed here; PostgreSQL/Redis integration tests were run against isolated local services instead.

## Run without Docker

Configure `DATABASE_URL`, `REDIS_URL`, the S3-compatible endpoint and SMTP settings in `backend/.env`. Use a dedicated PostgreSQL database and credentials. The database owner needs permission to create `pg_trgm` during migration (or have an operator install it first).

For Neon, use the unpooled connection string (Connect → Connection pooling off; the host has no `-pooler` suffix). The API keeps its own connection pool, and migrations need a direct session. Paste the string unchanged: the `postgresql://` scheme, `sslmode` and `channel_binding` are converted for asyncpg automatically. Docker Compose overrides `DATABASE_URL` with its local `postgres` service, so Neon applies to the non-Docker setup.

Cloudflare R2 works as the S3-compatible store: set both S3 endpoints to `https://<ACCOUNT_ID>.r2.cloudflarestorage.com` and `S3_REGION=auto`, use an R2 API token with Object Read & Write on the bucket only, keep public access off (media is served through short-lived signed links), and add a bucket CORS rule allowing `PUT` and `GET` with the `Content-Type` header from each frontend origin.

```sh
.venv/bin/alembic upgrade head
.venv/bin/anymob seed
.venv/bin/uvicorn anymob.main:app --reload --port 8000
```

Run workers in separate terminals, also from `backend/`:

```sh
.venv/bin/celery -A anymob.worker:celery worker --loglevel=info
.venv/bin/celery -A anymob.worker:celery beat --loglevel=info --schedule=/tmp/anymob-beat
```

Run only one Beat scheduler per environment. The API never creates or resets tables at startup. Apply reviewed Alembic migrations before starting a new application version.

## Deploy to Vercel

The API runs as the Vercel project `anymob-backend` (production: `https://anymob-backend.vercel.app`), separate from the Next.js project. Deploy from `backend/` with `vercel deploy --prod`; `vercel deploy` alone creates a preview. `vercel.json` selects the FastAPI preset and the Singapore region (`sin1`, next to Neon), `pyproject.toml` sets the entrypoint (`[tool.vercel] entrypoint`), and `.vercelignore` keeps `.env` files, tests and local tooling out of the upload.

Vercel has no Celery worker or beat, so:

- `OUTBOX_INLINE=true` holds each response until the request's transaction commits, delivers the email/media outbox events that request queued, then responds. A failed commit now returns 500 instead of a success response.
- The cron job in `vercel.json` calls `GET /api/v1/internal/cron/daily` (authorized by `CRON_SECRET`, which Vercel sends automatically) to run lifecycle expiry and retry undelivered outbox events. The Hobby plan allows one run per day; on Pro, change the schedule to `* * * * *`.
- `TRUSTED_IP_HEADER=x-vercel-forwarded-for` makes rate limits and audit logs use the real client IP; Vercel overwrites this header, so clients cannot spoof it.

Databases: production uses Neon `neondb` through the pooled (`-pooler`) host; previews and local development use `anymob_dev`. Production and preview have separate `SECRET_KEY`, `ENCRYPTION_KEY` and `CRON_SECRET` values in Vercel. Keep a backup of the production `ENCRYPTION_KEY` (readable in the Vercel project settings).

Migrations never run during a Vercel build. Before deploying a version that adds a migration, apply it to production from `backend/` using the direct (non-pooler) host:

```sh
DATABASE_URL="$(grep '^DATABASE_URL=' .env | cut -d= -f2- | sed 's#/anymob_dev?#/neondb?#')" .venv/bin/alembic upgrade head
```

Redis (rate limits and readiness) comes from the Upstash integration in the Vercel Marketplace. Add each production frontend origin to `CORS_ORIGINS` and to the R2 bucket's CORS policy.

## API conventions

JSON uses camelCase; request models also accept snake_case. IDs are server-generated UUID strings. Money is calculated using Python `Decimal` and PostgreSQL `NUMERIC`; JSON amounts are numbers for compatibility with the TypeScript frontend. Dates are UTC ISO timestamps; report-day boundaries use Asia/Karachi. Date-range timestamp filters require an explicit timezone.

List endpoints use `page` and `pageSize` (maximum 100), returning `items`, `total`, `page`, `pageSize`, `pageCount`, and `hasMore`. API errors use `{ "error": { "code": 422, "message": "...", "fields": [...] } }`. An `X-Request-ID` header identifies each response. See Swagger for each endpoint's validated request schema and query parameters.

Checkout, purchase and refund operations require `Idempotency-Key` (8–100 characters). Keep the same key while retrying the same request. Reusing it with a different payload returns 409.

| Area | Main endpoints |
| --- | --- |
| Authentication | `/auth/register`, `/auth/login`, `/auth/me`, `/auth/refresh`, `/auth/logout`, `/auth/challenge`, `/auth/challenge/verify`, `/auth/mfa/*`, `/auth/accept-invite` |
| Accounts | `/me`, `/me/addresses`, `/me/preferences/*`, `/users/{id}/profile` |
| Public catalog | `/categories`, `/categories/{id}/schema`, `/brands`, `/shops`, `/products`, `/ads`, `/search`, `/catalog/facets/{categoryId}`, `/homepage`, `/settings/public` |
| Vendor catalog | `/vendor/shop`, `/vendor/products`, `/vendor/products/bulk`, `/vendor/products/import-csv`, `/vendor/catalog-export`, `/me/ads` |
| Verification/accounts | `/vendor/verification`, `/vendor/payout-account`, `/vendor/bank-transfer-account` |
| Commerce | `/cart`, `/cart/items`, `/checkout`, `/order-groups/{id}`, `/orders`, `/vendor/orders`, `/returns`, `/vendor/returns` |
| Revenue | `/plans`, `/featured-packages`, `/vendor/purchases`, `/vendor/earnings`, `/vendor/payouts`, `/admin/transactions`, `/admin/refunds`, `/admin/payouts` |
| Communication | `/conversations`, `/conversations/{id}/messages`, `/notifications`, `/ws/notifications`, `/reviews`, `/products/{id}/questions` |
| Administration | `/admin/users`, `/admin/vendors`, `/admin/catalog/*`, `/admin/categories`, `/admin/brands`, `/admin/orders`, `/admin/returns`, `/admin/reports`, `/admin/settings`, `/admin/team`, `/admin/audit` |
| Analytics | `/events`, `/vendor/analytics`, `/admin/dashboard`, `/admin/transactions/stats/volume`, `/admin/payout-statistics` |
| Media | `/uploads`, `/uploads/{id}/complete`, `/uploads/{id}`, `/media/{id}` |

## Authentication and frontend connection

`POST /auth/login` returns `{token, user}` and sets HttpOnly access/refresh cookies. An MFA-enabled account instead receives `{mfaRequired: true, challenge}`; exchange the challenge and a TOTP code at `/auth/mfa/verify`. Use `/auth/mfa/setup` and `/auth/mfa/enable` to enroll an authenticator. Production admin endpoints require enrolled MFA.

Public registration accepts only `user` or `vendor`; vendor registration creates the associated store. All ownership comes from the authenticated session, never client-supplied owner/admin IDs. Refresh tokens rotate, replay revokes their family, logout/password recovery revoke sessions, and permissions are rechecked against the database.

For browsers, use `credentials: 'include'`, HTTPS in production, and an origin listed in `CORS_ORIGINS`. Cookie-authenticated mutations and refresh/logout require a trusted `Origin` header. Prefer a same-origin reverse proxy from Next.js `/api/v1/*` to FastAPI. Server/API clients can use `Authorization: Bearer <token>`.

The frontend migration must also replace the existing client-written auth/role cookies and mock middleware checks, not just fetch URLs. Current mock `actorId` and `buyerId` arguments should be removed or ignored by adapters. A single browser refresh operation should be shared by concurrent requests. WebSocket notifications use the access cookie, check Origin and revalidate sessions during the connection; reconnect after refreshing, and use REST notification/message history to recover missed events.

For verification/reset development, email codes appear in Mailpit. `SMS_PROVIDER=development` routes test SMS to Mailpit; it is rejected in production. Codes are randomly generated, hashed for verification, expire, are rate limited and have an attempt limit. No fixed OTP is accepted.

## Commerce and accounting behavior

- Store products and classified ads use separate tables/workflows. Classifieds cannot enter checkout.
- Checkout produces one order per store and one shared order group. Delivery/pickup, address ownership, payment method intersection, stock, coupons and fees are validated server-side.
- PostgreSQL row locks and unique idempotency constraints protect inventory and retries. Cancellation restores reserved stock once. Order items, addresses, prices, commission rates and return/holding windows are snapshots.
- Commission precedence: store override, nearest category override, plan rate, platform default. Rates are fractions (`0.08` means 8%).
- Online checkout creates one payment with per-order allocations. COD and direct bank transfers are vendor-collected, so they generate commission receivables rather than an erroneous second vendor payout.
- Refund requests remain pending until settlement is acknowledged with a transfer reference. Pending refunds reserve the refundable balance. Refund settlement creates offsetting ledger entries; it never rewrites history.
- Available payouts require completed orders, expired holding/return windows, no open return or dispute, and a configured encrypted payout account. Ledger entries can belong to only one payout. New refunds/returns are rechecked before approving payouts. Bulk actions are atomic.
- Bank transfer confirmation and manual payout/refund settlement record who acknowledged the actual transfer and its reference. These endpoints do not send money.
- Audit and ledger tables reject row updates/deletes through database triggers. Archive historical entities instead of deleting financial references.

**Payment provider boundary:** no real card/JazzCash/Easypaisa gateway is configured. `PAYMENT_PROVIDER=disabled` rejects online checkout. A non-production signed sandbox webhook exists for integration testing. It verifies HMAC, timestamp, currency, amount and event identity and rejects replays with changed payloads. Production requires a merchant-specific adapter, verified callbacks, provider refund handling, late-payment reconciliation and deployment credentials; simulated success cannot run in production. OAuth provider integrations and courier APIs are not included; courier/tracking entry is manual.

## Uploads and workers

1. Create upload metadata at `POST /uploads` with purpose, content type and exact size.
2. `PUT` the file body directly to `put.url` with the returned `put.headers` (`Content-Type`). The URL expires after 10 minutes and is signed for the declared type and exact size.
3. Call `/uploads/{id}/complete`, then poll `/uploads/{id}`.
4. Store the returned `/api/v1/media/{id}` URL in the listing once status is `ready`.

Images are decoded, bounded, re-encoded to WebP and stripped of EXIF metadata; browser upload URLs cannot overwrite the processed object. Verification files and message attachments require authorized access. PDFs are private, downloaded as attachments, and require a production malware scanning integration if accepted operationally. Catalog image URLs are public.

The transactional outbox handles email/SMS/media with retries and capped backoff. Email is delivered at least once with a stable Message-ID; providers may still deliver a duplicate after a worker crash. Beat handles unconfirmed/pickup expiry, shipped auto-completion, ad/subscription expiry and suspension expiry. Review failed outbox rows after repeated delivery failures.

## Catalog and moderation

Category schemas support inherited text, number, select, multi-select and boolean fields, specification groups, filter flags and variant keys. Products retain schema snapshots and validate edits against the current schema. Variant updates preserve historical IDs; removed variants are deactivated. Existing listings are not silently rewritten after a schema change.

Category deletion requires moving products, ads, children and brand links first. Report resolution groups active reports for the same target, requires a note, updates content/account state, and appends audit history. Stores awaiting verification or owned by suspended/banned users are hidden from public queries.

CSV import is bounded to 500 variant rows and atomic. It creates drafts; columns are `name,slug,categoryId,brandId,description,sku,price,stock,attributes,variantAttributes,images`. JSON columns contain objects/arrays; repeated slugs group variants. Export protects spreadsheet formula cells.

Development seeding creates settings, the four departments, smartphone fields and a free Starter plan. It does not create fake purchases, user credentials or production admin accounts. Demo reset is disabled unless `ENVIRONMENT=development`, `DEMO_RESET_ENABLED=true`, and the database name ends in `_demo`. It requires Super Admin and the exact confirmation `RESET DEMO DATA`, retains the bootstrap admin, resets persistent carts/data, and revokes all sessions. The response instructs the frontend to clear browser cart storage too.

## Tests and deployment

The suite uses real PostgreSQL and Redis, not SQLite. Tests refuse databases without a `_test` suffix and truncate only that configured test database. Use a dedicated Redis test database.

```sh
export ENVIRONMENT=test
export DATABASE_URL=postgresql+asyncpg://anymob:anymob@localhost:5432/anymob_test
export REDIS_URL=redis://localhost:6379/15
export SECRET_KEY=test-only-secret-at-least-thirty-two-characters
.venv/bin/alembic upgrade head
.venv/bin/alembic check
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

CI in `../.github/workflows/backend.yml` runs lint, migrations, integration tests, and migration downgrade/upgrade checks. The local suite covers stock races, atomic checkout/imports/bulk payouts, role and ownership restrictions, token/OTP/MFA replay, payment replay, refund accounting, dispute holds, moderation, settings conflicts, background jobs and immutable history.

For production: use managed PostgreSQL/Redis/storage, separate credentials from development, enable secure cookies/TLS, run migrations as a release job, run workers separately, and configure SMTP/merchant services. Keep the encryption key backed up with access controls; losing it makes payout accounts and MFA secrets unreadable. Back up PostgreSQL and object storage, retain point-in-time recovery, and verify restores in an isolated environment. The Compose credentials and test secrets are development-only.

Remaining work from the full approved plan includes the Next.js service-layer/auth migration, buyer checkout/account pages, real provider integrations, advanced per-product analytics, automated price/duplicate-image moderation, and production end-to-end/deployment validation. The current backend establishes and tests the persistent domain workflows for that integration.
