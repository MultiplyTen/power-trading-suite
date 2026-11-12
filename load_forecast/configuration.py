"""Configuration is the only runtime parameter source."""
from pathlib import Path
import os
import copy
import pandas as pd
import yaml

def read_config(path):
    path=Path(path).resolve()
    c=yaml.safe_load(path.read_text(encoding='utf-8'))
    c['_root']=str(path.parent)
    validate(c)
    return c

def resolve(c, value):
    return Path(c['_root'])/value

def validate(c):
    r,t,s=c['run'],c['training'],c['strategy']
    def require(ok,msg):
        if not ok: raise ValueError('配置错误：'+msg)
    require(r['mode'] in ['forecast','backtest','intraday'],'run.mode无效')
    require(0<=r['decision_cutoff_hour']<=23,'截点小时必须为0—23')
    require(0<=r.get('decision_cutoff_minute',0)<=59,'截点分钟必须在0—59')
    require(r['observation_lag_days']>=2,'日前模式业务观测至少滞后2天')
    require(0<=r['reference_days']<=30,'参考天数必须在0—30之间')
    require(t['mode'] in ['full','rolling','incremental'],'训练模式无效')
    require(t['mode']!='incremental','内置Ridge不支持增量学习；请选择full或rolling')
    require(t['retrain_every_days']>0 and t['window_days']>=t['min_history_days']>0,'训练窗口设置不合法')
    require(t['half_life_days']>0 and t['scale_floor']>0,'半衰期和尺度下限必须大于0')
    require(s['k_min']<=1<=s['k_max'] and s['k_min']>=0 and s['k_step']>0,'系数范围必须包含1且步长为正')
    require(abs(round((s['k_max']-s['k_min'])/s['k_step'])*s['k_step']-(s['k_max']-s['k_min']))<1e-8,'系数步长不能整除范围')
    require(0<=s['probability_deadband']<=.5 and 0<s['tail_fraction']<=1 and s['risk_lambda']>=0,'概率/风险参数越界')
    require(s['deviation_lower_ratio']<=1<=s['deviation_upper_ratio'],'回收边界必须包含1')
    require(c['input']['time_label'] in ['interval_start','interval_end'],'时间标签语义不合法')
    require(c['input']['customer_unit'] in ['MWh','kWh','MW','kW'],'客户单位不支持')
    require(c['input']['system_unit']=='MW' and c['input']['price_unit']=='CNY_per_MWh','统调和价格单位必须为MW及CNY_per_MWh')
    require(c['price_direction']['reference']=='lag2_same_hour','暂只支持lag2_same_hour参考价')
    b=c['backtest']
    if r['mode']=='backtest':
        if b.get('window_mode','fixed')=='rolling_relative':
            require(b.get('test_end'),'回测模式rolling_relative仍需test_end作为窗口锚点')
            require(int(b.get('test_days',31))>0 and int(b.get('validation_days',30))>0 and int(b.get('selection_days',30))>0,'滚动窗口天数必须为正')
        else:
            require(all(b[k] for k in ['validation_start','validation_end','test_start','test_end']),'回测日期不能为空')
            a,z,d,e=[pd.Timestamp(b[k]) for k in ['validation_start','validation_end','test_start','test_end']]
            require(a<=z<d<=e,'必须训练/验证在先、测试在后')
            if b.get('model_selection_end'):
                require(pd.Timestamp(b['model_selection_start'])<=pd.Timestamp(b['model_selection_end'])<a,'模型选参必须早于策略验证')
    require(t.get('day_type_granularity','binary') in ['six','binary'],'日型粒度只支持six/binary')
    require(t.get('calendar_features','basic') in ['basic','enhanced'],'calendar_features只支持basic/enhanced')
    require(0<t.get('day_type_soft_weight',0.5)<=1,'日型软权重必须在(0,1]')
    for k in ['max_daily_adjustment_energy','max_daily_expected_loss']:
        require(s[k] is None or s[k]>=0,k+'不可为负')

def target_day(c):
    v=os.environ.get('POWER_TARGET_DATE') or c['run']['target_date']
    return (pd.Timestamp.now(tz=c['project']['timezone']).tz_localize(None).normalize()+pd.Timedelta(days=1)) if v=='tomorrow' else pd.Timestamp(v).normalize()

def cutoff(c, day):
    return pd.Timestamp(day).normalize()-pd.Timedelta(days=1)+pd.Timedelta(hours=c['run']['decision_cutoff_hour'],minutes=c['run'].get('decision_cutoff_minute',0))
