import csv
from datetime import UTC, datetime
import io
import json
from pathlib import Path
import time
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from goat.accounts import erase, profile, register, validate_init_data
from goat.billing import history, statistics
from goat.db import one, query, rows, scalar
from goat.finance import Calculation, calculate
from goat.gift_preview import GiftPreviews
from goat.terminal import CloseInput, PortfolioInput, Preferences, add_item, catalog, close_item, demo, portfolios, save_prefs, signals


def create_api(db, config, lifespan):
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    previews = GiftPreviews()

    @app.get("/gift-preview/{slug}")
    async def gift_preview(slug: str):
        image = await previews.resolve(slug)
        if image is None:
            raise HTTPException(404, "Превью Telegram недоступно")
        return RedirectResponse(image, status_code=302, headers={"Cache-Control": "public, max-age=3600"})

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' https: data:; connect-src 'self'; object-src 'none'; base-uri 'self'; "
            "frame-ancestors 'self' https://web.telegram.org"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    async def identity(authorization: Annotated[str, Header()] = ""):
        if not authorization.startswith("tma "):
            raise HTTPException(401, "Откройте терминал через Telegram")
        try:
            user = validate_init_data(authorization[4:], config.bot_token, int(time.time()))
        except (ValueError, TypeError, OverflowError):
            raise HTTPException(401, "Сессия Telegram недействительна")
        async with db.transaction() as conn:
            erased_at = await scalar(conn, "select erased_at from users where id=:id", id=user["id"])
            if erased_at and user["signed_at"] <= erased_at.timestamp():
                raise HTTPException(401, "Откройте терминал заново")
            result = await register(conn, user["id"], user.get("username"),
                                    " ".join(filter(None, (user.get("first_name"), user.get("last_name")))) or "Telegram",
                                    user.get("photo_url"))
            count = await scalar(conn, """
                insert into rate_limits(identity,bucket,hits) values(:uid,:bucket,1)
                on conflict(identity) do update set hits=case when rate_limits.bucket=excluded.bucket
                   then rate_limits.hits+1 else 1 end,bucket=excluded.bucket returning hits
            """, uid=str(user["id"]), bucket=int(time.time()) // 60)
        if count > 100:
            raise HTTPException(429, "Слишком много запросов")
        if result["blocked"]:
            raise HTTPException(403, "Аккаунт заблокирован")
        return user["id"]

    User = Annotated[int, Depends(identity)]

    async def require_access(conn, uid):
        data = await profile(conn, uid)
        if not data or not data["active"]:
            raise HTTPException(403, "Нужна действующая подписка GOAT PRO")
        return data

    @app.get("/health")
    async def health():
        try:
            async with db.transaction() as conn:
                await query(conn, "select 1")
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ok"}

    @app.get("/api/demo")
    async def demonstration():
        return demo()

    @app.get("/api/market-status")
    async def market_status():
        async with db.transaction() as conn:
            status = {row["component"]: row for row in await rows(conn, "select * from system_health")}
        output = []
        portals_fallback = ("license_required" if not config.portals_data_licensed else
                            "key_required" if not config.portals_api_key else "initializing")
        mrkt_fallback = "key_required" if not config.mrkt_api_key else "initializing"
        for name, fallback in (("Portals", portals_fallback), ("MRKT", mrkt_fallback)):
            current = status.get(name, {"component": name, "status": fallback})
            if current["status"] == "online" and (datetime.now(UTC)-current["checked_at"]).total_seconds() > 180:
                current["status"] = "stale"
            output.append(current)
        return output

    @app.get("/api/public-config")
    async def public_config():
        return {"bot_username": getattr(app.state, "bot_username", "")}

    @app.get("/api/me")
    async def me(uid: User):
        async with db.transaction() as conn:
            result = await profile(conn, uid)
            result["admin"] = uid in config.admins
            result["payments"] = await history(conn, uid)
            result["prefs"] = Preferences.model_validate(result["prefs"])
            return result

    @app.put("/api/me/preferences")
    async def prefs(uid: User, data: Preferences):
        async with db.transaction() as conn:
            await save_prefs(conn, uid, data)
        return data

    @app.delete("/api/me", status_code=204)
    async def remove(uid: User):
        async with db.transaction() as conn:
            await erase(conn, uid)

    @app.post("/api/calculator")
    async def calculator(data: Calculation):
        return JSONResponse(json.loads(json.dumps(calculate(data), default=str)))

    @app.get("/api/signals")
    async def feed(uid: User, past: bool = False):
        async with db.transaction() as conn:
            access = await require_access(conn, uid)
            return await signals(conn, Preferences.model_validate(access["prefs"]), history=past)

    @app.get("/api/catalog")
    async def gifts_catalog(uid: User, q: str = ""):
        async with db.transaction() as conn:
            await require_access(conn, uid)
            return await catalog(conn, q)

    @app.post("/api/signals/{id_}/verify")
    async def verify(uid: User, id_: str):
        async with db.transaction() as conn:
            await require_access(conn, uid)
            item = await one(conn, "select * from trading_signals where id=:id", id=id_)
            if not item:
                raise HTTPException(404, "Сигнал не найден")
            await query(conn, """
                insert into system_settings(key,value) values(:key,jsonb_build_object('status','queued','checked_at',now()))
                on conflict(key) do update set value=excluded.value
                where system_settings.value->>'status'<>'queued'
                  and (system_settings.value->>'checked_at')::timestamptz<now()-interval '10 seconds'
            """, key=f"verify:{id_}")
        return {"status": "queued", "last_observation": item["observed_at"]}

    @app.get("/api/signals/{id_}/verification")
    async def verification(uid: User, id_: str):
        async with db.transaction() as conn:
            await require_access(conn, uid)
            return await scalar(conn, "select value from system_settings where key=:key",
                                key=f"verify:{id_}") or {"status": "source_unavailable"}

    @app.get("/api/portfolio")
    async def portfolio(uid: User):
        async with db.transaction() as conn:
            return await portfolios(conn, uid)

    @app.post("/api/portfolio", status_code=201)
    async def add(uid: User, item: PortfolioInput):
        async with db.transaction() as conn:
            return {"id": await add_item(conn, uid, item)}

    @app.post("/api/portfolio/from-signal/{id_}", status_code=201)
    async def add_from_signal(uid: User, id_: str, virtual: bool = False):
        async with db.transaction() as conn:
            if virtual:
                item = next((entry for entry in demo() if entry["id"] == id_), None)
            else:
                await require_access(conn, uid)
                item = await one(conn, """
                    select * from trading_signals where id=:id and observed_at>now()-interval '3 minutes'
                       and state in ('active_offer','potential','risky')
                """, id=id_)
            if not item:
                raise HTTPException(404, "Актуальный сигнал не найден")
            d = item["details"]
            data = PortfolioInput(title=d["title"], cost=d["cost"], currency=d["currency"], virtual=virtual)
            simulation = {"id": id_, "proceeds": d["proceeds"], "liquidity": d["liquidity"]} if virtual else None
            return {"id": await add_item(conn, uid, data, None if virtual else d["id"], simulation)}

    @app.post("/api/portfolio/{id_}/close", status_code=204)
    async def close(uid: User, id_: int, data: CloseInput):
        async with db.transaction() as conn:
            await close_item(conn, uid, id_, data.proceeds)

    @app.get("/api/admin/statistics")
    async def stats(uid: User, days: int = 30, format: str = "json"):
        if uid not in config.admins:
            raise HTTPException(403, "Доступ запрещён")
        if days not in {0, 1, 7, 30}:
            raise HTTPException(422, "Некорректный период")
        async with db.transaction() as conn:
            data = await statistics(conn, days)
        if format == "csv":
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["period_days", *data])
            writer.writerow([days, *data.values()])
            return Response(output.getvalue(), media_type="text/csv",
                            headers={"Content-Disposition": "attachment; filename=goat-statistics.csv"})
        return data

    @app.get("/api/legal")
    async def legal():
        async with db.transaction() as conn:
            row = await one(conn, "select value from system_settings where key='legal'")
        return row["value"] if row else {
            "owner": "Данные оператора уточняются через /support.",
            "terms": "GOAT Gifts AI показывает расчётные рыночные сценарии. Решения о покупке и продаже принимает пользователь. Подписка оплачивается разово в Telegram Stars.",
            "privacy": "Сохраняются Telegram ID, профиль, настройки, портфель, переписка поддержки и история платежей. Запрос на удаление доступен в профиле. Финансовые записи и журнал хранятся для учёта.",
            "risk": "Цена, ликвидность и доступность подарка могут измениться. Возможен убыток.",
        }

    static = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    if static.is_dir():
        app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

        @app.get("/")
        async def index():
            return FileResponse(static / "index.html", headers={"Cache-Control": "no-cache"})

    return app

