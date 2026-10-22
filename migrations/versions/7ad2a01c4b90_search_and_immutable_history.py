"""Search indexes and append-only financial/audit history.

Revision ID: 7ad2a01c4b90
Revises: 5afdc181177c
"""

from alembic import op

revision = "7ad2a01c4b90"
down_revision = "5afdc181177c"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE INDEX ix_products_name_trgm ON products USING gin (name gin_trgm_ops)")
    op.execute("CREATE INDEX ix_classifieds_title_trgm ON classifieds USING gin (title gin_trgm_ops)")
    op.execute("CREATE INDEX ix_shops_name_trgm ON shops USING gin (name gin_trgm_ops)")
    op.execute(
        "CREATE INDEX ix_products_search ON products USING gin (to_tsvector('simple', name || ' ' || description))"
    )
    op.execute(
        "CREATE INDEX ix_classifieds_search ON classifieds USING gin (to_tsvector('simple', title || ' ' || description))"
    )
    op.execute("""
        CREATE FUNCTION anymob_immutable_history() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'Audit and ledger history are append-only';
        END;
        $$ LANGUAGE plpgsql
    """)
    for table in ["audit_logs", "ledger_entries"]:
        op.execute(
            f"CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION anymob_immutable_history()"
        )


def downgrade():
    for table in ["audit_logs", "ledger_entries"]:
        op.execute(f"DROP TRIGGER immutable_history ON {table}")
    op.execute("DROP FUNCTION anymob_immutable_history()")
    for index in [
        "ix_products_name_trgm",
        "ix_classifieds_title_trgm",
        "ix_shops_name_trgm",
        "ix_products_search",
        "ix_classifieds_search",
    ]:
        op.execute(f"DROP INDEX {index}")
    # pg_trgm can be shared with other schemas; keep the extension installed.
