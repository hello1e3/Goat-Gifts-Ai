"""Store Portals floor and enable deal alerts for existing subscribers."""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE market_listings ADD COLUMN floor numeric CHECK(floor > 0)")
    op.execute("""
        UPDATE user_settings
        SET prefs=jsonb_set(prefs,'{signals}','true'::jsonb,true)
        WHERE coalesce((prefs->>'signals')::boolean,false)=false
    """)


def downgrade():
    op.execute("ALTER TABLE market_listings DROP COLUMN floor")

