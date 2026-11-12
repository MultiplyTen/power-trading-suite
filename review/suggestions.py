# -*- coding: utf-8 -*-
"""全范围候选扫描（进程池并行）；分任务窗口（负荷短/价格风险长）；
候选选择与验证均要求配置改善幅度及日块bootstrap CI下界>0；风险证据另查尾损。"""
import json,subprocess,sys,hashlib,logging,os
from concurrent.futures import ProcessPoolExecutor,as_completed
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from database.store import input_fingerprint
from database.trading import hourly, canonical_fingerprint
import numpy as np
import pandas as pd
import yaml
from common import resolve,digest,dump,get_path,stamp
from candidate_checks import (daily_metric, daily_improvement, passes_improvement,
                              risk_validation_guard, RISK_PARAMETERS)

BOOTSTRAP_SAMPLES=2000

def _run_subprocess(worker_path,job_path):
 """模块级：进程池可pickle。隔离进程执行单个候选回测任务。"""
 return subprocess.run([sys.executable,str(worker_path),str(job_path)],capture_output=True,text=True)

def day_bootstrap_ci(delta,n=BOOTSTRAP_SAMPLES,seed=42):
 """日块bootstrap：对逐日差序列（正=候选改善）按日有放回重采样，返回均值95%CI与点估计。
 样本<2天时返回无CI（None,None,均值）；空序列返回(None,None,None)。"""
 d=np.asarray(delta,float);d=d[np.isfinite(d)]
 if not len(d):return [None,None,None]
 mean=float(d.mean())
 if len(d)<2:return [None,None,mean]
 rng=np.random.default_rng(seed);boot=rng.choice(d,size=(n,len(d)),replace=True).mean(axis=1)
 return [float(np.quantile(boot,.025)),float(np.quantile(boot,.975)),mean]

