# -*- coding: utf-8 -*-
"""配置驱动入口：python main.py。所有路径相对项目目录。"""
import hashlib,json,logging,sys,os
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from database.runtime import prepare
from database.store import suite_lock
import numpy as np
import pandas as pd
import yaml
from database.trading import prices as db_prices, forecast_load, published_load_forecast, actual_load as db_actual_load, record
from load_input import read_load,historical_load
from weather_features import get_weather
from price_prediction import features,rolling,metrics
from coefficient_optimization import optimize,optimize_exact,scenario_weights,neighborhood,pnl,risk,haircut
from excel_report import write_book
from report import write_html

ROOT=Path(__file__).resolve().parent

def run(root=ROOT):
    prepare('coefficient_opt')
    c=yaml.safe_load((root/'config.yaml').read_text(encoding='utf-8'))
    if not (0<c['cvar_alpha']<1 and 0<c['confidence']<1):raise ValueError('CVaR 与置信度须在 0—1 之间')
    if not (0<c['k_step']<=c['smooth_limit'] and c['k_min']<=1<=c['k_max']):raise ValueError('系数范围、步长或平滑限制无效')
    if abs((c['k_max']-c['k_min'])/c['k_step']-round((c['k_max']-c['k_min'])/c['k_step']))>1e-7:raise ValueError('系数上下限必须落在同一网格')
    if c['lag_days']<2:raise ValueError('当前输入接口没有价格发布时间审计，必须 lag_days>=2')
    if c['backtest_window_days']<90 or not 1<=c['validation_days']<c['backtest_window_days']:raise ValueError('回测至少90天，验证天数须小于总窗口')
    if c['lambda_risk']<0 or c['error_shrink']<0 or c['deviation_cost_per_mwh']<0:raise ValueError('风险权重、误差折减与偏差费不得为负')
    if c['daily_loss_limit']!='auto' and float(c['daily_loss_limit'])<0:raise ValueError('日亏损限额不得为负')
    if not c['research_mode']:raise ValueError('生产模式暂不可用：现有价格/实际负荷接口缺少逐记录 available_time，需完成结算口径、单位和可用时间认证；不能只改布尔开关绕过')
    target=os.environ.get('POWER_TARGET_DATE') or c['forecast_date']
    day=(pd.Timestamp.now(tz='Asia/Shanghai').tz_localize(None).normalize()+pd.Timedelta(days=1)) if target=='tomorrow' else pd.Timestamp(target).normalize()
    out=root/c['output_dir'];out.mkdir(parents=True,exist_ok=True)
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s',handlers=[logging.FileHandler(out/'run.log',encoding='utf-8'),logging.StreamHandler()],force=True)
    prices,sha=db_prices(root/c['db_path'])
    # 阶段5（2026-10-14）：M3不再自带运行时价格预测，必须消费M2完整发布，消除双轨口径；
    # price_prediction仅保留为场景校准与M4调参的历史回测工具。
    if not c.get('consume_m2_publication',True):raise ValueError('consume_m2_publication=false已不支持：M3运行时价格必须来自M2统一发布')
    published,published_meta=published_load_forecast(day,root/c['db_path'])
    if published is None:
        raise ValueError('目标日%s缺少M2完整发布（load/da/rt齐全）；请先运行 python main.py load。M3不自带运行时价格预测。'%day.date())
    forecast=published['load'];load_source=published_meta
    index=pd.date_range(prices.index.min(),day+pd.Timedelta(hours=23),freq='h');prices=prices.reindex(index)
    last=day-pd.Timedelta(days=c['lag_days']);start=last-pd.Timedelta(days=c['backtest_window_days']-1);split=last-pd.Timedelta(days=c['validation_days']-1)
    actual,load,basis=db_actual_load(index,c['lag_days'],root/c['db_path'])
    load.loc[forecast.index]=forecast
    weather=get_weather(c,index,day,root);x=features(prices,load,weather,c)
    oos=rolling(prices,x,start-pd.Timedelta(days=30),last,c).join(prices).join(actual.rename('actual_load')).join(load.rename('load_hat'))
    oos['da_error']=oos.da-oos.da_hat;oos['rt_error']=oos.rt-oos.rt_hat;oos['spread_error']=oos.da_error-oos.rt_error
    for col in ['da','rt']:oos[col+'_lag'+str(c['lag_days'])]=x[col+'_lag'+str(c['lag_days'])]
    warm=oos[oos.index<start];window=oos[oos.index>=start]
    required=['da','rt','da_hat','rt_hat','actual_load','load_hat']
    good=window[required].notna().all(axis=1)&np.isfinite(window[required]).all(axis=1)&window.load_hat.gt(0)
    if len(window)!=c['backtest_window_days']*24 or not good.all():
        bad=window.index[~good].astype(str).tolist()[:10];raise ValueError(f'90天回测不能组成完整每日场景，缺失/无效位置：{bad}；不补零、不删除部分小时后计算整日风险')
    train=window[window.index<split-pd.Timedelta(days=c['lag_days']-1)];test=window[window.index>=split]
    def arrays(frame):return tuple(frame[n].to_numpy().reshape(-1,24) for n in ['load_hat','actual_load'])+( (frame.da-frame.rt).to_numpy().reshape(-1,24), )
    def scene_p(index,mask=None):
        # 整日成对场景按半衰期指数加权；null/0 等权。mask 为天数级有效性掩码（可选）。
        hl=c.get('scenario_weight_halflife_days')
        if not hl:return None
        days=np.asarray(index[::24].normalize())
        if mask is not None:days=days[np.asarray(mask)]
        return scenario_weights(days,hl)
    std=warm.groupby(warm.index.hour).spread_error.std().reindex(range(24)).to_numpy()
    if not np.isfinite(std).all():raise ValueError('前置价格误差校准样本不足')
    logging.info('联合优化：训练 %s 天，独立验证 %s 天',len(train)//24,len(test)//24)
    p_train=scene_p(train.index)
    kt,train_risk=optimize(*arrays(train),c,std,p=p_train)
    # 精确解（LP/MILP）：与网格解同限额、同权重对拍；可行时作为冻结验证系数。
    kt_exact,train_exact=optimize_exact(*arrays(train),c,std,limit=train_risk['daily_loss_limit'],weights=p_train)
    solver_note=None
    if kt_exact is not None and train_exact.get('feasible'):
        solver_note=f'训练段精确解较网格 {train_exact["objective"]-train_risk["objective"]:+.2f} 元/日'
        logging.info('训练段%s',solver_note)
        kt=kt_exact;train_risk={**train_risk,**{k:train_exact[k] for k in ['objective','mean','cvar','exceed_probability','feasible']}}
    else:
        solver_note=f'训练段精确解未达可行（{train_exact.get("status","lp_failed")}），保留网格解'
        logging.info('%s',solver_note)
    f,l,s=arrays(test);test_values=pnl(kt,f,l,s,c);neutral=pnl(np.ones(24),f,l,s,c)
    # 验证期冻结系数、风险限额和超参数，不以验证结果重新筛选。
    daily=pd.DataFrame({'日期':pd.date_range(split,last),'实际收益':test_values,'中性系数收益':neutral,'相对中性增量':test_values-neutral})
    daily['累计收益']=daily['实际收益'].cumsum();daily['回撤']=daily['累计收益']-np.maximum.accumulate(np.r_[0,daily['累计收益']])[1:]
    sensitivity=pd.DataFrame([{'统一系数':k,**risk(pnl(np.full(24,k),f,l,s,c),c,train_risk['daily_loss_limit'])} for k in [.8,.9,1,1.1,1.2]])
    if published is not None:
        target=published[['da','rt']].rename(columns={'da':'da_hat','rt':'rt_hat'})
        target['training_end']=last;target['decision_cutoff']=day-pd.Timedelta(days=1)+pd.Timedelta(hours=c.get('decision_cutoff_hour',12),minutes=c.get('decision_cutoff_minute',30))
    raw=(target.da_hat-target.rt_hat).to_numpy()
    std_final=window.groupby(window.index.hour).spread_error.std().reindex(range(24)).to_numpy()
    # 整日成对残差保留跨小时与价格/负荷误差依赖，禁止逐小时独立随机拼接。
    err=window.spread_error.to_numpy().reshape(-1,24);lf,la,_=arrays(window)
    fs=np.broadcast_to(forecast.to_numpy(),lf.shape).copy();ls=fs+(la-lf)
    ss=raw+err
    valid=(ls>=0).all(axis=1);excluded=int((~valid).sum())
    if valid.sum()<30:raise ValueError('非负负荷的完整联合场景少于30天，不能输出优化系数')
    p_final=scene_p(window.index,mask=valid)
    k_grid,final_grid=optimize(fs[valid],ls[valid],ss[valid],c,std_final,p=p_final)
    k_exact,final_exact=optimize_exact(fs[valid],ls[valid],ss[valid],c,std_final,limit=final_grid['daily_loss_limit'],weights=p_final)
    exact_gap=None
    if k_exact is not None and final_exact.get('feasible'):
        exact_gap=final_exact['objective']-final_grid['objective']
        logging.info('最终场景精确解较网格 %+.2f 元/日（%s）',exact_gap,final_exact['solver'])
        k,final_risk=k_exact,final_exact
    else:
        logging.info('最终场景精确解未达可行（%s），保留网格解；超限=%.4f',final_exact.get('status','lp_failed'),final_grid['exceed_probability'])
        k,final_risk=k_grid,final_grid
    feasible=final_risk['feasible']
    # 邻域稳健度：±1格可行方向的加权效用退化（正值越大解越稳，NaN=该方向不可行）。
    nb_up,nb_down=neighborhood(k,fs[valid],ls[valid],ss[valid],c,std_final,weights=p_final)
    nb_fin=np.concatenate([nb_up[np.isfinite(nb_up)],nb_down[np.isfinite(nb_down)]])
    frame=pd.DataFrame({'小时':range(24),'研究系数':k,'网格搜索解':k_grid,'实际采用系数':np.ones(24),'预测负荷_MWh':forecast.to_numpy(),'研究申报电量_MWh':forecast.to_numpy()*k,'实际申报基准_MWh':forecast.to_numpy(),'预测日前价':target.da_hat.to_numpy(),'预测实时价':target.rt_hat.to_numpy(),'原始日前减实时价差':raw,'向零折减价差':haircut(raw,std_final,c['error_shrink']),'价差误差标准差':std_final,'邻域效用退化_上调':nb_up,'邻域效用退化_下调':nb_down,'边界触达':np.isclose(k,c['k_min'],atol=c['k_step']/2)|np.isclose(k,c['k_max'],atol=c['k_step']/2),'平滑触达':np.r_[False,np.isclose(abs(np.diff(k)),c['smooth_limit'],atol=0.005)]})
    frame.insert(0,'目标时刻',target.index)
    frame['风控状态']='研究模式，采用1；'+('场景风险可行' if feasible else '超限预算内无可行解（加权口径），不可申报')
    frame['决策理由']=[('日前偏贵，倾向下调' if s>0 else '实时偏贵，倾向上调' if s<0 else '价差中性')+'；整日联合风险/平滑约束可使结果偏离单小时方向' for s in raw]
    meta={'目标日':str(day.date()),'结果性质':'研究模式，实际采用系数为1；研究系数仅供复核','价格发布来源':published_meta,'价格风险校准模型':'coefficient-suite-v1.2-exact-optimizer','训练窗口':[str(train.index.min()),str(train.index.max())],'独立验证窗口':[str(test.index.min()),str(test.index.max())],'前置误差校准窗口':[str(warm.index.min()),str(warm.index.max())],'最终场景窗口':[str(window.index.min()),str(window.index.max())],'历史负荷预测来源':basis,'明日负荷来源':load_source,'剔除负负荷整日场景数':excluded,'训练风险':train_risk,'验证前信息隔离天数':c['lag_days']-1,'验证实际风险':risk(test_values,c,train_risk['daily_loss_limit']),'最终场景风险':final_risk,'求解器':{'主解':final_risk.get('solver','五起点离散坐标搜索'),'网格对照':final_grid['solver'],'精确解较网格_元每日':exact_gap,'超限约束强制':final_risk.get('exceed_enforced',False),'训练段对拍':solver_note},'场景权重':{'半衰期天':c.get('scenario_weight_halflife_days'),'口径':'整日成对场景按日指数加权（最近一天最大），训练段与最终场景同权重；null/0为等权，跨小时与负荷-价差依赖保留'},'邻域稳健度':{'可行方向数':int(len(nb_fin)),'最大退化_元每日':float(np.nanmax(nb_fin)) if len(nb_fin) else None,'平均退化_元每日':float(np.nanmean(nb_fin)) if len(nb_fin) else None,'口径':'主解±1格(0.01)可行邻域（满足平滑与系数域，不含超限预算检查）的加权效用退化；正值越大解越稳；负值表示该方向可改善目标——主解为网格解时说明坐标搜索未收敛（精确解不可行被弃用的情形），主解为精确解时说明解被超限预算顶在约束边界，均应触发人工复查'},'自动限额口径':'max(k=1未折减日均收益绝对值的2倍, k=1日损失的CVaR)；修复基准收益≈0时限额≈0将所有解误判不可行的缺陷；验证期固定训练限额，不由候选解反推','收益口径':'相对于实际负荷全部按日前价结算，(Q-L)*(RT-DA)-偏差获利回收（±20%带外按方向价差回收，2026-10-14业务确认）；另列相对k=1增量','保守风控公式':'场景收益=(Q-L)*(RT-DA)-偏差获利回收-abs(Q-L)*error_shrink*sigma_spread；向零价差仅为方向诊断，持仓风控使用方向不利折减','置信度解释':'90%置信度对应经验超限比例<=10%，不是总体概率保证','差异提示':'明日负荷与价格强制消费M2同一发布（缺失即停止，不自带运行时价格预测，2026-10-14阶段5消除双轨）；历史未提供原始预测时使用因果基准','价格输入快照SHA256':sha,'天气来源':'Open-Meteo previous_day2 广州代理；原始发布时间未认证，WMO缺失时保留缺失哑变量','披露特征':'中调预测曲线作为候选特征；备用/约束语义未认证，不跨口径相加','参数':c,'资料':['https://open-meteo.com/en/docs/previous-runs-api','https://open-meteo.com/en/docs']}
    metric=metrics(test,c);errors=window[['da_error','rt_error','spread_error']]
    hourly=errors.groupby(errors.index.hour).agg(['mean','std','count']);hourly.columns=['_'.join(v) for v in hourly.columns];hourly=hourly.reset_index(names='小时')
    tables={'24小时系数':frame,'系数敏感性':sensitivity,'独立验证逐日收益':daily,'价格预测指标':metric,'分小时误差分布':hourly,'价格逐小时回测':window.reset_index(names='目标时刻'),'冻结验证系数':pd.DataFrame({'小时':range(24),'冻结系数':kt}),'模型参数与审计':pd.DataFrame([{'项目':a,'内容':json.dumps(b,ensure_ascii=False,default=str)} for a,b in meta.items()])}
    name=day.strftime('%Y%m%d');stamp=pd.Timestamp.now(tz='Asia/Shanghai').strftime('%H%M%S')
    excel_name=f'coefficients_{name}_{stamp}.xlsx';html_name=f'decision_{name}_{stamp}.html'
    write_book(out/excel_name,tables)
    write_html(out/html_name,frame,daily,metric,errors,meta,sensitivity)
    window.to_csv(out/f'walkforward_{name}.csv.gz',index_label='timestamp')
    (out/f'audit_{name}.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    direct=frame.set_index('目标时刻').rename(columns={'预测负荷_MWh':'load','预测日前价':'da','预测实时价':'rt','实际采用系数':'k','研究系数':'research_k'})[['load','da','rt','k','research_k']]
    record('coefficient',direct,dict(c,input_forecast_source=load_source),root/c['db_path'])
    replay=window[['load_hat','da_hat','rt_hat']].rename(columns={'load_hat':'load','da_hat':'da','rt_hat':'rt'}).dropna();replay['k']=1.;replay['research_k']=1.
    record('coefficient',replay,c,root/c['db_path'],status='research_replay_no_original_publication',tag='replay_neutral_k1')
    producer={'status':'completed_research','run_time':str(pd.Timestamp.now(tz='Asia/Shanghai').tz_localize(None)),'excel':excel_name,'html':html_name,'config_snapshot':c,'config_sha256':hashlib.sha256((root/'config.yaml').read_bytes()).hexdigest(),'model_version':'coefficient-suite-v1.2-exact-optimizer','price_publication':published_meta}
    (out/f'producer_{name}.json').write_text(json.dumps(producer,ensure_ascii=False,indent=2),encoding='utf-8')
    logging.info('完成：%s；研究解风险可行=%s，实际采用系数=1',out/excel_name,feasible)
    return meta

if __name__=='__main__':
    try:
        with suite_lock():run()
    except Exception as exc:
        logging.exception('运行失败：%s',exc);sys.exit(1)
