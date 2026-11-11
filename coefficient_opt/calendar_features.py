# -*- coding: utf-8 -*-
import numpy as np
import pandas as pd
from functools import lru_cache

import sys
from pathlib import Path
sys.path.insert(0,str(next(p for p in Path(__file__).resolve().parents if (p/'database/runtime.py').is_file())))
from database.runtime import day_details

def calendar(index):
    days=index.normalize();unique=days.unique()
    try:
        work={d:day_details(d.date())[0] for d in unique}
        holiday={d:day_details(d.date())[1] and not work[d] for d in unique}
    except NotImplementedError as exc:raise ValueError('日历年份超出chinese_calendar公布范围；请更新日历后运行') from exc
    x=pd.DataFrame(index=index);x['workday']=[float(work[d]) for d in days];x['holiday']=[float(holiday[d]) for d in days]
    x['makeup_workday']=((days.weekday>=5)&x.workday.eq(1)).astype(float)
    for off,name in [(-1,'post_holiday'),(1,'pre_holiday')]:
        neighboring={d:float(day_details((d+pd.Timedelta(days=off)).date())[1] and not holiday[d]) for d in unique}
        x[name]=[neighboring[d] for d in days]
    x['annual_sin']=np.sin(2*np.pi*index.dayofyear/365.25);x['annual_cos']=np.cos(2*np.pi*index.dayofyear/365.25)
    for j in range(1,4):
        x[f'hour_sin{j}']=np.sin(2*np.pi*j*index.hour/24);x[f'hour_cos{j}']=np.cos(2*np.pi*j*index.hour/24)
        x[f'work_sin{j}']=x[f'hour_sin{j}']*x.workday;x[f'work_cos{j}']=x[f'hour_cos{j}']*x.workday
    return x
