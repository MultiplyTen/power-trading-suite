"""Daily rolling origins; only validation chooses candidates, then freeze for test."""
import hashlib,json,copy
import numpy as np
import pandas as pd
from configuration import resolve,cutoff
from models.base import create,LinearPlugin
from features.load import make_features,baseline
from features.calendar import calendar
from weather.features import groups,VARS,add_weather
from evaluation.metrics import regression,classification,day_ci
from strategy.optimizer import decide,gain,recovery,daily_constraints

def dense(table,end):
    s=table.set_index('valid_time').value.sort_index()
    return s.reindex(pd.date_range(s.index.min(),end+pd.Timedelta(hours=23),freq='h'))

def resolve_windows(c,end):
    """rolling_relative：窗口相对锚点滚动（forecast锚=目标日，backtest锚=test_end保持固定，
    M4候选回测窗口稳定可比）；fixed 保留原固定日期行为。返回更新后的config深拷贝。"""
    b=c['backtest']
    if b.get('window_mode','fixed')!='rolling_relative':return c
    test_days=int(b.get('test_days',31));validation_days=int(b.get('validation_days',30));selection_days=int(b.get('selection_days',30))
    test_end=pd.Timestamp(end).normalize()
    test_start=test_end-pd.Timedelta(days=test_days-1)
    validation_end=test_start-pd.Timedelta(days=1);validation_start=validation_end-pd.Timedelta(days=validation_days-1)
    selection_end=validation_start-pd.Timedelta(days=1);selection_start=selection_end-pd.Timedelta(days=selection_days-1)
    c2=copy.deepcopy(c)
    c2['backtest'].update(dict(model_selection_start=str(selection_start.date()),model_selection_end=str(selection_end.date()),
        validation_start=str(validation_start.date()),validation_end=str(validation_end.date()),
        test_start=str(test_start.date()),test_end=str(test_end.date())))
    return c2

def walk(f,x,c,start,end,alpha,plugin,classification_mode=False,kind='load',save_key=None):
    out=pd.Series(np.nan,index=f.index);last=None;model=None;endday=pd.Timestamp(end);traininfo=[]
    # Fixed blocks never train on any label inside the predicted block. Features
    # remain D-2 for each target day, even when parameters refit weekly.
    for d in pd.date_range(start,endday,freq=f'{c["training"]["retrain_every_days"]}D'):
        until=min(d+pd.Timedelta(days=c['training']['retrain_every_days']-1),endday)
        label_end=d-pd.Timedelta(days=c['run']['observation_lag_days'])+pd.Timedelta(hours=23)
        mask=(f.index<=label_end)&f.target.notna()&f.baseline.notna()&f.scale.notna()
        if c['training']['mode']=='rolling':mask&=f.index>=d-pd.Timedelta(days=c['training']['window_days'])
        predmask=(f.index>=d)&(f.index<until+pd.Timedelta(days=1))
        if mask.sum()<max(240,c['training']['min_history_days']*24) or not predmask.any():continue
        model=create(plugin,alpha,classification_mode);model.fit(x.loc[mask],f.loc[mask,'target'])
        model.meta=dict(label_end=str(label_end),fit_origin=str(d),feature_version='2.0',alpha=alpha,data_hash=hashlib.sha256(pd.util.hash_pandas_object(f.loc[mask]).values.tobytes()).hexdigest())
        p=model.predict_proba(x.loc[predmask]) if classification_mode else model.predict(x.loc[predmask])
        if kind=='load':p=np.maximum(f.loc[predmask,'baseline']+f.loc[predmask,'scale']*p,0)
        elif kind=='price':p=f.loc[predmask,'baseline']+p
        out.loc[predmask]=p;traininfo.append(dict(origin=str(d),label_end=str(label_end),rows=int(mask.sum())))
    if save_key and model is not None:model.save(resolve(c,c['project']['model_dir'])/(save_key+'.joblib'))
    return out,model,traininfo

