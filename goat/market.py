import asyncio
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import json
import re
from typing import Protocol
from urllib.parse import urlparse
from uuid import UUID

import httpx

from goat.db import one, query, rows
from goat.finance import Calculation, calculate


@dataclass(frozen=True)
class Gift:
    id: str
    collection: str
    number: int
    title: str
    model: str
    backdrop: str
    symbol: str
    rarity: str | None
    image: str | None

    @property
    def comparable(self):
        number = str(self.number)
        tier = number if self.number <= 100 or len(set(number)) == 1 or self.number % 100 == 0 else "regular"
        return (self.collection.casefold(), self.model.casefold(), self.backdrop.casefold(),
                self.symbol.casefold(), self.rarity, tier)


@dataclass(frozen=True)
class Listing:
    market: str
    external_id: str
    gift: Gift
    price: Decimal
    currency: str
    url: str | None
    transferable_at: datetime | None
    floor: Decimal | None = None


@dataclass(frozen=True)
class Sale:
    market: str
    external_id: str
    gift: Gift
    price: Decimal
    currency: str
    sold_at: datetime


@dataclass(frozen=True)
class Offer:
    id: str
    price: Decimal


class Marketplace(Protocol):
    name: str

    async def listings(self) -> list[Listing]: ...
    async def sales(self) -> list[Sale]: ...
    async def get_listing(self, external_id: str) -> Listing | None: ...
    async def offer(self, listing: Listing) -> Offer | None: ...


def date(value):
    return datetime.fromisoformat(value) if value else None


def positive(value):
    result = Decimal(value)
    if not result.is_finite() or result <= 0:
        raise ValueError("Invalid market price")
    return result


def safe_image(value):
    if isinstance(value, str):
        parsed = urlparse(value)
        if parsed.scheme == "https" and parsed.hostname and not parsed.username:
            return value
    return None


class Portals:
    name = "Portals"

    def __init__(self, client: httpx.AsyncClient, token: str):
        self.client = client
        self.token = token
        self.next_call = 0.0

    async def get(self, path, **params):
        clock = asyncio.get_running_loop().time
        await asyncio.sleep(max(0, self.next_call - clock()))
        response = await self.client.get("https://portal-market.com/partners" + path,
                                         params=params, headers={"Authorization": f"partners {self.token}"})
        reset = response.headers.get("X-RateLimit-Reset", "0")
        pause = max(1.1, float(reset) if reset.isdecimal() and response.status_code == 429 else 0)
        self.next_call = clock() + pause
        response.raise_for_status()
        return response.json(parse_float=Decimal)

    @staticmethod
    def gift(raw):
        attributes = {a["type"].casefold(): a for a in raw["attributes"]}
        model, backdrop, symbol = (attributes[name] for name in ("model", "backdrop", "symbol"))
        slug = raw.get("tg_id", "")
        id_ = slug.casefold() if re.fullmatch(r"[A-Za-z][A-Za-z0-9]*-[0-9]+", slug) else "portals:" + raw["id"]
        return Gift(id_, raw["name"], raw["external_collection_number"], raw["name"],
                    model["value"], backdrop["value"], symbol["value"],
                    "|".join(str(attr.get("rarity_per_mille", "")) for attr in (model, backdrop, symbol)),
                    safe_image(raw.get("photo_url")))

    def listing(self, raw):
        gift = self.gift(raw)
        slug = raw.get("tg_id", "")
        url = "https://t.me/nft/" + slug if not gift.id.startswith("portals:") else None
        floor = Decimal(raw.get("floor_price") or "0")
        return Listing(self.name, raw["id"], gift, positive(raw["price"]), "GRAM", url,
                       date(raw.get("unlocks_at")), floor or None)

    async def listings(self):
        result = await self.get("/nfts/search", status="listed", limit=40, sort_by="listed_at desc")
        return [self.listing(raw) for raw in result["results"] if raw["status"] == "listed"]

    async def sales(self):
        result = await self.get("/market/actions/", action_types="purchase", limit=20)
        output = []
        for raw in result["actions"]:
            if raw["type"] == "purchase":
                gift = self.gift(raw["nft"])
                identity = sha256(f"{gift.id}/{raw['created_at']}/{raw['amount']}".encode()).hexdigest()
                output.append(Sale(self.name, identity, gift, positive(raw["amount"]), "GRAM", date(raw["created_at"])))
        return output

    async def get_listing(self, external_id):
        result = await self.get("/nfts/search", ids=str(UUID(external_id)), status="listed", limit=1)
        return next((self.listing(raw) for raw in result["results"]
                     if raw["id"] == external_id and raw["status"] == "listed"), None)

    async def offer(self, listing):
        result = await self.get("/nfts/search", ids=str(UUID(listing.external_id)), status="listed", limit=1)
        raw = next((raw for raw in result["results"] if raw["id"] == listing.external_id), None)
        if not raw:
            return None
        try:
            offer = await self.get("/collection-offers/" + str(UUID(raw["collection_id"])) + "/top")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        if not offer or offer.get("status") != "pending" or offer.get("sender_id"):
            return None
        if offer["current_nfts"] >= offer["max_nfts"] or (
            offer.get("expires_at") and date(offer["expires_at"]) <= datetime.now(UTC)
        ):
            return None
        for field, value in (("models", listing.gift.model), ("backdrops", listing.gift.backdrop),
                             ("symbols", listing.gift.symbol)):
            allowed = offer["scope"][field]
            if allowed and value not in allowed:
                return None
        return Offer(offer["id"], positive(offer["amount"]))


