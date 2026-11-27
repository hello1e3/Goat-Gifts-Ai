import asyncio
from contextlib import asynccontextmanager, suppress
import hmac
import logging
import re

from aiogram import Bot
from aiogram.types import BotCommand, MenuButtonWebApp, Update, WebAppInfo
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import HTTPException, Request
import httpx
import uvicorn

from goat.accounts import notify_admins
from goat.api import create_api
from goat.billing import init_plan, statistics
from goat.bot import make_dispatcher, stats_text
from goat.config import Config
from goat.db import Database, query, scalar
from goat.jobs import fanout, loop, outbox
from goat.market import MRKT, Portals, health, scan
from goat.telegram import poll

config = Config()
db = Database(config.dsn)


@asynccontextmanager
async def lifespan(app):
    async with db.transaction() as conn:
        def migrate(sync_conn):
            settings = AlembicConfig("alembic.ini")
            settings.attributes["connection"] = sync_conn
            command.upgrade(settings, "head")
        await conn.run_sync(migrate)
        await init_plan(conn, config)

    if config.environment == "production":
        if not config.bot_token or not config.admins or not config.app_url.startswith("https://"):
            raise RuntimeError("Production requires BOT_TOKEN, ADMIN_IDS and an HTTPS APP_URL")
    bot = Bot(config.bot_token) if config.bot_token else None
    dispatcher = make_dispatcher(db, config) if bot else None
    app.state.bot = bot
    app.state.dispatcher = dispatcher
    app.state.bot_username = (await bot.get_me()).username if bot else ""
    tasks = []
    async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
        status = {}
        adapters = []
        if config.mrkt_api_key:
            adapters.append(MRKT(client, config.mrkt_api_key))
        else:
            status["MRKT"] = "key_required"
        if not config.portals_data_licensed:
            status["Portals"] = "license_required"
        elif not config.portals_api_key:
            status["Portals"] = "key_required"
        else:
            adapters.append(Portals(client, config.portals_api_key))
        try:
            if bot and config.run_background:
                await bot.set_my_commands([BotCommand(command=cmd, description=label) for cmd, label in (
                    ("start", "Главное меню"), ("buy", "Купить или продлить GOAT PRO"), ("profile", "Профиль"),
                    ("support", "Поддержка"), ("paysupport", "Поддержка платежей"), ("terms", "Правила сервиса"))])
                if config.app_url.startswith("https://"):
                    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
                        text="GOAT NFT", web_app=WebAppInfo(url=config.app_url)))
                # Bothost injects its own WEBHOOK_URL even when the application
                # has not been configured for webhook delivery.  Only opt in
                # when the matching secret is explicitly supplied; otherwise
                # long polling remains the safe default.
                if config.webhook_url and config.webhook_secret:
                    if not re.fullmatch(r"[A-Za-z0-9_-]{24,256}",config.webhook_secret) or not config.webhook_url.startswith("https://"):
                        raise RuntimeError("Webhook requires HTTPS and a secret of at least 24 characters")
                    await bot.set_webhook(config.webhook_url, secret_token=config.webhook_secret,
                                          allowed_updates=dispatcher.resolve_used_update_types())
                else:
                    tasks.append(asyncio.create_task(loop(3, lambda: poll(db, bot, dispatcher))))

                async def deliver_one():
                    return .08 if await outbox(db, bot, config.app_url) else 1
                tasks.append(asyncio.create_task(loop(1, deliver_one)))

            async def distribute():
                async with db.lease(710002) as leader:
                    if leader:
                        async with db.transaction() as conn:
                            await fanout(conn)
                            await health(conn, "distribution", "online")
                            report = await scalar(conn, "select value from system_settings where key='report'")
                            from datetime import UTC, datetime
                            now = datetime.now(UTC)
                            if report and report["hour"] == now.hour:
                                data = await statistics(conn, 1)
                                await notify_admins(conn, config.admins, f"report:{now.date()}",
                                                    text=stats_text(data), emoji="chart")

            if config.run_background:
                tasks.append(asyncio.create_task(loop(30, lambda: scan(db, adapters, status))))
                tasks.append(asyncio.create_task(loop(15, distribute)))
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError):
                    await task
            if bot:
                await bot.session.close()
            await db.engine.dispose()


app = create_api(db, config, lifespan)


@app.post("/webhook")
@app.post("/telegram/webhook")
async def webhook(request: Request):
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not config.webhook_url or not config.webhook_secret or not hmac.compare_digest(
            secret.encode(), config.webhook_secret.encode()):
        raise HTTPException(403, "Forbidden")
    if not app.state.dispatcher:
        raise HTTPException(503, "Telegram unavailable")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 256_000:
            raise HTTPException(413, "Update too large")
    update = Update.model_validate_json(raw, context={"bot": app.state.bot})
    await app.state.dispatcher.feed_update(app.state.bot, update)
    return {"ok": True}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=config.port, access_log=False)

