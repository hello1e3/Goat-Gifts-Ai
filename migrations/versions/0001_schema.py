"""Initial schema."""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

DDL = """
CREATE TABLE referral_sources (code text PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE users (
 id bigint PRIMARY KEY, username text, name text NOT NULL, photo text, source text REFERENCES referral_sources,
 created_at timestamptz NOT NULL DEFAULT now(), seen_at timestamptz NOT NULL DEFAULT now(),
 blocked boolean NOT NULL DEFAULT false, unreachable boolean NOT NULL DEFAULT false,
 erased boolean NOT NULL DEFAULT false
);
CREATE UNIQUE INDEX users_username ON users(lower(username)) WHERE username IS NOT NULL;
CREATE TABLE user_settings (
 user_id bigint PRIMARY KEY REFERENCES users ON DELETE CASCADE,
 prefs jsonb NOT NULL DEFAULT '{}'
);
CREATE TABLE subscription_plans (
 id text PRIMARY KEY, description text NOT NULL, price integer CHECK(price > 0),
 enabled boolean NOT NULL DEFAULT true, approval boolean NOT NULL,
 discount integer CHECK(discount > 0), discount_until timestamptz
);
CREATE TABLE subscriptions (user_id bigint PRIMARY KEY REFERENCES users, frozen_at timestamptz);
CREATE TABLE orders (
 id uuid PRIMARY KEY, user_id bigint NOT NULL REFERENCES users, plan_id text NOT NULL REFERENCES subscription_plans,
 description text NOT NULL, amount integer NOT NULL CHECK(amount > 0),
 status text NOT NULL CHECK(status IN ('pending_approval','approved','awaiting_payment','paid','activated','rejected','expired','refunded')),
 duration_seconds integer NOT NULL DEFAULT 2592000, created_at timestamptz NOT NULL DEFAULT now(),
 expires_at timestamptz NOT NULL DEFAULT now()+interval '1 day', checkout_at timestamptz, promo text
);
CREATE INDEX orders_owner ON orders(user_id,created_at DESC);
CREATE INDEX orders_pending ON orders(created_at) WHERE status='pending_approval';
CREATE TABLE payments (
 charge text PRIMARY KEY, order_id uuid NOT NULL UNIQUE REFERENCES orders, amount integer NOT NULL CHECK(amount > 0),
 currency text NOT NULL CHECK(currency='XTR'), paid_at timestamptz NOT NULL
);
CREATE INDEX payments_paid_at ON payments(paid_at);
CREATE TABLE refunds (
 charge text PRIMARY KEY REFERENCES payments, refunded_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE access_grants (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, user_id bigint NOT NULL REFERENCES users,
 kind text NOT NULL CHECK(kind IN ('paid','manual_grant','promo','trial','lifetime')),
 order_id uuid UNIQUE REFERENCES orders, actor bigint, reason text,
 starts_at timestamptz NOT NULL, ends_at timestamptz, revoked_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now(), CHECK(ends_at IS NULL OR ends_at>starts_at)
);
CREATE INDEX grants_owner ON access_grants(user_id,ends_at) WHERE revoked_at IS NULL;
CREATE TABLE gifts (
 id text PRIMARY KEY, collection text NOT NULL, number integer NOT NULL, title text NOT NULL,
 model text NOT NULL, backdrop text NOT NULL, symbol text NOT NULL, rarity text, image text
);
CREATE TABLE gift_attributes (gift_id text REFERENCES gifts ON DELETE CASCADE, kind text, value text NOT NULL,
 PRIMARY KEY(gift_id,kind));
CREATE TABLE market_listings (
 market text, external_id text, gift_id text NOT NULL REFERENCES gifts, price numeric NOT NULL CHECK(price>0),
 currency text NOT NULL, url text, transferable_at timestamptz, observed_at timestamptz NOT NULL,
 active boolean NOT NULL DEFAULT true, PRIMARY KEY(market, external_id)
);
CREATE TABLE market_sales (
 market text, external_id text, gift_id text NOT NULL REFERENCES gifts, price numeric NOT NULL CHECK(price>0),
 currency text NOT NULL, sold_at timestamptz NOT NULL, PRIMARY KEY(market, external_id)
);
CREATE INDEX market_sales_recent ON market_sales(sold_at DESC);
CREATE TABLE market_offers (
 market text, external_id text, details jsonb NOT NULL, observed_at timestamptz NOT NULL,
 PRIMARY KEY(market,external_id)
);
CREATE TABLE currency_rates (
 base text, quote text, observed_at timestamptz, rate numeric NOT NULL CHECK(rate>0), source text NOT NULL,
 PRIMARY KEY(base,quote,observed_at)
);
CREATE TABLE marketplace_fees (
 market text PRIMARY KEY, fees jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE trading_signals (
 id text PRIMARY KEY, listing_market text NOT NULL, listing_id text NOT NULL,
 strategy text NOT NULL, details jsonb NOT NULL, state text NOT NULL, observed_at timestamptz NOT NULL,
 FOREIGN KEY(listing_market,listing_id) REFERENCES market_listings
);
CREATE INDEX signals_recent ON trading_signals(observed_at DESC);
CREATE TABLE notification_deliveries (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, user_id bigint NOT NULL REFERENCES users,
 dedupe text NOT NULL, payload jsonb NOT NULL, attempts int NOT NULL DEFAULT 0,
 status text NOT NULL DEFAULT 'queued', run_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(user_id,dedupe)
);
CREATE INDEX delivery_ready ON notification_deliveries(run_at,id) WHERE status='queued';
CREATE TABLE portfolios (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, user_id bigint NOT NULL REFERENCES users,
 virtual boolean NOT NULL, UNIQUE(user_id,virtual)
);
CREATE TABLE portfolio_items (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, portfolio_id bigint NOT NULL REFERENCES portfolios ON DELETE CASCADE,
 gift_id text REFERENCES gifts, title text NOT NULL, cost numeric NOT NULL CHECK(cost>0),
 currency text NOT NULL, bought_at timestamptz NOT NULL DEFAULT now(), proceeds numeric CHECK(proceeds>=0),
 sold_at timestamptz
);
CREATE TABLE portfolio_transactions (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, item_id bigint NOT NULL REFERENCES portfolio_items ON DELETE CASCADE,
 kind text NOT NULL, amount numeric NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE paper_trades (
 item_id bigint PRIMARY KEY REFERENCES portfolio_items ON DELETE CASCADE,
 available_at timestamptz NOT NULL
);
CREATE TABLE support_tickets (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, user_id bigint NOT NULL REFERENCES users,
 category text NOT NULL, status text NOT NULL DEFAULT 'new', created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX support_owner ON support_tickets(user_id,id DESC);
CREATE TABLE support_messages (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, ticket_id bigint NOT NULL REFERENCES support_tickets ON DELETE CASCADE,
 actor bigint, body text, media_type text, file_id text, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE promo_codes (
 code text PRIMARY KEY, kind text NOT NULL CHECK(kind IN ('percent','fixed','days')),
 value integer NOT NULL CHECK(value>0), expires_at timestamptz NOT NULL,
 max_uses integer NOT NULL CHECK(max_uses>0), per_user integer NOT NULL CHECK(per_user>0),
 new_only boolean NOT NULL, enabled boolean NOT NULL DEFAULT true, actor bigint NOT NULL
);
ALTER TABLE orders ADD FOREIGN KEY(promo) REFERENCES promo_codes(code);
CREATE TABLE promo_redemptions (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, code text NOT NULL REFERENCES promo_codes,
 user_id bigint NOT NULL REFERENCES users, order_id uuid UNIQUE REFERENCES orders,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX promo_redemptions_code ON promo_redemptions(code,user_id);
CREATE TABLE broadcasts (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, actor bigint NOT NULL, segment text NOT NULL,
 payload jsonb NOT NULL, status text NOT NULL DEFAULT 'draft', run_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE broadcast_deliveries (
 broadcast_id bigint REFERENCES broadcasts, delivery_id bigint REFERENCES notification_deliveries ON DELETE CASCADE,
 PRIMARY KEY(broadcast_id,delivery_id)
);
CREATE TABLE custom_emojis (category text PRIMARY KEY, emoji_id text NOT NULL);
CREATE TABLE admin_audit_logs (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, actor bigint NOT NULL,
 action text NOT NULL, target text, details jsonb NOT NULL DEFAULT '{}',
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE FUNCTION immutable_audit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Audit log is append-only'; END;
$$;
CREATE TRIGGER audit_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON admin_audit_logs
 FOR EACH STATEMENT EXECUTE FUNCTION immutable_audit();
CREATE TABLE system_settings (key text PRIMARY KEY, value jsonb NOT NULL);
CREATE TABLE system_health (component text PRIMARY KEY, status text NOT NULL, checked_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE rate_limits (identity text PRIMARY KEY, bucket bigint NOT NULL, hits integer NOT NULL);
"""

def upgrade():
    from sqlalchemy import text

    # Keep the PL/pgSQL body intact while executing DDL statements separately.
    normal, function = DDL.split("CREATE FUNCTION", 1)
    function, remaining = function.split("$$;", 1)
    for statement in normal.split(";") + ["CREATE FUNCTION" + function + "$$"] + remaining.split(";"):
        if statement.strip():
            op.execute(text(statement))


def downgrade():
    raise RuntimeError("Restore a reviewed backup to roll back the initial schema")
