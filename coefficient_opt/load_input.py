# -*- coding: utf-8 -*-
"""严格读取 24 点负荷；允许显式历史因果基准，禁止伪造原始预测。"""
from pathlib import Path
import numpy as np
import pandas as pd
from price_input import time_index

COLUMNS=['客户组合预测_MWh','负荷预测','load_hat','预测负荷','forecast_load']
def read_load(path,fallback,sheet,date):
    primary=Path(str(path).replace('YYYYMMDD',date.strftime('%Y%m%d'))) if path else None
    source=primary if primary and primary.exists() else Path(fallback)
    with pd.ExcelFile(source) as x:
        sheets=x.sheet_names if source==primary else [sheet]
        for name in sheets:
            if name not in x.sheet_names:continue
            f=pd.read_excel(x,sheet_name=name);col=next((n for n in COLUMNS if n in f),None)
            if col is None:continue
            try:t=pd.DatetimeIndex(time_index(f,f'{source}/{name}'))
            except ValueError:
                # 无日期的手填表只接受明确 0..23 小时且恰好 24 行。
                h=next((n for n in ['小时','hour'] if n in f),None)
                if source==primary or h is None or len(f)!=24 or set(f[h])!=set(range(24)):raise
                t=date+pd.to_timedelta(f[h],unit='h')
            s=pd.Series(pd.to_numeric(f[col],errors='coerce').to_numpy(),index=t)
            s=s[s.index.normalize()==date]
            if s.index.has_duplicates:raise ValueError(f'{source}/{name}/{col}：目标日重复小时')
            s=s.reindex(pd.date_range(date,periods=24,freq='h'))
            if s.isna().any() or not np.isfinite(s).all() or (s<0).any():raise ValueError(f'{source}/{name}/{col}：必须有目标日 24 个非负有限值，不能补零')
            return s,{'file':str(source),'sheet':name,'column':col}
    raise ValueError(f'{source}：未找到目标日负荷预测，请提供配套输出或 {sheet} sheet')

def historical_load(path,index,lag=2,forecast_path=None):
    f=pd.read_csv(path);t=time_index(f,path);actual=pd.Series(pd.to_numeric(f.actual_load,errors='coerce').to_numpy(),index=t).sort_index()
    if actual.index.has_duplicates:raise ValueError(f'{path}：实际负荷有重复时刻')
    actual=actual.reindex(index)
    if (actual<0).any():raise ValueError(f'{path}：负荷不能为负数')
    if forecast_path:
        f=pd.read_csv(forecast_path);t=time_index(f,forecast_path)
        if 'available_time' not in f:raise ValueError('历史负荷预测必须提供 available_time')
        available=pd.to_datetime(f.available_time,errors='coerce')
        if available.isna().any() or (available>t.dt.normalize()-pd.Timedelta(days=1)+pd.Timedelta(hours=9)).any():raise ValueError('历史负荷预测存在缺失或晚于申报截点的可用时间')
        forecast=pd.Series(f.load_hat.to_numpy(),index=t)
        if forecast.index.has_duplicates:raise ValueError('历史负荷预测时刻重复')
        forecast=forecast.reindex(index);basis='原始历史预测快照'
    else:
        # 截至 D-2 的同小时 28 天均值；这是可复算基准，不冒充当时留存的生产预测。
        forecast=actual.groupby(actual.index.hour).transform(lambda s:s.shift(lag).rolling(28,min_periods=14).mean())
        basis='因果28日同小时负荷基准重建，非原始预测快照'
    return actual,forecast,basis
