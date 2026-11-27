"""Manual bank-card payment requests."""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE manual_payments (
            id uuid PRIMARY KEY,
            user_id bigint NOT NULL REFERENCES users,
            amount numeric(12,2) NOT NULL CHECK(amount > 0),
            currency text NOT NULL DEFAULT 'RUB',
            status text NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','approved','rejected')),
            created_at timestamptz NOT NULL DEFAULT now(),
            reviewed_at timestamptz,
            actor bigint,
            grant_id bigint REFERENCES access_grants
        );
        CREATE INDEX manual_payments_pending ON manual_payments(created_at)
            WHERE status='pending';
    """)


def downgrade():
    op.execute("DROP TABLE manual_payments")

