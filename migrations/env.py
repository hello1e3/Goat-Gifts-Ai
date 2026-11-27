from alembic import context
from sqlalchemy import text

from goat.config import Config

connection = context.config.attributes.get("connection")
if connection is None:
    import asyncio
    from sqlalchemy.ext.asyncio import create_async_engine

    async def migrate():
        engine = create_async_engine(Config().dsn)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(run)
        finally:
            await engine.dispose()

    def run(conn):
        conn.execute(text("select pg_advisory_xact_lock(710000)"))
        context.configure(connection=conn)
        with context.begin_transaction():
            context.run_migrations()

    asyncio.run(migrate())
else:
    connection.execute(text("select pg_advisory_xact_lock(710000)"))
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()
