"""Subscription terms for manual payments."""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE manual_payments ADD COLUMN duration_seconds integer NOT NULL DEFAULT 2592000 CHECK(duration_seconds > 0)")


def downgrade():
    op.execute("ALTER TABLE manual_payments DROP COLUMN duration_seconds")

