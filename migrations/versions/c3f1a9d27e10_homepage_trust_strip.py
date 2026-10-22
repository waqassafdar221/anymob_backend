"""Fill the homepage trust strip for databases seeded before it had default items.

Revision ID: c3f1a9d27e10
Revises: 8b26c0df7081
Create Date: 2026-10-22
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "c3f1a9d27e10"
down_revision = "8b26c0df7081"
branch_labels = None
depends_on = None

TRUST_STRIP = [
    {"title": "100% Genuine Phones", "description": "All phones are verified & original."},
    {"title": "Fast & Secure Delivery", "description": "Across Pakistan."},
    {"title": "24/7 Customer Support", "description": "We're here to help."},
    {"title": "Safe Transactions", "description": "Your trust, our priority."},
]


def upgrade():
    # Only rows still holding the old empty default; an admin's own items are left alone.
    op.execute(
        sa.text(
            "UPDATE settings SET value = jsonb_set(value, '{trustStrip}', CAST(:items AS jsonb)) "
            "WHERE key = 'homepage' AND COALESCE(jsonb_array_length(value->'trustStrip'), 0) = 0"
        ).bindparams(items=json.dumps(TRUST_STRIP))
    )


def downgrade():
    # Data-only change: earlier code renders any trust strip, so nothing needs reverting.
    pass
