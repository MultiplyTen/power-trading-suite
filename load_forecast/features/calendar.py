import numpy as np
import pandas as pd
from datetime import timedelta

import sys
from pathlib import Path
sys.path.insert(0,str(next(p for p in Path(__file__).resolve().parents if (p/'database/runtime.py').is_file())))
from database.runtime import day_details

def calendar(index, feature_set='basic'):
    """Public-calendar features; no customer production plan is inferred.

    ``basic`` keeps the historical feature schema for comparisons. ``enhanced``
    adds weekday shape and statutory-holiday position/recovery features. Future
    public calendar dates are allowed; future load or price values are not read.
    """
    if feature_set not in ('basic', 'enhanced'):
        raise ValueError('calendar_features只支持basic/enhanced')
    days=index.normalize();unique=days.unique()
    try:
        work={d:day_details(d.date())[0] for d in unique}
        holiday={d:day_details(d.date())[1] and not work[d] for d in unique}
    except NotImplementedError as exc:raise ValueError('日历年份超出chinese_calendar公布范围；请更新日历后运行') from exc
    x=pd.DataFrame(index=index);x['workday']=[float(work[d]) for d in days];x['holiday']=[float(holiday[d]) for d in days]
    x['makeup_workday']=((days.weekday>=5)&x.workday.eq(1)).astype(float)
    for off,name in [(-1,'post_holiday'),(1,'pre_holiday')]:
        neighboring={d:float(day_details((d+timedelta(days=off)).date())[1] and not holiday[d]) for d in unique}
        x[name]=[neighboring[d] for d in days]
    x['annual_sin']=np.sin(2*np.pi*index.dayofyear/365.25);x['annual_cos']=np.cos(2*np.pi*index.dayofyear/365.25)
    for j in range(1,4):
        x[f'hour_sin{j}']=np.sin(2*np.pi*j*index.hour/24);x[f'hour_cos{j}']=np.cos(2*np.pi*j*index.hour/24)
        x[f'work_sin{j}']=x[f'hour_sin{j}']*x.workday;x[f'work_cos{j}']=x[f'hour_cos{j}']*x.workday
    if feature_set == 'enhanced':
        _enhance(x, days, unique)
    return x


def _enhance(x, days, unique):
    # Schema is fixed even when a training window lacks some weekdays.
    for weekday, name in enumerate(('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')):
        flag = (days.dayofweek == weekday).astype(float)
        x[f'weekday_{name}'] = flag
        # One harmonic per weekday limits the additional hourly interactions.
        x[f'weekday_{name}_hour_sin'] = flag * x['hour_sin1']
        x[f'weekday_{name}_hour_cos'] = flag * x['hour_cos1']

    details = {}

    def statutory(day):
        if day not in details:
            workday, named_holiday = day_details(day.date())
            # Makeup workdays can carry a holiday name; ordinary weekends do not
            # become statutory holidays just because they are non-working days.
            details[day] = bool(named_holiday and not workday)
        return details[day]

    def distance_to_edge(day, direction):
        distance = 0
        while statutory(day + timedelta(days=direction * (distance + 1))):
            distance += 1
        return distance

    position = {}
    remaining = {}
    recovery = {offset: {} for offset in (1, 2, 3)}
    pre = {}
    post = {}
    for day in unique:
        is_holiday = statutory(day)
        position[day] = distance_to_edge(day, -1) + 1 if is_holiday else 0
        # Number of statutory-holiday days after this date; last day is zero.
        remaining[day] = distance_to_edge(day, 1) if is_holiday else 0
        pre[day] = float(not is_holiday and statutory(day + timedelta(days=1)))
        post[day] = float(not is_holiday and statutory(day - timedelta(days=1)))
        for offset in (1, 2, 3):
            end = day - timedelta(days=offset)
            recovery[offset][day] = float(
                not is_holiday and statutory(end)
                and all(not statutory(end + timedelta(days=k)) for k in range(1, offset + 1))
            )
    # Enhanced neighbors use actual statutory days, excluding named makeup days;
    # basic retains its prior neighbor indicators for the historical comparison.
    x['pre_holiday'] = [pre[day] for day in days]
    x['post_holiday'] = [post[day] for day in days]
    x['holiday_day_index'] = [float(position[day]) for day in days]
    x['holiday_days_remaining'] = [float(remaining[day]) for day in days]
    for offset in (1, 2, 3):
        x[f'post_holiday_day{offset}'] = [recovery[offset][day] for day in days]
