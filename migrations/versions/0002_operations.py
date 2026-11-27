"""Record operational state for support, registration and paper positions."""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE users ADD COLUMN registered boolean NOT NULL DEFAULT true")
    op.execute("ALTER TABLE users ADD COLUMN erased_at timestamptz")
    op.execute("ALTER TABLE support_tickets ADD COLUMN unread_admin integer NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE support_tickets ADD COLUMN unread_user integer NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE paper_trades ADD COLUMN simulated_proceeds numeric CHECK(simulated_proceeds>=0)")
    op.execute("ALTER TABLE paper_trades ADD COLUMN scenario text")


def downgrade():
    op.execute("ALTER TABLE paper_trades DROP COLUMN scenario, DROP COLUMN simulated_proceeds")
    op.execute("ALTER TABLE support_tickets DROP COLUMN unread_user, DROP COLUMN unread_admin")
    op.execute("ALTER TABLE users DROP COLUMN erased_at, DROP COLUMN registered")
