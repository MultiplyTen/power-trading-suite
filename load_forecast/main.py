"""Run without CLI arguments. Alternate configs may be selected by POWER_CONFIG."""
import hashlib,json,logging,os,sys,traceback
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from database.runtime import prepare
from database.store import suite_lock
from database.trading import record
import pandas as pd
from configuration import read_config,resolve,target_day,cutoff
from inputs.quality import load_inputs,readiness
from evaluation.disclosure import disclosure_status, disclosure_target_coverage
from weather.features import read_weather
from evaluation.rolling_backtest import run_models
from reports.excel import write_book,template
from reports.html_report import render
from reports.writeback import append_record

OVERVIEW=['decision_cutoff','load_baseline','load_hat','load_p10','load_p90','system_load_hat','da_hat','rt_hat','da_p10','da_p90','rt_p10','rt_p90','da_ref','rt_ref','da_direction','rt_direction','spread_hat','prob_rt_above_da','research_k','adopted_k','declaration_quantity','scenario_count','expected_increment','tail_loss','reason','availability_status','price_basis_status']

def dump(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str,allow_nan=False),encoding='utf-8')

def run(config_path=None):
    prepare('load_forecast')
    config_path=Path(config_path or os.environ.get('POWER_CONFIG',Path(__file__).with_name('config.yaml'))).resolve();c=read_config(config_path)
    output=resolve(c,c['project']['output_dir']);output.mkdir(parents=True,exist_ok=True)
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(message)s',handlers=[logging.StreamHandler(),logging.FileHandler(output/'run.log',encoding='utf-8')],force=True)
    day=target_day(c);now=pd.Timestamp.now(tz=c['project']['timezone']).tz_localize(None)
    runid=now.strftime('%Y%m%d_%H%M%S')+'_'+hashlib.sha256(config_path.read_bytes()).hexdigest()[:6]
    manifest=dict(run_id=runid,target_date=str(day.date()),mode=c['run']['mode'],config_sha256=hashlib.sha256(config_path.read_bytes()).hexdigest(),run_time=str(now),automatic_submission=False)
    templatepath=resolve(c,'data/market_input_template.xlsx')
    if not templatepath.exists():template(templatepath,c['input']['sheets'])
    try:tables,native,sources=load_inputs(c)
    except FileNotFoundError as exc:
        manifest.update(status='missing_input',message='输入文件不存在，请填写data/market_input_template.xlsx并配置路径',detail=str(exc));dump(output/'run_manifest.json',manifest);logging.error(manifest['message']);return manifest
    manifest['config_snapshot']={k:v for k,v in c.items() if k!='_root'};manifest['model_version']=c['project'].get('model_version','load-suite-v1')
    manifest['sources']=sources
    disclosure_input=disclosure_status(c)
    disclosure_input["target_coverage"]=disclosure_target_coverage(c,day)
    manifest['disclosure_input']=disclosure_input
    if c['run']['mode']=='intraday':
        manifest.update(status='intraday_requires_versioned_feed',message='日内接口已保留；须提供带真实发布时间的日内行情和单独验证模型，不能复用日前结果冒充日内更新');dump(output/'run_manifest.json',manifest);logging.warning('预测未生成：'+manifest['message']);return manifest
    end=day if c['run']['mode']=='forecast' else pd.Timestamp(c['backtest']['test_end'])
    issues=readiness(c,tables,end)
    if issues:
        manifest.update(status='blocked_missing_or_unverified_data',issues=issues)
        dump(output/'data_quality.json',manifest);dump(output/'run_manifest.json',manifest)
        write_book(output/'数据缺口清单.xlsx',{'数据缺口':pd.DataFrame(issues)})
        logging.warning('预测未生成：'+json.dumps(issues,ensure_ascii=False));return manifest
    # Trim all actual targets to one fixed forecast origin; long-horizon may not see later actuals.
    if c['run']['mode']=='forecast':
        last=day-pd.Timedelta(days=c['run']['observation_lag_days'])+pd.Timedelta(hours=23)
        for name in ['portfolio','customers','system_load','day_ahead','real_time']:tables[name]=tables[name][tables[name].valid_time<=last].copy()
    hist,live,weather_status=read_weather(c,day,live=c['run']['mode']=='forecast' and day>=now.normalize())
    if c['run']['strict_availability'] and not hist.get('availability_basis',pd.Series(dtype=str)).eq('verified_original_release').all():
        manifest.update(status='blocked_weather_availability',message='固定提前量历史预报尚无原始发布时间凭证');dump(output/'run_manifest.json',manifest);logging.warning('预测未生成：'+manifest['message']);return manifest
    # On a present-day run the target weather must be a receipt before the cutoff.
    if c['run']['mode']=='forecast' and day>=now.normalize():
        hist=hist[hist.valid_time<day]
        if live.empty:
            # 阻塞原因必须同时进 run.log 和控制台，否则上层只会看到手填兜底的二级报错。
            cut=cutoff(c,day);age=c['weather'].get('max_live_cache_age_hours',12)
            msg=('目标日%s没有截点前合格的天气预报：决策截点%s，合格接收窗口%s至%s。'
                 '请在截点之前双击“只更新天气”重新抓取后再跑；超龄批次不会被采用，也不能用实况或旧日期替代。'
                 %(day.date(),cut,cut-pd.Timedelta(hours=age),cut))
            manifest.update(status='blocked_missing_target_forecast_weather',message=msg,weather=weather_status);dump(output/'run_manifest.json',manifest);logging.warning('预测未生成：'+msg);return manifest
        hist=pd.concat([hist,live[live.valid_time>=day]],ignore_index=True)
    result=run_models(c,tables,hist,end,logging.info)
    # rolling_relative窗口解析结果写入manifest（config原值可能仍是fixed日期）。
    manifest['resolved_backtest_windows']=result.get('resolved_windows',{})
    from evaluation.audit import diagnostics
    audits=diagnostics(result,c)
    decisions=result['decisions'];lo=day if c['run']['mode']=='forecast' else pd.Timestamp(c['backtest']['test_start']);hi=end+pd.Timedelta(hours=23)
    overview=decisions.loc[lo:hi,OVERVIEW].copy()
    if c['run']['mode']=='forecast' and (len(overview)!=24 or overview[['load_hat','system_load_hat','da_hat','rt_hat']].isna().any().any()):raise ValueError('目标日不足24个有效预测，请检查历史覆盖和天气')
    # Source unit remains a research assumption until explicitly confirmed.
    if not c['input'].get('unit_confirmed',False):overview['reason']=overview.reason.astype(str)+'；负荷MWh单位为待核验假设'
    customer=pd.concat([a.loc[lo:hi,['object','prediction','baseline','p10','p90','selected_model']] for k,a in result['loads'].items() if k not in ['portfolio','system_load']])
    mapping=tables['mapping'].set_index('entity_id')
    customer['customer_name']=customer['object'].map(mapping['customer_name']) if 'customer_name' in mapping else customer['object']
    customer['city_name']=customer['object'].map(mapping['city_name'])
    quality=dict(sources=sources,weather=weather_status,disclosure=disclosure_input,strict_availability=c['run']['strict_availability'],price_basis_confirmed=c['input']['price_basis_confirmed'],load_unit_confirmed=c['input'].get('unit_confirmed',False),strategy=result['strategy_summary'],optional_inputs='披露供需/备用曲线按原范围进入价格特征消融；备用语义、条件约束生效状态与生产计划仍未认证，禁止跨口径相加；日内模式缺数据停用')
    reference=pd.DataFrame()
    if c['run']['mode']=='forecast' and c['run']['reference_days']:
        from reference import make_reference
        reference=make_reference(c,tables,hist,live,result,day,logging.info)
    sheets={'明日24点总览' if c['run']['mode']=='forecast' else '测试期逐小时':overview,'客户负荷明细':customer,'全省负荷预测':result['loads']['system_load'].loc[lo:hi,['prediction','baseline','p10','p90','selected_model']],'日前实时价格预测':result['prices'].loc[lo:hi,['da_hat','rt_hat','da_p10','da_p90','rt_p10','rt_p90','spread_hat','prob_rt_above_da']],'系数决策依据':overview[['research_k','adopted_k','declaration_quantity','scenario_count','expected_increment','tail_loss','reason']],'未来参考预测':reference,'量化特征':result['features'].loc[lo:hi],'参数与模型信息':pd.DataFrame([dict(object=k,**v) for k,v in result['parameters'].items()]),'数据质量与来源':pd.DataFrame(sources),'回测指标':result['metrics']}
    sheets.update({'分组与区间验证':audits['grouped_metrics'],'日块比较区间':audits['day_block_comparisons'],'概率校准':audits['probability_calibration'],'逐日策略结果':audits['daily_strategy']})
    xlsx=output/('predict_'+runid+'.xlsx');write_book(xlsx,sheets)
    html=output/('report_'+runid+'.html');render(html,overview,result['metrics'],quality,result['parameters'],reference)
    overview.to_csv(output/'predictions.csv',encoding='utf-8-sig');customer.to_csv(output/'customer_predictions.csv',encoding='utf-8-sig');reference.to_csv(output/'reference_predictions.csv',encoding='utf-8-sig',index=False)
    result['features'].loc[lo:hi].to_csv(output/'features.csv',encoding='utf-8-sig');result['metrics'].to_csv(output/'backtest_metrics.csv',index=False);result['validation'].to_csv(output/'validation_metrics.csv',index=False);result['coefficients'].to_csv(output/'model_coefficients.csv',index=False);result['training_trace'].to_csv(output/'training_trace.csv',index=False)
    # Persist honest post-freeze errors for subsequent interval/strategy evaluation.
    decisions.loc[pd.Timestamp(c['backtest']['test_start']):].to_csv(output/'oos_predictions.csv.gz',compression='gzip')
    dump(output/'parameters.json',result['parameters']);dump(output/'data_quality.json',quality)
    for key,value in audits.items():value.to_csv(output/(key+'.csv'),index=key=='daily_strategy')
    manifest['weather_input_hashes']={key:hashlib.sha256(resolve(c,c['weather'][key]).read_bytes()).hexdigest() for key in ['history_file','future_file'] if resolve(c,c['weather'][key]).exists()}
    if c['run']['write_back']:manifest['writeback']=append_record(resolve(c,c['input']['excel_path']),overview,runid,output)
    manifest.update(status='completed_research' if not c['run']['strict_availability'] else 'completed',excel=xlsx.name,html=html.name,rows=len(overview),customer_rows=len(customer),reference_rows=len(reference),strategy=result['strategy_summary'])
    manifest['output_hashes']={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in [xlsx,html,output/'predictions.csv']}
    direct=overview[['load_hat','da_hat','rt_hat']].rename(columns={'load_hat':'load','da_hat':'da','rt_hat':'rt'})
    manifest['database_run_id']=record('load',direct,c,resolve(c,c['db_path']))
    replay=decisions.loc[pd.Timestamp(c['backtest']['test_start']):,['load_hat','da_hat','rt_hat']].rename(columns={'load_hat':'load','da_hat':'da','rt_hat':'rt'}).dropna()
    record('load',replay,c,resolve(c,c['db_path']),status='research_replay_no_original_publication',tag='replay')
    dump(output/'run_manifest.json',manifest);logging.info('完成：'+str(xlsx));return manifest

if __name__=='__main__':
    try:
        with suite_lock():result=run()
        print(json.dumps(result,ensure_ascii=False,indent=2,default=str))
        sys.exit(0 if result['status'].startswith('completed') else 2)
    except Exception as exc:
        logging.exception('运行失败：%s',exc);sys.exit(1)
