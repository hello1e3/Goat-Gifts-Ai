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
from goat.valuation import FairValueEngine
from goat.search import number_kind


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
        tier = number if number_kind(self.number) != "regular" else "regular"
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


PORTALS_DEFAULT_FEES = {
    "buy_fee": "0", "sell_fee": ".02", "transfer": "0",
    "network": "0", "conversion": "0", "extra_sell": "0",
}


def market_reference(listing, listings):
    estimate = FairValueEngine(listings).evaluate(listing)
    return estimate['fair_value'], estimate['comparable_asks'], estimate['reference_basis']


def listing_analysis(listing, listings, fees=None, target_roi=Decimal("5"), sales=(), engine=None):
    estimate = (engine or FairValueEngine(listings, sales)).evaluate(listing)
    target = estimate['fair_value']
    calc = dict.fromkeys(('cost', 'proceeds', 'profit', 'roi', 'break_even', 'max_buy',
                         'conservative_profit', 'optimistic_profit', 'buy_commission',
                         'sell_commission', 'transfer', 'fx_impact'))
    if fees is not None:
        parameters = dict(buy=listing.price, sell=target or 0, currency=listing.currency,
                          **{key: fees[key] for key in PORTALS_DEFAULT_FEES}, target_roi=target_roi,
                          conservative_sell=estimate['conservative_sale'])
        computed = calculate(Calculation(**parameters))
        if target is not None:
            calc.update(computed)
            parameters['sell'] = estimate['optimistic_sale']
            calc['optimistic_profit'] = calculate(Calculation(**parameters))['profit']
        else:
            for key in ('cost', 'buy_commission', 'transfer', 'break_even'):
                calc[key] = computed[key]
    return {
        **asdict(listing.gift), **calc, **estimate, "floor": listing.floor,
        'currency': listing.currency, 'fees_configured': fees is not None,
        'listing_id': listing.external_id,
        "purchase": listing.price, "sale": target, "market": listing.market,
        "exit_market": listing.market, "url": listing.url,
        "transferable_at": listing.transferable_at, "history": [],
        "risk": estimate['reference_basis'] + ('. Комиссии не настроены.' if fees is None else
                 '. Сценарии основаны на распределении цен аналогов; срок продажи неизвестен.'),
    }


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
        self.webapp = token.startswith("tma ")
        self.next_call = 0.0
        self.lock = asyncio.Lock()
        self.rare_offset = 0
        self.scan_offset = 0
        self.scan_complete = False
        self.api_errors = 0
        self.sales_offset = 0
        self.collection_ids = {}
        self.enriched_at = {}

    async def get(self, path, **params):
        async with self.lock:
            for attempt in range(3):
                try:
                    return await self._get(path, **params)
                except httpx.HTTPStatusError as exc:
                    if attempt == 2 or exc.response.status_code not in (429,500,502,503,504):
                        raise
                    await asyncio.sleep(2 ** attempt)
                except httpx.TransportError:
                    self.api_errors += 1
                    if attempt == 2:
                        raise
                    await asyncio.sleep(2 ** attempt)

    async def _get(self, path, **params):
        clock = asyncio.get_running_loop().time
        await asyncio.sleep(max(0, self.next_call - clock()))
        base = "https://portal-market.com/api" if self.webapp else "https://portal-market.com/partners"
        authorization = self.token if self.webapp else f"partners {self.token}"
        response = await self.client.get(base + path, params=params, headers={
            "Authorization": authorization, "User-Agent": "GOAT-NFT/1.0",
        })
        reset = response.headers.get('Retry-After',response.headers.get("X-RateLimit-Reset", "0"))
        try:
            pause = max(1.1,min(float(reset),60)) if response.status_code == 429 else 1.1
        except ValueError:
            pause = 2
        self.next_call = clock() + pause
        if response.is_error:
            self.api_errors += 1
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
        if raw.get('collection_id'):
            self.collection_ids[gift.collection.casefold()] = raw['collection_id']
        # Open the exact Portals listing, not Telegram's read-only collectible
        # page.  The user can review the current price and complete the purchase
        # inside the official Portals mini app.
        url = "https://t.me/portals/market?startapp=gift_" + str(raw["id"])
        floor = Decimal(raw.get("floor_price") or "0")
        return Listing(self.name, raw["id"], gift, positive(raw["price"]), "GRAM", url,
                       date(raw.get("unlocks_at")), floor or None)

    async def listings(self):
        output = {}
        # Bounded work per cycle, unlimited cursor progression across cycles.
        # The market moves during offset pagination: coverage is not a snapshot.
        self.scan_complete = False
        for offset in dict.fromkeys((0, self.scan_offset)):
            result = await self.get('/nfts/search', status='listed', limit=100,
                                    offset=offset, sort_by='listed_at desc')
            page = result.get('results', [])
            for raw in page:
                if raw.get('status') == 'listed':
                    item = self.listing(raw)
                    output[item.external_id] = item
            if offset == self.scan_offset:
                if not page:
                    self.scan_complete = True
                    self.scan_offset = 0
                else:
                    self.scan_offset += len(page)
        try:
            result = await self.get('/nfts/search', status='listed', limit=100,
                offset=self.rare_offset, sort_by='external_collection_number asc')
            page = result.get('results', [])
            self.rare_offset = self.rare_offset + len(page) if page else 0
            for raw in page:
                if raw.get('status') == 'listed':
                    item = self.listing(raw)
                    output[item.external_id] = item
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            pass
        return list(output.values())

    async def get_listings(self, ids):
        found = {}
        for offset in range(0,len(ids),100):
            result = await self.get('/nfts/search', ids=','.join(ids[offset:offset+100]),
                                    status='listed',limit=100,offset=0)
            for raw in result.get('results',[]):
                if raw.get('status') == 'listed':
                    item = self.listing(raw)
                    found[item.external_id] = item
        return found

    async def comparables(self, listing):
        collection_id = self.collection_ids.get(listing.gift.collection.casefold())
        if not collection_id:
            collections = await self.get('/collections', search=listing.gift.collection, limit=100)
            match = next((c for c in collections.get('collections', [])
                          if str(c.get('name', '')).casefold() == listing.gift.collection.casefold()), None)
            if not match or not match.get('id'):
                return []
            collection_id = match['id']
            self.collection_ids[listing.gift.collection.casefold()] = collection_id
        result = await self.get('/nfts/search',collection_ids=collection_id,
            filter_by_models=listing.gift.model,
            status='listed',sort_by='price asc',limit=100,offset=0)
        return [self.listing(raw) for raw in result.get('results',[]) if raw.get('status')=='listed']

    async def search(self, text, offset=0, limit=60):
        query = text.strip()[:80]
        if not query:
            result = await self.get("/nfts/search", status="listed", limit=min(limit, 100),
                                    offset=max(0, offset), sort_by="listed_at desc")
            return [self.listing(raw) for raw in result.get("results", [])
                    if raw.get("status") == "listed"]
        collections = await self.get("/collections", search=query, limit=10)
        ids = [str(item["id"]) for item in collections.get("collections", []) if item.get("id")]
        if not ids:
            return []
        result = await self.get("/nfts/search", collection_ids=",".join(ids), status="listed",
                                limit=min(limit, 100), offset=max(0, offset), sort_by="price asc")
        return [self.listing(raw) for raw in result.get("results", []) if raw.get("status") == "listed"]

    async def sales(self):
        if self.webapp:
            # The reverse-engineered WebApp contract documents listings only.
            return []
        result = await self.get("/market/actions/", action_types="purchase", limit=20,
                                offset=self.sales_offset,period='one_month')
        self.sales_offset = (self.sales_offset + len(result['actions'])) if result['actions'] and self.sales_offset < 700 else 0
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
        if self.webapp:
            return None
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
    endpoint = "https://api.tgmrkt.io/api/v1/gifts/saling"

    def __init__(self, client: httpx.AsyncClient, token: str):
        self.client = client
        self.token = token
        self.cache: dict[str, Listing] = {}

    @staticmethod
    def field(raw, *names, default=""):
        for name in names:
            value = raw.get(name)
            if isinstance(value, dict):
                value = value.get("name") or value.get("title") or value.get("value")
            if value not in (None, ""):
                return value
        return default

    @classmethod
    def gift(cls, raw):
        collection = str(cls.field(raw, "collectionName", "collection", "name", default="MRKT Gift"))
        number = int(cls.field(raw, "number", "giftNumber", "collectionNumber", default=0))
        slug = re.sub(r"[^A-Za-z0-9]", "", collection) + f"-{number}"
        gift_id = slug.casefold() if number > 0 and re.fullmatch(
            r"[A-Za-z][A-Za-z0-9]*-[0-9]+", slug
        ) else "mrkt:" + str(cls.field(raw, "id", "giftId", default=sha256(
            json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()))
        rarity = cls.field(raw, "rarity", "rarityName", "rarityPercent", default=None)
        image = cls.field(raw, "photoUrl", "imageUrl", "previewUrl", "image", "photo", default=None)
        return Gift(
            gift_id, collection, number, str(cls.field(raw, "title", "name", default=collection)),
            str(cls.field(raw, "modelName", "model", default="—")),
            str(cls.field(raw, "backdropName", "backgroundName", "backdrop", "background", default="—")),
            str(cls.field(raw, "symbolName", "symbol", default="—")),
            str(rarity) if rarity not in (None, "") else None, safe_image(image),
        )

    @classmethod
    def listing(cls, raw):
        external_id = str(cls.field(raw, "id", "giftId", "listingId"))
        if not external_id:
            raise ValueError("MRKT listing has no id")
        amount = positive(Decimal(str(cls.field(raw, "salePrice", "price"))).scaleb(-9))
        return Listing(cls.name, external_id, cls.gift(raw), amount, "TON",
                       f"https://t.me/mrkt/app?startapp={external_id}", None)

    async def page(self, cursor=""):
        response = await self.client.post(self.endpoint, headers={
            "Authorization": self.token,
            "Referer": "https://cdn.tgmrkt.io/",
            "User-Agent": "GOAT-NFT/1.0",
        }, json={
            "collectionNames": [], "modelNames": [], "backdropNames": [], "symbolNames": [],
            "ordering": "Price", "lowToHigh": True, "maxPrice": None, "minPrice": None,
            "mintable": None, "number": None, "count": 20, "cursor": cursor,
            "query": None, "promotedFirst": False,
        })
        response.raise_for_status()
        result = response.json(parse_float=Decimal)
        if not isinstance(result, dict) or not isinstance(result.get("gifts"), list):
            raise ValueError("Invalid MRKT response")
        return result

    async def listings(self):
        output = []
        cursor = ""
        for _ in range(3):
            result = await self.page(cursor)
            page = [self.listing(raw) for raw in result["gifts"]]
            output.extend(page)
            cursor = str(result.get("cursor") or "")
            if len(result["gifts"]) < 20 or not cursor:
                break
        self.cache = {item.external_id: item for item in output}
        return output

    async def sales(self):
        # The public reverse-engineered contract documents active listings only.
        return []

    async def get_listing(self, external_id):
        return self.cache.get(external_id)

    async def offer(self, listing):
        return None


def candidates(listing, sales, listings, offer, fees, now, engine=None):
    if listing.market not in fees:
        return []
    details = listing_analysis(listing, listings, fees[listing.market], sales=sales, engine=engine)
    result = []
    if (details['evidence'] in ('sales','asks') and details['profit'] is not None
            and details['profit'] > 0 and details['conservative_profit'] > 0):
        result.append(('floor', 'potential', details))
    if offer and (not listing.transferable_at or listing.transferable_at <= now):
        settings = fees[listing.market]
        calc = calculate(Calculation(buy=listing.price, sell=offer.price, currency=listing.currency,
                         **{key: settings[key] for key in PORTALS_DEFAULT_FEES},
                         conservative_sell=offer.price))
        if calc['profit'] > 0:
            result.append(('instant', 'active_offer', {
                **details, **calc, 'sale': offer.price, 'offer_id': offer.id,
                'reference_basis': 'Действующее предложение покупки',
                'risk': 'Офер может быть исполнен другим продавцом до вашей сделки.',
                'confidence': 'high', 'evidence': 'offer',
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
                if isinstance(adapter, Portals):
                    async with db.transaction() as conn:
                        cursor = await one(conn, "select value from system_settings where key='portals_cursor'")
                    if cursor:
                        adapter.scan_offset = int(cursor['value']['offset'])
                        adapter.rare_offset = int(cursor['value'].get('rare_offset',0))
                listed = await adapter.listings()
            except (httpx.HTTPError, ValueError, KeyError, TypeError, ArithmeticError):
                async with db.transaction() as conn:
                    await health(conn, adapter.name, "unavailable")
                continue
            if isinstance(adapter,Portals) and listed:
                # Fetch a comparable group explicitly instead of relying on
                # accidental matches in a small mixed page. Rotate groups.
                key = lambda item: (item.gift.collection,item.gift.model)
                target = min(listed,key=lambda item:adapter.enriched_at.get(key(item),0))
                try:
                    extra = await adapter.comparables(target)
                    adapter.enriched_at[key(target)] = datetime.now(UTC).timestamp()
                    listed = list({item.external_id:item for item in [*listed,*extra]}.values())
                except (httpx.HTTPError,ValueError,KeyError,TypeError,ArithmeticError):
                    pass  # Keep the available listing page if enrichment fails.
            try:
                sold = await adapter.sales()
                sales_status = 'not_supported' if getattr(adapter,'webapp',False) else 'online'
            except (httpx.HTTPError,ValueError,KeyError,TypeError,ArithmeticError):
                sold = []
                sales_status = 'unavailable'
            current.extend(listed)
            async with db.transaction() as conn:
                await health(conn, adapter.name, "online")
                await health(conn, adapter.name+':sales',sales_status)
                if isinstance(adapter, Portals):
                    await query(conn, """insert into system_settings(key,value) values('portals_cursor',cast(:v as jsonb))
                        on conflict(key) do update set value=excluded.value""",
                        v=json.dumps({'offset':adapter.scan_offset,'rare_offset':adapter.rare_offset}))
                for sale in sold:
                    await store_gift(conn, sale.gift)
                    await query(conn, """insert into market_sales values(:m,:e,:g,:p,:c,:s)
                      on conflict do nothing""", m=sale.market, e=sale.external_id, g=sale.gift.id,
                                p=sale.price, c=sale.currency, s=sale.sold_at)
                payload = [dict(m=item.market,e=item.external_id,g=item.gift.id,p=str(item.price),
                    c=item.currency,u=item.url,t=item.transferable_at,o=datetime.now(UTC),
                    f=str(item.floor) if item.floor is not None else None) for item in listed]
                await query(conn,"""insert into gifts(id,collection,number,title,model,backdrop,symbol,rarity,image)
                    select id,collection,number,title,model,backdrop,symbol,rarity,image
                    from jsonb_to_recordset(cast(:data as jsonb)) as x(id text,collection text,number bigint,
                    title text,model text,backdrop text,symbol text,rarity text,image text)
                    on conflict(id) do update set image=excluded.image""",
                    data=json.dumps(list({i.gift.id:asdict(i.gift) for i in listed}.values()),default=str))
                await query(conn,"""update trading_signals t set state='stale'
                    from jsonb_to_recordset(cast(:data as jsonb)) as x(m text,e text,p numeric)
                    where t.listing_market=x.m and t.listing_id=x.e and (t.details->>'purchase')::numeric<>x.p""",
                    data=json.dumps(payload,default=str))
                await query(conn,"""insert into market_listings(market,external_id,gift_id,price,currency,url,transferable_at,observed_at,floor)
                    select m,e,g,p,c,u,t,o,f from jsonb_to_recordset(cast(:data as jsonb))
                    as x(m text,e text,g text,p numeric,c text,u text,t timestamptz,o timestamptz,f numeric)
                    on conflict(market,external_id) do update set price=excluded.price,floor=excluded.floor,
                    active=true,observed_at=excluded.observed_at,transferable_at=excluded.transferable_at,url=excluded.url""",
                    data=json.dumps(payload,default=str))
        async with db.transaction() as conn:
            persisted = await rows(conn, """
              select g.*,s.market,s.external_id,s.price,s.currency,s.sold_at from market_sales s
              join gifts g on g.id=s.gift_id where s.sold_at>now()-interval '30 days'
              order by s.sold_at desc
            """)
        for row in persisted:
            gift = Gift(**{field: row[field] for field in Gift.__dataclass_fields__})
            sales_data.append(Sale(row["market"], row["external_id"], gift, row["price"], row["currency"], row["sold_at"]))
        by_name = {adapter.name: adapter for adapter in adapters}
        async with db.transaction() as conn:
            peer_rows = await rows(conn, """select g.*, l.market,l.external_id,l.price,l.currency,
                l.url,l.transferable_at,l.floor from market_listings l join gifts g on g.id=l.gift_id
                where l.active and l.observed_at>now()-interval '15 minutes'""")
        peers = [Listing(row['market'], row['external_id'],
                 Gift(**{field: row[field] for field in Gift.__dataclass_fields__}),
                 row['price'],row['currency'],row['url'],row['transferable_at'],row['floor'])
                 for row in peer_rows]
        engine = FairValueEngine(peers, sales_data)
        diagnostics = {'analyzed': len(current), 'profitable': 0, 'insufficient': 0,
                       'asks_only': 0, 'fees_missing': 0, 'non_positive': 0,
                       'collections_in_batch': len({item.gift.collection for item in current})}
        analyses, pending, offers = [], [], []
        for listing in current:
            details = listing_analysis(listing, peers, fees.get(listing.market), engine=engine)
            diagnostics['insufficient'] += int(details['evidence'] == 'none')
            diagnostics['asks_only'] += int(details['evidence'] == 'asks')
            diagnostics['fees_missing'] += int(not details['fees_configured'])
            diagnostics['non_positive'] += int(details['profit'] is not None and details['profit'] <= 0)
            analyses.append(dict(m=listing.market,e=listing.external_id,d=details))
            if listing.market not in fees:
                continue
            adapter = by_name[listing.market]
            try:
                offer = await adapter.offer(listing)
                if offer:
                    offers.append(dict(m=listing.market,id=offer.id,d=asdict(offer)))
                options = candidates(listing,sales_data,peers,offer,fees,now,engine=engine)
                if options:
                    pending.append((listing,options))
            except (httpx.HTTPError,ValueError,KeyError,TypeError,ArithmeticError):
                continue
        verified, changes = [], []
        for adapter in adapters:
            group = [(item,options) for item,options in pending if item.market==adapter.name]
            try:
                if isinstance(adapter,Portals):
                    found_map = await adapter.get_listings([item.external_id for item,_ in group])
                else:
                    found_map = {item.external_id:await adapter.get_listing(item.external_id) for item,_ in group}
            except (httpx.HTTPError,ValueError,KeyError,TypeError,ArithmeticError):
                continue
            for listing,options in group:
                found = found_map.get(listing.external_id)
                if found is None or found.price!=listing.price or found.gift!=listing.gift:
                    changes.append(dict(m=listing.market,e=listing.external_id,s='unavailable' if found is None else 'stale'))
                    continue
                for strategy,state,details in options:
                    identity=sha256(f"{listing.market}/{listing.external_id}/{strategy}/{details['exit_market']}".encode()).hexdigest()
                    verified.append(dict(id=identity,m=listing.market,e=listing.external_id,strategy=strategy,d=details,state=state))
        diagnostics['profitable']=len(verified)
        async with db.transaction() as conn:
            await query(conn,"""insert into market_analysis(market,external_id,details)
                select m,e,d from jsonb_to_recordset(cast(:data as jsonb)) as x(m text,e text,d jsonb)
                on conflict(market,external_id) do update set details=excluded.details,observed_at=now()""",
                data=json.dumps(analyses,default=str))
            await query(conn,"""update trading_signals t set state='stale'
                from jsonb_to_recordset(cast(:data as jsonb)) as x(m text,e text)
                where t.listing_market=x.m and t.listing_id=x.e""",data=json.dumps(analyses,default=str))
            await query(conn,"""update trading_signals t set state=x.s
                from jsonb_to_recordset(cast(:data as jsonb)) as x(m text,e text,s text)
                where t.listing_market=x.m and t.listing_id=x.e""",data=json.dumps(changes))
            await query(conn,"""insert into market_offers
                select m,id,d,now() from jsonb_to_recordset(cast(:data as jsonb)) as x(m text,id text,d jsonb)
                on conflict(market,external_id) do update set details=excluded.details,observed_at=now()""",
                data=json.dumps(list({(o['m'],o['id']):o for o in offers}.values()),default=str))
            await query(conn,"""insert into trading_signals
                select id,m,e,strategy,d,state,now() from jsonb_to_recordset(cast(:data as jsonb))
                as x(id text,m text,e text,strategy text,d jsonb,state text)
                on conflict(id) do update set details=excluded.details,state=excluded.state,observed_at=now()""",
                data=json.dumps(verified,default=str))
        async with db.transaction() as conn:
            diagnostics['updated_at'] = datetime.now(UTC).isoformat()
            diagnostics['duration_seconds'] = (datetime.now(UTC) - now).total_seconds()
            diagnostics['sources'] = {a.name: {'offset': getattr(a,'scan_offset',None),
                'reached_end': getattr(a,'scan_complete',False), 'api_errors': getattr(a,'api_errors',0)}
                for a in adapters}
            await query(conn, """insert into system_settings(key,value) values('market_diagnostics',cast(:v as jsonb))
                on conflict(key) do update set value=excluded.value""",v=json.dumps(diagnostics))
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

