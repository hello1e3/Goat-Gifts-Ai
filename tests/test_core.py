from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import httpx
import pytest
from sqlalchemy.exc import DBAPIError

from goat.accounts import grant, manage_access, profile, register, validate_init_data
from goat.api import create_api
from goat.billing import DAY, approve, checkout, order, payment, record_refund, redeem, sales, statistics
from goat.bot import make_dispatcher, save_message, select_ticket
from goat.config import Config
from goat.db import one, query, rows, scalar
from goat.finance import Calculation, calculate
from goat.jobs import EmojiManager, fanout, outbox
from goat.market import Gift, Listing, MRKT, Offer, Portals, Sale, candidates, scan
from goat.terminal import PortfolioInput, Preferences, add_item, close_item, portfolios, save_prefs, signals

TOKEN = "123456789:TEST_DATA_ONLY"


def init_data(uid, when=None):
    data = {"auth_date": str(int(time.time() if when is None else when)),
            "user": json.dumps({"id": uid, "first_name": f"Test {uid}"})}
    secret = hmac.digest(b"WebAppData", TOKEN.encode(), hashlib.sha256)
    body = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    return urlencode({**data, "hash": hmac.new(secret, body.encode(), hashlib.sha256).hexdigest()})


def test_authentication():
    assert validate_init_data(init_data(20), TOKEN, int(time.time()))["id"] == 20
    with pytest.raises(ValueError):
        validate_init_data(init_data(20, 123), TOKEN, int(time.time()))
    with pytest.raises(ValueError):
        validate_init_data(init_data(20).replace("20", "21", 1), TOKEN, int(time.time()))
    with pytest.raises(ValueError):
        validate_init_data(init_data(20) + "&auth_date=0", TOKEN, int(time.time()))


def test_decimal_and_currency_conversion():
    v = calculate(Calculation(buy="10", sell="30", buy_fee=".02", sell_fee=".10", transfer=".3",
                              network=".1", conversion=".05", extra_sell=".2", fx=".5", fx_shock_pct="-10"))
    assert v["cost"] == Decimal("10.65")
    assert v["proceeds"] == Decimal("13.30")
    assert v["profit"] == Decimal("2.65")
    assert v["downside"]["20"] == Decimal("-.05")
    assert v["fx_impact"] == Decimal("-1.35")
    assert v["break_even"] * Decimal(".45") == Decimal("10.85")


async def user(conn, uid=1):
    return await register(conn, uid, f"test_{uid}", f"Test {uid}", source="synthetic")


async def pay(conn, purchase, uid=1, charge="synthetic-charge"):
    assert await checkout(conn, purchase["id"], uid, purchase["amount"], "XTR")
    assert await payment(conn, purchase["id"], uid, purchase["amount"], "XTR", charge, datetime.now(UTC), [])


async def test_manual_payment_idempotency_price_snapshot_renewal_refund(db):
    async with db.transaction() as conn:
        await user(conn)
        first = await order(conn, 1, [9001])
        assert first["status"] == "pending_approval"
        assert not await checkout(conn, first["id"], 1, 250, "XTR")
        await query(conn, "update subscription_plans set price=400,approval=false")
        await approve(conn, first["id"], 9001, True)
        assert not await checkout(conn, first["id"], 1, 400, "XTR")
        await pay(conn, first)
        assert not await payment(conn, first["id"], 1, 250, "XTR", "synthetic-charge", datetime.now(UTC), [])
        previous = (await profile(conn, 1))["ends_at"]
        renewal = await order(conn, 1, [])
        assert renewal["status"] == "awaiting_payment"
        assert renewal["amount"] == 400
        await pay(conn, renewal, charge="synthetic-renewal")
        assert (await profile(conn, 1))["ends_at"] == previous + timedelta(days=30)
        data = await sales(conn, 0)
        assert (data["sales"], data["gross"], data["first_purchases"], data["renewals"]) == (2, 650, 1, 1)
    async with db.transaction() as conn:
        assert await record_refund(conn, "synthetic-charge", 1, 9001)
        assert not await record_refund(conn, "synthetic-charge", 1)
        data = await sales(conn, 1)
        assert (data["net"], data["refund_count"]) == (400, 1)
        assert (await profile(conn, 1))["active"]


