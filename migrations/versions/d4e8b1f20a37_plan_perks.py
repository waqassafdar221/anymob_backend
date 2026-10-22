"""Plan perks for the admin plan editor and vendor plan cards.

Revision ID: d4e8b1f20a37
Revises: c3f1a9d27e10
Create Date: 2026-10-22
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d4e8b1f20a37"
down_revision = "c3f1a9d27e10"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "plans",
        sa.Column("perks", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
    )


def downgrade():
    op.drop_column("plans", "perks")
