# -*- coding: utf-8 -*-
"""SQLite is authoritative; exact source bytes materialize legacy model inputs.

Relational tables provide queryable versions of every active business dataset.
Do not edit mirrors with SQL: publish validated full-file versions instead.
"""
import contextlib
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import uuid
import gzip
import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / 'database' / 'master.db'

SCHEMA = '''
CREATE TABLE IF NOT EXISTS input_files(
 path TEXT PRIMARY KEY, role TEXT NOT NULL, active_version INTEGER);
CREATE TABLE IF NOT EXISTS input_versions(
 id INTEGER PRIMARY KEY, path TEXT NOT NULL REFERENCES input_files(path),
 sha256 TEXT NOT NULL, imported_at TEXT NOT NULL, original_name TEXT NOT NULL,
 note TEXT NOT NULL, batch_id TEXT NOT NULL, payload BLOB NOT NULL,
 UNIQUE(path,sha256));
CREATE TABLE IF NOT EXISTS input_tables(
 name TEXT PRIMARY KEY, path TEXT NOT NULL, sheet TEXT NOT NULL,
 columns_json TEXT NOT NULL, keys_json TEXT NOT NULL, UNIQUE(path,sheet));
CREATE TABLE IF NOT EXISTS input_quality(
 version INTEGER NOT NULL, table_name TEXT NOT NULL, rows INTEGER NOT NULL,
 first_time TEXT, last_time TEXT, missing_json TEXT NOT NULL,
 PRIMARY KEY(version,table_name));
CREATE TABLE IF NOT EXISTS maintenance_log(
 id INTEGER PRIMARY KEY, time TEXT NOT NULL, action TEXT NOT NULL,
 batch_id TEXT, detail_json TEXT NOT NULL);
'''

def now():
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat()

def sha(b):
    return hashlib.sha256(b).hexdigest()

def quote(name):
    return '"' + name.replace('"', '""') + '"'

def connect(path=None):
    path=path or os.environ.get('POWER_TRADING_DB') or DB
    path = Path(path)
    if not path.is_file():
        raise ValueError('统一数据库不存在，请恢复备份：' + str(path))
    db = sqlite3.connect(str(path), timeout=30)
    db.execute('PRAGMA foreign_keys=ON')
    if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
        db.close()
        raise ValueError('统一数据库完整性校验失败，请保留故障文件并恢复备份')
    return db

@contextlib.contextmanager
def suite_lock(root=ROOT):
    """Exclude data updates while an orchestrated model run is in progress."""
    lock = Path(root) / 'database' / '.suite.lock'
    inherited = os.environ.get('POWER_SUITE_LOCK_TOKEN')
    if inherited and lock.exists() and json.loads(lock.read_text(encoding='utf-8')).get('token') == inherited:
        yield
        return
    token = uuid.uuid4().hex
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ValueError('数据库正在更新或程序正在运行；请稍后重试。若上次崩溃，先核对 database/.suite.lock 内PID再处理残留锁。')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump({'pid': os.getpid(), 'token': token, 'time': now()}, f)
        os.environ['POWER_SUITE_LOCK_TOKEN'] = token
        yield
    finally:
        if inherited is None:
            os.environ.pop('POWER_SUITE_LOCK_TOKEN', None)
        else:
            os.environ['POWER_SUITE_LOCK_TOKEN'] = inherited
        lock.unlink(missing_ok=True)