async def test_subscription_terms_have_separate_orders_and_duration(db):
    async with db.transaction() as conn:
        await user(conn)
        monthly = await order(conn, 1, [], term_days=30)
        half_year = await order(conn, 1, [], term_days=180)
        year = await order(conn, 1, [], term_days=365)
        assert monthly["plan_id"] == "GOAT PRO"
        assert half_year["plan_id"] == "GOAT PRO 180"
        assert year["plan_id"] == "GOAT PRO 365"
        assert (monthly["amount"], half_year["amount"], year["amount"]) == (250, 1250, 2250)
        assert (monthly["duration_seconds"], half_year["duration_seconds"], year["duration_seconds"]) == (
            30 * DAY, 180 * DAY, 365 * DAY)


async def test_hundred_free_accesses_are_not_sales(db):
    async with db.transaction() as conn:
        for uid in range(1000, 1100):
            await user(conn, uid)
            await grant(conn, uid, DAY * 30, "manual_grant", actor=9001, reason="synthetic")
        data = await statistics(conn, 0)
        assert data["free_grants"] == data["free_subscribers"] == data["users"] == 100
        assert data["sales"] == data["gross"] == data["net"] == data["average"] == data["conversion"] == 0
        assert await scalar(conn, "select count(*) from payments") == 0


async def test_revoke_freeze_expiry_and_audit_immutability(db):
    async with db.transaction() as conn:
        await user(conn)
        await grant(conn, 1, DAY, "trial")
        await manage_access(conn, 1, "freeze", 9)
        assert not (await profile(conn, 1))["active"]
        await manage_access(conn, 1, "unfreeze", 9)
        assert (await profile(conn, 1))["active"]
        await query(conn, "update access_grants set starts_at=now()-interval '2 days',ends_at=now()-interval '1 hour'")
        assert not (await profile(conn, 1))["active"]
        await fanout(conn)
        assert await scalar(conn, "select count(*) from notification_deliveries where dedupe like 'reminder:%'") == 1
        await grant(conn, 1, None, "lifetime", actor=9)
        assert (await profile(conn, 1))["lifetime"]
        await manage_access(conn, 1, "revoke", 9)
        assert not (await profile(conn, 1))["active"]
    with pytest.raises(DBAPIError):
        async with db.transaction() as conn:
            await query(conn, "delete from admin_audit_logs")


async def test_promo_limits_and_support_history(db):
    async with db.transaction() as conn:
        await user(conn)
        await user(conn, 2)
        await query(conn, """insert into promo_codes values
            ('DEMO','days',3,now()+interval '1 day',1,1,false,true,9),
            ('HALF','percent',50,now()+interval '1 day',2,1,true,true,9)""")
        await redeem(conn, 1, "demo")
        with pytest.raises(ValueError, match="Лимит"):
            await redeem(conn, 2, "DEMO")
        discounted = await order(conn, 2, [], "HALF")
        assert discounted["amount"] == 125
        await approve(conn, discounted["id"], 9, False)
        assert (await order(conn, 2, [], "HALF"))["amount"] == 125
        tid = await scalar(conn, "insert into support_tickets(user_id,category) values(1,'Оплата') returning id")
        await select_ticket(conn, 1, tid)
        await save_message(conn, tid, 1, "Synthetic attachment", {"type": "photo", "media": "synthetic-file-id"})
        assert (await one(conn, "select * from support_messages where ticket_id=:id", id=tid))["media_type"] == "photo"
        assert await scalar(conn, "select value from system_settings where key='ticket:1'") == tid


