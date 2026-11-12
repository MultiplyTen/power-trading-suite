# -*- coding: utf-8 -*-
"""人工执行 apply 才回写；双配置备份、事务日志、陈旧建议检查与回滚。"""
import copy,csv,json,os,uuid
from contextlib import contextmanager
from pathlib import Path
import yaml
from common import resolve,digest,stamp,dump,get_path,set_path
from candidate_checks import passes_improvement,risk_evidence_passed,RISK_PARAMETERS

@contextmanager
def locked(root):
 path=root/'adjust.lock';root.mkdir(parents=True,exist_ok=True)
 try:fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
 except FileExistsError:raise ValueError(f'存在配置调整锁：{path}。确认没有进程运行后检查 transactions 中的事务状态，再处理锁文件。')
 os.close(fd)
 try:yield
 finally:path.unlink(missing_ok=True)

def atomic(path,data):
 temp=path.with_name(path.name+'.new');temp.write_bytes(data);os.replace(temp,path)

def log(root,action,tx,items):
 p=root/'adjust_log.csv';exists=p.exists()
 with p.open('a',newline='',encoding='utf-8-sig') as f:
  w=csv.DictWriter(f,fieldnames=['time','action','transaction','project','parameter','old_value','new_value','reason','expected_improvement'])
  if not exists:w.writeheader()
  for item in items:w.writerow({'time':stamp(),'action':action,'transaction':tx,**{k:item.get(k) for k in ['project','parameter','old_value','new_value','reason']},'expected_improvement':item.get('improvement')})

def validate_value(param,value):
 if param in ['model_name'] and value not in ['ridge','extra_trees']:raise ValueError('模型插件不在允许注册表')
 if param=='models.portfolio.name' and value not in ['baseline_ridge','residual_zero']:raise ValueError('负荷模型插件不在允许注册表')
 if param=='training.calendar_features' and value not in ['basic','enhanced']:raise ValueError('日历特征仅允许basic/enhanced')
 if param.endswith('use_weather') or param=='use_load_feature':
  if not isinstance(value,bool):raise ValueError('特征开关必须为布尔值')
 if param in ['confidence'] and not 0<float(value)<1:raise ValueError('置信度须在0—1')
 if param in ['smooth_limit'] and not .01<=float(value)<=.4:raise ValueError('平滑约束无效')
 if param=='backtest_window_days' and (not isinstance(value,int) or value<90):raise ValueError('回测窗口至少90天')
 if param in ['ridge_alpha','lambda_risk','error_shrink','models.portfolio.half_life_days','models.portfolio.ridge_alpha'] and float(value)<=0:raise ValueError('参数必须大于0')
 if param in ['t_base','models.portfolio.cooling_threshold'] and not -20<=float(value)<=50:raise ValueError('温度阈值超出允许范围')

