# -*- coding: utf-8 -*-
"""不可变预测版本、实际值版本及按发布时间对账。"""
import json,sqlite3,logging
from pathlib import Path
import numpy as np
import pandas as pd
from common import resolve,digest,stamp,dump

SCHEMA='''
CREATE TABLE runs(id TEXT PRIMARY KEY, kind TEXT, published_at TEXT, model_version TEXT, config_json TEXT, source TEXT, sha TEXT, status TEXT);
CREATE TABLE predictions(run_id TEXT, target TEXT, load REAL, da REAL, rt REAL, k REAL, research_k REAL, PRIMARY KEY(run_id,target));
CREATE TABLE actuals(target TEXT, metric TEXT, value REAL, available_at TEXT, evidence TEXT, source_sha TEXT, ingested_at TEXT, PRIMARY KEY(target,metric,source_sha));
CREATE TABLE audit_events(time TEXT, action TEXT, detail TEXT);
'''
def connect(path,initialize=False):
 path=Path(path)
 if not path.exists():
  if not initialize:raise ValueError(f'对账数据库不存在：{path}。首次使用请执行 python main.py init；若曾使用，请从备份恢复，禁止覆盖旧库。')
  path.parent.mkdir(parents=True,exist_ok=True)
  with sqlite3.connect(path) as db:db.executescript(SCHEMA);db.execute('INSERT INTO audit_events VALUES(?,?,?)',(stamp(),'initialize','明确初始化新数据库'))
 try:
  db=sqlite3.connect(path)
  if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('完整性检查失败')
  required={'runs','predictions','actuals','audit_events'};existing={v[0] for v in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
  if not required.issubset(existing):raise ValueError('表结构缺失')
  return db
 except Exception as e:raise ValueError(f'数据库损坏或结构无效：{path}。请保留原文件，从备份恢复到新路径后修改 data.db_path；不得直接重建。详细：{e}') from e

def ingest_predictions(db,c):
 exchange=resolve(c,c['exchange_dir'])
 for path in sorted(exchange.glob('**/record.json')):
  m=json.loads(path.read_text(encoding='utf-8'));source=(path.parent/m['file']).resolve()
  if digest(source)!=m['sha256']:raise ValueError(f'{path}：预测文件哈希不符，拒绝入库')
  existed=db.execute('SELECT sha FROM runs WHERE id=?',(m['run_id'],)).fetchone()
  if existed:
   if existed[0]!=m['sha256']:raise ValueError('同一预测版本内容被改写：'+m['run_id'])
   continue
  f=pd.read_excel(source,sheet_name=m['sheet']);f['target']=pd.to_datetime(f[m['columns']['target']])
  if f.target.duplicated().any():raise ValueError(f'{source}：重复目标小时')
  rows=[]
  for _,r in f.iterrows():
   def val(k):
    col=m['columns'].get(k);v=None if col is None else r[col]
    return None if v is None or pd.isna(v) else float(v)
   rows.append((m['run_id'],str(r.target),val('load'),val('da'),val('rt'),val('k'),val('research_k')))
  with db:
   db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)',(m['run_id'],m['kind'],m['published_at'],m['model_version'],json.dumps(m['config'],ensure_ascii=False),str(source),m['sha256'],m['status']))
   db.executemany('INSERT INTO predictions VALUES(?,?,?,?,?,?,?)',rows)
   db.execute('INSERT INTO audit_events VALUES(?,?,?)',(stamp(),'ingest_prediction',str(path)))

