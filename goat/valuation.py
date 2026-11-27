"""Evidence-based valuations. Asking prices are never labelled completed sales."""
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal


def quantile(values, fraction):
    ordered = sorted(values)
    position = Decimal(len(ordered) - 1) * Decimal(str(fraction))
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def clean(samples):
    if len(samples) < 4:
        return samples
    values = [price for price, _ in samples]
    q1, q3 = quantile(values, '.25'), quantile(values, '.75')
    spread = q3 - q1
    if not spread:
        return [(price, weight) for price, weight in samples if price == q1]
    return [(price, weight) for price, weight in samples
            if q1 - Decimal('1.5') * spread <= price <= q3 + Decimal('1.5') * spread]


def weighted(samples, fraction):
    threshold = sum(weight for _, weight in samples) * Decimal(str(fraction))
    total = Decimal(0)
    for price, weight in sorted(samples):
        total += weight
        if total >= threshold:
            return price
    return samples[-1][0]


class FairValueEngine:
    def __init__(self, listings=(), sales=(), now=None):
        self.now = now or datetime.now(UTC)
        self.asks, self.sales = defaultdict(list), defaultdict(list)
        for item in listings:
            self.asks[(item.market, item.currency, item.gift.comparable)].append(item)
        for item in sales:
            if self.now - timedelta(days=30) <= item.sold_at <= self.now:
                self.sales[(item.market, item.currency, item.gift.comparable)].append(item)

    def evaluate(self, listing):
        key = (listing.market, listing.currency, listing.gift.comparable)
        sales = {s.external_id: s for s in self.sales[key]}.values()
        asks = {a.external_id: a for a in self.asks[key]
                if a.external_id != listing.external_id and a.gift.id != listing.gift.id}.values()
        sold = []
        for sale in sales:
            age = Decimal(str((self.now - sale.sold_at).total_seconds())) / Decimal(86400)
            weight = (Decimal(1) / (Decimal(1) + age)).quantize(Decimal('.000000001'))
            sold.append((sale.price, weight))
        sold = clean(sold)
        offered = clean([(a.price, Decimal(1)) for a in asks])
        result = dict(fair_value=None, conservative_sale=None, optimistic_sale=None,
                      confidence='insufficient', reference_basis='Недостаточно сопоставимых данных',
                      sales_count=len(sold), comparable_asks=len(offered),
                      liquidity=len(sold), estimated_days_to_sell=None, evidence='none')
        if len(sold) >= 3:
            result.update(fair_value=weighted(sold, '.5'), conservative_sale=weighted(sold, '.25'),
                          optimistic_sale=weighted(sold, '.75'), evidence='sales',
                          confidence='medium' if len(sold) < 10 else 'high',
                          reference_basis='Недавние продажи с совпадающими характеристиками')
        elif len(offered) >= 3:
            result.update(fair_value=weighted(offered, '.5'), conservative_sale=weighted(offered, '.25'),
                          optimistic_sale=weighted(offered, '.75'), evidence='asks', confidence='low',
                          reference_basis='Цены аналогичных объявлений; цена продажи не подтверждена')
        return result