def apply(c):
 out=resolve(c,c['output_dir']);files=sorted(out.glob('suggestions_*.yaml'))
 if not files:raise ValueError('没有调参建议；请先运行复盘')
 suggestion=files[-1];doc=yaml.safe_load(suggestion.read_text(encoding='utf-8'));items=doc.get('suggestions',[])
 if not items:return {'status':'no_changes','message':'没有通过验证的建议，配置未改动'}
 original=json.loads(suggestion.with_suffix('.validated.json').read_text(encoding='utf-8'))
 if any(i not in original.get('suggestions',[]) for i in items):raise ValueError('建议值或证据被编辑：只可删除条目进行选择性采纳；改值后必须重新回测')
 if len({(i['project'],i['parameter']) for i in items})!=len(items):raise ValueError('建议条目重复')
 paths={k:resolve(c,v)/'config.yaml' for k,v in c['project_paths'].items()};root=out/'adjustments'
 with locked(root):
  pending=list((root/'transactions').glob('*.json')) if (root/'transactions').exists() else []
  if any(json.loads(p.read_text(encoding='utf-8')).get('status')=='prepared' for p in pending):raise ValueError('存在中断事务，请先执行 rollback 恢复')
  configs={k:yaml.safe_load(p.read_text(encoding='utf-8')) for k,p in paths.items()};before={k:p.read_bytes() for k,p in paths.items()}
  for k,p in paths.items():
   if digest(p)!=original['config_hashes'][k]:raise ValueError(f'{k} 配置已变化，建议过期；请重新复盘，禁止覆盖人工修改')
  for item in items:
   project,param=item['project'],item['parameter']
   if project not in c['candidate_grid'] or param not in c['candidate_grid'][project]:raise ValueError('不允许回写此参数：'+param)
   if (not item.get('validated')
       or not passes_improvement(item.get('selection_improvement'),item.get('selection_ci95'),c['improve_threshold'])
       or not passes_improvement(item.get('improvement'),item.get('holdout_ci95'),c['improve_threshold'])):
    raise ValueError('建议未同时通过选择/验证改善阈值与CI门槛；旧证据需重新复盘')
   if item.get('task')=='risk' or param in RISK_PARAMETERS:
    minimum=c.get('risk_candidate_validation',{}).get('min_complete_days',10)
    if not all(risk_evidence_passed(item.get(name),minimum) for name in ['risk_selection_validation','risk_holdout_validation']):
     raise ValueError('风险建议缺少同限额尾损、超限比例及场景可行率不恶化的完整证据')
   if item.get('evidence_scope')=='research':
    if not configs['coefficient'].get('research_mode') or configs['load_forecast']['run'].get('strict_availability'):raise ValueError('研究建议仅可作用于研究模式，不能回写生产配置')
   if get_path(configs[project],param)!=item['old_value']:raise ValueError('旧参数值不匹配：'+param)
   validate_value(param,item['new_value']);set_path(configs[project],param,item['new_value'])
  tx=stamp().replace(':','').replace('.','')+'_'+uuid.uuid4().hex[:8];backup=root/'backups'/tx;backup.mkdir(parents=True)
  for k,data in before.items():(backup/(k+'.yaml')).write_bytes(data)
  import hashlib
  after={k:yaml.safe_dump(cfg,allow_unicode=True,sort_keys=False).encode() for k,cfg in configs.items()}
  record={'id':tx,'status':'prepared','created_at':stamp(),'items':items,'backups':{k:str((backup/(k+'.yaml')).relative_to(root)) for k in paths},'before_hashes':{k:hashlib.sha256(v).hexdigest() for k,v in before.items()},'after_hashes':{k:hashlib.sha256(v).hexdigest() for k,v in after.items()}}
  journal=root/'transactions'/(tx+'.json');dump(journal,record)
  try:
   for k,p in paths.items():atomic(p,after[k])
   record['status']='applied';dump(journal,record);log(root,'apply',tx,items)
  except Exception:
   for k,p in paths.items():atomic(p,before[k])
   record['status']='failed_restored';dump(journal,record);raise
  return {'status':'applied','transaction':tx,'items':items}

def rollback(c):
 out=resolve(c,c['output_dir']);root=out/'adjustments';paths={k:resolve(c,v)/'config.yaml' for k,v in c['project_paths'].items()}
 with locked(root):
  candidates=[]
  for p in sorted((root/'transactions').glob('*.json')):
   r=json.loads(p.read_text(encoding='utf-8'))
   if r['status'] in ['applied','prepared']:candidates.append((p,r))
  if not candidates:return {'status':'no_changes','message':'没有可回滚的配置调整'}
  journal,r=candidates[-1];before={}
  for k,p in paths.items():
   if digest(p) not in ([r['after_hashes'][k],r['before_hashes'][k]] if r['status']=='prepared' else [r['after_hashes'][k]]):raise ValueError(f'{k} 在调整后又被修改，拒绝覆盖；请先保存人工修改')
   b=root/r['backups'][k]
   if digest(b)!=r['before_hashes'][k]:raise ValueError('备份校验失败：'+str(b))
   before[k]=b.read_bytes()
  r['status']='prepared';dump(journal,r)
  for k,p in paths.items():atomic(p,before[k])
  r['status']='rolled_back';r['rolled_back_at']=stamp();dump(journal,r);log(root,'rollback',r['id'],r['items']);return {'status':'rolled_back','transaction':r['id']}