def read_actuals(db,c):
 for which,columns in [('load_excel',{'实际负荷':'load'}),('price_excel',{'日前价格':'da','实时价格':'rt'})]:
  path=resolve(c,c['data'][which]);sha=digest(path)
  with pd.ExcelFile(path) as book:
   found=set()
   for sheet in book.sheet_names:
    f=pd.read_excel(book,sheet_name=sheet)
    for col,kind in columns.items():
     actual_col=col if col in f else '价格' if sheet in ([col.replace('价格',''),col] if which=='price_excel' else []) and '价格' in f else None
     if actual_col is None:continue
     if kind in found:raise ValueError(f'{path}：{kind} 同时出现在多个sheet，请消除歧义')
     found.add(kind)
     if '目标时刻' in f:t=pd.to_datetime(f['目标时刻'])
     elif '日期' in f and '小时' in f:t=pd.to_datetime(f['日期'])+pd.to_timedelta(f['小时'],unit='h')
     else:raise ValueError(f'{path}/{sheet}：缺少目标时刻或日期/小时')
     if t.duplicated().any():raise ValueError(f'{path}/{sheet}：目标小时重复')
     available_col=next((v for v in ['可用时间','available_time'] if v in f),None)
     available=pd.to_datetime(f[available_col],errors='coerce') if available_col else t.dt.normalize()+pd.Timedelta(days=c['actual_lag_days'])
     evidence='verified_column' if available_col else 'assumed_D_plus_lag'
     rows=[]
     for i,v in enumerate(pd.to_numeric(f[actual_col],errors='coerce')):
      if not np.isfinite(v) or pd.isna(available.iloc[i]):logging.warning('%s/%s 第%s行 %s或可用时间缺失，跳过',path,sheet,i+2,actual_col);continue
      if kind=='load' and v<0:raise ValueError(f'{path}/{sheet} 第{i+2}行：负荷为负')
      if available.iloc[i]<t.iloc[i]:raise ValueError(f'{path}/{sheet} 第{i+2}行：实际值可用时间早于目标时刻')
      rows.append((str(t.iloc[i]),kind,float(v),str(available.iloc[i]),evidence,sha,stamp()))
     # 实际值按文件版本留存；不同文件修订且未提供真正可用时间时只从当前入库时刻起有效。
     with db:
      for row in rows:
       previous=db.execute('SELECT value FROM actuals WHERE target=? AND metric=? ORDER BY ingested_at DESC LIMIT 1',row[:2]).fetchone()
       if previous and previous[0]!=row[2] and not available_col:row=(*row[:3],stamp(),*row[4:])
       db.execute('INSERT OR IGNORE INTO actuals VALUES(?,?,?,?,?,?,?)',row)
   if found!=set(columns.values()):raise ValueError(f'{path}：未找到所需实际值列 {set(columns.values())-found}')

def reconcile(db,c):
 today=pd.Timestamp(c['review_date']).normalize() if c['review_date']!='today' else pd.Timestamp.now(tz='Asia/Shanghai').tz_localize(None).normalize()
 lags=c.get('actual_lag_days_by_metric',{})
 cutoff=today-pd.Timedelta(days=max(lags.get(k,c['actual_lag_days']) for k in ['load','da','rt']))
 p=pd.read_sql_query('SELECT p.*,r.kind,r.published_at,r.status,r.config_json FROM predictions p JOIN runs r ON p.run_id=r.id',db)
 if p.empty:return p,p,cutoff
 p['target']=pd.to_datetime(p.target);p['published_at']=pd.to_datetime(p.published_at,format="mixed")
 a=pd.read_sql_query("SELECT * FROM actuals WHERE NOT (metric IN ('da','rt') AND value=0)",db)
 if not a.empty:a=a[pd.to_datetime(a.available_at,format="mixed")<=today+pd.Timedelta(hours=23,minutes=59,seconds=59)]
 if not a.empty:
  a['target']=pd.to_datetime(a.target);a=a.sort_values(['available_at','ingested_at']).drop_duplicates(['target','metric'],keep='last');wide=a.pivot(index='target',columns='metric',values='value').rename(columns=lambda x:'actual_'+x);p=p.join(wide,on='target')
 for col in ['actual_load','actual_da','actual_rt']:
  if col not in p:p[col]=np.nan
 p['review_status']='待复盘：实际值未齐'
 for kind,cols in [('load',['actual_load']),('coefficient',['actual_load','actual_da','actual_rt'])]:
  kind_lag=max(lags.get(col.replace('actual_',''),c['actual_lag_days']) for col in cols)
  due=p.target.dt.normalize()<=today-pd.Timedelta(days=kind_lag)
  ok=p.kind.eq(kind)&p[cols].notna().all(axis=1)&due;p.loc[ok,'review_status']='已复盘'
  p.loc[p.kind.eq(kind)&~due,'review_status']='待复盘：尚未到该指标滞后天数'

 # 历史回放不冒充当时正式发布；可展示研究复盘，但必须保留晚发布标识。
 p['late_publication']=p.published_at>p.target.dt.normalize()-pd.Timedelta(days=1)+pd.Timedelta(hours=9)
 done=p[p.review_status.eq('已复盘')].copy();pending=p[~p.review_status.eq('已复盘')].copy()
 # 同时刻多发布版本全部存库，指标仅取最新一个，避免重复权重。
 done['analysis_scope']=np.where(done.status.eq('production')&~done.late_publication,'production','research')
 done=done.sort_values('published_at').drop_duplicates(['kind','target','analysis_scope'],keep='last').sort_values(['target','kind'])
 return done,pending,cutoff
