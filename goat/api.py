import csv
from dataclasses import asdict
from datetime import UTC, datetime
import io
import json
from pathlib import Path
import time
from typing import Annotated

import httpx

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from goat.accounts import erase, profile, register, validate_init_data
from goat.billing import history, statistics
from goat.db import one, query, rows, scalar
from goat.finance import Calculation, calculate
from goat.gift_preview import GiftPreviews
from goat.market import PORTALS_DEFAULT_FEES, Portals, listing_analysis
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
        return []  # Production does not publish synthetic prices or trades.

    @app.get("/api/market-status")
    async def market_status():
        async with db.transaction() as conn:
            status = {row["component"]: row for row in await rows(conn, "select * from system_health")}
        output = []
        portals_fallback = ("key_required" if not config.portals_api_key else
                            "license_required" if not config.portals_api_key.startswith("tma ")
                            and not config.portals_data_licensed else "initializing")
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
    async def gifts_catalog(uid: User, q: str = "", offset: int = 0, limit: int = 60, session: str = ''):
        from uuid import UUID, uuid4
        offset, limit = max(0, offset), max(1, min(limit, 100))
        q = q.strip()[:80]
        async with db.transaction() as conn:
            await require_access(conn, uid)
            await query(conn,"delete from catalog_sessions where expires_at<now()")
            saved = await one(conn,"select * from catalog_sessions where id=:s and user_id=:u and search=:q",
                              s=UUID(session),u=uid,q=q) if session else None
            if session and not saved:
                raise HTTPException(409,'Поиск устарел. Нажмите «Найти», чтобы обновить результаты.')
            if not saved:
                ids = [row['external_id'] for row in await rows(conn,"""select l.external_id
                    from market_listings l join gifts g on g.id=l.gift_id
                    join market_analysis a using(market,external_id)
                    where l.market='Portals' and l.active and l.observed_at>now()-interval '15 minutes'
                    and (:q='' or concat_ws(' ',g.title,g.collection,g.model,g.backdrop,g.symbol,g.number)
                    ilike '%'||:q||'%') order by l.external_id""",q=q)]
                session = str(uuid4())
                await query(conn,"""insert into catalog_sessions values(:s,:u,:q,cast(:ids as jsonb),now()+interval '10 minutes')""",
                            s=UUID(session),u=uid,q=q,ids=json.dumps(ids))
            else:
                ids = saved['ids']
            cached = await rows(conn,"""select 'catalog:'||l.market||':'||l.external_id as id,
                'floor' as strategy,case when l.active then 'listed' else 'unavailable' end as state,
                l.observed_at,a.details from market_listings l join market_analysis a using(market,external_id)
                where l.market='Portals' and l.external_id=any(cast(:ids as text[])) order by l.external_id""",
                ids=ids[offset:offset+limit])
            diagnostic = await scalar(conn, "select value from system_settings where key='market_diagnostics'")
        return {'items': cached, 'has_more': offset+limit < len(ids), 'total':len(ids),'session':session,
                'offset': offset, 'coverage': diagnostic, 'source': 'background_cache'}

    @app.get('/api/nearby')
    async def nearby(uid: User):
        async with db.transaction() as conn:
            access = await require_access(conn,uid)
            prefs = Preferences.model_validate(access['prefs'])
            return await rows(conn,"""select 'catalog:'||l.market||':'||l.external_id as id,
                'floor' as strategy,'watch' as state,l.observed_at,a.details
                from market_listings l join market_analysis a using(market,external_id)
                where l.market='Portals' and l.active and l.observed_at>now()-interval '15 minutes'
                and (a.details->>'cost')::numeric<=:budget
                and not exists(select 1 from trading_signals t where t.listing_market=l.market
                    and t.listing_id=l.external_id and t.state in ('potential','active_offer')
                    and t.observed_at>now()-interval '3 minutes')
                order by (a.details->>'roi')::numeric desc nulls last,l.external_id limit 20""",
                budget=prefs.budget)

    async def resolve_listing(conn, id_):
        if id_.startswith('catalog:Portals:'):
            from uuid import UUID
            return 'Portals', str(UUID(id_.split(':',2)[2]))
        item = await one(conn, "select listing_market,listing_id from trading_signals where id=:id",id=id_)
        if not item:
            raise HTTPException(404, "Идентификатор предложения неизвестен")
        return item['listing_market'], item['listing_id']

    @app.post("/api/signals/{id_}/verify")
    async def verify(uid: User, id_: str):
        async with db.transaction() as conn:
            await require_access(conn, uid)
            market, external_id = await resolve_listing(conn,id_)
            fee = await scalar(conn,"select fees from marketplace_fees where market=:m",m=market)
            peers = await rows(conn, """select g.*,l.market,l.external_id,l.price,l.currency,l.url,
                l.transferable_at,l.floor from market_listings l join gifts g on g.id=l.gift_id
                where l.market=:m and l.active and l.observed_at>now()-interval '15 minutes'""",m=market)
        adapter = getattr(app.state,'market_adapters',{}).get(market)
        if not adapter:
            raise HTTPException(503, "Источник не подключён")
        try:
            import asyncio
            listing = await asyncio.wait_for(adapter.get_listing(external_id), timeout=12)
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError):
            raise HTTPException(503, "Не удалось связаться с Portals. Попробуйте позже.")
        if listing is None:
            async with db.transaction() as conn:
                await query(conn,"update market_listings set active=false where market=:m and external_id=:e",m=market,e=external_id)
                await query(conn,"update trading_signals set state='unavailable' where listing_market=:m and listing_id=:e",m=market,e=external_id)
            return {'status':'unavailable','message':'Лот продан или снят с продажи'}
        from goat.market import Gift, Listing, Sale, store_gift
        pool = [Listing(row['market'],row['external_id'],
                Gift(**{k:row[k] for k in Gift.__dataclass_fields__}),row['price'],row['currency'],
                row['url'],row['transferable_at'],row['floor']) for row in peers]
        async with db.transaction() as conn:
            sold = await rows(conn,"""select g.*,s.market,s.external_id,s.price,s.currency,s.sold_at
                from market_sales s join gifts g on g.id=s.gift_id
                where s.market=:m and s.sold_at>now()-interval '30 days' and g.collection=:c""",
                m=market,c=listing.gift.collection)
        sales = [Sale(row['market'],row['external_id'],Gift(**{k:row[k] for k in Gift.__dataclass_fields__}),
                      row['price'],row['currency'],row['sold_at']) for row in sold]
        details = listing_analysis(listing,pool,fee,sales=sales)
        async with db.transaction() as conn:
            await store_gift(conn,listing.gift)
            await query(conn,"""update market_listings set price=:p,observed_at=now(),active=true
                where market=:m and external_id=:e""",p=listing.price,m=market,e=external_id)
            await query(conn,"""insert into market_analysis(market,external_id,details)
                values(:m,:e,cast(:d as jsonb)) on conflict(market,external_id)
                do update set details=excluded.details,observed_at=now()""",
                m=market,e=external_id,d=json.dumps(details,default=str))
            await query(conn,"update trading_signals set state='stale' where listing_market=:m and listing_id=:e",
                        m=market,e=external_id)
        return {'status':'available','item':{'id':f'catalog:{market}:{external_id}',
                'strategy':'floor','state':'listed','observed_at':datetime.now(UTC),'details':details}}

    @app.get('/api/admin/diagnostics')
    async def diagnostics(uid: User):
        if uid not in config.admins:
            raise HTTPException(403, 'Доступ запрещён')
        async with db.transaction() as conn:
            data = await scalar(conn,"select value from system_settings where key='market_diagnostics'") or {}
            data['unique_lots'] = await scalar(conn,"select count(*) from market_listings")
            data['collections_seen'] = await scalar(conn,"select count(distinct collection) from gifts")
            data['fresh_lots'] = await scalar(conn,"select count(*) from market_listings where active and observed_at>now()-interval '15 minutes'")
            data['tasks'] = await rows(conn,"select * from system_health")
        return data

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

    @app.get('/api/portfolio/history')
    async def portfolio_history(uid: User):
        async with db.transaction() as conn:
            return await rows(conn,"""select t.*,i.title,i.currency,p.virtual from portfolio_transactions t
                join portfolio_items i on i.id=t.item_id join portfolios p on p.id=i.portfolio_id
                where p.user_id=:uid order by t.created_at desc,t.id desc""",uid=uid)

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
                if id_.startswith('catalog:'):
                    market, external_id = await resolve_listing(conn,id_)
                    item = await one(conn,"""select a.details from market_analysis a
                        join market_listings l using(market,external_id)
                        where a.market=:m and a.external_id=:e and l.active
                        and l.observed_at>now()-interval '15 minutes'""",m=market,e=external_id)
                else:
                    item = await one(conn, """
                    select * from trading_signals where id=:id and observed_at>now()-interval '3 minutes'
                       and state in ('active_offer','potential','risky')
                """, id=id_)
            if not item:
                raise HTTPException(404, "Актуальный сигнал не найден")
            d = item["details"]
            data = PortfolioInput(title=d["title"], cost=d.get("cost") or d['purchase'], currency=d["currency"], virtual=virtual)
            simulation = {"id": id_, "proceeds": d["proceeds"], "liquidity": d["liquidity"]} if virtual else None
            return {"id": await add_item(conn, uid, data, None if virtual else d["id"], simulation)}

    @app.post("/api/portfolio/{id_}/close", status_code=204)
    async def close(uid: User, id_: int, data: CloseInput):
        async with db.transaction() as conn:
            await close_item(conn, uid, id_, data.proceeds)

    @app.patch('/api/portfolio/{id_}', status_code=204)
    async def edit_position(uid: User, id_: int, data: PortfolioInput):
        async with db.transaction() as conn:
            item = await one(conn,"""update portfolio_items i set title=:title,cost=:cost
                from portfolios p where i.portfolio_id=p.id and p.user_id=:uid and i.id=:id
                and i.sold_at is null and i.currency=:currency returning i.id""",
                uid=uid,id=id_,title=data.title,cost=data.cost,currency=data.currency)
            if not item:
                raise HTTPException(404,'Открытая позиция не найдена')
            await query(conn,"insert into portfolio_transactions(item_id,kind,amount) values(:id,'cost_correction',:v)",
                        id=id_,v=data.cost)

    @app.delete('/api/portfolio/{id_}', status_code=204)
    async def delete_position(uid: User, id_: int):
        async with db.transaction() as conn:
            item = await one(conn,"""delete from portfolio_items i using portfolios p
                where i.portfolio_id=p.id and p.user_id=:uid and i.id=:id returning i.id""",uid=uid,id=id_)
            if not item:
                raise HTTPException(404,'Позиция не найдена')

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

