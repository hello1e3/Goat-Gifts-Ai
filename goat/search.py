"""Shared search semantics for catalog and opportunities."""
from decimal import Decimal, InvalidOperation


def number_kind(number):
    n = int(number)
    text = str(n)
    if n <= 100: return 'low'
    if len(set(text)) == 1: return 'repeat'
    if text == text[::-1]: return 'palindrome'
    if n % 100 == 0: return 'round'
    return 'regular'


def matches(item, prefs, q='', strategy='all', market='all', number='all', thresholds=False):
    d = item['details']
    def amount(key):
        try: return Decimal(str(d[key])) if d.get(key) is not None else None
        except (InvalidOperation, ValueError): return None
    cost = amount('cost')
    if cost is None or cost > prefs.budget: return False
    if prefs.markets and d.get('market') not in prefs.markets: return False
    if prefs.strategies and item['strategy'] not in prefs.strategies: return False
    if prefs.collections and str(d.get('collection','')).casefold() not in {c.casefold() for c in prefs.collections}: return False
    if strategy != 'all' and item['strategy'] != strategy: return False
    if market != 'all' and d.get('market') != market: return False
    kind = number_kind(d.get('number') or 0)
    if number == 'rare' and kind == 'regular': return False
    if number not in ('all','rare') and kind != number: return False
    text = ' '.join(str(d.get(k,'')) for k in ('title','collection','model','backdrop','symbol','number')).casefold()
    for token in q.strip().casefold().split():
        if token.startswith('#') and token[1:].isdigit():
            if str(d.get('number')) != token[1:]: return False
        elif token not in text: return False
    if thresholds:
        for key, minimum in (('roi',prefs.roi),('profit',prefs.min_profit),('liquidity',prefs.liquidity)):
            value = amount(key)
            if value is None:
                if minimum > 0: return False
            elif value < minimum: return False
    return True


def filtered(items, prefs, q='', strategy='all', market='all', number='all', sort='latest', thresholds=False):
    result = [i for i in items if matches(i,prefs,q,strategy,market,number,thresholds)]
    if sort in ('roi','cost'):
        result.sort(key=lambda i: (i['details'].get(sort) is None,
            (-1 if sort=='roi' else 1)*Decimal(str(i['details'].get(sort) or 0)),i['id']))
    else:
        result.sort(key=lambda i:(str(i['observed_at']),i['id']),reverse=True)
    return result

