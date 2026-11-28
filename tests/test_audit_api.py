import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
import json
from uuid import uuid4

import httpx

from goat.accounts import grant, register
from goat.api import create_api
from goat.config import Config
from goat.db import query
from goat.market import Gift, Listing, listing_analysis, store_gift
from goat.finance import Calculation, calculate
from goat.terminal import Preferences, signals, save_prefs
from test_core import TOKEN, init_data


@asynccontextmanager
async def lifespan(app):
    yield


async def seed(conn,price='5'):
    uid=str(uuid4())
    gift=Gift('gift-'+uid,'Test',154,'Test','M','B','S','1|1|1',None)
    item=Listing('Portals',uid,gift,Decimal(price),'GRAM',None,None)
    await store_gift(conn,gift)
    await query(conn,"""insert into market_listings(market,external_id,gift_id,price,currency,observed_at)
        values('Portals',:id,:g,:p,'GRAM',now())""",id=uid,g=gift.id,p=item.price)
    fees=dict(buy_fee='0',sell_fee='.02',transfer='0',network='0',conversion='0',extra_sell='0')
    details=listing_analysis(item,[],fees)
    await query(conn,"insert into market_analysis(market,external_id,details) values('Portals',:id,cast(:d as jsonb))",
                id=uid,d=json.dumps(details,default=str))
    return item


async def seed_mrkt(conn, price='13'):
    external_id='mrkt-listing-'+str(uuid4())
    gift=Gift('mrkt-gift-'+external_id,'Plush Pepe',77,'Plush Pepe','Green','Blue','Star','1|2|3',None)
    item=Listing('MRKT',external_id,gift,Decimal(price),'TON',
                 f'https://t.me/mrkt/app?startapp={external_id}',None)
    await store_gift(conn,gift)
    await query(conn,"""insert into market_listings(market,external_id,gift_id,price,currency,url,observed_at)
        values('MRKT',:id,:g,:p,'TON',:u,now())""",id=external_id,g=gift.id,p=item.price,u=item.url)
    fees=dict(buy_fee='0',sell_fee='.02',transfer='0',network='0',conversion='0',extra_sell='0')
    details=listing_analysis(item,[],fees)
    await query(conn,"insert into market_analysis(market,external_id,details) values('MRKT',:id,cast(:d as jsonb))",
                id=external_id,d=json.dumps(details,default=str))
    return item


async def test_mrkt_catalog_filter_and_verification(db):
    async with db.transaction() as conn:
        await register(conn,1,None,'Test')
        await grant(conn,1,86400,'trial')
        await save_prefs(conn,1,Preferences(capital=20,roi=0))
        await seed(conn,'5')
        target=await seed_mrkt(conn)
    app=create_api(db,Config(bot_token=TOKEN,admin_ids='1'),lifespan)
    class Adapter:
        async def get_listing(self, external_id):
            return target if external_id==target.external_id else None
    app.state.market_adapters={'MRKT':Adapter()}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        headers={'Authorization':'tma '+init_data(1)}
        result=(await client.get('/api/catalog?market=MRKT',headers=headers)).json()
        assert result['total']==1
        assert result['items'][0]['id']==f'catalog:MRKT:{target.external_id}'
        verified=await client.post('/api/signals/'+result['items'][0]['id']+'/verify',headers=headers)
        assert verified.status_code==200
        assert verified.json()['item']['details']['market']=='MRKT'
        assert verified.json()['item']['details']['url'].startswith('https://t.me/mrkt/app?startapp=')


