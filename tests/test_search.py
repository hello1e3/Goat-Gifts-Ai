from datetime import UTC, datetime
from decimal import Decimal
import pytest
from goat.search import filtered, number_kind
from goat.terminal import Preferences


@pytest.mark.parametrize('number,kind',[(1,'low'),(100,'low'),(777,'repeat'),(12321,'palindrome'),(4500,'round'),(12345,'regular')])
def test_special_numbers(number,kind):
    assert number_kind(number)==kind


def item(id_,cost='10',roi='10',profit='1',number=154):
    return dict(id=id_,strategy='floor',observed_at=datetime.now(UTC),details=dict(
        title='Vice Cream',collection='Vice Cream',market='Portals',cost=cost,roi=roi,
        profit=profit,number=number,liquidity=0))


def test_filters_apply_before_pagination_and_require_real_estimates():
    prefs=Preferences(capital=20,roi=5,min_profit='.5',collections=['vice cream'])
    values=[item('ok'),item('expensive',cost='21'),item('lowroi',roi='4'),
            item('loss',profit='-.1'),item('unknown',roi=None,profit=None)]
    assert [v['id'] for v in filtered(values,prefs,thresholds=True)]==['ok']
    assert not filtered(values,prefs,strategy='instant',thresholds=True)
    assert not filtered(values,prefs,market='MRKT',thresholds=True)


def test_exact_hash_search_and_rare_filters():
    values=[item('rare',number=77),item('regular',number=1777),item('other',number=888)]
    prefs=Preferences(capital=20)
    assert [v['id'] for v in filtered(values,prefs,q='cream #77')]==['rare']
    assert {v['id'] for v in filtered(values,prefs,number='rare')}=={'rare','other'}
    assert not filtered(values,prefs,q='not a gift')

