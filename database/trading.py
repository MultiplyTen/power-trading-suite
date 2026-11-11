# -*- coding: utf-8 -*-
"""四类小时事实、写入版本及模型发布记录。业务读取不依赖输出Excel。"""
import json,sqlite3,hashlib,uuid
from pathlib import Path
import numpy as np
import pandas as pd
from database.store import DB,ROOT,connect,quote,now,sha

KINDS={'负荷':'load','日前价':'da','实时价':'rt','负荷预测':'manual_load'}
DDL='''
CREATE TABLE IF NOT EXISTS 接入批次(id TEXT PRIMARY KEY, imported_at TEXT, source TEXT, sha256 TEXT, payload BLOB, summary_json TEXT);
CREATE TABLE IF NOT EXISTS 接入修订(id INTEGER PRIMARY KEY, batch_id TEXT, kind TEXT, date TEXT, hour INTEGER, old_value REAL, new_value REAL, available_at TEXT, source_cell TEXT);
'''

def create_tables(db):
    db.executescript(DDL)
    for name in KINDS:
        db.execute('CREATE TABLE IF NOT EXISTS '+quote(name)+' (date TEXT NOT NULL,hour INTEGER NOT NULL CHECK(hour>=0 AND hour<24),value REAL,updated_at TEXT NOT NULL,source TEXT NOT NULL,available_at TEXT,evidence TEXT,source_sha TEXT,source_cell TEXT,PRIMARY KEY(date,hour))')
    # Normalized physical record tables are retained, with Chinese read views.
    for name,kind in [('负荷预测记录','load'),('系数决策记录','coefficient')]:
        quantity=',p.load*p.k AS declaration_quantity,p.load*p.research_k AS research_declaration_quantity' if kind=='coefficient' else ''
        db.execute('CREATE VIEW IF NOT EXISTS '+quote(name)+" AS SELECT p.*,r.published_at,r.model_version,r.config_json,r.status,r.source"+quantity+" FROM predictions p JOIN runs r ON p.run_id=r.id WHERE r.kind='"+kind+"'")

def db_path(config,root):
    return (Path(root)/config['db_path']).resolve()