async def test_broadcast_consent_and_unsubscribe(db):
    async with db.transaction() as conn:
        for uid in (1, 2, 3):
            await user(conn, uid)
        await save_prefs(conn, 1, Preferences(marketing=True))
        await save_prefs(conn, 3, Preferences(marketing=True))
        await query(conn, "update users set unreachable=true where id=3")
        await query(conn, """
            insert into broadcasts(actor,segment,payload,status) values(9,'all','{"text":"synthetic"}','queued')
        """)
        await fanout(conn)
        assert [r["user_id"] for r in await rows(conn, "select user_id from notification_deliveries")] == [1]
        await save_prefs(conn, 1, Preferences(marketing=False))

    class Bot:
        async def send_message(self, *args, **kwargs):
            pytest.fail("Cannot send after consent was withdrawn")

    assert await outbox(db, Bot(), "")


async def test_emoji_and_fallback(db):
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import SendMessage

    class Bot:
        def __init__(self):
            self.attempts = []
            self.reject = False

        async def send_message(self, uid, body, **kwargs):
            self.attempts.append(kwargs)
            if kwargs.get("entities") and self.reject:
                raise TelegramBadRequest(SendMessage(chat_id=uid, text=body), "unsupported custom emoji")
            return body

    async with db.transaction() as conn:
        await query(conn, "insert into custom_emojis values('gift','123')")
        bot = Bot()
        assert await EmojiManager.send(bot, conn, 1, "Synthetic message", "gift") == "🎁 Synthetic message"
        assert bot.attempts[0]["entities"][0].length == 2
        bot.reject = True
        assert await EmojiManager.send(bot, conn, 1, "Synthetic fallback", "gift") == "🎁 Synthetic fallback"
        assert "entities" not in bot.attempts[-1]


async def test_portfolios_api_isolation_and_role(db):
    config = Config(bot_token=TOKEN, admin_ids="9")

    @asynccontextmanager
    async def lifespan(app):
        yield

    app = create_api(db, config, lifespan)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        a = {"Authorization": "tma " + init_data(1)}
        b = {"Authorization": "tma " + init_data(2)}
        admin = {"Authorization": "tma " + init_data(9)}
        assert (await client.get("/api/me")).status_code == 401
        assert (await client.get("/api/me", headers=a)).json()["id"] == 1
        assert (await client.get("/api/signals", headers=a)).status_code == 403
        assert (await client.get("/api/admin/statistics", headers=a)).status_code == 403
        assert (await client.get("/api/admin/statistics?format=csv", headers=admin)).status_code == 200
        assert (await client.get("/api/demo")).json()[0]["state"] == "demo"
        item = await client.post("/api/portfolio", headers=a, json={"title": "TEST gift", "cost": "8"})
        assert item.status_code == 201
        id_ = item.json()["id"]
        assert (await client.get("/api/portfolio", headers=b)).json() == []
        assert (await client.post(f"/api/portfolio/{id_}/close", headers=b, json={"proceeds": 9})).status_code == 400
        assert (await client.post(f"/api/portfolio/{id_}/close", headers=a, json={"proceeds": 9})).status_code == 204
        async with db.transaction() as conn:
            await grant(conn, 1, DAY, "trial")
        assert (await client.get("/api/signals", headers=a)).status_code == 200
        assert (await client.delete("/api/me", headers=a)).status_code == 204


