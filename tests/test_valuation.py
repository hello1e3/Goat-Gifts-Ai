from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from goat.market import Gift, Listing, Sale, listing_analysis, candidates, Portals
from goat.valuation import FairValueEngine

FEES = dict(buy_fee='.01',sell_fee='.025',transfer='0',network='.1',conversion='0',extra_sell='.2')


def listing(price='129'):
    return Listing('Portals','subject',Gift('locket-154','Lucky Locket',154,'Lucky Locket',
        'Rare Model','Black','Star','1|2|3',None),Decimal(price),'GRAM',None,None,Decimal('16'))


def test_lucky_locket_collection_floor_is_not_a_valuation():
    item = listing()
    peers = [replace(item,external_id=str(i),price=Decimal('16'),gift=replace(item.gift,
             id=f'other-{i}',model='Common Model',number=151+i)) for i in range(5)]
    d = listing_analysis(item,peers,FEES)
    assert d['fair_value'] is None and d['profit'] is None
    assert d['confidence'] == 'insufficient'
    assert d['cost'] == Decimal('130.39')


def test_sales_scenarios_and_outlier_do_not_use_collection_floor():
    item, now = listing(), datetime.now(UTC)
    sales = [Sale('Portals',str(i),replace(item.gift,id=f'other-{i}',number=164+i),
                  Decimal(price),'GRAM',now) for i,price in enumerate(['150','151','152','153','9999'])]
    d = listing_analysis(item,[],FEES,sales=sales)
    assert d['fair_value'] == 151
    assert d['sales_count'] == 4
    assert d['conservative_profit'] == Decimal('15.66')
    assert d['conservative_profit'] <= d['profit'] <= d['optimistic_profit']


def test_missing_fees_cannot_produce_profit():
    assert listing_analysis(listing(),[])['profit'] is None
    assert not listing_analysis(listing(),[])['fees_configured']


def test_wrong_currency_old_sales_and_different_background_are_excluded():
    item, now = listing(), datetime.now(UTC)
    sales = [Sale('Portals',str(i),replace(item.gift,backdrop='White'),Decimal(200),'GRAM',now)
             for i in range(4)]
    sales += [Sale('Portals','old',item.gift,Decimal(200),'GRAM',now-timedelta(days=31)),
              Sale('Portals','currency',item.gift,Decimal(200),'TON',now)]
    assert FairValueEngine([],sales).evaluate(item)['fair_value'] is None


@pytest.mark.parametrize('budget',[10,50,100,200,500])
def test_budget_is_an_upper_bound_including_fees(budget):
    from goat.finance import Calculation, calculate
    prices = [Decimal('5'),Decimal('30'),Decimal('100'),Decimal('190'),Decimal('499')]
    affordable = [p for p in prices if calculate(Calculation(buy=p,sell=p,buy_fee='.01',network='.1'))['cost'] <= budget]
    assert all(p*Decimal('1.01')+Decimal('.1') <= budget for p in affordable)
    assert Decimal('5') in affordable
    assert Decimal('499') not in affordable


async def test_portals_cursor_advances_by_actual_page_size_and_deduplicates():
    import httpx
    item = listing()
    async with httpx.AsyncClient() as client:
        adapter = Portals(client,'tma test')
        calls=[]
        async def get(path,**params):
            calls.append(params['offset'])
            return {'results':[{'id':str(i),'status':'listed'} for i in range(params['offset'],params['offset']+20)] if params['offset'] < 60 else []}
        adapter.get=get
        adapter.listing=lambda raw:replace(item,external_id=raw['id'])
        for _ in range(4):
            batch=await adapter.listings()
            assert len(batch)==len({v.external_id for v in batch})
        assert 20 in calls and 40 in calls and 60 in calls
        assert adapter.scan_offset==0 and adapter.scan_complete


def test_webapp_without_sales_produces_preliminary_opportunity():
    item=listing('5')
    peers=[replace(item,external_id=str(i),price=Decimal(price),
            gift=replace(item.gift,id=f'locket-{155+i}',number=155+i,symbol=f'Symbol {i}'))
            for i,price in enumerate(('8','9','10','11'))]
    found=candidates(item,[],peers,None,{'Portals':FEES},datetime.now(UTC))
    assert len(found)==1
    d=found[0][2]
    assert d['evidence']=='asks' and d['confidence']=='low'
    assert d['comparison_match']=='model_backdrop' and d['liquidity']==0
    assert d['conservative_sale']==8 and d['conservative_profit']>0


def test_cheaper_competitor_prevents_false_positive_even_when_outlier():
    item=listing('5')
    peers=[replace(item,external_id=str(i),price=Decimal(price),
            gift=replace(item.gift,id=f'locket-{155+i}',number=155+i))
            for i,price in enumerate(('1','10','10','10','10','10'))]
    d=listing_analysis(item,peers,FEES)
    assert d['conservative_sale']==1
    assert candidates(item,[],peers,None,{'Portals':FEES},datetime.now(UTC))==[]


def test_model_baseline_handles_sparse_backgrounds_without_inventing_premium():
    item=listing('5')
    peers=[replace(item,external_id=str(i),price=Decimal(price),
            gift=replace(item.gift,id=f'other-{i}',number=155+i,backdrop=f'Background {i}',symbol=f'Symbol {i}'))
            for i,price in enumerate(('8','9','10','11'))]
    d=listing_analysis(item,peers,FEES)
    assert d['comparison_match']=='model'
    assert d['evidence']=='asks' and d['confidence']=='low'
    assert d['conservative_sale']==8
    assert d['profit']>0 and d['conservative_profit']>0
    assert candidates(item,[],peers,None,{'Portals':FEES},datetime.now(UTC))


async def test_portals_resolves_missing_collection_and_loads_model_peers():
    import httpx
    async with httpx.AsyncClient() as client:
        adapter=Portals(client,'tma test')
        calls=[]
        async def get(path,**params):
            calls.append((path,params))
            if path=='/collections':
                return {'collections':[{'id':'wrong','name':'Lucky Locket Extra'},{'id':'right','name':'Lucky Locket'}]}
            return {'results':[]}
        adapter.get=get
        await adapter.comparables(listing())
        assert calls[1][1]['collection_ids']=='right'
        assert calls[1][1]['filter_by_models']=='Rare Model'
        assert 'filter_by_backdrops' not in calls[1][1]
        await adapter.comparables(listing())
        assert len([call for call in calls if call[0]=='/collections'])==1


def test_special_serial_gets_model_baseline_without_number_premium():
    item=replace(listing('5'),gift=replace(listing().gift,number=11))
    peers=[replace(item,external_id=str(i),price=Decimal(price),
            gift=replace(item.gift,id=f'ordinary-{i}',number=164+i))
            for i,price in enumerate(('8','9','10','11'))]
    d=listing_analysis(item,peers,FEES)
    assert d['comparison_match']=='model_without_number_premium'
    assert d['conservative_sale']==8 and d['confidence']=='low'
    assert d['conservative_profit']>0


async def test_portals_batch_verification_splits_requests_without_per_lot_calls():
    import httpx
    async with httpx.AsyncClient() as client:
        adapter=Portals(client,'tma test')
        calls=[]
        async def get(path,**params):
            calls.append(params)
            return {'results':[]}
        adapter.get=get
        assert await adapter.get_listings([str(i) for i in range(205)])=={}
        assert [len(c['ids'].split(',')) for c in calls]==[100,100,5]
        assert all(c['status']=='listed' for c in calls)

