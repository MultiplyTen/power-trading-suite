"""建议与真实申报对账；无事前发布版本时不冒充执行偏差。"""
import json
import pandas as pd
from database.raw_store import ensure

def analyze(db,c,out):
    ensure(db)
    today=pd.Timestamp.now(tz='Asia/Shanghai').tz_localize(None).normalize() if c['review_date']=='today' else pd.Timestamp(c['review_date']).normalize()
    end=today+pd.Timedelta(hours=23,minutes=59,seconds=59)
    lags=c.get('actual_lag_days_by_metric',{})
    d=pd.read_sql_query('SELECT * FROM 申报记录',db)
    settlements=pd.read_sql_query('SELECT * FROM 结算价对照',db)
    def eligible(f,key):
        if f.empty:return f
        return f[(pd.to_datetime(f.available_at,format='mixed')<=end)&(pd.to_datetime(f.date)<=today-pd.Timedelta(days=lags.get(key,0)))].copy()
    d=eligible(d,'declaration');settlements=eligible(settlements,'settlement')
    rows=[]
    for r in d.itertuples():
        target=str(pd.Timestamp(r.date)+pd.Timedelta(hours=r.hour));cutoff=str(pd.Timestamp(r.date)-pd.Timedelta(days=1)+pd.Timedelta(hours=c.get('decision_cutoff_hour',12),minutes=c.get('decision_cutoff_minute',30)))
        # 发布必须发生于D-1申报截止之前，且排除事后回放。
        p=db.execute("SELECT p.load*p.k,p.load*p.research_k,r.id,r.status FROM predictions p JOIN runs r ON r.id=p.run_id WHERE r.kind='coefficient' AND p.target=? AND r.published_at<=? AND r.status NOT LIKE '%replay%' ORDER BY r.published_at DESC LIMIT 1",(target,cutoff)).fetchone()
        recommendation=p[0] if p else None
        rows.append(dict(date=r.date,hour=r.hour,executed_MWh=r.value,recommended_MWh=recommendation,deviation_MWh=r.value-recommendation if recommendation is not None else None,run_id=p[2] if p else '',status='有事前建议可对账' if recommendation is not None else '缺少事前发布建议，不能计算执行偏差',model_scope=p[3] if p else '',source=r.source))
    comparisons=[]
    for r in settlements.itertuples():
        table='日前价' if r.entity=='da' else '实时价'
        a=db.execute('SELECT value FROM '+table+' WHERE date=? AND hour=? AND available_at<=?',(r.date,r.hour,str(end))).fetchone()
        comparisons.append(dict(date=r.date,hour=r.hour,price_type=r.entity,settlement_price=r.value,spot_price=a[0] if a else None,difference=r.value-a[0] if a else None,status='仅口径差异，不替换预测主价'))
    for name,items in [('申报执行偏差',rows),('结算口径对照',comparisons)]:
        # 固定列，空数据仍有表头和说明，不制造0偏差。
        cols=['date','hour','executed_MWh','recommended_MWh','deviation_MWh','run_id','status','model_scope','source'] if name=='申报执行偏差' else ['date','hour','price_type','settlement_price','spot_price','difference','status']
        pd.DataFrame(items,columns=cols).to_csv(out/(name+'.csv'),index=False,encoding='utf-8-sig')
    result=dict(review_date=str(today.date()),declarations=len(rows),comparable_declarations=sum(r['recommended_MWh'] is not None for r in rows),settlement_points=len(comparisons),lags=lags)
    (out/'执行结算复盘摘要.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result
