# -*- coding: utf-8 -*-
"""维护统一数据库：python database/manage.py --help"""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from database.store import ROOT,DB,connect,check,materialize,publish,activate,backup,suite_lock

def main():
    p=argparse.ArgumentParser(description='统一数据库：版本更新、校验、导出和回退')
    sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('check');sub.add_parser('catalog');sub.add_parser('backup');sub.add_parser('export')
    s=sub.add_parser('import');s.add_argument('--manifest',required=True);s.add_argument('--note',required=True)
    s=sub.add_parser('activate');s.add_argument('--manifest',required=True);s.add_argument('--note',required=True)
    s=sub.add_parser('versions');s.add_argument('--dataset',required=True)
    a=p.parse_args()
    with suite_lock():
        if a.command in ['check','catalog']:
            with connect() as db:result=check(db)
            (ROOT/'database/catalog.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            if a.command=='check':result={k:v for k,v in result.items() if k!='tables'}
        elif a.command=='backup':result={'backup':str(backup())}
        elif a.command=='export':result={'restored_files':materialize()}
        elif a.command=='versions':
            with connect() as db:
                result=[dict(zip(['id','sha256','imported_at','note','active'],r)) for r in db.execute('SELECT v.id,v.sha256,v.imported_at,v.note,v.id=f.active_version FROM input_versions v JOIN input_files f ON v.path=f.path WHERE v.path=? ORDER BY v.id',(a.dataset,))]
        else:
            manifest=Path(a.manifest).resolve();doc=json.loads(manifest.read_text(encoding='utf-8'))
            if a.command=='import':
                items=doc['files']
                for item in items:item['file']=str((manifest.parent/item['file']).resolve())
                result=publish(items,a.note)
            else:result=activate(doc['versions'],a.note)
            # Commit is complete before compatibility caches are regenerated.
            # A failure here is recoverable by export; DB remains authoritative.
            result['restored_files']=materialize()
        print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':
    try:main()
    except Exception as e:print('数据库操作失败：'+str(e),file=sys.stderr);sys.exit(1)
