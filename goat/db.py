from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


class Database:
    def __init__(self, dsn: str):
        self.engine = create_async_engine(dsn, pool_size=5, max_overflow=3, pool_pre_ping=True,
                                          hide_parameters=True)

    @asynccontextmanager
    async def lease(self, key: int):
        async with self.engine.connect() as conn:
            locked = await scalar(conn, "select pg_try_advisory_lock(:key)", key=key)
            try:
                yield locked
            finally:
                if locked:
                    await query(conn, "select pg_advisory_unlock(:key)", key=key)

    def transaction(self):
        return self.engine.begin()


async def query(conn, sql, **params):
    return await conn.execute(text(sql), params)


async def scalar(conn, sql, **params):
    return (await query(conn, sql, **params)).scalar_one_or_none()


async def one(conn, sql, **params):
    row = (await query(conn, sql, **params)).mappings().one_or_none()
    return dict(row) if row else None


async def rows(conn, sql, **params):
    return [dict(row) for row in (await query(conn, sql, **params)).mappings()]
