"""Long/wide hourly and quarter-hour inputs, with cell lineage and explicit units."""
import datetime
import re
from pathlib import Path
import numpy as np
import pandas as pd

ALIASES={'date':['日期','date'],'time':['时间','时段','小时','hour','time'],
         'value':['实际值','用电量','实际电量','统调负荷','日前价格','实时价格','日前价','实时价','value'],
         'entity_id':['客户ID','客户编号','entity_id'],'available_time':['可用时间','available_time'],
         'version':['版本','version'],'unit':['单位','unit']}

def time_minutes(v):
    if isinstance(v,(datetime.time,datetime.datetime)): return v.hour*60+v.minute
    s=str(v).strip().replace('时',':00')
    if re.fullmatch(r'\d{1,2}:\d{2}(:00)?',s):
        h,m=map(int,s.split(':')[:2])
        if h>24 or m>=60 or (h==24 and m):raise ValueError('时间超界：'+s)
        return h*60+m
    if re.fullmatch(r'\d+(\.0+)?',s):return int(float(s))*60
    raise ValueError('无法识别时间列：'+s)

def normalize(frame, entity, unit, kind, label='interval_start', price_method='require_hourly_source', source='', aliases=None):
    f=frame.copy();names=dict(ALIASES)
    if aliases:names.update(aliases)
    rename={}
    for canonical,choices in names.items():
        hits=[x for x in f if x in choices]
        if len(hits)>1:raise ValueError(f'{source}：字段{canonical}存在冲突列{hits}')
        if hits:rename[hits[0]]=canonical
    f=f.rename(columns=rename);f['source_row']=np.arange(len(f))+2
    if 'date' not in f:raise ValueError(f'{source}：缺少日期列')
    if 'value' not in f:
        time_cols=[]
        for col in f:
            try:time_minutes(col);time_cols.append(col)
            except ValueError:pass
        if len(time_cols) not in [24,96]:raise ValueError(f'{source}：需要数值列，或24/96个时间列；识别到{len(time_cols)}列')
        f=f.melt(id_vars=[x for x in f if x not in time_cols],value_vars=time_cols,var_name='time',value_name='value')
        f['source_column']=f['time'].astype(str)
    else:f['source_column']='value'
    if 'time' not in f:raise ValueError(f'{source}：缺少时间列')
    minute=f.time.map(time_minutes)
    if (minute>1440).any():raise ValueError(f'{source}：时刻超出24:00')
    uniques=sorted(minute.unique());quarter=any(x%60 for x in uniques)
    duration=15 if quarter else 60
    stamp=pd.to_datetime(f.date,errors='raise').dt.normalize()+pd.to_timedelta(minute,unit='m')
    if label=='interval_end':stamp-=pd.Timedelta(minutes=duration)
    elif (minute==1440).any():raise ValueError(f'{source}：起点标签不能包含24:00，请明确interval_end')
    f['valid_time']=stamp;f['entity_id']=f.get('entity_id',entity).astype(str) if 'entity_id' in f else entity
    original=f.value.copy();f['value']=pd.to_numeric(f.value,errors='coerce')
    bad=original.notna()&f.value.isna()
    if bad.any():raise ValueError(f'{source}：非数值单元格，源行{f.loc[bad,"source_row"].tolist()[:8]}')
    if 'unit' in f and not f.unit.dropna().isin([unit]).all():raise ValueError(f'{source}：行单位与配置{unit}冲突')
    factor=.001 if unit in ['kWh','kW'] else 1
    f['value']*=factor;f['unit']='MWh' if kind=='interval_energy' else ('CNY_per_MWh' if kind=='price' else 'MW')
    f['source']=source;f['native_interval_minutes']=duration;f['native_time_label']=f.time.astype(str)
    if 'available_time' not in f:f['available_time']=pd.NaT
    f['available_time']=pd.to_datetime(f.available_time,errors='raise',format='mixed')
    if 'version' not in f:f['version']='unversioned'
    keys=['entity_id','valid_time','version']
    conflict=f.groupby(keys,dropna=False).value.nunique(dropna=False)>1
    if conflict.any():raise ValueError(f'{source}：冲突重复值{conflict[conflict].index.tolist()[:3]}；请分离来源版本')
    f=f.drop_duplicates(keys)
    if f.duplicated(['entity_id','valid_time']).any():
        raise ValueError(f'{source}：多版本数据必须先按历史截点选择快照，不能将最终修订版直接用于回测')
    native=f.copy()
    if quarter:
        if kind=='price' and price_method!='simple_mean_proxy':raise ValueError(f'{source}：15分钟价格需要小时结算来源或显式simple_mean_proxy配置')
        if kind=='instantaneous_power':raise ValueError(f'{source}：瞬时功率需先按明确方法积分，本适配器不自动近似')
        f['valid_time']=f.valid_time.dt.floor('h');group=f.groupby(['entity_id','valid_time'],sort=True)
        rows=[]
        for (eid,ts),a in group:
            good=len(a)==4 and a.value.notna().all()
            value=(a.value.sum() if kind=='interval_energy' else a.value.mean()) if good else np.nan
            row=a.iloc[0].to_dict();row.update(entity_id=eid,valid_time=ts,value=value,source_row=','.join(a.source_row.astype(str)),source_column=','.join(a.source_column),available_time=a.available_time.max() if a.available_time.notna().all() else pd.NaT,native_time_label=','.join(a.native_time_label));rows.append(row)
        f=pd.DataFrame(rows)
    return f.sort_values(['entity_id','valid_time']),native

def read_file(path,sheet,db_path=None,**kwargs):
    path=Path(path)
    if db_path:
        from database.trading import market_frame,dataset_frame
        kinds={'portfolio.csv':('负荷','portfolio','MWh'),'day_ahead.csv':('日前价','day_ahead','CNY_per_MWh'),'real_time.csv':('实时价','real_time','CNY_per_MWh')}
        f=market_frame(*kinds[path.name],path=db_path) if path.name in kinds else dataset_frame(path,db_path)
    else:f=pd.read_csv(path) if path.suffix in ['.csv','.gz'] else pd.read_excel(path,sheet_name=sheet)
    return normalize(f,source=f'{path.name}:{sheet}',**kwargs)
