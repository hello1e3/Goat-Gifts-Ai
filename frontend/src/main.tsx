import {useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode} from 'react'
import {createRoot} from 'react-dom/client'
import {motion, MotionConfig} from 'framer-motion'
import {AreaSeries, ColorType, createChart, type UTCTimestamp} from 'lightweight-charts'
import {Activity, ArrowDownRight, ArrowLeft, ArrowRight, ArrowUpRight, BarChart3, Bell, ChartNoAxesCombined, ChevronDown, CircleHelp, FlaskConical, Gem, Globe2, LayoutDashboard, Radio, ScanLine, Search, Settings2, ShieldCheck, UserRound, Wallet, X} from 'lucide-react'
import './style.css'
import snoop from './art/snoopdogg-223555.jpg'
import pepe from './art/plushpepe-1.jpg'
import cap from './art/durovscap-1.jpg'
import goatMark from './art/goat-mark.png'

const demoArt:Record<string,string>={'snoopdogg-223555':snoop,'plushpepe-1':pepe,'durovscap-1':cap}

type Telegram = {initData:string; ready:()=>void; expand:()=>void; openTelegramLink:(url:string)=>void; HapticFeedback?:{selectionChanged:()=>void}}
const tg = (window as Window & {Telegram?: {WebApp?: Telegram}}).Telegram?.WebApp
const auth = tg?.initData ?? ''
type RecordData = Record<string, string|number|boolean|null>
type Point = {date:string;price:string}
type Signal = {id:string;strategy:string;state:string;observed_at:string;details:RecordData & {title:string;currency:string;history:Point[];downside:RecordData}}
type Market = {component:string;status:string;checked_at?:string}
type Prefs = {limit_per_gift:boolean;capital:string; roi:string; max_cost:string; min_profit:string; liquidity:number; max_age_seconds:number; frequency_minutes:number; quiet_start:number|null; quiet_end:number|null; collections:string[]; markets:string[]; strategies:string[]; signals:boolean; reminders:boolean; marketing:boolean}
type Profile = {id:number;name:string;photo?:string;active:boolean;admin:boolean;lifetime:boolean;frozen_at?:string;ends_at?:string;prefs:Prefs;payments:RecordData[]}
type Item = {id:number;title:string;cost:string;currency:string;virtual:boolean;estimate?:string;proceeds?:string;sold_at?:string;available_at?:string;scenario?:string}
const nf = new Intl.NumberFormat('ru-RU',{maximumFractionDigits:2})
const f = (v:unknown) => v == null || !Number.isFinite(Number(v)) ? '—' : nf.format(Number(v))
const fmtDate = (v:unknown) => v ? new Date(String(v)).toLocaleString('ru-RU',{dateStyle:'short',timeStyle:'short'}) : '—'
const strategyNames:Record<string,string> = {floor:'Floor Flip',cross:'Cross-market',instant:'Instant Flip'}
const statusNames:Record<string,string> = {license_required:'Ожидает разрешения',partner_contract_required:'Ожидает доступа',key_required:'Нужен API-ключ',unavailable:'Недоступен',disabled:'Отключён',online:'Подключён',stale:'Данные устарели',initializing:'Подключение'}
const nav = [{name:'Главная',icon:LayoutDashboard},{name:'Сканер',icon:ScanLine},{name:'Портфель',icon:Wallet},{name:'Аналитика',icon:BarChart3},{name:'Профиль',icon:UserRound}]
const metricLabels: Record<string,string> = {
 fair_value:'Рыночный ориентир', conservative_sale:'Консервативный сценарий цены', optimistic_sale:'Оптимистичный сценарий цены', optimistic_profit:'Оптимистичная прибыль',
 cost:'Полная себестоимость', proceeds:'Чистая выручка', profit:'Расчётная прибыль', roi:'ROI, %',
 break_even:'Точка безубыточности', max_buy:'Максимальная цена покупки', conservative_profit:'Консервативная прибыль',
 transfer:'Перевод', buy_commission:'Комиссия покупки', sell_commission:'Комиссия продажи', fx_impact:'Влияние курса',
}
async function api<T>(path:string, options:RequestInit={}):Promise<T> {
 const res=await fetch('/api'+path,{...options,headers:{...options.headers,...(auth?{Authorization:'tma '+auth}:{}),...(options.body?{'Content-Type':'application/json'}:{})}})
 if(!res.ok){const data=await res.json().catch(()=>({}));throw new Error(typeof data.detail==='string'?data.detail:'Проверьте введённые данные')}
 return res.status===204?undefined as T:res.json()
}
const post = (data?:unknown):RequestInit => ({method:'POST',body:data===undefined?undefined:JSON.stringify(data)})
function Chart({points,decor=false}:{points:Point[];decor?:boolean}){
 const ref=useRef<HTMLDivElement>(null)
 useEffect(()=>{
  if(!ref.current||!points.length)return
  const chart=createChart(ref.current,{autoSize:true,layout:{background:{type:ColorType.Solid,color:'transparent'},textColor:'#84919c',fontSize:11,attributionLogo:false},
   grid:{vertLines:{visible:false},horzLines:{color:'#222932'}},rightPriceScale:{borderVisible:false},timeScale:{borderVisible:false},crosshair:{vertLine:{color:'#698b71'},horzLine:{color:'#698b71'}},
   handleScroll:!decor,handleScale:!decor})
  const series=chart.addSeries(AreaSeries,{lineColor:'#a5f47b',topColor:'rgba(132,234,104,.28)',bottomColor:'rgba(132,234,104,.01)',lineWidth:2,priceLineVisible:false,lastValueVisible:false})
  const unique=new Map<number,number>()
  for(const p of points){const at=Math.floor(new Date(p.date).getTime()/1000);if(Number.isFinite(at)&&Number.isFinite(Number(p.price)))unique.set(at,Number(p.price))}
  series.setData([...unique].sort((a,b)=>a[0]-b[0]).map(([time,value])=>({time:time as UTCTimestamp,value})))
  chart.timeScale().fitContent()
  return ()=>chart.remove()
 },[points,decor])
 return <div ref={ref} className="chart" aria-label="График цен"/>
}
function telegramSlug(signal:Signal){
 const slug=signal.state==='demo'?signal.details.telegram_slug:signal.details.id
 return typeof slug==='string'&&/^[a-z][a-z0-9]{0,69}-[1-9][0-9]{0,9}$/i.test(slug)&&
        slug.endsWith('-'+String(signal.details.number))?slug.toLowerCase():undefined
}
function Art({signal,large=false}:{signal:Signal;large?:boolean}){
 const d=signal.details, slug=telegramSlug(signal)
 const original=typeof d.image==='string'&&d.image.startsWith('https://')?d.image:
  slug?(signal.state==='demo'?demoArt[slug]:'/gift-preview/'+slug):undefined
 const [failed,setFailed]=useState<string>()
 const showOriginal=original&&failed!==original
 return <div className={`art gift-art ${large?'art-large':''}`}>
  {showOriginal?<img src={original} alt={`${d.title} #${d.number} · ${d.backdrop}`} loading={large?'eager':'lazy'} referrerPolicy="no-referrer" onError={()=>setFailed(original)}/>:
   <div className="gift-unavailable"><Gem size={25}/><small>Превью Telegram недоступно</small></div>}
  {d.number!=null&&<span className="gift-number">#{d.number}</span>}
 </div>
}
function SignalCard({signal,onClick}:{signal:Signal;onClick:()=>void}){
 const d=signal.details
 const profitable=Number(d.profit)>0
 return <button className="signal-card" onClick={onClick}>
  <div className="signal-media"><Art signal={signal}/><span className="strategy">{d.market}</span><span className="card-link"><ArrowUpRight size={16}/></span></div>
  <div className="signal-body"><div className="signal-heading"><div><strong>{d.title} <span>#{d.number}</span></strong><small>{d.model} · {d.backdrop}</small></div><span className="roi">{Number(d.roi)>0?'+':''}{f(d.roi)}%</span></div>
  <div className="gift-attributes"><span>{d.symbol}</span><span>{d.evidence==='asks'?'Предварительный расчёт':strategyNames[signal.strategy]}</span></div>
  <div className="card-values"><div><small>Цена сейчас</small><b>{f(d.purchase)} {d.currency}</b></div><div><small>Оценка · {d.evidence==='asks'?'по объявлениям':d.evidence==='sales'?'по продажам':'нет аналогов'}</small><b>{f(d.sale)} {d.currency}</b></div><div><small>Расчётная прибыль</small><b className={profitable?'green':''}>{d.profit==null?'Нужны аналоги':`${profitable?'+':''}${f(d.profit)} ${d.currency}`}</b></div></div></div>
 </button>
}
function Field({label,children}:{label:string;children:ReactNode}){return <label className="field"><span>{label}</span>{children}</label>}
function App(){
 const [page,setPage]=useState('Главная')
 const [samples,setSamples]=useState<Signal[]>([])
 const [live,setLive]=useState<Signal[]>([])
 const [watch,setWatch]=useState<Signal[]>([])
 const [catalogItems,setCatalogItems]=useState<Signal[]>([])
 const [markets,setMarkets]=useState<Market[]>([])
 const [me,setMe]=useState<Profile>()
 const [items,setItems]=useState<Item[]>([])
 const [transactions,setTransactions]=useState<RecordData[]>([])
 const [selected,setSelected]=useState<Signal>()
 const [loading,setLoading]=useState(true)
 const [showSplash,setShowSplash]=useState(true)
 const [toast,setToast]=useState('')
 const [range,setRange]=useState(14)
 const [filter,setFilter]=useState('all')
 const [market,setMarket]=useState('all')
 const [sort,setSort]=useState('latest')
 const [legal,setLegal]=useState<RecordData>()
 const [calculator,setCalculator]=useState(false)
 const [calculated,setCalculated]=useState<(RecordData & {downside:RecordData})>()
 const [period,setPeriod]=useState(30)
 const [admin,setAdmin]=useState<RecordData>()
 const [past,setPast]=useState<Signal[]>([])
 const [virtual,setVirtual]=useState(false)
 const [closing,setClosing]=useState<number>()
 const [editing,setEditing]=useState<Item>()
 const [prefs,setPrefs]=useState<Prefs>()
 const [showPrefs,setShowPrefs]=useState(false)
 const [advancedPrefs,setAdvancedPrefs]=useState(false)
 const [search,setSearch]=useState('')
 const [searchInput,setSearchInput]=useState('')
 const [scannerMode,setScannerMode]=useState<'opportunities'|'market'>('opportunities')
 const [catalogPage,setCatalogPage]=useState(1)
 const [catalogHasMore,setCatalogHasMore]=useState(false)
 const [catalogTotal,setCatalogTotal]=useState(0)
 const [catalogSession,setCatalogSession]=useState('')
 const [diagnostics,setDiagnostics]=useState<Record<string,unknown>>()
 const [busy,setBusy]=useState(false)
 const [pendingVerify,setPendingVerify]=useState<string>()
 const loadMe=async()=>{const data=await api<Profile>('/me');setMe(data);setPrefs(data.prefs);return data}
 const loadItems=async()=>{const [positions,history]=await Promise.all([api<Item[]>('/portfolio'),api<RecordData[]>('/portfolio/history')]);setItems(positions);setTransactions(history)}
 const liveFeed=async()=>{const [signals,nearby]=await Promise.all([api<Signal[]>('/signals'),api<Signal[]>('/nearby')]);setLive(signals);setWatch(nearby)}
 const catalogFeed=async(q='',page=1)=>{const params=new URLSearchParams({offset:String((page-1)*20),limit:'20'});if(q)params.set('q',q);if(page>1&&catalogSession)params.set('session',catalogSession);const data=await api<{items:Signal[];has_more:boolean;total:number;session:string}>('/catalog?'+params);setCatalogItems(data.items);setCatalogPage(page);setCatalogHasMore(data.has_more);setCatalogTotal(data.total);setCatalogSession(data.session)}
 const announce=(e:unknown)=>setToast(e instanceof Error?e.message:String(e))
 const change=(name:string)=>{tg?.HapticFeedback?.selectionChanged();setPage(name);setFilter('all');setMarket('all');window.scrollTo(0,0)}
 const run=async(task:()=>Promise<void>)=>{if(busy)return;setBusy(true);try{await task()}catch(e){announce(e)}finally{setBusy(false)}}
 useEffect(()=>{
  tg?.ready();tg?.expand()
  Promise.allSettled([api<Signal[]>('/demo').then(setSamples),api<Market[]>('/market-status').then(setMarkets),
   ...(auth?[loadMe().then(profile=>profile.active?liveFeed():undefined),loadItems()]:[])])
   .then(results=>{const failure=results.find(result=>result.status==='rejected');if(failure?.status==='rejected')announce(failure.reason)})
   .finally(()=>setLoading(false))
 },[])
 useEffect(()=>{if(loading)return;const id=window.setTimeout(()=>setShowSplash(false),650);return()=>clearTimeout(id)},[loading])
 useEffect(()=>{if(!toast)return;const id=window.setTimeout(()=>setToast(''),4800);return()=>clearTimeout(id)},[toast])
 useEffect(()=>{
  if(!me?.active)return
  const id=window.setInterval(()=>{void liveFeed().catch(()=>setLive([]));void api<Market[]>('/market-status').then(setMarkets).catch(()=>{})},30000)
  return()=>clearInterval(id)
 },[me?.active])
 useEffect(()=>{
  if(!pendingVerify)return
  const id=window.setInterval(()=>{void api<{status:string;checked_at?:string}>('/signals/'+pendingVerify+'/verification').then(result=>{
   if(result.status==='queued')return
   setPendingVerify(undefined);setToast(({available:'Цена и наличие подтверждены.',stale:'Цена изменилась.',unavailable:'Предложение недоступно.',source_unavailable:'Источник недоступен.'} as Record<string,string>)[result.status]??result.status)
   void liveFeed().catch(()=>setLive([]))
  }).catch(e=>{setPendingVerify(undefined);announce(e)})},2500)
  return()=>clearInterval(id)
 },[pendingVerify])
 useEffect(()=>{
  if(page==='Владелец')void api<RecordData>(`/admin/statistics?days=${period}`).then(setAdmin).catch(announce)
  if(page==='Владелец')void api<Record<string,unknown>>('/admin/diagnostics').then(setDiagnostics).catch(announce)
  if(page==='Аналитика'&&me?.active)void api<Signal[]>('/signals?past=true').then(setPast).catch(announce)
 },[page,period,me?.active])
 const isDemo=!me?.active
 const source=isDemo?samples:live
 const searchable=isDemo?samples:(scannerMode==='opportunities'?live:catalogItems)
 const display=useMemo(()=>searchable.filter(s=>{
    const q=search.trim().toLocaleLowerCase('ru-RU')
    const haystack=[s.details.title,s.details.collection,s.details.model,s.details.backdrop,s.details.symbol,s.details.number].join(' ').toLocaleLowerCase('ru-RU')
    return (!q||haystack.includes(q))&&(filter==='all'||s.strategy===filter)&&(market==='all'||s.details.market===market)
   })
    .sort((a,b)=>sort==='roi'?Number(b.details.roi)-Number(a.details.roi):sort==='cost'?Number(a.details.cost)-Number(b.details.cost):0),[searchable,filter,market,sort,search])
 const chart=useMemo(()=>(source[0]?.details.history??[]).slice(-range),[source,range])
 const portfolio=items.filter(i=>i.virtual===virtual)
 const moneyTotals=useMemo(()=>Object.fromEntries(['TON','GRAM'].map(currency=>{
  const entries=portfolio.filter(item=>item.currency===currency)
  const open=entries.filter(item=>!item.sold_at);const complete=open.every(item=>item.estimate!=null)
  const value=complete?open.reduce((sum,item)=>sum+Number(item.estimate),0):null
  return [currency,{value,count:open.length,unknown:open.filter(item=>item.estimate==null).length,invested:entries.filter(item=>!item.sold_at).reduce((sum,item)=>sum+Number(item.cost),0),
    realized:entries.filter(item=>item.sold_at).reduce((sum,item)=>sum+Number(item.proceeds)-Number(item.cost),0),
    unrealized:complete?open.reduce((sum,item)=>sum+Number(item.estimate)-Number(item.cost),0):null}]
 })),[portfolio])
 const [botName,setBotName]=useState('')
 useEffect(()=>{void api<{bot_username:string}>('/public-config').then(x=>setBotName(x.bot_username)).catch(()=>{})},[])
 const bot=(command='terminal')=>{if(!botName){setToast('Telegram-бот пока не подключён.');return}const url='https://t.me/'+botName+'?start='+command;if(tg?.openTelegramLink)tg.openTelegramLink(url);else window.open(url,'_blank','noopener')}
 function saveSettings(event:FormEvent<HTMLFormElement>){
  event.preventDefault();if(!prefs)return
  const form=new FormData(event.currentTarget)
  const next={...prefs}
  for(const key of ['capital','roi','max_cost','min_profit'] as const)if(form.has(key))next[key]=String(form.get(key))
  for(const key of ['liquidity','max_age_seconds','frequency_minutes'] as const)if(form.has(key))next[key]=Number(form.get(key))
  for(const key of ['quiet_start','quiet_end'] as const)if(form.has(key))next[key]=form.get(key)===''?null:Number(form.get(key))
  for(const key of ['collections','markets','strategies'] as const)if(form.has(key))next[key]=String(form.get(key)).split(',').map(x=>x.trim()).filter(Boolean)
  for(const key of ['signals','marketing','reminders','limit_per_gift'] as const)next[key]=form.get(key)==='on'
  void run(async()=>{const saved=await api<Prefs>('/me/preferences',{method:'PUT',body:JSON.stringify(next)});setPrefs(saved);setMe(prev=>prev?{...prev,prefs:saved}:prev);if(me?.active)await liveFeed();setToast('Настройки сохранены');setShowPrefs(false)})
 }
 async function exportStats(){
  const response=await fetch('/api/admin/statistics?format=csv&days='+period,{headers:{Authorization:'tma '+auth}})
  if(!response.ok)throw new Error('Экспорт недоступен')
  const url=URL.createObjectURL(await response.blob());const link=document.createElement('a');link.href=url;link.download='goat-statistics.csv';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000)
 }
 async function refreshGift(signal:Signal){
  const result=await api<{status:string;item?:Signal}>('/signals/'+signal.id+'/verify',post())
  const updated=result.item??{...signal,state:'unavailable'}
  setSelected(current=>current?.id===signal.id?updated:current)
  setCatalogItems(current=>current.map(item=>item.id===signal.id?updated:item))
  if(me?.active)await liveFeed()
  setToast(result.status==='available'?'Цена, наличие и оценка обновлены':'Лот продан или снят с продажи')
 }
 function openGift(signal:Signal){
  setSelected(signal)
  if(signal.state!=='demo')void run(()=>refreshGift(signal))
 }
 function signalGrid(entries:Signal[]){
  return loading?<div className="signal-grid">{[1,2,3].map(i=><div className="skeleton" key={i}/>)}</div>:
   entries.length?<div className="signal-grid">{entries.map(signal=><SignalCard key={signal.id} signal={signal} onClick={()=>openGift(signal)}/>)}</div>:
    <div className="empty"><ScanLine size={25}/><strong>Подходящих сигналов пока нет</strong><span>Проверьте фильтры и состояние площадок.</span></div>
 }
 return <MotionConfig reducedMotion="user"><div className="app-root">
  {showSplash&&<div className={`splash ${loading?'':'splash-ready'}`} aria-label="GOAT NFT загружается">
   <div className="splash-glow"/><img src={goatMark} alt=""/><strong>GOAT NFT</strong><small>умный сканер Telegram Gifts</small>
   <div className="splash-dots" aria-hidden="true"><i/><i/><i/></div>
  </div>}
  <div className="shell">
  <aside className="sidebar">
   <button className="brand" onClick={()=>change('Главная')}><span className="brand-icon"><img src={goatMark} alt=""/></span><span><strong>GOAT NFT</strong><small>TELEGRAM GIFTS</small></span></button>
   <div className="side-label">ПРИЛОЖЕНИЕ</div>
   <nav>{nav.map(n=><button title={n.name} key={n.name} className={page===n.name?'active':''} onClick={()=>change(n.name)}><n.icon size={19}/><span>{n.name}</span></button>)}
   {me?.admin&&<button className={page==='Владелец'?'active':''} onClick={()=>change('Владелец')}><ShieldCheck size={19}/><span>Владелец</span></button>}</nav>
   <div className="side-bottom"><button onClick={()=>bot('support')}><CircleHelp size={18}/><span>Поддержка</span></button><button onClick={()=>change('Профиль')} className="side-profile"><span className="avatar">{me?.photo?<img src={me.photo} alt=""/>:me?.name.slice(0,1)??'G'}</span><span><strong>{me?.name??'Гость'}</strong><small>{me?.active?'GOAT PRO':'Демо-доступ'}</small></span><ChevronDown size={14}/></button></div>
  </aside>
  <div className="work">
   <header className="top"><div className="breadcrumb"><span className="mobile-brand"><img src={goatMark} alt=""/></span><span>GOAT NFT</span><span>/</span><strong>{page}</strong></div><div className="top-actions"><span className={'mode '+(!isDemo?'live':'')}><span/>{isDemo?'Демо-режим':'GOAT PRO'}</span><button className="icon-button" title="Настройки уведомлений" onClick={()=>{change('Профиль');setShowPrefs(true)}}><Bell size={18}/></button><button className="avatar" title="Профиль" onClick={()=>change('Профиль')}>{me?.name.slice(0,1)??'G'}</button></div></header>
   <main><motion.div key={page} initial={{opacity:0,y:5}} animate={{opacity:1,y:0}} transition={{duration:.2}}>
   {page==='Главная'&&<>
    <div className="intro"><div><div className="eyebrow"><span/> ОБЗОР РЫНКА</div><h1>{me?'Привет, '+me.name.split(' ')[0]:'Маркет подарков'}</h1><p>Ищите настоящие Telegram Gifts и сравнивайте цены в GRAM.</p></div><button className="primary" onClick={()=>change('Сканер')}><ScanLine size={17}/> Найти подарок <ArrowRight size={16}/></button></div>
    {isDemo&&<div className="demo-note"><FlaskConical size={17}/><span><strong>Подарки из Telegram.</strong> Цены, сделки и график в демо синтетические.</span><button onClick={()=>change('Профиль')}>О GOAT PRO <ArrowUpRight size={14}/></button></div>}
    <div className="metrics">
     <div className="metric"><div><span className="metric-icon"><Radio size={18}/></span><span>Возможности</span></div><strong>{source.length}</strong><small>{isDemo?'Примеры в демо':'Совпадения с вашими фильтрами'}</small></div>
     <div className="metric"><div><span className="metric-icon"><ChartNoAxesCombined size={18}/></span><span>Средний ROI</span></div><strong className="green">{source.length?'+'+f(source.reduce((sum,s)=>sum+Number(s.details.roi),0)/source.length)+'%':'—'}</strong><small>После расчётных расходов</small></div>
     <div className="metric"><div><span className="metric-icon"><Wallet size={17}/></span><span>Торговый капитал</span></div><strong>{f(me?.prefs.capital??100)} <em>TON</em></strong><small>{me?'Из вашего профиля':'Рекомендуемый старт'}</small></div>
     <div className="metric"><div><span className="metric-icon muted-icon"><Globe2 size={17}/></span><span>Маркетплейсы</span></div><strong>{markets.filter(m=>m.status==='online').length}<em> / 2</em></strong><small>Подключённые источники</small></div>
    </div>
    <div className="overview"><section className="panel chart-panel"><div className="panel-head"><div><h2>Динамика цен</h2><p>{chart.length?source[0].details.title+' · '+source[0].details.currency:'История сопоставимых продаж'}</p></div><div className="segmented">{[7,14,30].map(n=><button key={n} className={range===n?'chosen':''} onClick={()=>setRange(n)}>{n} д</button>)}</div></div>{chart.length?<Chart points={chart}/>:<div className="empty">Пока нет достаточной истории продаж</div>}</section>
    <section className="panel connection"><div className="panel-head"><div><h2>Источники данных</h2><p>Состояние интеграций</p></div><Activity size={18}/></div>
      {markets.map((m,i)=><div className="market-row" key={m.component}><span className={'market-icon market-'+i}>{i===0?<Gem size={19}/>:<Activity size={18}/>}</span><div><strong>{m.component}</strong><small>{m.checked_at?'Проверено '+fmtDate(m.checked_at):'Маркетплейс Telegram Gifts'}</small></div><span className={'status '+(m.status==='online'?'online':'')}>{statusNames[m.status]??m.status}</span></div>)}
      <div className="market-foot"><ShieldCheck size={16}/> Статус показывает доступ терминала к данным.</div>
    </section></div>
    <section className="signals-section"><div className="section-head"><div><h2>{isDemo?'Примеры подарков':'Последние сигналы'}</h2><p>{isDemo?'Настоящие карточки, учебные цены в GRAM':'Расчётные торговые возможности'}</p></div><button className="ghost" onClick={()=>change('Сканер')}>Все подарки <ArrowRight size={16}/></button></div>{signalGrid(source.slice(0,3))}</section>
   </>}
   {page==='Сканер'&&<>
    <div className="intro"><div><div className="eyebrow"><span/> СКАНЕР PORTALS</div><h1>{scannerMode==='opportunities'?'Выгодные предложения':'Весь рынок подарков'}</h1><p>{isDemo?'Настоящие изображения и атрибуты; демо-цены показаны в GRAM.':scannerMode==='opportunities'?`Только положительные сигналы в пределах бюджета ${f(me?.prefs.capital)} GRAM · сеть TON.`:'Проанализированная часть рынка Portals. Фоновый обход продолжается.'}</p></div><button className="ghost" onClick={()=>{setShowPrefs(true);change('Профиль')}}><Settings2 size={18}/> Бюджет и фильтры</button></div>
    <div className="segmented scanner-tabs"><button className={scannerMode==='opportunities'?'chosen':''} onClick={()=>{setScannerMode('opportunities');setSearch('');setSearchInput('')}}>Выгодные</button><button className={scannerMode==='market'?'chosen':''} onClick={()=>{setScannerMode('market');setSearch('');setSearchInput('');if(!isDemo)void run(()=>catalogFeed('',1))}}>Весь рынок</button></div>
    <div className="panel filters"><Field label="Поиск подарка"><form className="search-box" role="search" onSubmit={e=>{e.preventDefault();setSearch(searchInput);if(!isDemo&&scannerMode==='market')void run(()=>catalogFeed(searchInput,1))}}><Search size={17}/><input type="search" inputMode="search" enterKeyHint="search" value={searchInput} onChange={e=>setSearchInput(e.target.value)} placeholder="Название, номер, модель…" aria-label="Название подарка"/>{searchInput&&<button type="button" className="search-clear" aria-label="Очистить поиск" onClick={()=>{setSearchInput('');setSearch('');if(!isDemo&&scannerMode==='market')void run(()=>catalogFeed('',1))}}><X size={15}/></button>}<button type="submit" className="search-submit" disabled={busy}>{busy?'Ищем…':'Найти'}</button></form></Field><Field label="Стратегия"><select value={filter} onChange={e=>setFilter(e.target.value)}><option value="all">Все</option>{Object.entries(strategyNames).map(([k,v])=><option key={k} value={k}>{v}</option>)}</select></Field>
    <Field label="Маркетплейс"><select value={market} onChange={e=>setMarket(e.target.value)}><option value="all">Portals</option><option>Portals</option></select></Field>
    <Field label="Порядок"><select value={sort} onChange={e=>setSort(e.target.value)}><option value="latest">Сначала новые</option><option value="roi">По доходности</option><option value="cost">По себестоимости</option></select></Field></div>{signalGrid(display)}
    {!isDemo&&scannerMode==='opportunities'&&!display.length&&watch.length>0&&<section><h2>Для наблюдения</h2><p>Укладываются в бюджет, но не подтверждены как прибыльные сделки. Проверьте достоверность оценки и комиссии.</p>{signalGrid(watch)}</section>}
    {!isDemo&&scannerMode==='market'&&<div className="pagination"><button className="ghost" disabled={busy||catalogPage===1} onClick={()=>void run(()=>catalogFeed(search,catalogPage-1))}><ArrowLeft size={16}/> Назад</button><strong>{catalogPage} / {Math.max(1,Math.ceil(catalogTotal/20))} · {catalogTotal} лотов</strong><button className="ghost" disabled={busy||!catalogHasMore} onClick={()=>void run(()=>catalogFeed(search,catalogPage+1))}>Дальше <ArrowRight size={16}/></button></div>}
   </>}
   {page==='Портфель'&&<>
    <div className="intro"><div><h1>Портфель</h1><p>Учёт позиций и виртуальные сделки.</p></div><div className="segmented"><button className={virtual?'chosen':''} onClick={()=>setVirtual(true)}>Виртуальный</button><button className={!virtual?'chosen':''} onClick={()=>setVirtual(false)}>Личный</button></div></div>
    {!me?<div className="panel empty"><Wallet/><strong>Войдите через Telegram</strong><span>Ваши позиции сохраняются в профиле.</span><button className="primary" onClick={()=>bot()}>Открыть бота</button></div>:<>
     <div className="metrics portfolio-metrics">{Object.entries(moneyTotals).map(([currency,totals])=><div className="metric" key={currency}><div><Wallet size={16}/>{currency} в открытых позициях</div><strong>{f(totals.invested)} <em>{currency}</em></strong><small>Подарков: {totals.count} · без оценки: {totals.unknown}</small><small>Оценочная чистая стоимость: {f(totals.value)}</small><small>Реализованная прибыль: {f(totals.realized)}</small><small>Нереализованный результат: {f(totals.unrealized)}</small></div>)}</div>
     <div className="panel list-panel"><h2>Свободный баланс</h2><p>{f(me?.prefs.capital)} GRAM · указан вами в настройках, не баланс кошелька.</p><h2>История сделок</h2>{transactions.filter(t=>t.virtual===virtual).map(t=><div className="table-row" key={String(t.id)}><span>{String(t.title)} · {({buy:'Покупка',sell:'Продажа',cost_correction:'Коррекция стоимости'} as Record<string,string>)[String(t.kind)]??t.kind}<small>{fmtDate(t.created_at)}</small></span><strong>{f(t.amount)} {t.currency}</strong></div>)}</div>
     <form className="panel filters" onSubmit={e=>{e.preventDefault();const form=e.currentTarget;const d=new FormData(form);void run(async()=>{await api('/portfolio',post({title:d.get('title'),cost:d.get('cost'),currency:d.get('currency'),virtual}));form.reset();await loadItems()})}}>
       <Field label="Название"><input name="title" required maxLength={120}/></Field><Field label="Себестоимость"><input name="cost" type="number" step="any" min=".000000001" required/></Field><Field label="Валюта"><select name="currency"><option>TON</option><option>GRAM</option></select></Field><button className="primary" disabled={busy}>Добавить</button>
     </form>
     <div className="panel list-panel"><h2>Позиции</h2>{!portfolio.length&&<p>Здесь появятся добавленные подарки.</p>}{portfolio.map(i=><div className="position" key={i.id}><div><strong>{i.title}</strong><small>{i.sold_at?'Закрыта '+fmtDate(i.sold_at):`Себестоимость ${f(i.cost)} ${i.currency}`}</small></div><div><button className="ghost" onClick={()=>setEditing(i)}>Изменить</button><button className="ghost" onClick={()=>{if(window.confirm('Удалить эту позицию и её историю?'))void run(async()=>{await api('/portfolio/'+i.id,{method:'DELETE'});await loadItems()})}}>Удалить</button><span>{i.sold_at?`Результат ${f(Number(i.proceeds)-Number(i.cost))} ${i.currency}`:i.estimate?`Оценка ${f(i.estimate)} ${i.currency}`:'Оценки пока нет'}</span>{!i.sold_at&&<button className="ghost" onClick={()=>i.scenario?void run(async()=>{await api('/portfolio/'+i.id+'/close',post({proceeds:'0'}));await loadItems()}):setClosing(closing===i.id?undefined:i.id)}>{i.scenario?'Симулировать продажу':'Закрыть'}</button>}</div>{closing===i.id&&<form onSubmit={e=>{e.preventDefault();const data=new FormData(e.currentTarget);void run(async()=>{await api('/portfolio/'+i.id+'/close',post({proceeds:data.get('proceeds')}));setClosing(undefined);await loadItems()})}}><Field label={i.available_at?'Выручка · после '+fmtDate(i.available_at):'Чистая выручка'}><input name="proceeds" required type="number" min="0" step="any"/></Field><button className="primary" disabled={busy}>Сохранить</button></form>}</div>)}</div>
    </>}
   </>}
   {page==='Аналитика'&&<>
    <div className="intro"><div><h1>Аналитика</h1><p>{isDemo?'Исследуйте синтетическую историю цен.':'Наблюдения по торговым стратегиям.'}</p></div><button className="primary" onClick={()=>setCalculator(true)}><ChartNoAxesCombined size={17}/> Калькулятор</button></div>
    <div className="panel chart-panel"><div className="panel-head"><h2>История цен {source[0]?.details.title}</h2></div>{chart.length?<Chart points={chart}/>:<div className="empty">История продаж пока недоступна</div>}</div>
    <div className="panel list-panel"><h2>{isDemo?'Демонстрационные сценарии':'История сигналов'}</h2>{(isDemo?samples:past).map(s=><button className="table-row" key={s.id} onClick={()=>setSelected(s)}><span>{s.details.title}</span><span>{strategyNames[s.strategy]}</span><span className="green">{f(s.details.roi)}%</span><ArrowUpRight size={16}/></button>)}{!isDemo&&!past.length&&<p>История пока пуста.</p>}</div>
   </>}
   {page==='Профиль'&&<>
    <div className="intro"><div><h1>Профиль</h1><p>Подписка, уведомления и данные аккаунта.</p></div></div>
    <div className="panel list-panel"><div className="panel-head"><div><h2>{me?.name??'Гостевой режим'}</h2><p>{me?`Telegram ID ${me.id}`:'Откройте приложение через Telegram для сохранения настроек.'}</p></div><span className="mode">{me?.active?'GOAT PRO':me?.frozen_at?'Доступ заморожен':'Демо-доступ'}</span></div>
     <p>{me?.lifetime?'Бессрочный доступ':me?.ends_at?`Окончание · ${fmtDate(me.ends_at)}`:'GOAT PRO · 30 дней · разовая оплата в Telegram Stars'}</p>
     <div className="inline"><button className="primary" onClick={()=>bot('buy')}>{me?.active?'Продлить':'Купить GOAT PRO'}</button><button className="ghost" onClick={()=>bot('support')}>Поддержка</button><button className="ghost" onClick={()=>void run(async()=>setLegal(await api<RecordData>('/legal')))}>Документы</button></div>
    </div>
    {me&&prefs&&<div className="panel list-panel"><div className="panel-head"><h2>Фильтры и уведомления</h2><button className="ghost" onClick={()=>setShowPrefs(!showPrefs)}><Settings2 size={16}/>{showPrefs?'Свернуть':'Настроить'}</button></div>
    {showPrefs&&<form className="settings-form" onSubmit={saveSettings}>
      {([['capital','Мой бюджет, GRAM · сеть TON'],['roi','Минимальная доходность, %'],['max_cost','Лимит подарка, GRAM (если включён ниже)']] as const).map(([key,label])=><Field key={key} label={label}><input type="number" step="any" name={key} defaultValue={prefs[key]??''}/></Field>)}
      {([['signals','Торговые уведомления'],['reminders','Напоминания о подписке'],['marketing','Согласие на рассылки']] as const).map(([key,label])=><label className="check" key={key}><input type="checkbox" name={key} defaultChecked={prefs[key]}/>{label}</label>)}
      <button type="button" className="ghost" onClick={()=>setAdvancedPrefs(!advancedPrefs)}>{advancedPrefs?'Скрыть дополнительные':'Дополнительные настройки'}</button>
      <label className="checkbox"><input type="checkbox" name="limit_per_gift" defaultChecked={prefs.limit_per_gift}/> Ограничить цену одного подарка отдельно от бюджета</label>{advancedPrefs&&<>{([['min_profit','Минимальная прибыль, TON'],['liquidity','Минимум продаж'],['frequency_minutes','Интервал уведомлений, мин.']] as const).map(([key,label])=><Field key={key} label={label}><input type="number" step="any" name={key} defaultValue={prefs[key]??''}/></Field>)}
      {([['collections','Только коллекции'],['markets','Площадки'],['strategies','Стратегии']] as const).map(([key,label])=><Field key={key} label={label}><input name={key} defaultValue={prefs[key].join(', ')}/></Field>)}</>}
      <button className="primary" disabled={busy}>Сохранить</button></form>}</div>}
    {me&&<div className="panel list-panel"><h2>История платежей</h2>{me.payments.length?me.payments.map(o=><div className="table-row" key={String(o.id)}><span>{fmtDate(o.created_at)}</span><span>{o.status}</span><strong>{o.amount} ⭐</strong></div>):<p>Платежей пока нет.</p>}
    <button className="ghost danger" onClick={()=>{if(window.confirm('Удалить профиль, портфель, настройки и переписку? Финансовые записи сохранятся.'))void run(async()=>{await api('/me',{method:'DELETE'});setMe(undefined);setItems([]);setLive([]);setToast('Профиль удалён')})}}>Удалить персональные данные</button></div>}
   </>}
   {page==='Владелец'&&<><div className="intro"><div><h1>Статистика владельца</h1><p>Подписки, выдачи и выручка в Telegram Stars.</p></div><button className="primary" onClick={()=>void run(exportStats)}>Экспорт CSV</button></div><div className="segmented">{[[1,'Сегодня'],[7,'7 дней'],[30,'30 дней'],[0,'Всё время']].map(([n,label])=><button key={n} className={period===n?'chosen':''} onClick={()=>setPeriod(Number(n))}>{label}</button>)}</div><div className="panel list-panel">
    {diagnostics&&<><h2>Диагностика рынка</h2>{Object.entries({unique_lots:'Уникальных лотов в базе',collections_seen:'Коллекций в базе',fresh_lots:'Актуальных лотов',analyzed:'Проанализировано за цикл',profitable:'Выгодных за цикл',insufficient:'Недостаточно данных',asks_only:'Только объявления',fees_missing:'Не настроены комиссии',non_positive:'Неположительная прибыль',duration_seconds:'Длительность цикла, сек.'}).map(([key,label])=><div className="table-row" key={key}><span>{label}</span><strong>{f(diagnostics[key])}</strong></div>)}<p>Обновлено: {fmtDate(diagnostics.updated_at)}. Обход по страницам не является полным снимком рынка.</p></>}
    {admin&&Object.entries(admin).map(([k,v])=><div className="table-row" key={k}><span>{adminLabels[k]??k}</span><strong>{f(v)}</strong></div>)}</div></>}
   </motion.div></main>
  </div>
  {editing&&<div className="overlay"><form className="modal panel" onSubmit={e=>{e.preventDefault();const d=new FormData(e.currentTarget);void run(async()=>{await api('/portfolio/'+editing.id,{method:'PATCH',body:JSON.stringify({title:d.get('title'),cost:d.get('cost'),currency:editing.currency,virtual:editing.virtual})});setEditing(undefined);await loadItems()})}}><h2>Изменить позицию</h2><Field label="Название"><input name="title" defaultValue={editing.title} required/></Field><Field label={'Себестоимость, '+editing.currency}><input type="number" min="0.000000001" step="any" name="cost" defaultValue={editing.cost} required/></Field><div className="inline"><button type="submit" className="primary" disabled={busy||!!editing.sold_at}>Сохранить</button><button type="button" className="ghost" onClick={()=>setEditing(undefined)}>Назад</button></div>{editing.sold_at&&<p>Проданная позиция закрыта для редактирования.</p>}</form></div>}
  {(selected||calculator||legal)&&<div className="overlay" onClick={()=>{setSelected(undefined);setCalculator(false);setLegal(undefined)}}><section role="dialog" aria-modal="true" className="modal panel" onClick={e=>e.stopPropagation()}><div className="panel-head"><h2>{selected?selected.details.title:calculator?'Финансовый калькулятор':'Документы сервиса'}</h2><button className="icon-button" aria-label="Закрыть" onClick={()=>{setSelected(undefined);setCalculator(false);setLegal(undefined)}}><X size={20}/></button></div>
    {selected&&<><Art signal={selected} large/><p>{selected.details.model} · {selected.details.backdrop} · {selected.details.symbol}{selected.details.rarity?' · '+selected.details.rarity:''}</p>
      <p>{strategyNames[selected.strategy]} · {selected.details.market} → {selected.details.exit_market} · {fmtDate(selected.observed_at)}</p>
      <p>Подтверждённых продаж · {selected.details.sales_count??selected.details.liquidity} · Аналогичных объявлений · {selected.details.comparable_asks??0}</p>
      <p>Достоверность: {({high:'высокая',medium:'средняя',low:'низкая — только объявления',insufficient:'недостаточно данных'} as Record<string,string>)[String(selected.details.confidence)]??'не определена'}. Срок перепродажи неизвестен.</p>
      {selected.details.history.length>0&&<Chart points={selected.details.history}/>}
      <dl>{Object.entries(metricLabels).map(([key,label])=><div key={key}><dt>{label}</dt><dd>{f(selected.details[key])}</dd></div>)}</dl>
      <p>{selected.details.risk}</p><div className="inline">
      {selected.state==='demo'&&telegramSlug(selected)&&<a className="ghost" href={'https://t.me/nft/'+telegramSlug(selected)} target="_blank" rel="noreferrer">Посмотреть в Telegram <ArrowUpRight size={16}/></a>}
      {selected.details.url&&selected.state!=='unavailable'&&<a className="primary" href={String(selected.details.url)} target="_blank" rel="noreferrer">Купить на Portals <ArrowUpRight size={16}/></a>}
      {selected.state!=='demo'&&<button className="ghost" disabled={busy} onClick={()=>void run(()=>refreshGift(selected))}>{busy?'Проверяем…':'Перепроверить'}</button>}
      <button className="ghost" disabled={busy} onClick={()=>void run(async()=>{if(!me)throw new Error('Для портфеля откройте терминал через Telegram');await api('/portfolio/from-signal/'+selected.id+'?virtual='+(selected.state==='demo'),post());await loadItems();setToast('Позиция добавлена')})}>{selected.state==='demo'?'В виртуальный портфель':'В портфель'}</button></div></>}
    {calculator&&<><form className="settings-form" onSubmit={e=>{e.preventDefault();const data=Object.fromEntries(new FormData(e.currentTarget));void run(async()=>setCalculated(await api('/calculator',post(data))))}}>
      {([['buy','Цена покупки','10'],['sell','Цена продажи','12'],['buy_fee','Комиссия покупки, доля','0'],['sell_fee','Комиссия продажи, доля','0'],['transfer','Перевод','0'],['network','Сетевые расходы','0'],['conversion','Конвертация','0'],['extra_sell','Расходы продажи','0'],['fx','Курс в валюту расчёта','1'],['fx_shock_pct','Изменение курса, %','0'],['target_roi','Целевой ROI, %','5']] as const).map(([k,label,value])=><Field key={k} label={label}><input type="number" step="any" name={k} defaultValue={value} required/></Field>)}
      <Field label="Валюта расчёта"><select name="currency"><option>TON</option><option>GRAM</option></select></Field><button className="primary" disabled={busy}>Рассчитать</button></form>
      {calculated&&<dl>{Object.entries(metricLabels).map(([key,label])=><div key={key}><dt>{label}</dt><dd>{f(calculated[key])}</dd></div>)}{Object.entries(calculated.downside).map(([key,value])=><div key={key}><dt>При падении на {key}%</dt><dd>{f(value)}</dd></div>)}</dl>}</>}
    {legal&&<dl>{Object.entries(legal).map(([k,v])=><div className="legal-row" key={k}><dt>{({owner:'Оператор',terms:'Правила',privacy:'Конфиденциальность',risk:'Рыночные риски'} as Record<string,string>)[k]}</dt><dd>{v}</dd></div>)}</dl>}
  </section></div>}
  {toast&&<div className="toast" role="status">{toast}<button className="icon-button" aria-label="Закрыть" onClick={()=>setToast('')}><X size={15}/></button></div>}
 </div></div></MotionConfig>
}
const adminLabels:Record<string,string>={users:'Пользователи',new_today:'Новые сегодня',new_week:'Новые за неделю',new_month:'Новые за месяц',active_today:'Активные сегодня',active_week:'Активные за неделю',blocked:'Заблокированные',subscribed:'Подписчики',paid_subscribers:'Платные',free_subscribers:'Бесплатные',expired:'Истёкшие',free_grants:'Бесплатные выдачи',conversion:'Конверсия, %',sales:'Покупки',gross:'Валовая выручка, Stars',average:'Средний чек, Stars',buyers:'Покупатели',first_purchases:'Первые покупки',renewals:'Продления',refunds:'Возвращено, Stars',refund_count:'Возвраты',net:'После возвратов, Stars',pending:'Ожидают одобрения'}
createRoot(document.getElementById('root')!).render(<App/>)

