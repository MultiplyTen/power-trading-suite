import numpy as np
import pandas as pd
from features.calendar import calendar
from weather.features import add_weather

def day_type_code(cal):
    """六类日型：0=周一 1=周二至四 2=周五 3=周末休 4=法定节假日 5=调休上班。
    大类（软回退用）：{0,1,2,5}=工作日类，{3,4}=休息类（周末与节假日互借）。
    节前日效应由 calendar() 的 pre_holiday 列进入残差回归特征，不占用日型类目。"""
    dow=pd.Series(cal.index.dayofweek,index=cal.index)
    code=pd.Series(np.where(cal.holiday.eq(1),4,
                   np.where(cal.makeup_workday.eq(1),5,
                   np.where(dow.eq(0),0,
                   np.where(dow.between(1,3),1,
                   np.where(dow.eq(4),2,3))))),index=cal.index)
    major=code.map({0:0,1:0,2:0,5:0,3:1,4:1})
    return code,major

def baseline(s,c,cal=None):
    t=c['training'];lag0=c['run']['observation_lag_days']
    if cal is None:cal=calendar(s.index)
    num=np.zeros(len(s));den=np.zeros(len(s));total=np.zeros(len(s));count=np.zeros(len(s))
    if t.get('day_type_granularity','binary')=='six':
        # 六类日型硬匹配优先；仅当同日型累计权重不足（<min_type_weight，约一个最老样本）
        # 时向同大类软借（分子×soft、证据量全计），仍无样本回退普通均值。
        # 日型比较用 shift（头部NaN不匹配），不用roll避免循环回绕。
        code,major=day_type_code(cal);soft=float(t.get('day_type_soft_weight',0.5))
        min_type_weight=float(t.get('day_type_min_weight',0.3))
        num_s=np.zeros(len(s));den_s=np.zeros(len(s))
        for lag in range(lag0,lag0+t['baseline_window_days']):
            y=s.shift(24*lag);valid=y.notna().to_numpy();w=2**(-lag/t['half_life_days']);yv=y.to_numpy()
            same=(code==code.shift(24*lag)).to_numpy()&valid
            samemaj=((major==major.shift(24*lag)).to_numpy()&valid)&~same
            num+=np.where(same,yv*w,0);den+=np.where(same,w,0)
            num_s+=np.where(samemaj,yv*w,0);den_s+=np.where(samemaj,w,0)
            total+=np.where(valid,yv,0);count+=valid
        need=den<min_type_weight
        num=np.where(need,num+soft*num_s,num);den=np.where(need,den+den_s,den)
    else:
        work=cal.workday
        for lag in range(lag0,lag0+t['baseline_window_days']):
            y=s.shift(24*lag).to_numpy();valid=np.isfinite(y);w=2**(-lag/t['half_life_days']);match=valid&(work.to_numpy()==work.shift(24*lag).to_numpy())
            num+=np.where(match,y*w,0);den+=match*w;total+=np.where(valid,y,0);count+=valid
    a=np.divide(num,den,out=np.full(len(s),np.nan),where=den>0);fallback=np.divide(total,count,out=np.full(len(s),np.nan),where=count>0)
    return pd.Series(np.where(np.isfinite(a),a,fallback),index=s.index)

def make_features(s,w,c,threshold):
    x=calendar(s.index);b=baseline(s,c,x);lag=c['run']['observation_lag_days']*24
    scale=s.shift(lag).rolling(c['training']['scale_window_hours'],min_periods=168).mean().clip(lower=c['training']['scale_floor'])
    for days in sorted(set([c['run']['observation_lag_days'],7,14])):
        if days>=c['run']['observation_lag_days']:x[f'load_lag{days}_relative']=(s.shift(days*24)-b)/scale
    x['baseline_vs_recentmean']=(b-scale)/scale
    x['recent7_relative']=(s.shift(lag).groupby(s.index.hour).transform(lambda g:g.rolling(7,min_periods=3).mean())-b)/scale
    x=add_weather(x,w,threshold)
    f=pd.DataFrame(dict(actual=s,baseline=b,scale=scale,target=(s-b)/scale),index=s.index)
    return f,x