def test_strategies_comparability_and_restrictions():
    now = datetime.now(UTC)
    gift = Gift("example-154", "Example", 154, "Example", "Model", "Back", "Sym", "10", None)
    special = Gift("example-100", "Example", 100, "Example", "Model", "Back", "Sym", "10", None)
    buy = Listing("Portals", "1", gift, Decimal(10), "GRAM", None, now-timedelta(minutes=1))
    alternative = Listing("MRKT", "2", gift, Decimal(14), "GRAM", None, now)
    history = [Sale(m, str(i), gift, Decimal(13), "GRAM", now) for m in ("Portals", "MRKT") for i in range(3)]
    history.append(Sale("Portals", "special", special, Decimal(999), "GRAM", now))
    fees = {m: dict(buy_fee="0", sell_fee=".01", transfer=".2", network="0", conversion="0", extra_sell="0")
            for m in ("Portals", "MRKT")}
    result = candidates(buy, history, [alternative], Offer("1", Decimal(12)), fees, now)
    assert {r[0] for r in result} == {"instant", "floor", "cross"}
    assert all(r[2]["liquidity"] == 6 for r in result)
    locked = Listing("Portals", "3", gift, Decimal(10), "GRAM", None, now+timedelta(days=1))
    assert {r[0] for r in candidates(locked, history, [alternative], Offer("1", Decimal(12)), fees, now)} == {"floor"}


def test_live_floor_discount_produces_real_calculated_profit_without_sales():
    now = datetime.now(UTC)
    gift = Gift("cheap-1", "Cheap", 1, "Cheap", "Model", "Back", "Sym", None, None)
    listing = Listing("Portals", "listing-1", gift, Decimal("8"), "GRAM", None, None,
                      floor=Decimal("10"))
    fees = {"Portals": dict(buy_fee=".01", sell_fee=".02", transfer="0", network="0",
                             conversion="0", extra_sell="0")}
    result = candidates(listing, [], [listing], None, fees, now)
    assert len(result) == 1 and result[0][0] == "floor"
    details = result[0][2]
    assert details["purchase"] == Decimal("8")
    assert details["sale"] == Decimal("10")
    assert details["profit"] == Decimal("1.72")
    assert details["roi"] > 20


async def test_mrkt_contract_pagination_and_nanoton_price():
    calls = []

    async def transport(request):
        calls.append((request.headers, json.loads(request.content)))
        cursor = calls[-1][1]["cursor"]
        count = 20 if not cursor else 1
        gifts = [{
            "id": f"gift-{cursor or 'first'}-{index}", "collectionName": "Plush Pepe", "number": index + 1,
            "salePrice": "13000000000", "model": {"name": "Green"},
            "backdrop": {"name": "Blue"}, "symbol": {"name": "Star"},
            "photoUrl": "https://cdn.tgmrkt.io/gift.webp",
        } for index in range(count)]
        return httpx.Response(200, json={"gifts": gifts, "cursor": "next" if not cursor else ""})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        adapter = MRKT(client, "SYNTHETIC")
        listings = await adapter.listings()

    assert len(listings) == 21
    assert listings[0].price == Decimal("13") and listings[0].currency == "TON"
    assert listings[0].gift.model == "Green" and listings[0].gift.image.endswith("gift.webp")
    assert calls[0][0]["Authorization"] == "SYNTHETIC"
    assert calls[0][0]["Referer"] == "https://cdn.tgmrkt.io/"
    assert calls[1][1]["cursor"] == "next"


