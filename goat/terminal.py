from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from typing import Literal

from pydantic import BaseModel, Field

from goat.db import one, query, rows, scalar
from goat.finance import Calculation, Currency, calculate
from goat.market import Gift, Listing, PORTALS_DEFAULT_FEES, listing_analysis


class Preferences(BaseModel):
    capital: Decimal = Field(default=Decimal(100), gt=0, le=1_000_000)
    roi: Decimal = Field(default=Decimal(5), ge=0, le=1000)
    max_cost: Decimal = Field(default=Decimal(1_000_000), gt=0, le=1_000_000)
    limit_per_gift: bool = False
    min_profit: Decimal = Field(default=Decimal(0), ge=0)
    collections: list[str] = Field(default_factory=list, max_length=30)
    markets: list[Literal["Portals", "MRKT"]] = Field(default_factory=list, max_length=2)
    strategies: list[Literal["instant", "cross", "floor"]] = Field(default_factory=list, max_length=3)
    liquidity: int = Field(default=0, ge=0, le=10000)
    max_age_seconds: int = Field(default=180, ge=15, le=180)
    frequency_minutes: int = Field(default=15, ge=1, le=1440)
    quiet_start: int | None = Field(default=None, ge=0, le=23)
    quiet_end: int | None = Field(default=None, ge=0, le=23)
    signals: bool = True
    marketing: bool = False
    reminders: bool = True

    @property
    def budget(self):
        return min(self.capital,self.max_cost) if self.limit_per_gift else self.capital