def interval(result,c,point='prediction'):
    a=result.copy();a['p10']=np.nan;a['p90']=np.nan
    # 异方差区间：误差先除以尺度（负荷scale=近期均值水平，价格无scale列则为1），
    # 分位数还原时乘当期尺度，使高负荷时段区间随水平放大；价格行为不变。
    scale=a['scale'] if 'scale' in a.columns else pd.Series(1.0,index=a.index)
    err=(a.actual-a[point])/scale
    for d,g in a.groupby(a.index.normalize()):
        stop=d-pd.Timedelta(days=c['run']['observation_lag_days'])+pd.Timedelta(hours=23)
        history=err[(err.index<=stop)&(err.index>=d-pd.Timedelta(days=c['strategy']['scenario_days']+c['run']['observation_lag_days']))].dropna()
        for ts in g.index:
            values=history[history.index.hour==ts.hour]
            if len(values)<14:values=history
            if len(values)>=14:
                s=scale.loc[ts] if ts in scale.index else 1.0
                a.loc[ts,'p10']=a.loc[ts,point]+values.quantile(.1)*s;a.loc[ts,'p90']=a.loc[ts,point]+values.quantile(.9)*s
    return a

def load_object(name,s,w,c,start,end,spec,tune=True):
    c=copy.deepcopy(c);c['training']['half_life_days']=spec.get('half_life_days',c['training']['half_life_days'])
    tune=tune and c['training'].get('selection_mode','validation')!='fixed'
    candidates={};models={};params={};trace=[];val=c['backtest'];validation=[]
    grid=[('no_weather',spec['ridge_alpha'],spec['cooling_threshold'],False)]
    grid += [(f'weather_a{alpha}_t{temp}',alpha,temp,True) for alpha in (c['training']['ridge_alpha_candidates'] if tune else [spec['ridge_alpha']]) for temp in (c['training']['cooling_threshold_candidates'] if tune else [spec['cooling_threshold']])]
    for label,alpha,temp,wx in grid:
        f,x=make_features(s,w,c,temp)
        if not wx or not spec.get('use_weather',True):x=x[[k for k in x if not k.startswith('wx_')]]
        p,model,tr=walk(f,x,c,start,end,alpha,spec['name'],save_key=hashlib.sha256((name+label).encode()).hexdigest()[:20]);candidates[label]=p;models[label]=model;params[label]=dict(alpha=alpha,cooling_threshold=temp,weather=wx);trace.extend([dict(object=name,candidate=label,**t) for t in tr])
    candidates['baseline']=f.baseline;candidates['lag7']=s.shift(168)
    vm=(s.index>=pd.Timestamp(val.get('model_selection_start',val['validation_start'])))&(s.index<pd.Timestamp(val.get('model_selection_end',val['validation_end']))+pd.Timedelta(days=1));common=vm&s.notna()
    for p in candidates.values():common&=p.notna()
    for label,p in candidates.items():validation.append(dict(object=name,candidate=label,**regression(s[common],p[common])))
    eligible=[r for r in validation if r['n'] and r.get('wmape') is not None]
    fallback=next(label for label,alpha,temp,wx in grid if wx and alpha==spec['ridge_alpha'] and temp==spec['cooling_threshold'])
    chosen=min(eligible,key=lambda r:r['wmape'])['candidate'] if tune and eligible else fallback
    a=f[['actual','baseline','scale']].copy();a['prediction']=candidates[chosen];a['selected_model']=chosen;a['object']=name
    for k,p in candidates.items():a['candidate__'+k]=p
    # The frozen chosen specification has honest errors only AFTER validation end.
    # No pre-freeze errors are admitted to intervals or strategy scenes.
    freeze=pd.Timestamp(val.get('model_selection_end',val['validation_end']))+pd.Timedelta(days=1)
    restricted=a.copy();restricted.loc[restricted.index<freeze,'actual']=np.nan
    inter=interval(restricted,c);a['p10']=inter.p10.clip(lower=0);a['p90']=inter.p90.clip(lower=0)
    model=models.get(chosen);co=[] if model is None else [dict(object=name,model=chosen,**r) for r in model.coefficients()]
    return a,validation,dict(selected=chosen,parameters=params.get(chosen,{}),selection_status='validation_selected' if eligible else 'default_insufficient_validation'),co,trace,candidates[grid[-1][0]]

