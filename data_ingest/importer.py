# -*- coding: utf-8 -*-
"""空格不覆盖、非空按日期小时更新；保留原始文件和逐格修订。"""
import json,uuid
from pathlib import Path
import pandas as pd
from database.store import backup,sha,quote
from database.trading import open_db,sync_actuals

def commit(path,template,changes,stats,raw=None):
    raw=Path(template).read_bytes() if raw is None else raw;digest=sha(raw);batch=uuid.uuid4().hex
    receipt=str(pd.Timestamp.now(tz='Asia/Shanghai').tz_localize(None))
    # 已有备份并不代表本批次成功；所有事实/修订/复盘实际值在同一事务内写入。
    with open_db(path) as current:
        needs_backup=False
        for c in changes:
            old=current.execute('SELECT value,source FROM '+quote(c['kind'])+' WHERE date=? AND hour=?',(c['date'],c['hour'])).fetchone()
            if not old or old[0]!=c['value'] or (c['kind']=='负荷预测' and old[1].startswith('模型/')):needs_backup=True;break
    backup_path=backup(path) if needs_backup else None
    with open_db(path) as db:
        db.execute('BEGIN IMMEDIATE')
        for c in changes:
            table=quote(c['kind']);old=db.execute('SELECT value,source FROM '+table+' WHERE date=? AND hour=?',(c['date'],c['hour'])).fetchone()
            if old and old[0]==c['value'] and not (c['kind']=='负荷预测' and old[1].startswith('模型/')):
                stats[c['kind']]['unchanged']+=1;continue
            key='updated' if old else 'new';stats[c['kind']][key]+=1;stats[c['kind']][key+'_days'].add(c['date'])
            # 同值重导不改变历史接收时间，也不制造新的实际值版本。
            source='模板导入/'+c['aggregation'];source_sha=digest+':'+batch
            db.execute('INSERT INTO '+table+' VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(date,hour) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at,source=excluded.source,available_at=excluded.available_at,evidence=excluded.evidence,source_sha=excluded.source_sha,source_cell=excluded.source_cell',
                       (c['date'],c['hour'],c['value'],receipt,source,receipt,'template_receipt_not_original_release',source_sha,c['cell']))
            db.execute('INSERT INTO 接入修订(batch_id,kind,date,hour,old_value,new_value,available_at,source_cell) VALUES(?,?,?,?,?,?,?,?)',(batch,c['kind'],c['date'],c['hour'],old[0] if old else None,c['value'],receipt,c['cell']))
        summary={k:{a:sorted(b) if isinstance(b,set) else b for a,b in v.items()} for k,v in stats.items()}
        db.execute('INSERT INTO 接入批次 VALUES(?,?,?,?,?,?)',(batch,receipt,str(Path(template).name),digest,raw,json.dumps(summary,ensure_ascii=False)))
        from database.trading import preserve_manual
        preserve_manual(db)
        sync_actuals(db)
    return {'batch_id':batch,'backup':str(backup_path) if backup_path else None,'sheets':summary}
