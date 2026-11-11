# -*- coding: utf-8 -*-
"""保存不可变运行快照，拒绝用当前配置冒充历史发布配置。"""
import json,shutil,hashlib
from pathlib import Path
import yaml
from common import resolve,digest,dump,stamp

def capture(c):
 exchange=resolve(c,c['exchange_dir']);exchange.mkdir(parents=True,exist_ok=True)
 for project,kind in [('load_forecast','load'),('coefficient','coefficient')]:
  root=resolve(c,c['project_paths'][project]);cfg=yaml.safe_load((root/'config.yaml').read_text(encoding='utf-8'))
  out=root/(cfg['project']['output_dir'] if kind=='load' else cfg['output_dir'])
  manifests=list(out.glob('producer_*.json'))
  if kind=='load' and (out/'run_manifest.json').exists():manifests.append(out/'run_manifest.json')
  for path in manifests:
   m=json.loads(path.read_text(encoding='utf-8'))
   if 'config_snapshot' not in m or not str(m.get('status','')).startswith('completed'):continue
   source=out/m['excel']
   if not source.exists():raise ValueError(f'{path}：预测文件不存在')
   sha=digest(source);rid=kind+'_'+hashlib.sha256((sha+m['config_sha256']).encode()).hexdigest()[:24];dest=exchange/rid
   if (dest/'record.json').exists():continue
   dest.mkdir(parents=True,exist_ok=True);shutil.copy2(source,dest/source.name)
   cols={'target':'目标时刻','load':'客户组合预测_MWh','da':'日前预测_元每MWh','rt':'实时预测_元每MWh','k':'采用系数','research_k':'研究系数'} if kind=='load' else {'target':'目标时刻','load':'预测负荷_MWh','da':'预测日前价','rt':'预测实时价','k':'实际采用系数','research_k':'研究系数'}
   if kind=='load':cols.pop('k');cols.pop('research_k')
   dump(dest/'record.json',{'run_id':rid,'kind':kind,'published_at':m['run_time'],'model_version':m.get('model_version','unknown'),'config':m['config_snapshot'],'config_sha256':m['config_sha256'],'file':source.name,'sheet':'明日24点总览' if kind=='load' else '24小时系数','columns':cols,'sha256':sha,'status':'research' if 'research' in m['status'] else 'production'})