class PortfolioInput(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    cost: Decimal = Field(gt=0, max_digits=24, decimal_places=9)
    currency: Currency = "TON"
    virtual: bool = False


class CloseInput(BaseModel):
    proceeds: Decimal = Field(ge=0, max_digits=24, decimal_places=9)


async def signals(conn, prefs: Preferences, history=False):
    if history:
        return await rows(conn, """
            select * from trading_signals where observed_at>=now()-interval '30 days'
            order by observed_at desc limit 100
        """)
    return await rows(conn, """
        select t.* from trading_signals t join system_health h on h.component=t.listing_market
        join system_health e on e.component=t.details->>'exit_market'
        cross join lateral (select case when t.details->>'currency' in ('TON','GRAM') then 1 else (
            select rate from currency_rates where base=t.details->>'currency' and quote='TON'
            and observed_at>now()-interval '24 hours' order by observed_at desc limit 1
        ) end as rate) fx
        where h.status='online' and e.status='online'
          and t.state in ('active_offer','potential','risky')
          and t.observed_at>now()-make_interval(secs=>:age)
          and (details->>'roi')::numeric>=:roi
          and (details->>'liquidity')::integer>=:liquidity
          and (details->>'profit')::numeric * fx.rate >= :profit
          and (details->>'cost')::numeric * fx.rate <= :cost
          and (cardinality(cast(:markets as text[]))=0 or listing_market=any(cast(:markets as text[])))
          and (cardinality(cast(:strategies as text[]))=0 or strategy=any(cast(:strategies as text[])))
          and (cardinality(cast(:collections as text[]))=0 or details->>'collection'=any(cast(:collections as text[])))
        order by (details->>'profit')::numeric desc, t.id
    """, age=prefs.max_age_seconds, roi=prefs.roi, liquidity=prefs.liquidity, profit=prefs.min_profit,
        cost=prefs.budget, markets=prefs.markets, strategies=prefs.strategies,
        collections=prefs.collections)


async def catalog(conn, search="", offset=0, limit=60):
    return await rows(conn, """
        select 'catalog:'||l.market||':'||l.external_id as id,'floor' as strategy,
               'listed' as state,l.observed_at,a.details
        from market_listings l join gifts g on g.id=l.gift_id
        join market_analysis a on a.market=l.market and a.external_id=l.external_id
        where l.market in ('Portals','MRKT') and l.active
          and l.observed_at>now()-interval '15 minutes'
          and (:search='' or concat_ws(' ',g.title,g.collection,g.model,g.backdrop,g.symbol,g.number)
               ilike '%'||:search||'%')
        order by l.external_id limit :limit offset :offset
    """,search=search.strip()[:80],limit=limit,offset=offset)

def demo():
    dataset = [
        ("snoopdogg-223555", "Snoop Dogg", 223555, "Old School", "Deep Cyan", "Eikthyrnir",
         "18.40", "20.30", "floor", "Portals", 14),
        ("plushpepe-1", "Plush Pepe", 1, "Pumpkin", "Onyx Black", "Illuminati",
         "12.00", "13.35", "floor", "Portals", 23),
        ("durovscap-1", "Durov’s Cap", 1, "Shadow", "Shamrock Green", "Ram of Amun",
         "27.60", "30.10", "instant", "Portals", 18),
    ]
    items = []
    now = datetime.now(UTC)
    for id_, name, number, model, backdrop, symbol, buy, sell, strategy, market, liquidity in dataset:
        sale = Decimal(sell)
        calc = calculate(Calculation(buy=buy, sell=sell, sell_fee=Decimal(".025"),
                                     transfer=Decimal(".15")))
        items.append({"id": id_, "strategy": strategy, "state": "demo", "observed_at": now, "details": {
            "title": name, "collection": name, "model": model, "number": number, "backdrop": backdrop,
            "symbol": symbol, "currency": "GRAM", "purchase": buy, "sale": sale, "market": market,
            "exit_market": ("Portals" if market == "MRKT" else market), "liquidity": liquidity,
            "telegram_slug": id_, "risk": "Изображение и атрибуты из Telegram. Цены и сделки учебные.",
            "history": [{"date": now-timedelta(days=i), "price": sale + Decimal(i % 4 - 2) / 4}
                        for i in reversed(range(14))], **calc,
        }})
    return items


async def portfolios(conn, uid):
    return await rows(conn, """
        select i.*,p.virtual,t.available_at,t.scenario,v.estimate,v.sale_min,v.sale_max,v.liquidity
        from portfolio_items i join portfolios p on p.id=i.portfolio_id
        left join paper_trades t on t.item_id=i.id
        left join lateral (
          select (a.details->>'proceeds')::numeric as estimate,
                 a.details->>'conservative_sale' as sale_min,a.details->>'optimistic_sale' as sale_max,
                 a.details->>'liquidity' as liquidity
          from market_analysis a join market_listings l using(market,external_id)
          where l.gift_id=i.gift_id and l.currency=i.currency
            and a.observed_at>now()-interval '15 minutes' and a.details->>'evidence'='sales'
          order by a.observed_at desc limit 1
        ) v on true
        where p.user_id=:uid order by i.id desc
    """, uid=uid)


async def add_item(conn, uid, item: PortfolioInput, gift=None, simulation=None):
    pid = await scalar(conn, """
        insert into portfolios(user_id,virtual) values(:uid,:virtual) on conflict(user_id,virtual)
        do update set virtual=excluded.virtual returning id
    """, uid=uid, virtual=item.virtual)
    id_ = await scalar(conn, """
        insert into portfolio_items(portfolio_id,gift_id,title,cost,currency)
        values(:pid,:gift,:title,:cost,:currency) returning id
    """, pid=pid, gift=gift, **item.model_dump(exclude={"virtual"}))
    await query(conn, "insert into portfolio_transactions(item_id,kind,amount) values(:id,'buy',:cost)",
                id=id_, cost=item.cost)
    if item.virtual:
        delay = 120 if simulation is None else 120 + max(0, 20 - int(simulation["liquidity"])) * 15
        if simulation is not None and await scalar(conn, """
            select exists(select 1 from paper_trades t join portfolio_items i on i.id=t.item_id
            where i.portfolio_id=:pid and i.sold_at is null and t.scenario=:scenario)
        """, pid=pid, scenario=simulation["id"]):
            raise ValueError("В портфеле уже есть открытая позиция по этому примеру")
        await query(conn, """
            insert into paper_trades(item_id,available_at,simulated_proceeds,scenario)
            values(:id,now()+make_interval(secs=>:delay),:proceeds,:scenario)
        """, id=id_, delay=delay, proceeds=simulation["proceeds"] if simulation else None,
                    scenario=simulation["id"] if simulation else None)
    return id_


async def close_item(conn, uid, id_, proceeds):
    result = await one(conn, """
        update portfolio_items i set proceeds=coalesce(
            (select simulated_proceeds from paper_trades where item_id=i.id),:proceeds),sold_at=now()
        from portfolios p where p.user_id=:uid and p.id=i.portfolio_id and i.id=:id and i.sold_at is null
          and not exists(select 1 from paper_trades t where t.item_id=i.id and t.available_at>now())
        returning i.id,i.proceeds
    """, uid=uid, id=id_, proceeds=proceeds)
    if result is None:
        raise ValueError("Позиция недоступна или задержка симуляции ещё действует")
    await query(conn, "insert into portfolio_transactions(item_id,kind,amount) values(:id,'sell',:amount)",
                id=id_, amount=result["proceeds"])


async def save_prefs(conn, uid, prefs):
    await query(conn, """
        insert into user_settings(user_id,prefs) values(:uid,cast(:prefs as jsonb))
        on conflict(user_id) do update set prefs=excluded.prefs
    """, uid=uid, prefs=prefs.model_dump_json())

