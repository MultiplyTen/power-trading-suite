# -*- coding: utf-8 -*-
from functools import lru_cache
import os,yaml
from database.store import ROOT,connect,materialize,suite_lock

def prepare(project):
    c=yaml.safe_load((ROOT/project/'config.yaml').read_text(encoding='utf-8'))
    os.environ['POWER_TRADING_DB']=str((ROOT/project/c['db_path']).resolve())
    with suite_lock():
        return materialize(ROOT,project,os.environ['POWER_TRADING_DB'])

@lru_cache(maxsize=1)
def calendar_rows():
    try:
        with connect() as db:
            return {date:(bool(work),bool(named)) for date,work,named in db.execute('SELECT date,is_workday,has_holiday_name FROM current_shared_calendar')}
    except ValueError:
        # 无数据库环境（如发布版仓库/全新克隆）：回退读随包日历快照（chinese-calendar 打包的公共节假日表，非私有数据）。
        import pandas as pd
        f=ROOT/'database'/'calendar.csv'
        if not f.exists():raise
        cal=pd.read_csv(f,dtype={'date':str,'is_workday':int,'has_holiday_name':int})
        return {r.date:(bool(r.is_workday),bool(r.has_holiday_name)) for r in cal.itertuples(index=False)}

def day_details(date):
    try:return calendar_rows()[str(date)]
    except KeyError:raise ValueError('统一数据库未覆盖日历日期 '+str(date)+'，请按数据库维护说明更新节假日/调休表')
