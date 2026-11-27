import {useEffect,useRef} from 'react'
import {AreaSeries,ColorType,createChart,type UTCTimestamp} from 'lightweight-charts'
type Point={date:string;price:string}
export default function PriceChart({points,decor=false}:{points:Point[];decor?:boolean}){
 const ref=useRef<HTMLDivElement>(null)
 useEffect(()=>{
  if(!ref.current||!points.length)return
  const chart=createChart(ref.current,{autoSize:true,layout:{background:{type:ColorType.Solid,color:'transparent'},textColor:'#84919c',fontSize:11,attributionLogo:false},
   grid:{vertLines:{visible:false},horzLines:{color:'#222932'}},rightPriceScale:{borderVisible:false},timeScale:{borderVisible:false},crosshair:{vertLine:{color:'#698b71'},horzLine:{color:'#698b71'}},
   handleScroll:!decor,handleScale:!decor})
  const series=chart.addSeries(AreaSeries,{lineColor:'#299bf0',topColor:'rgba(41,155,240,.22)',bottomColor:'rgba(41,155,240,.01)',lineWidth:2,priceLineVisible:false,lastValueVisible:false})
  const unique=new Map<number,number>()
  for(const p of points){const at=Math.floor(new Date(p.date).getTime()/1000);if(Number.isFinite(at)&&Number.isFinite(Number(p.price)))unique.set(at,Number(p.price))}
  series.setData([...unique].sort((a,b)=>a[0]-b[0]).map(([time,value])=>({time:time as UTCTimestamp,value})))
  chart.timeScale().fitContent()
  return ()=>chart.remove()
 },[points,decor])
 return <div ref={ref} className="chart" aria-label="График цен"/>
}

