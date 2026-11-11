"""Fixed-origin recursive reference. All execution fields remain empty."""
import hashlib
import numpy as np
import pandas as pd
from configuration import resolve,cutoff
from features.load import make_features
from models.base import LinearPlugin
from evaluation.rolling_backtest import dense,price_features
from weather.features import groups,VARS

def make_reference(c,tables,hist,live,result,day,progress=print):
    progress('生成固定起点30天参考；远期执行字段保持空白')
    end=day+pd.Timedelta(days=c['run']['reference_days']-1);cut=cutoff(c,day);lag=c['run']['observation_lag_days'];known=day-pd.Timedelta(days=lag)+pd.Timedelta(hours=23)
    # Never use future archive runs that would only be issued after this origin.
    past=hist[hist.valid_time<day].copy();future=live.copy()
    if len(future):future=future[pd.to_datetime(future.retrieved_at)<=cut]
    wxrows=[]
    for city,g in past.groupby('city_name'):
        doy=g.valid_time.dt.dayofyear
        for d in pd.date_range(day-pd.Timedelta(days=lag-1),end+pd.Timedelta(days=1)):
            live_day=future[(future.city_name==city)&(future.valid_time.dt.normalize()==d)] if len(future) else pd.DataFrame()
            distance=np.minimum(abs(doy-d.dayofyear),365-abs(doy-d.dayofyear));sample=g[distance<=30];byhour=sample.groupby(sample.valid_time.dt.hour)[VARS];means=byhour.mean();counts=byhour.size()
            for hour in range(24):
                ts=d+pd.Timedelta(hours=hour);matched=live_day[live_day.valid_time==ts] if len(live_day) else pd.DataFrame()
                if len(matched):row=matched.iloc[-1].to_dict();row['weather_reference']='fixed_origin_forecast'
                else:
                    enough=counts.get(hour,0)>=7
                    row={v:means.loc[hour,v] if enough else np.nan for v in VARS};row.update(valid_time=ts,city_name=city,weather_reference='historical_climatology_reference' if enough else 'missing_climatology')
                wxrows.append(row)
    combined=pd.concat([past[past.valid_time<=known],pd.DataFrame(wxrows)],ignore_index=True).drop_duplicates(['city_name','valid_time'],keep='last')
    wx,weights=groups(c,combined,tables,end+pd.Timedelta(days=1))
    # Portfolio weather exposure is fixed from observed historical energy at origin.
    if len(weights.dropna(how='all')):
        w=weights.loc[:day].dropna(how='all').iloc[-1];index=pd.date_range(day-pd.Timedelta(days=lag-1),end+pd.Timedelta(hours=23),freq='h');weighted=pd.DataFrame(0.,index=index,columns=VARS)
        for city,value in w.dropna().items():weighted+=wx[city].reindex(index)*value
        wx['portfolio']=pd.concat([wx['portfolio'][wx['portfolio'].index<index.min()],weighted])
    current={k:v.copy() for k,v in tables.items()};models={};maps=tables['mapping'].set_index('entity_id');objects={'system_load':('system_load',wx['province']),'portfolio':('portfolio',wx['portfolio'])}
    for eid in tables['customers'].entity_id.unique():objects[eid]=('customers',wx[maps.loc[eid,'city_name']])
    series={}
    for obj,(key,w) in objects.items():
        table=tables[key] if key!='customers' else tables[key][tables[key].entity_id==obj];series[obj]=dense(table,end)
        chosen=result['parameters'][obj]['selected']
        if chosen not in ['baseline','lag7']:models[obj]=LinearPlugin.load(resolve(c,c['project']['model_dir'])/(hashlib.sha256((obj+chosen).encode()).hexdigest()[:20]+'.joblib'),known)
    for obj in ['da','rt']:
        selected=result['parameters'][obj]['selected']
        if selected not in ['baseline','lag7']:models[obj]=LinearPlugin.load(resolve(c,c['project']['model_dir'])/(obj+'_'+selected+'.joblib'),known)
    records=[];syhist=result['features'].system_oos_prediction*10000
    for d in pd.date_range(known.normalize()+pd.Timedelta(days=1),end):
        idx=pd.date_range(d,periods=24,freq='h');pred={}
        for obj,(key,w) in objects.items():
            spec=result['parameters'][obj];temp=spec['parameters'].get('cooling_threshold',26);f,x=make_features(series[obj],w,c,temp);chosen=spec['selected']
            if chosen=='baseline':p=f.baseline.reindex(idx)
            elif chosen=='lag7':p=series[obj].shift(168).reindex(idx)
            else:
                m=models[obj];p=np.maximum(f.loc[idx,'baseline']+f.loc[idx,'scale']*m.predict(x.loc[idx,m.columns]),0)
            series[obj].loc[idx]=np.asarray(p);pred[obj]=np.asarray(p)
        syhist=syhist[~syhist.index.isin(idx)];syhist=pd.concat([syhist,pd.Series(pred['system_load'],index=idx)]).sort_index()
        f,x=price_features(current,syhist,wx['province'],c,end)
        for obj,key in [('da','day_ahead'),('rt','real_time')]:
            sel=result['parameters'][obj]['selected']
            if sel=='baseline':p=f.loc[idx,obj+'_base']
            elif sel=='lag7':p=f[obj].shift(168).reindex(idx)
            else:p=f.loc[idx,obj+'_base']+models[obj].predict(x.loc[idx,models[obj].columns])
            pred[obj]=np.asarray(p);new=pd.DataFrame(dict(valid_time=idx,value=np.asarray(p),entity_id=key));current[key]=pd.concat([current[key],new],ignore_index=True)
        if d>=day:
            for j,ts in enumerate(idx):
                rec=dict(valid_time=ts,forecast_origin=str(cut),lead_days=(d-day).days+1,load_hat=pred['portfolio'][j],system_load_hat=pred['system_load'][j],da_hat=pred['da'][j],rt_hat=pred['rt'][j],research_k=1.,adopted_k=np.nan,declaration_quantity=np.nan,weather_reference='fixed_origin_forecast' if len(future) and ts in set(future.valid_time) else 'historical_climatology_reference',status='reference_only_unvalidated_multistep')
                records.append(rec)
    return pd.DataFrame(records)