class MRKT:
    name = "MRKT"
    status = "partner_contract_required"


def candidates(listing, sales, listings, offer, fees, now):
    if listing.market not in fees:
        return []
    same = [sale for sale in sales if sale.currency == listing.currency
            and sale.gift.comparable == listing.gift.comparable and sale.sold_at >= now - timedelta(days=30)]
    options = []
    if offer and (not listing.transferable_at or listing.transferable_at <= now):
        options.append(("instant", listing.market, offer.price, "active_offer"))
    for market in {sale.market for sale in same}:
        prices = sorted(sale.price for sale in same if sale.market == market)
        if len(prices) < 3 or market not in fees:
            continue
        conservative = prices[(len(prices) - 1) // 4]
        if market == listing.market:
            options.append(("floor", market, min(conservative, listing.floor or conservative), "potential"))
        elif listing.transferable_at and listing.transferable_at <= now:
            asks = [item.price for item in listings if item.market == market and item.currency == listing.currency
                    and item.gift.comparable == listing.gift.comparable]
            if asks:
                options.append(("cross", market, min(conservative, min(asks)), "risky"))
    result = []
    for strategy, exit_market, target, state in options:
        buy, sell = fees[listing.market], fees[exit_market]
        calc = calculate(Calculation(
            buy=listing.price, sell=target, currency=listing.currency, buy_fee=buy["buy_fee"],
            sell_fee=sell["sell_fee"], transfer=buy["transfer"] if exit_market != listing.market else 0,
            network=buy["network"], conversion=buy["conversion"], extra_sell=sell["extra_sell"]))
        if calc["roi"] < 5:
            continue
        result.append((strategy, state, {
            **asdict(listing.gift), **calc, "floor": listing.floor, "purchase": listing.price, "sale": target,
            "market": listing.market, "exit_market": exit_market, "url": listing.url, "liquidity": len(same),
            "transferable_at": listing.transferable_at, "offer_id": offer.id if strategy == "instant" else None,
            "history": [{"price": sale.price, "date": sale.sold_at, "market": sale.market}
                        for sale in sorted(same, key=lambda s: s.sold_at)[-30:]],
            "risk": "Цена и доступность могут измениться. Выход зависит от ликвидности.",
        }))
    return result


async def health(conn, component, status):
    await query(conn, """insert into system_health(component,status) values(:c,:s)
        on conflict(component) do update set status=excluded.status,checked_at=now()""", c=component, s=status)


async def store_gift(conn, gift):
    await query(conn, """
        insert into gifts(id,collection,number,title,model,backdrop,symbol,rarity,image)
        values(:id,:collection,:number,:title,:model,:backdrop,:symbol,:rarity,:image)
        on conflict(id) do update set image=excluded.image
    """, **asdict(gift))


async def scan(db, adapters, status):
    async with db.lease(710001) as leader:
        if not leader:
            return
        await requested_checks(db, adapters)
        current = []
        sales_data = []
        now = datetime.now(UTC)
        async with db.transaction() as conn:
            for component, state in status.items():
                await health(conn, component, state)
            fee_rows = await rows(conn, "select * from marketplace_fees")
        fees = {row["market"]: row["fees"] for row in fee_rows}
        for adapter in adapters:
            try:
                listed = await adapter.listings()
                sold = await adapter.sales()
            except (httpx.HTTPError, ValueError, KeyError, TypeError, ArithmeticError):
                async with db.transaction() as conn:
                    await health(conn, adapter.name, "unavailable")
                continue
            current.extend(listed)
            async with db.transaction() as conn:
                await health(conn, adapter.name, "online")
                for sale in sold:
                    await store_gift(conn, sale.gift)
                    await query(conn, """insert into market_sales values(:m,:e,:g,:p,:c,:s)
                      on conflict do nothing""", m=sale.market, e=sale.external_id, g=sale.gift.id,
                                p=sale.price, c=sale.currency, s=sale.sold_at)
                for listing in listed:
                    await query(conn, """
                        update trading_signals set state='stale' where listing_market=:m and listing_id=:id
                        and (details->>'purchase')::numeric<>:p
                    """, m=listing.market, id=listing.external_id,p=listing.price)
                    await store_gift(conn, listing.gift)
                    await query(conn, """
                      insert into market_listings(market,external_id,gift_id,price,currency,url,transferable_at,observed_at)
                      values(:m,:e,:g,:p,:c,:u,:t,:o) on conflict(market,external_id) do update set
                      price=excluded.price,active=true,observed_at=excluded.observed_at,transferable_at=excluded.transferable_at
                    """, m=listing.market,e=listing.external_id,g=listing.gift.id,p=listing.price,c=listing.currency,
                                u=listing.url,t=listing.transferable_at,o=now)
        async with db.transaction() as conn:
            persisted = await rows(conn, """
              select g.*,s.market,s.external_id,s.price,s.currency,s.sold_at from market_sales s
              join gifts g on g.id=s.gift_id where s.sold_at>now()-interval '30 days'
              order by s.sold_at desc limit 10000
            """)
        for row in persisted:
            gift = Gift(**{field: row[field] for field in Gift.__dataclass_fields__})
            sales_data.append(Sale(row["market"], row["external_id"], gift, row["price"], row["currency"], row["sold_at"]))
        by_name = {adapter.name: adapter for adapter in adapters}
        for listing in current[:20]:
            if listing.market not in fees:
                continue
            adapter = by_name[listing.market]
            try:
                offer = await adapter.offer(listing)
                if offer:
                    async with db.transaction() as conn:
                        await query(conn, """
                            insert into market_offers values(:m,:id,cast(:d as jsonb),now())
                            on conflict(market,external_id) do update set details=excluded.details,observed_at=now()
                        """, m=listing.market,id=offer.id,d=json.dumps(asdict(offer),default=str))
                options = candidates(listing, sales_data, current, offer, fees, now)
                if not options:
                    continue
                found = await adapter.get_listing(listing.external_id)
            except (httpx.HTTPError, ValueError, KeyError, TypeError, ArithmeticError):
                continue
            async with db.transaction() as conn:
                if found is None or found.price != listing.price or found.gift != listing.gift:
                    await query(conn, """
                       update trading_signals set state=:state where listing_market=:m and listing_id=:id
                    """, state="unavailable" if found is None else "stale", m=listing.market, id=listing.external_id)
                    continue
                for strategy, state, details in options:
                    identity = sha256(f"{listing.market}/{listing.external_id}/{strategy}/{details['exit_market']}".encode()).hexdigest()
                    await query(conn, """
                        insert into trading_signals values(:id,:m,:listing,:strategy,cast(:d as jsonb),:state,now())
                        on conflict(id) do update set details=excluded.details,state=excluded.state,observed_at=now()
                    """, id=identity, m=listing.market, listing=listing.external_id, strategy=strategy,
                                d=json.dumps(details, default=str, ensure_ascii=False), state=state)
        async with db.transaction() as conn:
            await query(conn, """update trading_signals set state='stale'
                where state in ('active_offer','potential','risky') and observed_at<now()-interval '3 minutes'""")


async def requested_checks(db, adapters):
    by_name = {adapter.name: adapter for adapter in adapters}
    async with db.transaction() as conn:
        requests = await rows(conn, """
            select k.key,t.listing_market,t.listing_id,t.strategy,t.details from system_settings k
            join trading_signals t on t.id=substring(k.key from 8)
            where k.key like 'verify:%' and k.value->>'status'='queued'
            order by k.value->>'checked_at' limit 8
        """)
    for request in requests:
        adapter = by_name.get(request["listing_market"])
        status = "source_unavailable"
        if adapter:
            try:
                listing = await adapter.get_listing(request["listing_id"])
                if not listing:
                    status = "unavailable"
                elif listing.price != Decimal(request["details"]["purchase"]):
                    status = "stale"
                elif request["strategy"] == "instant":
                    offer = await adapter.offer(listing)
                    status = "available" if offer and offer.id == request["details"]["offer_id"] and (
                        offer.price == Decimal(request["details"]["sale"])) else "stale"
                else:
                    status = "available"
            except (httpx.HTTPError, ValueError, KeyError, TypeError, ArithmeticError):
                pass
        async with db.transaction() as conn:
            if status in {"unavailable", "stale"}:
                await query(conn, "update trading_signals set state=:s where id=substring(:key from 8)",
                            s=status, key=request["key"])
            await query(conn, "update system_settings set value=jsonb_build_object('status',cast(:s as text),'checked_at',now()) where key=:key",
                        s=status, key=request["key"])
