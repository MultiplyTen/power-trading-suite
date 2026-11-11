"""天气预报批次原子入库与决策截点取版本；不会将抓取时间伪装成发布时间。"""
import datetime,hashlib,io,json,time,uuid
from urllib.parse import urlencode
from urllib.request import urlopen
import numpy as np
import pandas as pd
from database.store import connect,backup,import_one

VARS=['temperature_2m','relative_humidity_2m','precipitation','shortwave_radiation','wind_speed_100m','cloud_cover','weather_code']
UNITS={'temperature_2m':'°C','relative_humidity_2m':'%','precipitation':'mm','shortwave_radiation':'W/m²','wind_speed_100m':'m/s','cloud_cover':'%','weather_code':'wmo code'}
BOUNDS=[(-90,65),(0,100),(0,1000),(0,2000),(0,150),(0,100),(0,99)]

def now():return pd.Timestamp.now(tz='Asia/Shanghai').tz_localize(None)

def ensure(db):
    db.execute('CREATE TABLE IF NOT EXISTS weather_batches(id TEXT PRIMARY KEY,received_at TEXT NOT NULL,source_url TEXT NOT NULL,sha256 TEXT NOT NULL,payload BLOB NOT NULL,start TEXT NOT NULL,end TEXT NOT NULL,locations_json TEXT NOT NULL,rows INTEGER NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS weather_points(batch_id TEXT NOT NULL REFERENCES weather_batches(id),location_id TEXT NOT NULL,city_name TEXT NOT NULL,region TEXT,valid_time TEXT NOT NULL,'+','.join(v+' REAL NOT NULL' for v in VARS)+',PRIMARY KEY(batch_id,location_id,valid_time))')
    db.execute('CREATE INDEX IF NOT EXISTS weather_received ON weather_batches(received_at)')

def locations(path):
    with connect(path) as db:
        f=pd.read_sql_query('SELECT location_id,city_name,region,latitude,longitude FROM current_load_guangdong_cities',db)
    if f.empty or f.location_id.isna().any() or f.location_id.duplicated().any():raise ValueError('城市坐标清单为空或重复')
    for col,bounds in [('latitude',(-90,90)),('longitude',(-180,180))]:
        if not pd.to_numeric(f[col],errors='coerce').between(*bounds).all():raise ValueError('城市坐标非法：'+col)
    return f

def validate(payload,locs,start,end):
    data=json.loads(payload)
    if isinstance(data,dict):data=[data]
    if not isinstance(data,list) or len(data)!=len(locs):raise ValueError('API返回城市数与请求不一致')
    idx=pd.date_range(start,pd.Timestamp(end)+pd.Timedelta(hours=23),freq='h');parts=[]
    for loc,item in zip(locs.to_dict('records'),data):
        if item.get('utc_offset_seconds')!=28800:raise ValueError(loc['city_name']+'时区不是北京时间')
        if abs(float(item['latitude'])-loc['latitude'])>.5 or abs(float(item['longitude'])-loc['longitude'])>.5:raise ValueError('返回网格与请求城市不匹配')
        if any(item.get('hourly_units',{}).get(k)!=v for k,v in UNITS.items()):raise ValueError(loc['city_name']+'天气单位不符合契约')
        f=pd.DataFrame(item['hourly']);f=f.rename(columns={'time':'valid_time'})
        ts=pd.DatetimeIndex(pd.to_datetime(f.valid_time,errors='raise'))
        if not ts.equals(idx):raise ValueError(loc['city_name']+'逐小时时间有缺口、重复或越界')
        for v,(lo,hi) in zip(VARS,BOUNDS):
            if v not in f:raise ValueError('缺少天气变量：'+v)
            f[v]=pd.to_numeric(f[v],errors='raise')
            if not f[v].between(lo,hi).all():raise ValueError(loc['city_name']+'/'+v+'缺失、非有限数或超范围')
        if not f.weather_code.mod(1).eq(0).all():raise ValueError('天气代码非整数')
        f['valid_time']=ts;f['location_id']=loc['location_id'];f['city_name']=loc['city_name'];f['region']=loc['region'];parts.append(f)
    return pd.concat(parts,ignore_index=True)

def select(path,cutoff,start,end,max_age_hours=12,required_ids=None):
    """选取截点前、未过期、所有城市全时段齐全的最近批次。"""
    cutoff=pd.Timestamp(cutoff);start=pd.Timestamp(start);end=pd.Timestamp(end)
    with connect(path) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='weather_batches'").fetchone():return pd.DataFrame()
        batches=db.execute('SELECT id,received_at,source_url,sha256,locations_json FROM weather_batches WHERE received_at<=? AND received_at>=? ORDER BY received_at DESC',(str(cutoff),str(cutoff-pd.Timedelta(hours=max_age_hours)))).fetchall()
        for bid,receipt,url,digest,ids in batches:
            expected=set(required_ids) if required_ids is not None else set(json.loads(ids))
            if not expected<=set(json.loads(ids)):continue
            f=pd.read_sql_query('SELECT * FROM weather_points WHERE batch_id=? AND valid_time>=? AND valid_time<=?',db,params=[bid,str(start),str(end)])
            f=f[f.location_id.isin(expected)].copy()
            idx=pd.date_range(start,end,freq='h')
            if set(f.location_id)!=expected or any(not pd.DatetimeIndex(pd.to_datetime(g.valid_time)).sort_values().equals(idx) for _,g in f.groupby('location_id')):continue
            f['valid_time']=pd.to_datetime(f.valid_time);f['retrieved_at']=pd.Timestamp(receipt);f['issue_time']=pd.NaT;f['assumed_available_time']=pd.Timestamp(receipt)
            f['availability_basis']='locally_archived_retrieval_time';f['mode']='live_snapshot';f['source_sha256']=digest;f['source_url']=url
            return f.drop(columns='batch_id')
    return pd.DataFrame()