def backup(path=DB):
    destination = Path(path).parent / 'backups' / (dt.datetime.now().strftime('%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:8] + '.sqlite')
    destination.parent.mkdir(exist_ok=True)
    with connect(path) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)
    backups = sorted(destination.parent.glob('*.sqlite'), key=lambda item: item.stat().st_mtime)
    for stale in backups[:-3]:
        stale.unlink(missing_ok=True)
    return destination

def parse(path, payload):
    if path.endswith(('.csv', '.csv.gz')):
        data = gzip.decompress(payload) if path.endswith('.gz') else payload
        return {'data': pd.read_csv(io.BytesIO(data), low_memory=False)}
    if path.endswith('.xlsx'):
        with pd.ExcelFile(io.BytesIO(payload)) as book:
            return {s: pd.read_excel(book, sheet_name=s) for s in book.sheet_names}
    return {}

def business_keys(frame):
    c = set(frame.columns)
    if {'location_id', 'valid_time'} <= c:
        # Each file is one forecast snapshot; revisions belong to file versions.
        return ['location_id', 'valid_time']
    if {'entity_id', 'date', 'hour'} <= c:
        return ['entity_id', 'date', 'hour']
    for col in ['timestamp', '目标时刻', 'date', 'entity_id', 'location_id']:
        if col in c:
            return [col]
    return []

def validate(frame, path):
    if not frame.columns.is_unique or any(not isinstance(x, str) for x in frame.columns):
        raise ValueError(path + '：字段必须是唯一文本名称')
    if any(x in frame for x in ['_version', '_row']):
        raise ValueError('保留字段冲突')
    keys = business_keys(frame)
    if keys and (frame[keys].isna().any().any() or frame.duplicated(keys).any()):
        raise ValueError(path + '：业务键为空或重复：' + ','.join(keys))
    if 'hour' in frame and not frame.hour.isin(range(24)).all():
        raise ValueError(path + '：小时须为0—23的整数')
    time_col = next((c for c in ['valid_time', '目标时刻', 'timestamp', 'date'] if c in frame), None)
    times = pd.to_datetime(frame[time_col], errors='raise') if time_col else pd.Series(dtype='datetime64[ns]')
    if time_col=='date' and 'hour' in frame:
        times=times+pd.to_timedelta(frame.hour,unit='h')
    if time_col and times.isna().any():
        raise ValueError(path + '：业务时间为空')
    if len(times) and ((times.dt.minute!=0)|(times.dt.second!=0)|(times.dt.microsecond!=0)).any():
        raise ValueError(path + '：当前数据契约要求整小时起点')
    for name in ['available_time', '可用时间', 'issue_time', 'retrieved_at', 'assumed_available_time']:
        if name in frame:
            pd.to_datetime(frame[name].dropna(), errors='raise', format='mixed')
    for col in ['value', 'actual_load', '实际负荷', '日前价格', '实时价格', 'temperature_2m',
                'relative_humidity_2m', 'precipitation', 'shortwave_radiation', 'wind_speed_100m', 'cloud_cover', 'latitude', 'longitude']:
        if col in frame:
            n = pd.to_numeric(frame[col], errors='raise')
            if np.isinf(n.dropna()).any():
                raise ValueError(path + '：不能包含无穷大')
            if col in ['actual_load','实际负荷'] and n.lt(0).any():
                raise ValueError(path + '：复盘/系数实际负荷不可为负')
    expected_unit = 'MW' if path.endswith('system_load.csv') else 'CNY_per_MWh' if path.endswith(('day_ahead.csv', 'real_time.csv')) else 'MWh'
    if 'unit' in frame and not frame.unit.eq(expected_unit).all():
        raise ValueError(path + '：计量单位应为' + expected_unit)
    # Raw negative loads are retained: existing model quarantines them with lineage.
    if path.endswith('calendar.csv'):
        for col in ['is_workday', 'has_holiday_name']:
            if not frame[col].isin([0, 1, False, True]).all():
                raise ValueError('日历布尔字段必须为0或1')
        if len(times) and (times.sort_values().diff().dropna() != pd.Timedelta(days=1)).any():
            raise ValueError('日历日期必须连续完整')
    return keys, times

def table_name(path, sheet):
    prefix = {'load_forecast':'load', 'coefficient_opt':'coef', 'review':'review', 'database':'shared'}[path.split('/')[0]]
    stem = Path(path).name.split('.')[0]
    return prefix + '_' + stem + ('' if sheet == 'data' else '_' + sha(sheet.encode())[:6])

def normalize(v):
    if pd.isna(v):
        return None
    if isinstance(v, (pd.Timestamp, dt.datetime, dt.date)):
        return str(v)
    if isinstance(v, np.generic):
        return v.item()
    return v

def column_type(col,series):
    if col in {'date','timestamp','目标时刻','valid_time','available_time','可用时间','issue_time','retrieved_at','assumed_available_time','source_sha256','source_url','version','entity_id','location_id','holiday_name'}:
        return 'TEXT'
    return 'INTEGER' if pd.api.types.is_bool_dtype(series) or pd.api.types.is_integer_dtype(series) else 'REAL' if pd.api.types.is_numeric_dtype(series) else 'TEXT'

def import_one(db, path, payload, original, note, batch, bootstrap=False, role='input'):
    catalog = db.execute('SELECT role,active_version FROM input_files WHERE path=?', (path,)).fetchone()
    if not catalog:
        if not bootstrap:
            raise ValueError('未注册的数据集：' + path + '。新增数据契约需开发维护。')
        db.execute('INSERT INTO input_files(path,role) VALUES(?,?)', (path,role))
    else:
        role = catalog[0]
    digest = sha(payload)
    existed = db.execute('SELECT id FROM input_versions WHERE path=? AND sha256=?', (path,digest)).fetchone()
    if existed:
        if catalog and existed[0] == catalog[1]:
            return {'path':path, 'status':'unchanged', 'version':existed[0]}
        raise ValueError('这是已有旧版本，请使用 activate 明确回退：' + path)
    tables = parse(path,payload) if role == 'input' else {}
    known_sheets = {r[0] for r in db.execute('SELECT sheet FROM input_tables WHERE path=?',(path,))}
    if known_sheets and set(tables) != known_sheets:
        raise ValueError(path + '：工作表结构变化，拒绝更新')
    version = db.execute('INSERT INTO input_versions(path,sha256,imported_at,original_name,note,batch_id,payload) VALUES(?,?,?,?,?,?,?)',
                         (path,digest,now(),original,note,batch,payload)).lastrowid
    for sheet,f in tables.items():
        keys,times = validate(f,path)
        name = table_name(path,sheet)
        existing = db.execute('SELECT columns_json,keys_json FROM input_tables WHERE name=?',(name,)).fetchone()
        columns = [(col,column_type(col,f[col])) for col in f.columns]
        if existing:
            old = json.loads(existing[0])
            if [x[0] for x in columns] != [x[0] for x in old] or keys != json.loads(existing[1]):
                raise ValueError(path + '：字段/业务键变化，拒绝更新；请保持原结构')
            columns = old
            for col,typ in columns:
                if typ in ['REAL','INTEGER'] and f[col].notna().any():
                    nums = pd.to_numeric(f[col],errors='raise')
                    if typ == 'INTEGER' and not nums.dropna().mod(1).eq(0).all():
                        raise ValueError(path + '：整数字段出现小数：' + col)
        else:
            db.execute('INSERT INTO input_tables VALUES(?,?,?,?,?)',(name,path,sheet,json.dumps(columns,ensure_ascii=False),json.dumps(keys,ensure_ascii=False)))
            db.execute('CREATE TABLE '+quote(name)+' (_version INTEGER NOT NULL REFERENCES input_versions(id), _row INTEGER NOT NULL,'+','.join(quote(c)+' '+t for c,t in columns)+', PRIMARY KEY(_version,_row))')
            db.execute('CREATE VIEW '+quote('current_'+name)+' AS SELECT t.* FROM '+quote(name)+' t JOIN input_files f ON t._version=f.active_version')
        marks=','.join('?' for _ in range(len(columns)+2))
        db.executemany('INSERT INTO '+quote(name)+' VALUES('+marks+')',((version,i+2,*[normalize(v) for v in row]) for i,row in enumerate(f.itertuples(index=False,name=None))))
        db.execute('INSERT INTO input_quality VALUES(?,?,?,?,?,?)',(version,name,len(f),str(times.min()) if len(times) else None,str(times.max()) if len(times) else None,json.dumps(f.isna().sum().astype(int).to_dict(),ensure_ascii=False)))
    db.execute('UPDATE input_files SET active_version=? WHERE path=?',(version,path))
    return {'path':path,'status':'imported','version':version,'sha256':digest}

def materialize(root=ROOT, project=None, db_path=None):
    """Exact bytes preserve file hashes and Excel semantics, overwrite edited caches."""
    root=Path(root);db=connect(db_path or os.environ.get('POWER_TRADING_DB') or root/'database/master.db')
    changed=[]
    try:
        for path,digest,payload in db.execute('SELECT f.path,v.sha256,v.payload FROM input_files f JOIN input_versions v ON f.active_version=v.id ORDER BY f.path'):
            if project and not (path.startswith(project+'/') or path.startswith('database/')):
                continue
            target=(root/path).resolve()
            if root.resolve() not in target.parents:
                raise ValueError('非法数据库导出路径')
            if sha(payload)!=digest:
                raise ValueError('库内原始文件哈希不符：'+path)
            if not target.exists() or sha(target.read_bytes()) != digest:
                target.parent.mkdir(parents=True,exist_ok=True)
                temp=target.with_name(target.name+'.new')
                temp.write_bytes(payload);os.replace(temp,target);changed.append(path)
    finally:
        db.close()
    return changed

def input_fingerprint(path=DB):
    """Changes to shared calendar/data invalidate every cached tuning candidate."""
    with connect(path) as db:
        versions=db.execute('SELECT f.path,v.sha256 FROM input_files f JOIN input_versions v ON f.active_version=v.id ORDER BY f.path').fetchall()
    shared_code=[sha(p.read_bytes()) for p in [Path(__file__),Path(__file__).with_name('runtime.py'),Path(__file__).with_name('trading.py')] if p.exists()]
    return sha(json.dumps([versions,shared_code],ensure_ascii=False).encode('utf-8'))

def check(db):
    if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or db.execute('PRAGMA foreign_key_check').fetchall():
        raise ValueError('数据库完整性或外键检查失败')
    if db.execute('SELECT f.path FROM input_files f LEFT JOIN input_versions v ON f.active_version=v.id WHERE v.id IS NULL OR f.path!=v.path').fetchall():
        raise ValueError('当前版本指针无效')
    for version,digest,payload in db.execute('SELECT id,sha256,payload FROM input_versions'):
        if sha(payload)!=digest:raise ValueError('原始文件哈希损坏，版本 '+str(version))
    tables=[]
    for name,path,sheet,cols,keys in db.execute('SELECT * FROM input_tables ORDER BY name'):
        for version,count in db.execute('SELECT version,rows FROM input_quality WHERE table_name=?',(name,)):
            if db.execute('SELECT count(*) FROM '+quote(name)+' WHERE _version=?',(version,)).fetchone()[0]!=count:
                raise ValueError('结构化表行数不符：'+name)
        n=db.execute('SELECT count(*) FROM '+quote('current_'+name)).fetchone()[0]
        tables.append({'table':'current_'+name,'source':path,'sheet':sheet,'rows':n,'columns':json.loads(cols),'keys':json.loads(keys)})
    shared_consistency(db)
    return {'status':'passed','schema_version':1,'files':db.execute('SELECT count(*) FROM input_files').fetchone()[0], 'versions':db.execute('SELECT count(*) FROM input_versions').fetchone()[0],'active_rows':sum(t['rows'] for t in tables),'shared_consistency':'passed','tables':tables}

def shared_consistency(db):
    """Reject conflicting aliases: shared inputs must be published as one batch."""
    def frame(path):
        item=db.execute('SELECT name FROM input_tables WHERE path=?',(path,)).fetchone()
        if item is None:return None
        return pd.read_sql_query('SELECT * FROM '+quote('current_'+item[0]),db)
    def series(path,col):
        f=frame(path)
        if f is None:return None
        t=(pd.to_datetime(f.date)+pd.to_timedelta(f.hour,unit='h')) if 'hour' in f else pd.to_datetime(f['timestamp' if 'timestamp' in f else '目标时刻'])
        return pd.Series(f[col].to_numpy(),index=t).sort_index()
    pairs=[('load_forecast/data/portfolio.csv','value','coefficient_opt/data/actual_load.csv','actual_load'),
           ('load_forecast/data/portfolio.csv','value','review/data/load.xlsx','实际负荷'),
           ('load_forecast/data/day_ahead.csv','value','coefficient_opt/data/price.xlsx','日前价格'),
           ('load_forecast/data/real_time.csv','value','coefficient_opt/data/price.xlsx','实时价格'),
           ('load_forecast/data/day_ahead.csv','value','review/data/price.xlsx','日前价格'),
           ('load_forecast/data/real_time.csv','value','review/data/price.xlsx','实时价格')]
    for a,x,b,y in pairs:
        left,right=series(a,x),series(b,y)
        if left is not None and right is not None:
            if not left.index.equals(right.index) or not np.allclose(left,right,equal_nan=True,rtol=1e-9,atol=1e-8):
                raise ValueError('跨模块同源数据不一致，请整批同步更新：'+a+' 与 '+b)
    customer,mapping=frame('load_forecast/data/customers.csv'),frame('load_forecast/data/customer_mapping.csv')
    if customer is not None and mapping is not None and not set(customer.entity_id)<=set(mapping.entity_id):
        raise ValueError('新增客户缺少客户地区映射')
    return 'passed'

def publish(items,note,path=DB,bootstrap=False):
    if not note.strip():raise ValueError('必须填写更新原因 --note')
    names=[i['dataset'] for i in items]
    with connect(path) as guard:
        if guard.execute("SELECT 1 FROM sqlite_master WHERE name='负荷'").fetchone() and any(n in {'load_forecast/data/portfolio.csv','load_forecast/data/day_ahead.csv','load_forecast/data/real_time.csv','coefficient_opt/data/actual_load.csv','coefficient_opt/data/price.xlsx','review/data/load.xlsx','review/data/price.xlsx'} for n in names):
            raise ValueError('四类小时事实已切换到数据接入模块，请填写输入模板后运行 data_ingest/main.py；旧文件版本仅保留为迁移档案。')
    if len(set(names))!=len(names):raise ValueError('同一批次不能重复指定数据集')
    payloads=[(i,Path(i['file']).read_bytes()) for i in items]
    saved=None if bootstrap else str(backup(path))
    batch=uuid.uuid4().hex
    with connect(path) as db:
        db.execute('BEGIN IMMEDIATE')
        results=[import_one(db,i['dataset'],b,str(i['file']),note,batch,bootstrap,i.get('role','input')) for i,b in payloads]
        shared_consistency(db)
        db.execute('INSERT INTO maintenance_log(time,action,batch_id,detail_json) VALUES(?,?,?,?)',(now(),'publish',batch,json.dumps({'note':note,'backup':saved,'items':results},ensure_ascii=False)))
    return {'batch':batch,'backup':saved,'items':results}

def activate(mapping,note,path=DB):
    with connect(path) as guard:
        if guard.execute("SELECT 1 FROM sqlite_master WHERE name='负荷'").fetchone() and any(n in {'load_forecast/data/portfolio.csv','load_forecast/data/day_ahead.csv','load_forecast/data/real_time.csv','coefficient_opt/data/actual_load.csv','coefficient_opt/data/price.xlsx','review/data/load.xlsx','review/data/price.xlsx'} for n in mapping):
            raise ValueError('小时事实请通过输入模板提交更正，旧迁移文件版本不再控制这些事实值。')
    saved=str(backup(path))
    with connect(path) as db:
        db.execute('BEGIN IMMEDIATE')
        for dataset,version in mapping.items():
            if not db.execute('SELECT 1 FROM input_versions WHERE path=? AND id=?',(dataset,version)).fetchone():
                raise ValueError('版本与数据集不匹配')
            db.execute('UPDATE input_files SET active_version=? WHERE path=?',(version,dataset))
        shared_consistency(db)
        db.execute('INSERT INTO maintenance_log(time,action,detail_json) VALUES(?,?,?)',(now(),'activate',json.dumps({'note':note,'versions':mapping,'backup':saved},ensure_ascii=False)))
    return {'backup':saved,'activated':mapping}
