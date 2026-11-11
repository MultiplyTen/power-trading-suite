# -*- coding: utf-8 -*-
import sys,logging,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from database.runtime import prepare
from database.store import suite_lock
from common import read_config,resolve,dump
from reconciliation import connect,ingest_predictions,read_actuals,reconcile
from snapshots import capture
from research_replay import import_replay
from database.trading import sync_actuals
from suggestions import generate
from reporting import reports
from apply_config import apply,rollback
from disclosure_review import run as run_disclosure_review

def main():
 prepare('review')
 c=read_config();out=resolve(c,c['output_dir']);out.mkdir(parents=True,exist_ok=True);logging.basicConfig(level=logging.INFO,format='%(asctime)s %(message)s',handlers=[logging.FileHandler(out/'run.log',encoding='utf-8'),logging.StreamHandler()])
 cmd=sys.argv[1] if len(sys.argv)>1 else 'review'
 if cmd in ['apply','rollback']:
  result=apply(c) if cmd=='apply' else rollback(c);print(json.dumps(result,ensure_ascii=False,indent=2));return
 if cmd not in ['init','review']:raise ValueError('支持命令：无参数复盘 / init / apply / rollback')
 db=connect(resolve(c,c['db_path']),initialize=cmd=='init')
 if cmd=='init':db.close();print('对账数据库初始化/完整性检查完成');return
 with db:
  sync_actuals(db)
 from execution import analyze
 analyze(db,c,out)
 done,pending,cutoff=reconcile(db,c);logging.info('已对账 %s 条，待复盘 %s 条',len(done),len(pending))
 candidates,suggestions=generate(db,c,cutoff);files=reports(c,done,pending,cutoff,candidates,suggestions)
 disclosure_review=run_disclosure_review(c,out)
 dump(out/'run_manifest.json',{'status':'completed','cutoff':str(cutoff.date()),'reconciled_rows':len(done),'pending_rows':len(pending),'reports':files,'suggestion_count':len(suggestions.get('suggestions',[])),'disclosure_review':disclosure_review,'config_changed':False});db.close();print('复盘完成；普通运行未改动配置。')

if __name__=='__main__':
 try:
  with suite_lock():main()
 except Exception as e:logging.exception('复盘失败：%s',e);sys.exit(1)