def price_features(tables,sy,wx,c,end):
    da=dense(tables['day_ahead'],end);rt=dense(tables['real_time'],end).reindex(da.index);sp=rt-da;x=calendar(da.index)
    for name,s in [('da',da),('rt',rt),('spread',sp)]:
        for lag in sorted(set([c['run']['observation_lag_days'],7,14])):
            if lag>=c['run']['observation_lag_days']:x[f'{name}_lag{lag}']=s.shift(lag*24)
        x[name+'_base']=baseline(s,c)
    # sy is a fixed-specification prequential prediction, never fitted actual load.
    x['system_oos_prediction']=sy.reindex(x.index)/10000
    sys=dense(tables['system_load'],end).reindex(x.index);x['system_predicted_change']=x.system_oos_prediction-sys.shift(24*c['run']['observation_lag_days'])/10000
    x=add_weather(x,wx,24)
    if c.get('disclosure',{}).get('use_features',False):
        from evaluation.disclosure import read_disclosure_features
        disclosure=read_disclosure_features(c)
        if not disclosure.empty:x=x.join(disclosure,how='left')
    f=pd.DataFrame(dict(da=da,rt=rt,spread=sp,da_base=baseline(da,c),rt_base=baseline(rt,c)),index=da.index)
    y=(sp>0).astype(float).where(sp.notna());f['majority_prob']=y.shift(c['run']['observation_lag_days']*24).rolling(c['models']['spread_direction']['probability_baseline_days']*24,min_periods=28*24).mean().clip(.001,.999)
    f['hour_prob']=baseline(y,c).clip(.001,.999);f['lag7_prob']=y.shift(168)*.8+.1
    f['da_ref']=da.shift(c['run']['observation_lag_days']*24);f['rt_ref']=rt.shift(c['run']['observation_lag_days']*24)
    # 尖峰two-part第一部分素材：因果滚动P90阈值与尖峰标签（阈值只用D-lag及更早观测）。
    spikes={}
    if c.get('training',{}).get('spike_two_part',False):
        lagh=c['run']['observation_lag_days']*24
        for name,series in [('da',da),('rt',rt)]:
            q=series.shift(lagh).rolling(90*24,min_periods=28*24).quantile(.9)
            spikes[name]=dict(label=(series>q).astype(float).where(series.notna()&q.notna()),quantile=q)
    return f,x,spikes

