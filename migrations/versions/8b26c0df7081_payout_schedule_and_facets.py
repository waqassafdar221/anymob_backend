"""Scheduled payout dates and specification indexes.

Revision ID: 8b26c0df7081
Revises: 7ad2a01c4b90
"""

import sqlalchemy as sa
from alembic import op

revision = "8b26c0df7081"
down_revision = "7ad2a01c4b90"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("shops", sa.Column("bank_account", sa.Text(), nullable=True))
    op.add_column("payouts", sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_classifieds_attributes", "classifieds", ["attributes"], postgresql_using="gin")
    op.create_index("ix_variants_attributes", "variants", ["attributes"], postgresql_using="gin")


def downgrade():
    op.drop_column("shops", "bank_account")
    op.drop_index("ix_variants_attributes", table_name="variants")
    op.drop_index("ix_classifieds_attributes", table_name="classifieds")
    op.drop_column("payouts", "scheduled_at")
