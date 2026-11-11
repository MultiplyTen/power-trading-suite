# -*- coding: utf-8 -*-
"""显式研究导入：不伪造原始发布日期，也不伪造历史执行系数。"""
import hashlib
from pathlib import Path
import pandas as pd
import yaml
from common import resolve,digest,dump,stamp

def import_replay(c):
 exchange=resolve(c,c['exchange_dir'])
 for project,kind in [('load_forecast','load'),('coefficient','coefficient')]:
  root=resolve(c,c['project_paths'][project]);cfg=yaml.safe_load((root/'config.yaml').read_text(encoding='utf-8'));out=root/(cfg['project']['output_dir'] if kind=='load' else cfg['output_dir'])
  if kind=='load':paths=[out/'oos_predictions.csv.gz']
  else:paths=list(out.glob('walkforward_*.csv.gz'))
  for source in paths:
   if not source.exists():continue
   f=pd.read_csv(source);f=f.rename(columns={f.columns[0]:'目标时刻'});f['目标时刻']=pd.to_datetime(f['目标时刻'])
   cols={'target':'目标时刻','load':'load_hat','da':'da_hat','rt':'rt_hat'}
   if kind=='coefficient':f['k']=1.;cols['k']='k';cols['research_k']='k'
   needed=list(cols.values());f=f[needed].dropna()
   rid='research_'+kind+'_'+digest(source)[:20];dest=exchange/rid
   if (dest/'record.json').exists():continue
   dest.mkdir(parents=True,exist_ok=True);book=dest/'history.xlsx';f.to_excel(book,sheet_name='研究回放',index=False)
   dump(dest/'record.json',{'run_id':rid,'kind':kind,'published_at':stamp(),'model_version':'reconstructed_load_model' if kind=='load' else 'reconstructed_price_neutral_k1_NOT_executed_policy','config':cfg,'config_sha256':digest(root/'config.yaml'),'file':book.name,'sheet':'研究回放','columns':cols,'sha256':digest(book),'status':'research_replay_no_original_publication'})