async def test_catalog_sessions_recheck_and_portfolio_isolation(db):
    async with db.transaction() as conn:
        for uid in (1,2):
            await register(conn,uid,None,'Test')
            await grant(conn,uid,86400,'trial')
            await save_prefs(conn,uid,Preferences(roi=0))
        items=[await seed(conn) for _ in range(43)]
    app=create_api(db,Config(bot_token=TOKEN,admin_ids='1'),lifespan)
    class Adapter:
        error=False
        missing=False
        async def get_listing(self,id_):
            if self.error:
                raise httpx.ConnectError('test outage')
            return None if self.missing else replace(next(x for x in items if x.external_id==id_),price=Decimal('6'))
    adapter=Adapter()
    app.state.market_adapters={'Portals':adapter}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        a,b=({'Authorization':'tma '+init_data(uid)} for uid in (1,2))
        first=(await client.get('/api/catalog?limit=20',headers=a)).json()
        assert first['total']==43 and len(first['items'])==20 and first['has_more']
        async with db.transaction() as conn:
            await seed(conn)
        second=(await client.get('/api/catalog',params={'limit':20,'offset':20,'session':first['session']},headers=a)).json()
        assert second['total']==43
        assert not {i['id'] for i in first['items']} & {i['id'] for i in second['items']}
        assert (await client.get('/api/catalog',params={'session':first['session']},headers=b)).status_code==409
        id_=first['items'][0]['id']
        fresh=await client.post('/api/signals/'+id_+'/verify',headers=a)
        assert fresh.status_code==200 and Decimal(str(fresh.json()['item']['details']['purchase']))==6
        adapter.error=True
        assert (await client.post('/api/signals/'+id_+'/verify',headers=a)).status_code==503
        adapter.error=False
        added=await client.post('/api/portfolio/from-signal/'+id_,headers=a)
        assert added.status_code==201
        pid=added.json()['id']
        assert (await client.patch(f'/api/portfolio/{pid}',json={'title':'Corrected','cost':'7','currency':'GRAM'},headers=a)).status_code==204
        results=await asyncio.gather(client.get('/api/portfolio',headers=a),client.get('/api/portfolio',headers=b))
        assert len(results[0].json())==1 and results[1].json()==[]
        assert (await client.delete(f'/api/portfolio/{pid}',headers=b)).status_code==404
        assert (await client.post(f'/api/portfolio/{pid}/close',json={'proceeds':'9'},headers=a)).status_code==204
        history=(await client.get('/api/portfolio/history',headers=a)).json()
        assert {entry['kind'] for entry in history}=={'buy','sell','cost_correction'}
        adapter.missing=True
        result=(await client.post('/api/signals/'+id_+'/verify',headers=a)).json()
        assert result['status']=='unavailable'
        assert (await client.get('/api/admin/diagnostics',headers=b)).status_code==403


async def test_database_budget_filters_include_all_affordable_prices(db):
    async with db.transaction() as conn:
        await query(conn,"insert into system_health(component,status) values('Portals','online')")
        for price in ('5','30','100','190','499'):
            item=await seed(conn,price)
            d=calculate(Calculation(buy=price,sell=Decimal(price)*Decimal('1.2'),
                                   buy_fee='.01',sell_fee='.02',network='.1'))
            d.update(market='Portals',exit_market='Portals',liquidity=3,purchase=price)
            await query(conn,"""insert into trading_signals values(:id,'Portals',:id,'floor',cast(:d as jsonb),'potential',now())""",
                        id=item.external_id,d=json.dumps(d,default=str))
        expected={10:{'5'},50:{'5','30'},100:{'5','30'},200:{'5','30','100','190'},500:{'5','30','100','190'}}
        for budget,prices in expected.items():
            feed=await signals(conn,Preferences(capital=budget,max_cost=100))
            assert {row['details']['purchase'] for row in feed}==prices
        limited=await signals(conn,Preferences(capital=200,max_cost=50,limit_per_gift=True))
        assert {row['details']['purchase'] for row in limited}=={'5','30'}


async def test_webapp_scan_reaches_profitable_tab_without_sales(db):
    from goat.market import scan
    from test_valuation import listing
    target=listing('5')
    peers=[replace(target,external_id=str(i),price=Decimal(price),
        gift=replace(target.gift,id=f'locket-{155+i}',number=155+i,symbol=f'Symbol {i}'))
        for i,price in enumerate(('8','9','10','11'))]
    class WebApp:
        name='Portals'
        webapp=True
        async def listings(self): return [target,*peers]
        async def sales(self): return []
        async def offer(self,item): return None
        async def get_listing(self,id_): return next(x for x in [target,*peers] if x.external_id==id_)
    await scan(db,[WebApp()],{})
    async with db.transaction() as conn:
        found=await signals(conn,Preferences(capital=20))
        assert len(found)==1
        assert found[0]['listing_id']==target.external_id
        assert found[0]['details']['evidence']=='asks'
        assert Decimal(found[0]['details']['profit'])>0


async def test_opening_gift_enriches_model_and_updates_profitable_tab(db):
    from goat.market import Portals
    async with db.transaction() as conn:
        await register(conn,1,None,'Test')
        await grant(conn,1,86400,'trial')
        await query(conn,"insert into system_health(component,status) values('Portals','online')")
        target=await seed(conn)
    peers=[replace(target,external_id=str(i),price=Decimal(price),
        gift=replace(target.gift,id=f'peer-{i}',number=155+i,backdrop=f'B{i}',symbol=f'S{i}'))
        for i,price in enumerate(('8','9','10','11'))]
    app=create_api(db,Config(bot_token=TOKEN,admin_ids='1'),lifespan)
    async with httpx.AsyncClient() as upstream:
        adapter=Portals(upstream,'tma test')
        async def get_listing(id_): return target
        async def comparables(item): return peers
        adapter.get_listing=get_listing
        adapter.comparables=comparables
        app.state.market_adapters={'Portals':adapter}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            result=await client.post(f'/api/signals/catalog:Portals:{target.external_id}/verify',
                headers={'Authorization':'tma '+init_data(1)})
            assert result.status_code==200
            details=result.json()['item']['details']
            assert details['comparison_match']=='model' and Decimal(details['profit'])>0
            async with db.transaction() as conn:
                feed=await signals(conn,Preferences(capital=20))
                assert len(feed)==1 and feed[0]['listing_id']==target.external_id


