# -*- coding: utf-8 -*-
"""价格 Excel 自动识别及可审计 SQLite 缓存。"""
import hashlib, logging, sqlite3
from pathlib import Path
import pandas as pd

ALIASES={'da':['日前价格','日前价','日前','da','day_ahead'], 'rt':['实时价格','实时价','实时','rt','real_time']}

def time_index(f, source):
    for col in ['目标时刻','timestamp','valid_time','时间戳']:
        if col in f:return pd.to_datetime(f[col],errors='raise')
    date=next((x for x in ['日期','date'] if x in f),None)
    hour=next((x for x in ['小时','hour','时间'] if x in f),None)
    if date and hour:
        h=f[hour]
        if pd.api.types.is_numeric_dtype(h):
            if not h.between(0,23).all():raise ValueError(f'{source}：小时必须是起点 0—23')
            return pd.to_datetime(f[date])+pd.to_timedelta(h,unit='h')
        return pd.to_datetime(f[date].astype(str)+' '+h.astype(str))
    raise ValueError(f'{source}：缺少目标时刻或日期/小时列')

def parse(path):
    pieces={}
    with pd.ExcelFile(path) as x:
        for sheet in x.sheet_names:
            f=pd.read_excel(x,sheet_name=sheet)
            for kind,names in ALIASES.items():
                col=next((n for n in names if n in f),None)
                if col is None and sheet in names:col=next((n for n in ['价格','value','电价'] if n in f),None)
                if col is None:continue
                t=time_index(f,f'{path}/{sheet}')
                values=pd.to_numeric(f[col],errors='coerce')
                for i in f.index[values.isna()]:logging.warning('%s/%s 第 %s 行 %s 缺失，保留缺失不填零',path,sheet,i+2,col)
                s=pd.Series(values.to_numpy(),index=pd.DatetimeIndex(t),name=kind)
                if s.index.has_duplicates:raise ValueError(f'{path}/{sheet}：重复目标时刻，须先确定版本')
                if kind in pieces:raise ValueError(f'{path}：多个 {kind} 来源，不能自动选择')
                pieces[kind]=s
    if set(pieces)!={'da','rt'}:raise ValueError(f'{path}：未识别日前与实时价格，两列或两个同名 sheet 均支持')
    out=pd.concat(pieces.values(),axis=1).sort_index()
    if not ((out.index.minute==0)&(out.index.second==0)).all():raise ValueError(f'{path}：只接受小时起点数据')
    return out

def read_prices(path,cache):
    path=Path(path);cache=Path(cache);cache.parent.mkdir(parents=True,exist_ok=True)
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    with sqlite3.connect(cache) as db:
        db.execute('CREATE TABLE IF NOT EXISTS sources(path TEXT PRIMARY KEY, sha TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS prices(source TEXT,t TEXT,da REAL,rt REAL,PRIMARY KEY(source,t))')
        key=str(path.resolve());old=db.execute('SELECT sha FROM sources WHERE path=?',(key,)).fetchone()
        if not old or old[0]!=digest:
            f=parse(path);rows=[(key,str(t),None if pd.isna(r.da) else float(r.da),None if pd.isna(r.rt) else float(r.rt)) for t,r in f.iterrows()]
            db.executemany('INSERT INTO prices VALUES(?,?,?,?) ON CONFLICT(source,t) DO UPDATE SET da=excluded.da,rt=excluded.rt WHERE da IS NOT excluded.da OR rt IS NOT excluded.rt',rows)
            # 当前工作簿视为快照；删除的日期不可残留在缓存中。
            db.execute('CREATE TEMP TABLE current_times(t TEXT PRIMARY KEY)');db.executemany('INSERT INTO current_times VALUES(?)',[(r[1],) for r in rows])
            db.execute('DELETE FROM prices WHERE source=? AND t NOT IN (SELECT t FROM current_times)',(key,))
            db.execute('INSERT OR REPLACE INTO sources VALUES(?,?)',(key,digest));logging.info('价格文件已解析，缓存增量更新 %s 行',len(f))
        else:logging.info('价格文件未变，直接使用 SQLite，无需解析 Excel')
        f=pd.read_sql_query('SELECT t,da,rt FROM prices WHERE source=? ORDER BY t',db,params=[key],parse_dates=['t']).set_index('t')
    return f,digest
