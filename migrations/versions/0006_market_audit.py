"""Cached, attributable valuations and market diagnostics."""
from alembic import op

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''CREATE TABLE market_analysis (
        market text NOT NULL, external_id text NOT NULL,
        details jsonb NOT NULL, observed_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(market,external_id))''')
    op.execute('CREATE INDEX market_listing_active_page ON market_listings(market,active,external_id)')
    op.execute('CREATE INDEX market_sales_gift_time ON market_sales(gift_id,sold_at DESC)')
    op.execute('''CREATE TABLE catalog_sessions (
        id uuid PRIMARY KEY,user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
        search text NOT NULL,ids jsonb NOT NULL,expires_at timestamptz NOT NULL)''')
    op.execute('CREATE INDEX catalog_sessions_expiry ON catalog_sessions(expires_at)')
    # Standard in-market resale, no withdrawal. Keep administrator overrides.
    # Source: https://docs.portals.tg/marketplace/fees (2026-11-27).
    op.execute('''INSERT INTO marketplace_fees(market,fees) VALUES('Portals',
        '{"buy_fee":"0","sell_fee":".02","transfer":"0","network":"0","conversion":"0","extra_sell":"0"}'::jsonb)
        ON CONFLICT(market) DO NOTHING''')
    # Invalidate estimates made by the previous collection-floor algorithm.
    op.execute("UPDATE trading_signals SET state='stale' WHERE state IN ('potential','risky','active_offer')")


def downgrade():
    op.execute('DROP TABLE catalog_sessions')
    op.execute('DROP TABLE market_analysis')
    op.execute('DROP INDEX market_listing_active_page')
    op.execute('DROP INDEX market_sales_gift_time')