def prices_fit(tables,sy,wx,c,start,end):
    b=c['backtest'];f,x,spikes=price_features(tables,sy,wx,c,end);vm=(f.index>=pd.Timestamp(b.get('model_selection_start',b['validation_start'])))&(f.index<pd.Timestamp(b.get('model_selection_end',b['validation_end']))+pd.Timedelta(days=1));validation=[];parameters={};co=[];trace=[]
    # 尖峰two-part第一部分：尖峰概率的prequential样本外分类（C固定0.1，logistic），
    # 输出作为第二部分回归特征；分类器特征用非披露列，绝不使用目标日实际价格。
    if spikes:
        wx_cols=[n for n in x if not n.startswith('disc_')]
        for name,info in spikes.items():
            z=pd.DataFrame(index=f.index);z['baseline']=0.;z['scale']=1.;z['target']=info['label'].where(x.system_oos_prediction.notna())
            p,_,tr=walk(z,x[wx_cols],c,start,end,0.1,'logistic_with_baseline_selection',classification_mode=True,kind='plain',save_key=name+'_spike_classifier')
            trace.extend([dict(object=name+'_spike',candidate='logistic_0.1',**t) for t in tr])
            x['spike_p_'+name]=p
            lagh=c['run']['observation_lag_days']*24
            x['spike_gap_'+name]=(f[name].shift(lagh)-info['quantile'].shift(lagh)).clip(lower=0)/100.
    for target,key in [('da','day_ahead'),('rt','real_time'),('probability','spread_direction')]:
        cls=target=='probability';spec=c['models'][key];z=pd.DataFrame(index=f.index);z['baseline']=0. if cls else f[target+'_base'];z['scale']=1.;z['target']=((f.spread>0).astype(float).where(f.spread.notna()) if cls else f[target]-z.baseline).where(x.system_oos_prediction.notna())
        candidates={};models={}
        disclosure_cols=[n for n in x if n.startswith('disc_')]
        spike_cols=[n for n in x if n.startswith('spike_')]
        base_cols=[n for n in x if not any(n.startswith(p) for p in ('wx_','system_','disc_','spike_'))]
        wx_cols=[n for n in x if n.startswith('wx_')]
        feature_groups=[('no_weather',base_cols),('weather',base_cols+wx_cols+[n for n in x if n.startswith('system_')])]
        if disclosure_cols:
            feature_groups += [('disclosure',base_cols+disclosure_cols),('weather_disclosure',base_cols+wx_cols+[n for n in x if n.startswith('system_')]+disclosure_cols)]
        if spike_cols:
            feature_groups += [('spike',base_cols+spike_cols),('weather_spike',base_cols+wx_cols+[n for n in x if n.startswith('system_')]+spike_cols)]
        for group,cols in feature_groups:
            for alpha in (spec['C_candidates'] if cls else c['training']['ridge_alpha_candidates']):
                label=group+'_'+str(alpha)
                p,m,tr=walk(z,x[cols],c,start,end,alpha,spec['name'],classification_mode=cls,kind='plain' if cls else 'price',save_key=target+'_'+label);candidates[label]=p;models[label]=m;trace.extend([dict(object=target,candidate=label,**t) for t in tr])
        if cls:
            for label in ['majority_prob','hour_prob','lag7_prob']:candidates[label]=f[label]
        else:candidates['baseline']=f[target+'_base'];candidates['lag7']=f[target].shift(168)
        common=vm&f.spread.notna()
        for p in candidates.values():common&=p.notna()
        rows=[dict(object=target,candidate=k,**(classification(f.spread[common],p[common]) if cls else regression(f.loc[common,target],p[common]))) for k,p in candidates.items()]
        metric='log_loss' if cls else 'mae';chosen=min([r for r in rows if r['n']],key=lambda r:r[metric])['candidate'];validation.extend(rows);parameters[target]={'selected':chosen}
        col='prob_rt_above_da' if cls else target+'_hat';f[col]=candidates[chosen]
        for label,p in candidates.items():f[target+'__'+label]=p
        m=models.get(chosen)
        if m:co.extend([dict(object=target,model=chosen,**r) for r in m.coefficients()])
        if not cls:
            z=pd.DataFrame(dict(actual=f[target],prediction=f[col]),index=f.index);z.loc[z.index<=pd.Timestamp(b.get('model_selection_end',b['validation_end']))+pd.Timedelta(hours=23),'actual']=np.nan
            inter=interval(z,c);f[target+'_p10']=inter.p10;f[target+'_p90']=inter.p90
    f['spread_hat']=f.rt_hat-f.da_hat
    for target in ['da','rt']:
        diff=f[target+'_hat']-f[target+'_ref'];band=c['price_direction']['display_neutral_band'];f[target+'_direction']=np.where(diff>band,'偏涨',np.where(diff<-band,'偏跌','相近'))
    return f,validation,parameters,co,trace,x