async def test_portals_webapp_token_uses_public_api_contract():
    requests = []

    async def transport(request):
        requests.append(request)
        return httpx.Response(200, json={"results": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        adapter = Portals(client, "tma SYNTHETIC")
        assert await adapter.listings() == []
        assert await adapter.sales() == []

    assert requests[0].url.path == "/api/nfts/search"
    assert requests[0].headers["Authorization"] == "tma SYNTHETIC"


async def test_portals_search_returns_cheapest_exact_purchase_links():
    from uuid import UUID

    listing_id = str(UUID(int=11))
    collection_id = str(UUID(int=12))
    requests = []
    raw = {
        "id": listing_id, "tg_id": "PlushPepe-7", "collection_id": collection_id,
        "name": "Plush Pepe", "external_collection_number": 7, "floor_price": "10",
        "price": "8", "status": "listed", "unlocks_at": None,
        "photo_url": "https://cdn.example.test/pepe.webp", "attributes": [
            {"type": "Model", "value": "Pumpkin", "rarity_per_mille": 10},
            {"type": "Backdrop", "value": "Onyx Black", "rarity_per_mille": 20},
            {"type": "Symbol", "value": "Illuminati", "rarity_per_mille": 30},
        ],
    }

    async def transport(request):
        requests.append(request)
        if request.url.path == "/api/collections":
            return httpx.Response(200, json={"collections": [{"id": collection_id}]})
        if request.url.path == "/api/nfts/search":
            return httpx.Response(200, json={"results": [raw], "total_count": 1})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        adapter = Portals(client, "tma SYNTHETIC")
        adapter.next_call = 0
        results = await adapter.search("pepe")

    assert len(results) == 1
    assert results[0].price == Decimal("8") and results[0].currency == "GRAM"
    assert results[0].url == f"https://t.me/portals/market?startapp=gift_{listing_id}"
    assert requests[0].url.params["search"] == "pepe"
    assert requests[1].url.params["collection_ids"] == collection_id
    assert requests[1].url.params["sort_by"] == "price asc"


async def test_market_unavailable_stale_and_restart(db):
    class Unavailable:
        name = "Portals"

        async def listings(self):
            raise httpx.ConnectError("synthetic unavailable")

    await scan(db, [Unavailable()], {"MRKT": "partner_contract_required"})
    async with db.transaction() as conn:
        assert await scalar(conn, "select status from system_health where component='Portals'") == "unavailable"
        await user(conn)
        await grant(conn, 1, DAY, "trial")
    await db.engine.dispose()
    async with db.transaction() as conn:
        assert (await profile(conn, 1))["active"]
        assert await signals(conn, Preferences()) == []


async def test_portals_contract_scan_and_verify(db):
    from uuid import UUID
    from goat.market import requested_checks

    now = datetime.now(UTC)
    listing_id = str(UUID(int=1))
    collection_id = str(UUID(int=2))
    gift = {"id": listing_id, "tg_id": "TestGift-154", "collection_id": collection_id, "name": "Test Gift",
            "external_collection_number": 154, "floor_price": "13", "price": "10", "status": "listed",
            "unlocks_at": (now-timedelta(minutes=1)).isoformat(), "photo_url": None, "attributes": [
                {"type": trait, "value": trait, "rarity_per_mille": 10}
                for trait in ("model", "backdrop", "symbol")]}
    paths = []
    active_price = "10"

    async def transport(request):
        paths.append(request.url.path)
        assert request.headers["Authorization"] == "partners SYNTHETIC"
        if request.url.path.endswith("/nfts/search"):
            return httpx.Response(200, json={"results": [{**gift, "price": active_price}], "total_count": 1})
        if request.url.path.endswith("/market/actions/"):
            return httpx.Response(200, json={"actions": [
                {"type": "purchase", "amount": "13", "created_at": (now-timedelta(hours=i+1)).isoformat(),
                 "nft": {**gift, "tg_id": f"TestGift-{154+i}", "external_collection_number": 154+i}}
                for i in range(3)]})
        if request.url.path.endswith("/top"):
            return httpx.Response(200, json={"id": "offer-test", "amount": "12", "status": "pending",
                "max_nfts": 2, "current_nfts": 1, "scope": {"models": ["model"], "backdrops": [], "symbols": []}})
        return httpx.Response(404)

    async with db.transaction() as conn:
        await query(conn, """
            insert into marketplace_fees(market,fees) values('Portals',
            '{"buy_fee":"0","sell_fee":".01","transfer":"0","network":"0","conversion":"0","extra_sell":"0"}')
        """)
        await query(conn, "insert into currency_rates values('GRAM','TON',now(),.5,'synthetic')")
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        adapter = Portals(client, "SYNTHETIC")
        upstream_get = adapter.get

        async def without_clock(path, **params):
            adapter.next_call = 0
            return await upstream_get(path, **params)

        adapter.get = without_clock
        await scan(db, [adapter], {"MRKT": "partner_contract_required"})
        async with db.transaction() as conn:
            feed = await signals(conn, Preferences())
            assert {entry["strategy"] for entry in feed} == {"floor", "instant"}
            entry = feed[0]["id"]
            await query(conn, """insert into system_settings values(:key,
                        jsonb_build_object('status','queued','checked_at',now()))""", key="verify:"+entry)
        active_price = "11"
        await requested_checks(db, [adapter])
        async with db.transaction() as conn:
            assert await scalar(conn, "select state from trading_signals where id=:id", id=entry) == "stale"
            await query(conn, "update trading_signals set observed_at=now()-interval '4 minutes'")
            assert await signals(conn, Preferences()) == []
    assert "/partners/market/actions/" in paths


async def test_telegram_successful_payment_event_and_duplicate(db):
    from aiogram import Bot
    from aiogram.client.session.base import BaseSession
    from aiogram.methods import AnswerPreCheckoutQuery, SendMessage
    from aiogram.types import Update

    class FakeSession(BaseSession):
        def __init__(self):
            super().__init__()
            self.calls = []

        async def close(self):
            pass

        async def make_request(self, bot, method, timeout=None):
            self.calls.append(method)
            return True

        async def stream_content(self, *args, **kwargs):
            yield b""

    config = Config(bot_token=TOKEN, admin_ids="987")
    session = FakeSession()
    bot = Bot(TOKEN, session=session)
    dp = make_dispatcher(db, config)
    tg_user = {"id": 102, "is_bot": False, "first_name": "Synthetic"}
    message = {"message_id": 1, "date": int(time.time()), "chat": {"id": 102, "type": "private"}, "from":tg_user}

    async def event(data):
        await dp.feed_update(bot, Update.model_validate(data, context={"bot": bot}))

    await event({"update_id": 1, "message": {**message, "text": "/start synthetic", "entities": [
        {"type": "bot_command", "offset": 0, "length": 6}]}})
    assert isinstance(session.calls[-1], SendMessage)
    async with db.transaction() as conn:
        purchase = await order(conn, 102, config.admins)
        await approve(conn, purchase["id"], 987, True)
    invoice = {"currency": "XTR", "total_amount": 250, "invoice_payload": str(purchase["id"])}
    await event({"update_id": 2, "pre_checkout_query": {**invoice, "id":"synthetic-checkout", "from":tg_user}})
    assert isinstance(session.calls[-1], AnswerPreCheckoutQuery) and session.calls[-1].ok
    data = {"update_id": 3, "message": {**message, "message_id": 3, "successful_payment": {
            **invoice, "telegram_payment_charge_id": "synthetic-telegram", "provider_payment_charge_id": ""}}}
    await event(data)
    await event(data)
    async with db.transaction() as conn:
        assert (await profile(conn, 102))["active"]
        assert (await sales(conn, 0))["gross"] == 250
        assert (await statistics(conn, 0))["users"] == 1
        assert await scalar(conn, "select count(*) from access_grants") == 1


async def test_virtual_demo_position_uses_model_proceeds_after_delay(db):
    from goat.terminal import demo

    scenario = demo()[0]
    details = scenario["details"]
    async with db.transaction() as conn:
        await user(conn)
        data = PortfolioInput(title=details["title"], cost=details["cost"], virtual=True)
        simulation = dict(id=scenario["id"], proceeds=details["proceeds"], liquidity=details["liquidity"])
        id_ = await add_item(conn, 1, data, simulation=simulation)
        with pytest.raises(ValueError, match="задержка"):
            await close_item(conn, 1, id_, Decimal(999))
        await query(conn, "update paper_trades set available_at=now()-interval '1 minute'")
        await close_item(conn, 1, id_, Decimal(999))
        result = await portfolios(conn, 1)
        assert result[0]["proceeds"] == details["proceeds"]

