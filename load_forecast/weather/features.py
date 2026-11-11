import numpy as np
import pandas as pd
from configuration import resolve,cutoff
from weather.client import fetch
from database.trading import dataset_frame

VARS=['temperature_2m','relative_humidity_2m','precipitation','shortwave_radiation','wind_speed_100m','cloud_cover']

def read_weather(c,day,live=False):
    path=resolve(c,c['weather']['history_file'])
    h=dataset_frame(path,resolve(c,c['db_path']));h['valid_time']=pd.to_datetime(h.valid_time)
    today=pd.Timestamp.now(tz=c['project']['timezone']).tz_localize(None).normalize()
    needed=min(day,today)-pd.Timedelta(days=1)
    if c['weather']['enable_network'] and (h.empty or h.valid_time.max()<needed+pd.Timedelta(hours=23)):
        start=(h.valid_time.max().normalize()+pd.Timedelta(days=1)) if len(h) else pd.Timestamp(c['backtest'].get('model_selection_start') or c['backtest']['validation_start'])-pd.Timedelta(days=c['training']['window_days']+c['training']['baseline_window_days'])
        locations=dataset_frame(resolve(c,c['weather']['locations_file']),resolve(c,c['db_path']));parts=[]
        for loc in locations.to_dict('records'):parts.append(fetch(loc,start,needed,'previous_runs',c,resolve(c,c['project']['cache_dir'])))
        h=pd.concat([h]+parts,ignore_index=True);path.parent.mkdir(parents=True,exist_ok=True);h.to_csv(path,index=False,compression='infer')
    if h.empty:raise ValueError('历史天气为空；请补充固定提前量预报或启用weather.enable_network')
    if not h['mode'].eq('previous_day'+str(c['weather']['previous_run_lead_days'])).all():raise ValueError('历史天气不是指定提前量预报；实况不得进入目标日预测')
    if h.duplicated(['city_name','valid_time']).any():raise ValueError('天气重复版本需先按截点筛选')
    status={'history':'固定提前量预报，原始可得时点未核验','future':'not_requested'}
    # Each hourly feature uses its own historic forecast; live values are replaced only
    # after a single fixed origin. No future day2 runs are admitted to long horizon.
    future=pd.DataFrame()
    if live:
        fp=resolve(c,c['weather']['future_file']);candidate=dataset_frame(fp,resolve(c,c['db_path']));candidate['valid_time']=pd.to_datetime(candidate.valid_time);candidate['retrieved_at']=pd.to_datetime(candidate.retrieved_at)
        cut=cutoff(c,day)
        from database.weather import select,locations
        db_path=resolve(c,c['db_path'])
        stored=select(db_path,cut,day,day+pd.Timedelta(days=7),c['weather']['max_live_cache_age_hours'],locations(db_path).location_id)
        if not stored.empty:candidate=stored
        if len(candidate):
            candidate=candidate[(candidate.retrieved_at<=cut)&((cut-candidate.retrieved_at)<=pd.Timedelta(hours=c['weather']['max_live_cache_age_hours']))]
            required=set(locations(db_path).location_id)
            required_hours=pd.date_range(day,day+pd.Timedelta(hours=23),freq='h')
            complete=set(candidate.location_id)==required and all(required_hours.isin(pd.DatetimeIndex(g.valid_time)).all() for _,g in candidate.groupby('location_id'))
            future=candidate if complete else pd.DataFrame()
        status['future']='合格固定起点缓存' if len(future) else '无截点前合格预报；远期只可气候参考'
    for col in VARS:
        if col not in h:h[col]=np.nan
    h=h.sort_values(['city_name','valid_time'])
    for v in ['precipitation','shortwave_radiation']:h[v]=h.groupby('city_name')[v].shift(-1)
    if len(future):
        future=future.sort_values(['city_name','valid_time'])
        for v in ['precipitation','shortwave_radiation']:future[v]=future.groupby('city_name')[v].shift(-1)
    return h,future,status

def groups(c,h,tables,end):
    result={'province':h.groupby('valid_time')[VARS].mean()}
    result.update({city:g.set_index('valid_time')[VARS] for city,g in h.groupby('city_name')})
    customer=tables['customers'].merge(tables['mapping'][['entity_id','city_name']],on='entity_id',validate='m:1');customer['date']=customer.valid_time.dt.normalize()
    daily=customer.groupby(['date','city_name']).value.sum(min_count=1).unstack().reindex(pd.date_range(customer.date.min(),end))
    weight=daily.shift(c['run']['observation_lag_days']).rolling(c['weather']['portfolio_weight_days'],min_periods=7).sum();weight=weight.div(weight.sum(axis=1),axis=0)
    a=h.copy();a['date']=a.valid_time.dt.normalize();weight.index.name='date';z=weight.stack().rename('weight').reset_index();a=a.merge(z,on=['date','city_name'],validate='m:1')
    for v in VARS:a[v]*=a.weight
    result['portfolio']=a.groupby('valid_time')[VARS].sum(min_count=1)
    return result,weight

def add_weather(x,w,threshold):
    w=w.reindex(x.index)
    x=x.copy();x['wx_cooling']=np.maximum(w.temperature_2m-threshold,0);x['wx_cooling_squared']=x.wx_cooling**2
    x['wx_temperature_change7']=w.temperature_2m-w.temperature_2m.shift(168)
    x['wx_humidity']=w.relative_humidity_2m/100;x['wx_heat_humidity']=x.wx_cooling*x.wx_humidity
    x['wx_rain_log']=np.log1p(w.precipitation.clip(lower=0));x['wx_radiation']=w.shortwave_radiation/1000
    x['wx_wind']=w.wind_speed_100m;x['wx_cloud']=w.cloud_cover/100
    x['wx_daytime_heat']=x.wx_cooling*((x.index.hour>=8)&(x.index.hour<=20))
    return x
