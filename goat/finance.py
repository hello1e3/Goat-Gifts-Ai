from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

Money = Decimal
Currency = Literal["TON", "GRAM"]
ZERO = Decimal(0)
ONE = Decimal(1)
HUNDRED = Decimal(100)


class Calculation(BaseModel):
    currency: Currency = "TON"
    buy: Decimal = Field(gt=0, max_digits=24, decimal_places=9)
    sell: Decimal = Field(ge=0, max_digits=24, decimal_places=9)
    buy_fee: Decimal = Field(default=ZERO, ge=0, lt=ONE)
    sell_fee: Decimal = Field(default=ZERO, ge=0, lt=ONE)
    transfer: Decimal = Field(default=ZERO, ge=0)
    network: Decimal = Field(default=ZERO, ge=0)
    conversion: Decimal = Field(default=ZERO, ge=0)
    extra_sell: Decimal = Field(default=ZERO, ge=0)
    fx: Decimal = Field(default=ONE, gt=0)
    fx_shock_pct: Decimal = Field(default=ZERO, gt=-100, le=1000)
    target_roi: Decimal = Field(default=Decimal(5), ge=0, le=1000)
    conservative_sell: Decimal | None = Field(default=None, ge=0)


def calculate(p: Calculation):
    fixed = p.transfer + p.network + p.conversion
    cost = p.buy * (ONE + p.buy_fee) + fixed
    sell_factor = (ONE - p.sell_fee) * p.fx

    def net(sale):
        return sale * sell_factor - p.extra_sell

    proceeds = net(p.sell)
    profit = proceeds - cost
    return {
        "currency": p.currency, "cost": cost, "proceeds": proceeds, "profit": profit,
        "roi": profit * HUNDRED / cost, "transfer": p.transfer,
        "buy_commission": p.buy * p.buy_fee, "sell_commission": p.sell * p.sell_fee * p.fx,
        "break_even": (cost + p.extra_sell) / sell_factor,
        "max_buy": max(ZERO, ((proceeds / (ONE + p.target_roi / HUNDRED)) - fixed) / (ONE + p.buy_fee)),
        "conservative_profit": net(p.conservative_sell if p.conservative_sell is not None
                                   else p.sell * Decimal(".95")) - cost,
        "fx_impact": p.sell * sell_factor * p.fx_shock_pct / HUNDRED,
        "downside": {str(pct): net(p.sell * (ONE - Decimal(pct) / HUNDRED)) - cost
                     for pct in (5, 10, 20)},
    }