def save(path,payload,f,url,receipt,start,end,locs):
    backup(path);bid=uuid.uuid4().hex;digest=hashlib.sha256(payload).hexdigest()
    with connect(path) as db:
        db.execute('BEGIN IMMEDIATE');ensure(db)
        db.execute('INSERT INTO weather_batches VALUES(?,?,?,?,?,?,?,?,?)',(bid,str(receipt),url,digest,payload,str(start),str(end),json.dumps(locs.location_id.tolist()),len(f)))
        columns=['location_id','city_name','region','valid_time']+VARS
        db.executemany('INSERT INTO weather_points VALUES('+','.join('?' for _ in range(12))+')',[(bid,*[str(v) if isinstance(v,pd.Timestamp) else v for v in row]) for row in f[columns].itertuples(index=False,name=None)])
        # 同事务发布旧load天气适配表；迁移代码/旧工具仍能读取当前版本。
        g=f.copy();g['retrieved_at']=str(receipt);g['issue_time']=None;g['assumed_available_time']=str(receipt);g['availability_basis']='locally_archived_retrieval_time';g['mode']='live_snapshot';g['source_sha256']=digest;g['source_url']=url
        cols=[r[1] for r in db.execute('PRAGMA table_info(load_weather_live)') if r[1] not in ['_version','_row']]
        if not cols:raise ValueError('缺少已注册未来天气数据契约，请恢复完整数据库')
        raw=g.reindex(columns=cols).to_csv(index=False).encode('utf-8')
        import gzip
        import_one(db,'load_forecast/data/weather_live.csv.gz',gzip.compress(raw,mtime=0),'weather_api','自动抓取未来天气',bid)
        db.execute('INSERT INTO maintenance_log(time,action,batch_id,detail_json) VALUES(?,?,?,?)',(str(receipt),'weather_refresh',bid,json.dumps({'rows':len(f),'cities':len(locs)},ensure_ascii=False)))
    return bid

def refresh(path,c,force=False,fetcher=None):
    cfg=c.get('weather_auto',{});enabled=cfg.get('enabled',True)
    if not enabled:return {'status':'disabled','message':'天气自动更新已由配置关闭'}
    days=int(cfg.get('forecast_days',7));ttl=float(cfg.get('refresh_interval_hours',1));timeout=float(cfg.get('timeout_seconds',40));attempts=int(cfg.get('retries',3))
    if not 1<=days<=14 or ttl<=0 or timeout<=0 or not 1<=attempts<=5:raise ValueError('天气更新配置非法')
    stamp=now();start=stamp.normalize();end=start+pd.Timedelta(days=days+1);locs=locations(path)
    if not force:
        cached=select(path,stamp,start,end+pd.Timedelta(hours=23),ttl,locs.location_id)
        if not cached.empty:return dict(status='cached',cities=len(locs),rows=len(cached),received_at=str(cached.retrieved_at.max()),message='刷新间隔内已有完整天气缓存，免重复请求')
    params=dict(latitude=','.join(locs.latitude.astype(str)),longitude=','.join(locs.longitude.astype(str)),hourly=','.join(VARS),timezone='Asia/Shanghai',wind_speed_unit='ms',start_date=str(start.date()),end_date=str(end.date()))
    url='https://api.open-meteo.com/v1/forecast?'+urlencode(params)
    def fetch(url,timeout):
        with urlopen(url,timeout=timeout) as r:return r.read()
    fetcher=fetcher or fetch;error=None
    for attempt in range(attempts):
        try:
            raw=fetcher(url,timeout);f=validate(raw,locs,start,end);receipt=now();bid=save(path,raw,f,url,receipt,start,end,locs)
            return dict(status='updated',batch_id=bid,cities=len(locs),rows=len(f),received_at=str(receipt),first=str(f.valid_time.min()),last=str(f.valid_time.max()),forecast_days=days,message='天气已校验并写入统一数据库；原始响应与历史版本保留')
        except Exception as exc:
            error=str(exc)
            if attempt+1<attempts:time.sleep(min(2**attempt,4))
    cached=select(path,now(),start+pd.Timedelta(days=1),start+pd.Timedelta(days=days,hours=23),cfg.get('max_cache_age_hours',12),locs.location_id)
    return dict(status='cached_after_failure' if not cached.empty else 'failed',error=error,cities=len(locs),rows=len(cached),received_at=str(cached.retrieved_at.max()) if not cached.empty else None,message='抓取失败，保留旧版本；模型仍按决策截点核验，过期/迟到天气不会强行使用')
