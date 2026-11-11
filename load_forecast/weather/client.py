"""Immutable Open-Meteo responses; receipt times are never backdated."""
import hashlib,json,time
from pathlib import Path
import pandas as pd
import requests

def fetch(location,start,end,mode,c,cache):
    w=c['weather'];suffix=f'_previous_day{w["previous_run_lead_days"]}' if mode=='previous_runs' else ''
    endpoints={'forecast':'https://api.open-meteo.com/v1/forecast','previous_runs':'https://previous-runs-api.open-meteo.com/v1/forecast','archive':'https://archive-api.open-meteo.com/v1/archive'}
    params=dict(latitude=location['latitude'],longitude=location['longitude'],start_date=str(pd.Timestamp(start).date()),end_date=str(pd.Timestamp(end).date()),timezone=c['project']['timezone'],wind_speed_unit='ms',hourly=','.join(v+suffix for v in w['variables']))
    response=None
    for attempt in range(w['retries']):
        try:
            response=requests.get(endpoints[mode],params=params,timeout=w['timeout_seconds']);response.raise_for_status();payload=response.json()
            if 'hourly' not in payload:raise ValueError('天气接口无hourly字段')
            break
        except (requests.RequestException,ValueError):
            if attempt+1==w['retries']:raise
            time.sleep(min(2**attempt,8))
    now=pd.Timestamp.now(tz=c['project']['timezone']).tz_localize(None);raw=response.content;digest=hashlib.sha256(raw).hexdigest()
    cache=Path(cache);cache.mkdir(parents=True,exist_ok=True);(cache/(digest+'.json')).write_bytes(raw)
    meta=dict(source_url=response.url,sha256=digest,retrieved_at=str(now),issue_time=None,requested_location=location,grid_latitude=payload['latitude'],grid_longitude=payload['longitude'],units=payload.get('hourly_units'),mode=mode,availability_basis='receipt_time' if mode=='forecast' else 'fixed_lead_unverified' if mode=='previous_runs' else 'reanalysis_not_forecast')
    (cache/(digest+'.meta.json')).write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding='utf-8')
    f=pd.DataFrame(payload['hourly']).rename(columns={'time':'valid_time',**{v+suffix:v for v in w['variables']}})
    f['valid_time']=pd.to_datetime(f.valid_time);f['city_name']=location['city_name'];f['retrieved_at']=now;f['issue_time']=pd.NaT;f['mode']='previous_day'+str(w['previous_run_lead_days']) if mode=='previous_runs' else mode;f['source_sha256']=digest;f['source_url']=response.url;f['availability_basis']=meta['availability_basis']
    f['assumed_available_time']=f.valid_time-pd.Timedelta(days=w['previous_run_lead_days']) if mode=='previous_runs' else now
    return f
