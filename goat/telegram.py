import asyncio

from aiogram.exceptions import TelegramRetryAfter

from goat.db import query, scalar


async def poll(db, bot, dispatcher):
    async with db.lease(710003) as leader:
        if not leader:
            return
        await bot.delete_webhook()
        async with db.transaction() as conn:
            state = await scalar(conn, "select value from system_settings where key='telegram_poll'")
        offset = state["next_id"] if state else None
        allowed = dispatcher.resolve_used_update_types()
        while True:
            try:
                updates = await bot.get_updates(offset=offset, timeout=25, allowed_updates=allowed)
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 1)
                continue
            for update in updates:
                await dispatcher.feed_update(bot, update)
                offset = update.update_id + 1
                async with db.transaction() as conn:
                    await query(conn, """
                        insert into system_settings(key,value) values('telegram_poll',
                            jsonb_build_object('next_id',cast(:offset as bigint)))
                        on conflict(key) do update set value=excluded.value
                    """, offset=offset)
