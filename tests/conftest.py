import os
from uuid import uuid4

from alembic import command
from alembic.config import Config as AlembicConfig
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

from goat.billing import init_plan
from goat.config import Config
from goat.db import Database


@pytest_asyncio.fixture
async def db():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL tests")
    config = Config(database_url=url, default_subscription_price_xtr=250)
    schema = "goat_test_" + uuid4().hex
    admin = create_async_engine(config.dsn)
    async with admin.begin() as conn:
        await conn.execute(text(f"create schema {schema}"))
    database = Database(config.dsn)
    await database.engine.dispose()
    database.engine = create_async_engine(config.dsn, connect_args={"server_settings": {"search_path": schema}})
    try:
        async with database.transaction() as conn:
            def migrate(sync_conn):
                settings = AlembicConfig("alembic.ini")
                settings.attributes["connection"] = sync_conn
                command.upgrade(settings, "head")
            await conn.run_sync(migrate)
            await init_plan(conn, config)
        yield database
    finally:
        await database.engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f"drop schema {schema} cascade"))
        await admin.dispose()
