# -*- coding: utf-8 -*-
"""历史预报与实时预报分开；WMO 编码采用类别哑变量。"""
import hashlib,json
from pathlib import Path
import numpy as np
import pandas as pd
import requests
from database.trading import dataset_frame

VARIABLES=['temperature_2m','relative_humidity_2m','precipitation','shortwave_radiation','wind_speed_100m','cloud_cover','weather_code']
def get_weather(c,index,target,root):
    path=root/c['weather_history_path'];f=dataset_frame(path,root/c['db_path'])
    f['valid_time']=pd.to_datetime(f.valid_time)
    if f.valid_time.duplicated().any():raise ValueError(f'{path}：天气存在重复时刻，请仅配置一个城市')
    f=f.set_index('valid_time')
    if 'mode' not in f or not f['mode'].astype(str).str.startswith('previous_day').all():raise ValueError('历史天气必须是历史预报，不可用目标日实况替代')
    today=pd.Timestamp.now(tz='Asia/Shanghai').tz_localize(None).normalize()
    if target>=today:
        from database.weather import select,locations
        locs=locations(root/c['db_path'])
        distance=(locs.latitude-c['city']['latitude'])**2+(locs.longitude-c['city']['longitude'])**2
        loc=locs.loc[distance.idxmin()]
        if distance.min()>.01:raise ValueError('系数模块城市没有对应天气坐标，请更新城市清单')
        cutoff=target-pd.Timedelta(days=1)+pd.Timedelta(hours=c.get('decision_cutoff_hour',12),minutes=c.get('decision_cutoff_minute',30))
        live=select(root/c['db_path'],cutoff,target,target+pd.Timedelta(hours=23),c.get('weather_max_cache_age_hours',12),[loc.location_id])
        if live.empty:raise ValueError('目标日缺少申报截止前、未过期的数据库天气预报；请先运行一键导入数据/天气更新，迟到预报不可回填历史截点')
        live=live.set_index('valid_time')
        f=pd.concat([f.loc[~f.index.isin(live.index)],live]).sort_index()
    x=pd.DataFrame(index=index)
    for col in VARIABLES:x[col]=pd.to_numeric(f[col],errors='coerce').reindex(index) if col in f else np.nan
    if x.loc[x.index.normalize()==target,'temperature_2m'].isna().any():raise ValueError('目标日天气不完整；请补充历史预报或打开 network_enabled 获取明日预报')
    x['CDD']=np.maximum(x.temperature_2m-c.get('t_base',26),0);x['heat_humidity']=x.CDD*x.relative_humidity_2m/100
    code=x.pop('weather_code')
    for name,codes in {'clear':[0,1],'cloud':[2,3],'fog':[45,48],'rain':[51,53,55,56,57,61,63,65,66,67,80,81,82],'snow':[71,73,75,77,85,86],'storm':[95,96,99]}.items():
        x['wmo_'+name]=code.isin(codes).astype(float).where(code.notna())
    return x