async def test_catalog_honors_budget_roi_collection_and_number_on_server(db):
    async with db.transaction() as conn:
        await register(conn,1,None,'Test')
        await grant(conn,1,86400,'trial')
        await save_prefs(conn,1,Preferences(capital=20,roi=5,min_profit='.5',collections=['Test']))
        good=await seed(conn,'10')
        expensive=await seed(conn,'30')
        unknown=await seed(conn,'5')
        for item in (good,expensive):
            details=listing_analysis(item,[],dict(buy_fee='0',sell_fee='.02',transfer='0',network='0',conversion='0',extra_sell='0'))
            details.update(roi='10',profit='1',liquidity=0)
            await query(conn,"update market_analysis set details=cast(:d as jsonb) where external_id=:id",d=json.dumps(details,default=str),id=item.external_id)
    app=create_api(db,Config(bot_token=TOKEN,admin_ids='1'),lifespan)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        headers={'Authorization':'tma '+init_data(1)}
        result=(await client.get('/api/catalog',headers=headers)).json()
        assert result['total']==1 and result['items'][0]['details']['purchase']=='10'
        assert (await client.get('/api/catalog?q=%23154',headers=headers)).json()['total']==1
        assert (await client.get('/api/catalog?number=rare',headers=headers)).json()['total']==0
        assert (await client.get('/api/catalog?strategy=instant',headers=headers)).json()['total']==0
        assert (await client.get('/api/catalog?market=MRKT',headers=headers)).json()['total']==0
        assert (await client.get('/api/catalog',params={'session':result['session'],'number':'rare'},headers=headers)).status_code==409

async def test_scan_rejects_cached_profit_when_live_model_is_cheaper(db):
    from goat.market import Portals,scan
    from goat.db import scalar
    async with db.transaction() as conn:
        target=await seed(conn,'23')
    expensive=[replace(target,external_id=str(uuid4()),price=Decimal(p),
        gift=replace(target.gift,id=f'expensive-{i}',number=164+i))
        for i,p in enumerate(('49.99','61','65','10000'))]
    cheap=replace(target,external_id=str(uuid4()),price=Decimal('19.49'),
        gift=replace(target.gift,id='cheap',number=8932,backdrop='Other'))
    async with httpx.AsyncClient() as client:
        adapter=Portals(client,'tma test')
        async def listings():return [target,*expensive]
        async def sales():return []
        async def offer(item):return None
        calls=0
        async def comparables(item):
            nonlocal calls
            calls+=1
            return expensive if calls==1 else [cheap,*expensive]
        async def get_listings(ids):return {i.external_id:i for i in [target,*expensive] if i.external_id in ids}
        adapter.listings=listings;adapter.sales=sales;adapter.offer=offer
        adapter.comparables=comparables;adapter.get_listings=get_listings
        await scan(db,[adapter],{})
    async with db.transaction() as conn:
        assert not await signals(conn,Preferences(capital=200))
        d=await scalar(conn,'select details from market_analysis where external_id=:id',id=target.external_id)
        assert Decimal(d['fair_value'])==Decimal('19.49')
        assert Decimal(d['profit'])<0 and d['comparison_checked_at']


async def test_catalog_allows_losses_without_changing_saved_roi(db):
    async with db.transaction() as conn:
        await register(conn,1,None,'Test');await grant(conn,1,86400,'trial')
        await save_prefs(conn,1,Preferences(capital=20,roi=5))
        item=await seed(conn,'10')
        await query(conn,"update market_analysis set details=details||'{\"roi\":-10,\"profit\":-1}'::jsonb")
    app=create_api(db,Config(bot_token=TOKEN,admin_ids='1'),lifespan)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        headers={'Authorization':'tma '+init_data(1)}
        assert (await client.get('/api/catalog',headers=headers)).json()['total']==0
        result=(await client.get('/api/catalog?profitable=false',headers=headers)).json()
        assert result['total']==1 and result['items'][0]['details']['profit']==-1