def open_db(path=DB):
    db=connect(path)
    names={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not set(KINDS)<=names:
        db.close();raise ValueError('缺少数据接入表，请联系维护人员迁移；不要重建现有数据库。')
    return db

def migrate(path=DB):
    """仅在本次版本升级时显式执行，已有事实表则拒绝再次覆盖。"""
    with connect(path) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='负荷'").fetchone():raise ValueError('接入表已存在，禁止重复迁移')
        create_tables(db)
        specs=[('负荷','load_portfolio'),('日前价','load_day_ahead'),('实时价','load_real_time')]
        for kind,table in specs:
            f=pd.read_sql_query('SELECT * FROM '+quote('current_'+table),db)
            for r in f.itertuples(index=False):
                t=str(pd.Timestamp(r.date)+pd.Timedelta(hours=int(r.hour)))
                prior=db.execute('SELECT available_at,evidence,source_sha,ingested_at FROM actuals WHERE target=? AND metric=? ORDER BY ingested_at DESC LIMIT 1',(t,KINDS[kind])).fetchone()
                available,evidence,source_sha,updated=prior or (None,'unverified_legacy', 'legacy_'+str(r._0),now())
                value=None if pd.isna(r.value) else float(r.value)
                db.execute('INSERT INTO '+quote(kind)+' VALUES(?,?,?,?,?,?,?,?,?)',(str(pd.Timestamp(r.date).date()),int(r.hour),value,updated,'迁移历史快照',available,evidence,source_sha,'原始档案:'+table))
        db.execute('INSERT INTO maintenance_log(time,action,detail_json) VALUES(?,?,?)',(now(),'migration_four_modules','保留旧输入档案，建立直接读取的小时事实表'))

def hourly(kind,path=DB):
    with open_db(path) as db:
        f=pd.read_sql_query('SELECT * FROM '+quote(kind)+' ORDER BY date,hour',db)
    if kind in ['日前价','实时价']:f.loc[f.value.eq(0),'value']=np.nan
    f['timestamp']=pd.to_datetime(f.date)+pd.to_timedelta(f.hour,unit='h')
    return f

def dataset_frame(source,path=DB):
    """天气、明细、地区映射等非四类手填数据从原版本化SQL表读取。"""
    # input_tables.path 一律存正斜杠；Windows 下 str(Path) 会给出反斜杠导致契约查不到。
    dataset=Path(source).resolve().relative_to(ROOT).as_posix()
    with open_db(path) as db:
        rows=db.execute('SELECT name FROM input_tables WHERE path=?',(dataset,)).fetchall()
        if len(rows)!=1:raise ValueError('未注册或多工作表的数据契约：'+dataset)
        f=pd.read_sql_query('SELECT * FROM '+quote('current_'+rows[0][0])+' ORDER BY _row',db)
        from database.raw_store import overlay_dataset
        f=overlay_dataset(db,rows[0][0],f.drop(columns=['_version','_row']))
    return f

def market_frame(kind,entity,unit,path=DB):
    f=hourly(kind,path)
    return pd.DataFrame({'entity_id':entity,'date':f.date,'hour':f.hour,'value':f.value,'unit':unit,'available_time':f.available_at,'version':f.source_sha})

def prices(path=DB):
    a=hourly('日前价',path).set_index('timestamp').value.rename('da')
    b=hourly('实时价',path).set_index('timestamp').value.rename('rt')
    f=pd.concat([a,b],axis=1).sort_index()
    return f,sha(f.to_csv().encode())

def actual_load(index,lag,path=DB):
    f=hourly('负荷',path);a=pd.Series(f.value.to_numpy(),index=f.timestamp).sort_index().reindex(index)
    if a.lt(0).any():raise ValueError('库内实际负荷有负值，请核对数据')
    baseline=a.groupby(a.index.hour).transform(lambda s:s.shift(lag).rolling(28,min_periods=14).mean())
    return a,baseline,'统一数据库实际负荷的因果28日同小时基准，非原始预测快照'

def forecast_load(day,path=DB,manual_only=False):
    day=pd.Timestamp(day).normalize();idx=pd.date_range(day,periods=24,freq='h')
    with open_db(path) as db:
        if not manual_only:
            ids=db.execute("SELECT DISTINCT r.id,r.published_at FROM runs r JOIN predictions p ON r.id=p.run_id WHERE r.kind='load' AND r.status NOT LIKE '%replay%' AND p.target>=? AND p.target<=? ORDER BY r.published_at DESC",(str(idx[0]),str(idx[-1]))).fetchall()
            for rid,published in ids:
                f=pd.read_sql_query('SELECT target,load FROM predictions WHERE run_id=? AND target>=? AND target<=? ORDER BY target',db,params=[rid,str(idx[0]),str(idx[-1])])
                s=pd.Series(f.load.to_numpy(),index=pd.to_datetime(f.target)).reindex(idx)
                if len(f)==24 and s.notna().all() and np.isfinite(s).all() and s.ge(0).all():return s,{'source':'统一库负荷预测记录','run_id':rid,'published_at':published}
    with open_db(path) as db:
        table='手填负荷预测' if db.execute("SELECT 1 FROM sqlite_master WHERE name='手填负荷预测'").fetchone() else '负荷预测'
        f=pd.read_sql_query('SELECT * FROM '+quote(table)+" WHERE source NOT LIKE '模型/%'",db)
    f['timestamp']=pd.to_datetime(f.date)+pd.to_timedelta(f.hour,unit='h')
    f=f[f.timestamp.dt.normalize()==day];s=pd.Series(f.value.to_numpy(),index=f.timestamp).reindex(idx)
    if s.isna().any() or not np.isfinite(s).all() or s.le(0).any():raise ValueError('目标日没有完整模型负荷预测，手填负荷预测也不足24个正数；请填写数据接入模板“负荷预测”sheet。')
    cutoff=day-pd.Timedelta(days=1)+pd.Timedelta(hours=9)
    return s,{'source':'统一库手填负荷预测兜底','latest_receipt':str(f.updated_at.max()),'late_for_D1_cutoff':bool((pd.to_datetime(f.available_at,format='mixed')>cutoff).any())}

def published_load_forecast(day,path=DB):
    """Read one complete M2 load/DA/RT publication for the target day."""
    day=pd.Timestamp(day).normalize();idx=pd.date_range(day,periods=24,freq='h')
    with open_db(path) as db:
        ids=db.execute("SELECT DISTINCT r.id,r.published_at,r.model_version FROM runs r JOIN predictions p ON r.id=p.run_id WHERE r.kind='load' AND r.status NOT LIKE '%replay%' AND p.target>=? AND p.target<=? ORDER BY r.published_at DESC",(str(idx[0]),str(idx[-1]))).fetchall()
        for rid,published,model_version in ids:
            f=pd.read_sql_query('SELECT target,load,da,rt FROM predictions WHERE run_id=? AND target>=? AND target<=? ORDER BY target',db,params=[rid,str(idx[0]),str(idx[-1])])
            if len(f)!=24:continue
            f['target']=pd.to_datetime(f['target'])
            out=f.set_index('target').reindex(idx)
            required=['load','da','rt']
            if out[required].isna().any().any() or not np.isfinite(out[required]).all().all() or not out['load'].ge(0).all():continue
            return out[required],{'source':'M2统一发布记录','run_id':rid,'published_at':published,'model_version':model_version}
    return None,{'source':'M2统一发布记录','status':'no_complete_publication'}

def sync_actuals(db):
    """直接读取事实表，保留每次修订的真实接收时间，不读取Excel。"""
    for kind,metric in list(KINDS.items())[:3]:
        for date,hour,value,updated,source,available,evidence,digest,cell in db.execute('SELECT * FROM '+quote(kind)).fetchall():
            if value is None or not np.isfinite(value) or available is None or (metric in ['da','rt'] and value==0):continue
            target=str(pd.Timestamp(date)+pd.Timedelta(hours=hour))
            db.execute('INSERT OR IGNORE INTO actuals VALUES(?,?,?,?,?,?,?)',(target,metric,float(value),available,evidence,digest,updated))

def record(kind,frame,config,path=DB,status='research',published=None,tag='forecast'):
    """调用模型内存结果写入，一次发布的参数/行记录原子提交。"""
    frame=frame.copy();published=published or str(pd.Timestamp.now(tz='Asia/Shanghai').tz_localize(None))
    cfg={k:v for k,v in config.items() if not k.startswith('_')}
    config_json=json.dumps(cfg,ensure_ascii=False,sort_keys=True,default=str)
    digest=sha((frame.to_csv()+config_json).encode())
    rid=kind+'_'+tag+'_'+uuid.uuid4().hex[:24]
    rows=[]
    for t,r in frame.iterrows():
        def value(c):
            v=r.get(c);return None if v is None or pd.isna(v) else float(v)
        rows.append((rid,str(pd.Timestamp(t)),value('load'),value('da'),value('rt'),value('k'),value('research_k')))
    if not rows or frame.index.has_duplicates:raise ValueError('发布记录为空或目标时刻重复')
    with open_db(path) as db:
        if 'replay' in status:
            old=db.execute('SELECT id FROM runs WHERE kind=? AND sha=? AND status=?',(kind,digest,status)).fetchone()
            if old:return old[0]
        model_version=config.get('model_version') or config.get('project',{}).get('model_version','database-direct-v2')
        db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)',(rid,kind,published,model_version,config_json,'model_memory',digest,status))
        db.executemany('INSERT INTO predictions VALUES(?,?,?,?,?,?,?)',rows)
        if kind=='load' and 'replay' not in status:
            preserve_manual(db)
            for _,target,value,*_ in rows:
                if value is None:continue
                t=pd.Timestamp(target)
                db.execute('INSERT INTO 负荷预测 VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(date,hour) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at,source=excluded.source,available_at=excluded.available_at,evidence=excluded.evidence,source_sha=excluded.source_sha,source_cell=excluded.source_cell',
                    (str(t.date()),t.hour,value,published,'模型/'+rid,published,'model_publication',digest,'predictions/'+rid+'/'+target))
    return rid

def canonical_fingerprint(path=DB):
    with open_db(path) as db:
        rows=[(kind,db.execute('SELECT * FROM '+quote(kind)+' ORDER BY date,hour').fetchall()) for kind in KINDS]
        from database.raw_store import EXTRA
        for kind in EXTRA:
            if db.execute('SELECT 1 FROM sqlite_master WHERE name=?',(kind,)).fetchone():
                rows.append((kind,db.execute('SELECT * FROM '+quote(kind)+' ORDER BY date,hour').fetchall()))
    return sha(json.dumps(rows,ensure_ascii=False,default=str).encode())

def preserve_manual(db):
    db.execute('CREATE TABLE IF NOT EXISTS 手填负荷预测 (date TEXT,hour INTEGER,value REAL,updated_at TEXT,source TEXT,available_at TEXT,evidence TEXT,source_sha TEXT,source_cell TEXT,PRIMARY KEY(date,hour))')
    db.execute("INSERT OR REPLACE INTO 手填负荷预测 SELECT * FROM 负荷预测 WHERE source NOT LIKE '模型/%'")