def run_models(c,tables,weather,end,progress=print):
    end=pd.Timestamp(end)
    c=resolve_windows(c,end)  # rolling_relative：窗口随目标日滚动；fixed 保持原固定日期
    start=pd.Timestamp(c['backtest'].get('model_selection_start',c['backtest']['validation_start']))-pd.Timedelta(days=c['training']['min_history_days'])
    wx,weights=groups(c,weather,tables,end);results={};validations=[];params={};coefs=[];traces=[]
    objects=[('system_load',tables['system_load'],wx['province'],c['models']['system_load']),('portfolio',tables['portfolio'],wx['portfolio'],c['models']['portfolio'])]
    maps=tables['mapping'].set_index('entity_id')
    for eid,t in tables['customers'].groupby('entity_id'):
        city=maps.loc[eid,'city_name']
        if city not in wx:raise ValueError('客户天气城市缺失：'+city)
        objects.append((eid,t,wx[city],c['models']['customer']))
    fixed_system=None
    for name,table,w,spec in objects:
        progress('拟合负荷：'+name)
        s=dense(table,end);a,v,p,co,tr,_=load_object(name,s,w,c,start,end,spec)
        results[name]=a;validations.extend(v);params[name]=p;coefs.extend(co);traces.extend(tr)
        if name=='system_load':
            f,x=make_features(s,w,c,spec['cooling_threshold']);fixed_system,_,_=walk(f,x,c,start,end,spec['ridge_alpha'],spec['name'],save_key='system_fixed')
    progress('拟合日前、实时价格及价差概率')
    prices,v,p,co,tr,px=prices_fit(tables,fixed_system,wx['province'],c,start,end);validations.extend(v);params.update(p);coefs.extend(co);traces.extend(tr)
    progress('计算风险场景与系数')
    from strategy.validation import evaluate_policy
    decision,policy_details=evaluate_policy(prices,results['portfolio'],c);decision=decision.join(results['system_load'][['prediction']].rename(columns={'prediction':'system_load_hat'}),validate='1:1')
    decision['decision_cutoff']=decision.index.normalize().map(lambda day: cutoff(c,day))
    decision['availability_status']='research_business_date_only' if not c['run']['strict_availability'] else 'strict_snapshot'
    decision['price_basis_status']='confirmed' if c['input']['price_basis_confirmed'] else 'price_basis_pending'
    rows=[];lo=pd.Timestamp(c['backtest']['test_start']);hi=pd.Timestamp(c['backtest']['test_end'])+pd.Timedelta(hours=23)
    for obj,a in results.items():
        t=a.loc[lo:hi]
        for col in ['prediction','baseline']+[k for k in a if k.startswith('candidate__')]:
            met=regression(t.actual,t[col]);rows.append(dict(object=obj,model=col,**met))
    for obj in ['da','rt']:
        a=decision.loc[lo:hi];met=regression(a[obj],a[obj+'_hat']);good=a[[obj,obj+'_hat',obj+'_ref']].notna().all(axis=1)
        met['direction_accuracy']=float((np.sign(a.loc[good,obj]-a.loc[good,obj+'_ref'])==np.sign(a.loc[good,obj+'_hat']-a.loc[good,obj+'_ref'])).mean());rows.append(dict(object=obj,model=params[obj]['selected'],**met))
    a=decision.loc[lo:hi];rows.append(dict(object='spread',model=params['probability']['selected'],**classification(a.spread,a.prob_rt_above_da)))
    daily=a.groupby(a.index.normalize()).adopted_increment.sum(min_count=1)
    strategy_summary=dict(adopted_increment=float(a.adopted_increment.sum()),research_increment=float(a.research_increment.sum()),worst_adopted_day=float(daily.min()),negative_adopted_days=int((daily<0).sum()),adjusted_hours=int((a.adopted_k.fillna(1)!=1).sum()),research_adjusted_hours=int((a.research_k.fillna(1)!=1).sum()),day_block_ci=day_ci(a.adopted_increment,a.index,c['backtest']['bootstrap_samples']),gate=policy_details,research_only=True)
    # 回传实际使用的窗口（rolling_relative模式下与config原值不同），供manifest审计。
    resolved={k:str(v) for k,v in c['backtest'].items() if k.endswith('_start') or k.endswith('_end')}
    return dict(decisions=decision,loads=results,prices=prices,validation=pd.DataFrame(validations),metrics=pd.DataFrame(rows),parameters=params,coefficients=pd.DataFrame(coefs),training_trace=pd.DataFrame(traces),features=px,weights=weights,strategy_summary=strategy_summary,resolved_windows=resolved)
