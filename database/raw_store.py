"""原始接入扩展表。每文件事务、原始字节和逐主键版本可追溯。"""
import json,uuid
from pathlib import Path
import pandas as pd
from database.store import backup,sha,quote
from database.trading import open_db,sync_actuals

EXTRA={'用户明细':['date','hour','entity'], '统调负荷':['date','hour'], '结算价对照':['date','hour','entity'], '申报记录':['date','hour'], '运行用电量对照':['date','hour']}
CORE=['负荷','日前价','实时价','负荷预测']
def ensure(db):
    for name,keys in EXTRA.items():
        maximum=96 if name=='统调负荷' else 23;minimum=1 if name=='统调负荷' else 0
        db.execute('CREATE TABLE IF NOT EXISTS '+quote(name)+f' (date TEXT NOT NULL,hour INTEGER NOT NULL CHECK(hour BETWEEN {minimum} AND {maximum}),entity TEXT NOT NULL DEFAULT "",name TEXT,value REAL NOT NULL,updated_at TEXT NOT NULL,source TEXT NOT NULL,available_at TEXT NOT NULL,evidence TEXT,source_sha TEXT,source_cell TEXT,PRIMARY KEY('+','.join(keys)+'))')
    db.execute('CREATE TABLE IF NOT EXISTS 原始接入修订(id INTEGER PRIMARY KEY,batch_id TEXT,kind TEXT,key_json TEXT,old_json TEXT,new_json TEXT,available_at TEXT)')

def commit_raw(path,name,raw,result):
    if result['errors']:raise ValueError('禁止提交校验失败文件')
    digest=sha(raw);batch=uuid.uuid4().hex;receipt=str(pd.Timestamp.now(tz='Asia/Shanghai').tz_localize(None))
    with open_db(path) as check:
        existing=check.execute('SELECT id FROM 接入批次 WHERE sha256=?',(digest,)).fetchone()
        if existing:
            return dict(batch_id=existing[0],changes={},backup=None,status='already_imported')
    # 审计批次也写库，写入前完整备份。
    saved=backup(path);stats={}
    with open_db(path) as db:
        db.execute('BEGIN IMMEDIATE');ensure(db)
        for r in result['records']:
            kind=r['kind'];keys=EXTRA.get(kind,['date','hour']);where=' AND '.join(quote(k)+'=?' for k in keys);values=[r[k] for k in keys]
            old=db.execute('SELECT * FROM '+quote(kind)+' WHERE '+where,values).fetchone()
            cols=[d[0] for d in db.execute('SELECT * FROM '+quote(kind)+' LIMIT 0').description];previous=dict(zip(cols,old)) if old else None
            counts=stats.setdefault(kind,dict(new=0,updated=0,unchanged=0))
            if previous and previous['value']==r['value'] and (kind!='用户明细' or previous['name']==r['name']):counts['unchanged']+=1;continue
            counts['updated' if old else 'new']+=1
            record=dict(date=r['date'],hour=r['hour'],value=r['value'],updated_at=receipt,source=name,available_at=receipt,evidence='raw_file_receipt_not_original_release',source_sha=digest+':'+batch,source_cell=r['cell'])
            if kind in EXTRA:record.update(entity=r['entity'],name=r['name'])
            columns=list(record);updates=','.join(quote(k)+'=excluded.'+quote(k) for k in columns if k not in keys)
            db.execute('INSERT INTO '+quote(kind)+' ('+','.join(quote(k) for k in columns)+') VALUES('+','.join('?' for _ in columns)+') ON CONFLICT('+','.join(keys)+') DO UPDATE SET '+updates,list(record.values()))
            db.execute('INSERT INTO 原始接入修订(batch_id,kind,key_json,old_json,new_json,available_at) VALUES(?,?,?,?,?,?)',(batch,kind,json.dumps(dict(zip(keys,values))),json.dumps(previous,ensure_ascii=False),json.dumps(record,ensure_ascii=False),receipt))
        summary={k:v for k,v in result.items() if k!='records'};summary['changes']=stats
        db.execute('INSERT INTO 接入批次 VALUES(?,?,?,?,?,?)',(batch,receipt,name,digest,raw,json.dumps(summary,ensure_ascii=False)))
        sync_actuals(db)
    return dict(batch_id=batch,changes=stats,backup=str(saved))

def cross_check(path,tolerance=.01):
    with open_db(path) as db:
        rows=db.execute('SELECT a.date,a.hour,a.value,b.value FROM 负荷 a JOIN 运行用电量对照 b USING(date,hour) ORDER BY a.date,a.hour').fetchall()
    return [dict(date=d,hour=h,retail=a,organization=b,relative_difference=abs(a-b)/abs(a)) for d,h,a,b in rows if a and abs(a-b)/abs(a)>tolerance]

def overlay_dataset(db,name,f):
    table='用户明细' if name=='load_customers' else '统调负荷' if name=='load_system_load' else None
    if table is None or not db.execute('SELECT 1 FROM sqlite_master WHERE name=?',(table,)).fetchone():return f
    a=pd.read_sql_query('SELECT * FROM '+quote(table),db)
    if a.empty:return f
    if table=='统调负荷':
        a['hour']=(a.hour-1)//4
        a=a.groupby(['date','hour'],as_index=False).agg(value=('value','mean'),points=('value','size'),available_at=('available_at','max'),source_sha=('source_sha','last'))
        a=a[a.points.eq(4)].copy();a['entity']='system_load'
    extra=pd.DataFrame(dict(entity_id=a.entity,date=a.date,hour=a.hour,value=a.value,unit='MW' if table=='统调负荷' else 'MWh',available_time=a.available_at,version=a.source_sha))
    return pd.concat([f,extra],ignore_index=True).drop_duplicates(['entity_id','date','hour'],keep='last').sort_values(['entity_id','date','hour']).reset_index(drop=True)

def system_feature(index,path,lag_days=2):
    """每预测日D仅使用D-lag同小时且D-1 09:00已接收的原始96点。"""
    with open_db(path) as db:
        exists=db.execute("SELECT 1 FROM sqlite_master WHERE name='统调负荷'").fetchone()
        if not exists:return pd.Series(float('nan'),index=index)
        a=pd.read_sql_query('SELECT * FROM 统调负荷',db)
    if a.empty:return pd.Series(float('nan'),index=index)
    a['hour']=(a.hour-1)//4
    a=a.groupby(['date','hour'],as_index=False).agg(value=('value','mean'),points=('value','size'),available_at=('available_at','max'))
    a=a[a.points.eq(4)].copy();a['target']=pd.to_datetime(a.date)+pd.to_timedelta(a.hour,unit='h')+pd.Timedelta(days=max(2,lag_days))
    eligible=pd.to_datetime(a.available_at,format='mixed')<=a.target.dt.normalize()-pd.Timedelta(days=1)+pd.Timedelta(hours=9)
    return a[eligible].set_index('target').value.reindex(index)
