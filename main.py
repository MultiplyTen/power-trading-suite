# -*- coding: utf-8 -*-
"""接入→负荷→系数→复盘；所有阶段共享一个数据库，apply仍为显式操作。"""
import sys,subprocess,os
from pathlib import Path
import pandas as pd
import yaml
from database.store import suite_lock
from database.trading import forecast_load
ROOT=Path(__file__).resolve().parent

def settings():
    configs={p:yaml.safe_load((ROOT/p/'config.yaml').read_text(encoding='utf-8')) for p in ['data_ingest','load_forecast','coefficient_opt','review','disclosure_ingest']}
    paths={(ROOT/p/c['db_path']).resolve() for p,c in configs.items()}
    if len(paths)!=1:raise ValueError('四个config.yaml的db_path必须指向同一个数据库')
    # 信息边界一致性（2026-10-14口径重构：全部用已知数据跑预测）：
    # M2/M3同一观测边界=库内最新完整日；晚到修订靠review多版本拦截+裁决，不再放大全局滞后。
    obs=configs['load_forecast']['run']['observation_lag_days'];lag=configs['coefficient_opt']['lag_days']
    if obs!=lag:raise ValueError(f'load_forecast.run.observation_lag_days({obs})与coefficient_opt.lag_days({lag})不一致；两模块必须使用同一信息边界，请同步修改后再运行。')
    arrival=configs['review'].get('actual_lag_days_by_metric',{});arrival_max=max([v for k,v in arrival.items() if k not in ('declaration','settlement')]+[0]);rlag=configs['review'].get('actual_lag_days',0)
    if obs<=arrival_max:raise ValueError(f'观测滞后({obs}天)必须大于日常最大到达滞后({arrival_max}天，review.actual_lag_days_by_metric的load/da/rt)；请核对日常导入节奏或调大观测滞后。')
    if obs<=rlag:raise ValueError(f'观测滞后({obs}天)必须大于review调参回放滞后actual_lag_days({rlag}天)；回放窗口须严格在预测信息边界之内。')
    def day(v):
        v=os.environ.get('POWER_TARGET_DATE') or v
        return pd.Timestamp.now(tz='Asia/Shanghai').tz_localize(None).normalize()+pd.Timedelta(days=1) if v=='tomorrow' else pd.Timestamp(v).normalize()
    if configs['load_forecast']['run']['mode']=='forecast' and day(configs['load_forecast']['run']['target_date'])!=day(configs['coefficient_opt']['forecast_date']):raise ValueError('负荷模块target_date与系数模块forecast_date不一致，请同步目标日')
    os.environ['POWER_TRADING_DB']=str(next(iter(paths)))
    return configs,next(iter(paths))

def run(project,args=()):
    env=os.environ.copy();env.setdefault('PYTHONIOENCODING','utf-8');env.setdefault('PYTHONUTF8','1')
    result=subprocess.run([sys.executable,str(ROOT/project/'main.py'),*args],cwd=ROOT/project,env=env)
    if result.returncode:raise RuntimeError(f'{project}运行失败（退出码{result.returncode}）；请查看该模块output/run.log，后续阶段已停止')

def main():
    command=sys.argv[1] if len(sys.argv)>1 else 'all';configs,path=settings()
    if command in ['input','export','weather']:
        run('data_ingest',([command] if command in ['export','weather'] else [])+sys.argv[2:]);return
    if command=='disclosure':run('disclosure_ingest');return
    if command in ['review','apply','rollback']:
        run('review',[] if command=='review' else [command]);return
    if command=='init':run('data_ingest',['init',*sys.argv[2:]]);return
    if command not in ['all','load','coefficient']:raise ValueError('支持：all/disclosure/input/load/coefficient/review/export/apply/rollback/init')
    if command=='all':run('disclosure_ingest');run('data_ingest')
    if command in ['all','load']:
        try:run('load_forecast')
        except RuntimeError as exc:
            if command=='load':raise
            day=configs['coefficient_opt']['forecast_date'];day=(pd.Timestamp.now(tz='Asia/Shanghai').tz_localize(None).normalize()+pd.Timedelta(days=1)) if day=='tomorrow' else pd.Timestamp(day)
            try:forecast_load(day,path,manual_only=True)
            except Exception as fallback:
                # 兜底也失败时必须保留负荷模块的原始原因，否则只显示手填模板提示，掩盖真实阻塞点。
                raise RuntimeError(str(exc).rstrip()+'｜手填兜底同样不可用：目标日'+str(day.date())+'没有完整24点正数手填负荷预测。请先处理上面负荷模块的失败原因（详见 load_forecast/output/run.log 与控制台输出的数据/天气齐备状态）；确需手填兜底再填 data_ingest/data/输入模板.xlsx 的“负荷预测”sheet。兜底原始错误：'+str(fallback)) from None
            print('负荷模型未完成，但库内目标日手填预测完整；系数模块将以手填预测作为兜底。',flush=True)
            os.environ['POWER_FORCE_MANUAL_FORECAST']='1'
        if command=='load':return
    run('coefficient_opt')
    if command=='all':run('review')

if __name__=='__main__':
    try:
        with suite_lock():main()
    except Exception as e:print('运行失败：'+str(e),file=sys.stderr);sys.exit(1)