def generate(db,c,cutoff):
 root=Path(c['_root']);out=resolve(c,c['output_dir']);out.mkdir(parents=True,exist_ok=True);cache=out/'candidate_runs';cache.mkdir(exist_ok=True)
 actual=pd.read_sql_query('SELECT * FROM actuals',db);rows=[];suggest=[]
 date=pd.Timestamp(c['review_date']).normalize() if c['review_date']!='today' else pd.Timestamp.now().normalize()
 base_doc={'created_at':stamp(),'review_date':str(date.date()),'improve_threshold':c['improve_threshold'],'validation_contract':'review-v1.2.1','decision_rule':'选择与验证均要求改善幅度>=improve_threshold且日块bootstrap(2000次)CI下界>0；风险候选还要求同限额尾损/超限比例/场景可行率不恶化','status':'insufficient_data','suggestions':[],'config_hashes':{k:digest(resolve(c,v)/'config.yaml') for k,v in c['project_paths'].items()}}
 dest=out/f'suggestions_{date.strftime("%Y%m%d")}.yaml'
 def finish():
  dest.write_text(yaml.safe_dump(base_doc,allow_unicode=True,sort_keys=False),encoding='utf-8');dump(dest.with_suffix('.validated.json'),base_doc);dump(out/'suggestion_history'/(base_doc['created_at'].replace(':','')+'.json'),base_doc);frame=pd.DataFrame(rows);frame.to_csv(out/'candidate_comparison.csv',index=False);return frame,base_doc
 if actual.empty:return finish()
 actual['target']=pd.to_datetime(actual.target);actual['available_at']=pd.to_datetime(actual.available_at,format="mixed")
 actual=actual[(actual.available_at<=date+pd.Timedelta(hours=23,minutes=59))&(actual.target.dt.normalize()<=cutoff)]
 if actual.groupby(['target','metric']).value.nunique().gt(1).any():
  base_doc['blocked_reason']='检测到实际值修订版本，当前回测适配器不能重建逐历史截点版本；不生成建议。';return finish()
 actual=actual.sort_values(['available_at','ingested_at']).drop_duplicates(['target','metric'],keep='last');counts=actual.groupby(actual.target.dt.normalize()).size();complete=counts[counts.eq(72)].index
 if len(complete)<15:return finish()
 # 晚到值（修订版/迁移批次时间戳）不再阻塞建议（2026-10-14口径重构：全部用已知数据跑预测）：
 # M2/M3按最终快照回放，个别晚到值对相应评估日的污染有限且已透明记录；多版本修订仍由上方检查硬拦截待裁决。
 horizon=complete.max()-pd.Timedelta(days=90)
 recent=actual[actual.target.dt.normalize()>=horizon]
 assumed_release=recent.target.dt.normalize()+pd.Timedelta(days=c['actual_lag_days'],hours=9)
 late=recent[recent.available_at>assumed_release]
 if not late.empty:
  days=sorted({str(d) for d in late.target.dt.normalize().dt.date})
  base_doc['late_arrival_notice']={'条数':int(len(late)),'日期':days[:12],'口径':f"晚于{c['actual_lag_days']}天日常到达口径；其中迁移批次时间戳（available_at=首次入库时刻，非源发布时刻）占大头，真实数据早已存在，影响限于相应评估日，透明记录不阻塞"}
  logging.warning('晚到实际值%d条（%s），最终快照回放口径下已透明记录',len(late),','.join(days[:4]))
 # 分任务窗口（2026-10-14阶段4）：负荷类30天，价格/风险类延长至60天提高统计功效；
 # 各任务前2/3天选择、后1/3天验证，禁止在尾段择优。
 task_windows={}
 for task in ['load','price','risk']:
  w=int(c.get('suggest_window_days_by_task',{}).get(task,c['suggest_window_days']))
  end=complete.max();start=end-pd.Timedelta(days=w-1);split=start+pd.Timedelta(days=w*2//3)
  task_windows[task]=dict(window=w,start=start,split=split,end=end,ok=bool(len(complete[(complete>=start)&(complete<=end)])==w))
 base_doc['task_windows']={k:{'window':v['window'],'start':str(v['start'].date()),'split':str(v['split'].date()),'end':str(v['end'].date()),'data_sufficient':v['ok']} for k,v in task_windows.items()}
 projects={k:resolve(c,v) for k,v in c['project_paths'].items()};configs={k:yaml.safe_load((v/'config.yaml').read_text(encoding='utf-8')) for k,v in projects.items()}
 # 复盘回填与前置训练源若不同，禁止把一套实际值当成另一套的调参证据。
 wide=actual.pivot(index='target',columns='metric',values='value')
 checks=[]
 lc=configs['load_forecast'];source=hourly('负荷',resolve(c,c['db_path']))
 checks.append(('load',pd.Series(source.value.to_numpy(),index=source.timestamp),'统一数据库实际负荷'))
 for metric,series,name in checks:
  paired=pd.concat([series.rename('source'),wide[metric].rename('review')],axis=1).dropna()
  if not np.isclose(paired.source,paired.review,rtol=1e-7,atol=1e-8).all():
   base_doc['blocked_reason']=name+'与对账实际值不一致，请先同步来源；本次不生成建议';return finish()
 fingerprints={k:hashlib.sha256(''.join(digest(p) for p in sorted(list(v.rglob('*.py'))+list((v/'data').glob('*'))) if p.is_file() and '__pycache__' not in p.parts).encode()).hexdigest() for k,v in projects.items()}
 shared_fingerprint=input_fingerprint(resolve(c,c['db_path']))+canonical_fingerprint(resolve(c,c['db_path']))
 def make_job(project,task,param=None,value=None,history=None,reference_risk=None):
  # 指纹含任务窗口起止：窗口变化自动使旧缓存失效；同指纹命中则跳过重跑。
  m=task_windows[task]
  key=hashlib.sha256(json.dumps([project,task,param,value],sort_keys=True).encode()).hexdigest()[:16];path=cache/(key+'.csv');jp=cache/(key+'.json')
  job={'project_root':str(projects[project]),'task':task,'parameter':param,'value':value,'start':str(m['start'].date()),'end':str(m['end'].date()),'lag':c['actual_lag_days'],'output':str(path),'history':str(history) if history else None,'reference_risk':str(reference_risk) if reference_risk else None,'reference_risk_sha256':digest(reference_risk) if reference_risk else None}
  fingerprint=hashlib.sha256(json.dumps({'job':job,'config':base_doc['config_hashes'][project],'data_code':fingerprints[project],'shared_database':shared_fingerprint,'worker':digest(root/'tuning_worker.py'),'risk_reference':digest(root/'reference_risk.py'),'history':digest(history) if history else None},sort_keys=True).encode()).hexdigest();receipt=cache/(key+'.receipt.json')
  cached=path.exists() and receipt.exists() and json.loads(receipt.read_text(encoding='utf-8')).get('fingerprint')==fingerprint
  if not cached:dump(jp,job)
  return dict(key=key,path=path,jp=jp,fingerprint=fingerprint,cached=cached,job=job)
 def execute_batch(pending,strict=True):
  # 进程池并行（tuning_worker无套件锁、只读数据库，可安全并发）；单任务或单核时串行回退。
  # strict=True（基准任务）失败即抛出中止；strict=False（候选任务）失败记录到条目，
  # 由收集循环标"不能评估"（如网格值超出可用历史），不阻断其他候选——逐候选隔离。
  todo=[p for p in pending if not p['cached']]
  if not todo:return
  workers=min(4,os.cpu_count() or 1)
  def accept(job_entry,result):
   (cache/(job_entry['key']+'.log')).write_text(result.stdout+result.stderr,encoding='utf-8')
   if result.returncode:
    err=job_entry['path'].with_suffix('.error.json')
    message=json.loads(err.read_text(encoding='utf-8'))['error'] if err.exists() else result.stderr[-500:]
    if strict:raise ValueError(message)
    job_entry['failed']=message;return
   dump(cache/(job_entry['key']+'.receipt.json'),{'fingerprint':job_entry['fingerprint'],'created_at':stamp()})
  if workers<=1 or len(todo)==1:
   for p in todo:accept(p,subprocess.run([sys.executable,str(root/'tuning_worker.py'),str(p['jp'])],capture_output=True,text=True))
   return
  with ProcessPoolExecutor(max_workers=workers) as ex:
   futs={ex.submit(_run_subprocess,str(root/'tuning_worker.py'),str(p['jp'])):p for p in todo}
   for fut in as_completed(futs):accept(futs[fut],fut.result())
 def load_frame(job_entry):
  frame=pd.read_csv(job_entry['path'],parse_dates=['timestamp']).set_index('timestamp')
  frame.attrs['artifact_sha256']=digest(job_entry['path'])
  return frame
 # 批1（并行）：各任务基准（窗口数据不足的任务整体跳过，不阻断其他任务）；
 # 价格基准输出同时作为风险任务的历史输入。
 baseline_jobs={}
 if task_windows['load']['ok']:baseline_jobs['load']=make_job('load_forecast','load')
 if task_windows['price']['ok']:baseline_jobs['price']=make_job('coefficient','price')
 if not baseline_jobs:
  base_doc['blocked_reason']='各任务窗口实际数据均不足（负荷30天/价格与风险60天）；不生成建议。';return finish()
 baselines={}
 try:
  execute_batch(list(baseline_jobs.values()))
 except Exception as e:
  base_doc['blocked_reason']='基准回测失败：'+str(e);logging.warning('候选基准失败：%s',e);return finish()
 for task in ['load','price']:
  if task not in baseline_jobs:continue
  try:
   f=load_frame(baseline_jobs[task])
   if task=='price':
    for metric in ['da','rt']:
     paired=pd.concat([f[metric].rename('source'),wide[metric].rename('review')],axis=1).dropna()
     if not np.isclose(paired.source,paired.review,rtol=1e-7,atol=1e-8).all():raise ValueError('价格训练源与复盘实际值不一致，请先同步')
   baselines[task]=(f,baseline_jobs[task]['path'])
  except Exception as e:
   rows.append({'project':'load_forecast' if task=='load' else 'coefficient','task':task,'status':'基准失败','reason':str(e)})
   logging.warning('%s基准失败（该任务候选跳过）：%s',task,e)
 risk_params=RISK_PARAMETERS
 def task_of(project,param):
  return 'load' if project=='load_forecast' else ('risk' if param in risk_params else 'price')
 # 批2（并行）：负荷/价格候选 + 风险基准（依赖批1价格基准输出）。
 candidate_jobs={};risk_baseline=None
 if task_windows['risk']['ok'] and 'price' in baselines:
  risk_baseline=make_job('coefficient','risk',history=baselines['price'][1])
  try:
   execute_batch([risk_baseline]);baselines['risk']=(load_frame(risk_baseline),risk_baseline['path'])
  except Exception as e:
   rows.append({'project':'coefficient','task':'risk','status':'基准失败','reason':str(e)});risk_baseline=None
 for project,grid in c['candidate_grid'].items():
  for param,values in grid.items():
   old=get_path(configs[project],param);task=task_of(project,param)
   if not task_windows[task]['ok']:
    rows.append({'project':project,'task':task,'parameter':param,'old':json.dumps(old),'new':json.dumps(values[0]),'status':'跳过','reason':f"任务窗口{task_windows[task]['window']}天实际数据不足"});continue
   if task not in baselines:
    rows.append({'project':project,'task':task,'parameter':param,'old':json.dumps(old),'new':json.dumps(values[0]),'status':'跳过','reason':'该任务基准不可用'});continue
   if task=='risk' and risk_baseline is None:
    rows.append({'project':project,'task':task,'parameter':param,'old':json.dumps(old),'new':json.dumps(values[0]),'status':'跳过','reason':'风险任务基准不可用'});continue
   for value in values:
    if value==old:continue
    candidate_jobs[(project,param,json.dumps(value))]=make_job(project,task,param,value,history=baselines['price'][1] if task=='risk' else None,reference_risk=baselines['risk'][1] if task=='risk' else None)
 execute_batch(list(candidate_jobs.values()),strict=False)
 # 日块bootstrap CI：对逐日差序列（正=候选改善）做按日有放回重采样，取均值的95%区间。
 def day_ci(delta):
  return day_bootstrap_ci(delta,BOOTSTRAP_SAMPLES,42)
 def loss(frame,task,mask):
  a=frame.loc[mask]
  if task=='risk':return -float(a.profit.mean())
  if task=='load':
   denom=float(a.actual.abs().sum());return float((a.actual-a.prediction).abs().sum()/denom) if denom>0 else float('inf')
  return float((abs(a.da-a.da_hat).mean()+abs(a.rt-a.rt_hat).mean())/2)
 def periods(frame,task):
  m=task_windows[task]
  return ((frame.index>=m['start'])&(frame.index<m['split']),(frame.index>=m['split'])&(frame.index<=m['end']+pd.Timedelta(hours=23)))
 frames={}
 for key,j in candidate_jobs.items():
  project,param,_=key;task=task_of(project,param);row={'project':project,'task':task,'parameter':param}
  if j.get('failed'):
   rows.append({**row,'old':json.dumps(get_path(configs[project],param)),'new':json.dumps(j['job']['value']),'status':'不能评估','reason':j['failed']});continue
  try:
   f=load_frame(j);frames[key]=f
   rows.append({**row,'old':json.dumps(get_path(configs[project],param)),'new':json.dumps(j['job']['value']),'status':'候选'})
  except Exception as e:rows.append({**row,'status':'不能评估','reason':str(e)})
 for key,f in frames.items():
  project,param,_=key;task=task_of(project,param)
  row=next(r for r in rows if r.get('project')==project and r.get('parameter')==param and r.get('new')==json.dumps(candidate_jobs[key]['job']['value']))
  try:
   b=baselines[task][0];mask1,mask2=periods(f,task);b1,b2=periods(b,task)
   if (f.loc[mask1|mask2].select_dtypes(include='number').isna().any().any()):raise ValueError('候选回测有缺失，不允许缩小分母挑选样本')
   if not f.loc[mask1|mask2].index.equals(b.loc[b1|b2].index):raise ValueError('候选与基准样本时刻不一致')
   old1,old2=loss(b,task,b1),loss(b,task,b2);new1,new2=loss(f,task,mask1),loss(f,task,mask2)
   imp1=(old1-new1)/max(abs(old1),1.0 if task=='risk' else 1e-9);imp2=(old2-new2)/max(abs(old2),1.0 if task=='risk' else 1e-9)
   # 日级差（正=候选改善）：loss类（负荷/价格）=基准日指标−候选日指标；收益类（风险）=候选日收益−基准日收益。
   d_sel=daily_improvement(b.loc[b1],f.loc[mask1],task)
   d_hold=daily_improvement(b.loc[b2],f.loc[mask2],task)
   ci_sel=day_ci(d_sel);ci_hold=day_ci(d_hold)
   row.update({'status':'已回测','selection_old_loss':old1,'selection_new_loss':new1,'selection_improvement':imp1,
    'selection_ci_low':ci_sel[0],'selection_ci_high':ci_sel[1],'selection_daily_mean':ci_sel[2],
    'holdout_old_loss':old2,'holdout_new_loss':new2,'holdout_improvement':imp2,
    'holdout_ci_low':ci_hold[0],'holdout_ci_high':ci_hold[1],'holdout_daily_mean':ci_hold[2],
    'artifact':str(candidate_jobs[key]['path'].relative_to(out))})
   if task=='risk':
    minimum=c.get('risk_candidate_validation',{}).get('min_complete_days',10)
    row['risk_selection_validation']=risk_validation_guard(b.loc[b1],f.loc[mask1],minimum)
    row['risk_holdout_validation']=risk_validation_guard(b.loc[b2],f.loc[mask2],minimum)
  except Exception as e:row.update({'status':'不能评估','reason':str(e)})
 from reconciliation import reconcile
 from metrics import analyze
 reviewed,_,_=reconcile(db,c);weakness=analyze(reviewed)['weakness'];priority='load' if weakness.startswith('负荷') else 'price' if weakness.startswith('价格') else 'risk';base_doc['weakness']=weakness;base_doc['priority_task']=priority
 # Holdout never ranks candidates: choose once on selection evidence, then independently validate it.
 eligible=[r for r in rows if r.get('status')=='已回测' and r['task']==priority
           and passes_improvement(r['selection_improvement'],[r.get('selection_ci_low'),r.get('selection_ci_high')],c['improve_threshold'])
           and (r['task']!='risk' or r.get('risk_selection_validation',{}).get('status')=='passed')]
 if eligible:
  chosen=max(eligible,key=lambda r:r['selection_improvement']);base_doc['selected_candidate']=chosen
  passes=passes_improvement(chosen['holdout_improvement'],[chosen.get('holdout_ci_low'),chosen.get('holdout_ci_high')],c['improve_threshold'])
  if priority=='risk':passes=passes and chosen.get('risk_holdout_validation',{}).get('status')=='passed'
  base_doc['holdout_passed']=passes
  base_doc['scope_warning']='当前输入缺少完整发布时间审计，建议仅可回写研究模式配置；不能用于生产申报。'
  w=task_windows[priority]['window']
  base_doc['research_candidate']={'project':chosen['project'],'task':priority,'parameter':chosen['parameter'],'old_value':json.loads(chosen['old']),'new_value':json.loads(chosen['new']),'improvement':chosen['holdout_improvement'],'selection_improvement':chosen['selection_improvement'],'selection_ci95':[chosen['selection_ci_low'],chosen['selection_ci_high']],'holdout_ci95':[chosen['holdout_ci_low'],chosen['holdout_ci_high']],'reason':f"前{w*2//3}天选择、后{w-w*2//3}天验证（日块bootstrap {BOOTSTRAP_SAMPLES} 次，改善幅度>={c['improve_threshold']:.1%}且CI下界>0；风险另查尾损）；"+('验证通过' if passes else '验证幅度/CI或风险非恶化证据未通过')}
  if priority=='risk':
   base_doc['research_candidate']['risk_selection_validation']=chosen['risk_selection_validation']
   base_doc['research_candidate']['risk_holdout_validation']=chosen['risk_holdout_validation']
  if passes:
   base_doc['suggestions']=[dict(base_doc['research_candidate'],evidence_scope='research',validated=True)]
   base_doc['status']='research_suggestion_ready'
 else:
  base_doc['blocked_reason']=f"当前短板优先任务 {priority} 的候选未同时满足：选择段改善>={c['improve_threshold']:.1%}且日块CI下界>0（风险另需同限额尾损不恶化证据，含窗口不足）；其他任务仅列对照，本次不生成参数变更。"
 return finish()
