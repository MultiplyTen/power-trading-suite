import hashlib
import pandas as pd
from configuration import resolve,cutoff
from inputs.excel_reader import read_file
from database.trading import dataset_frame

SPECS={'portfolio':('portfolio','interval_energy'),'customers':('customer','interval_energy'),
       'system_load':('system_load','interval_mean_power'),'day_ahead':('day_ahead','price'),'real_time':('real_time','price')}

def load_inputs(c):
    tables={};native={};sources=[]
    for name,(entity,kind) in SPECS.items():
        i=c['input'];path=resolve(c,i.get('directory','./data'))/(name+'.csv') if i.get('format','excel')=='csv' else resolve(c,i['excel_path'])
        if name in ['portfolio','customers']:kind=i['customer_value_kind'];unit=i['customer_unit']
        else:unit=i['system_unit'] if name=='system_load' else i['price_unit']
        t,n=read_file(path,i['sheets'][name],db_path=resolve(c,c['db_path']),entity=entity,unit=unit,kind=kind,label=i['time_label'],price_method=i['price_15m_to_hour'],aliases=i.get('aliases'))
        if name in ['portfolio','customers'] and kind=='interval_mean_power':t['unit']='MWh'  # 1-hour average MW × 1h
        negative=(t.value<0) if kind!='price' else pd.Series(False,index=t.index)
        if negative.any():
            if i.get('negative_load_policy','error')!='exclude_with_report':raise ValueError(f'{path.name}：负负荷数据须核对；可配置exclude_with_report隔离，不能自动截断')
            sources.append({'check':'隔离负负荷','file':path.name,'rows':t.loc[negative,['entity_id','valid_time','value','source_row']].astype(str).to_dict('records'),'action':'仅模型视图置空；原始CSV和native保留'})
            t.loc[negative,'value']=float('nan')
        tables[name]=t;native[name]=n
        sources.append(dict(file='SQLite:'+name,sha256=hashlib.sha256(t.to_csv(index=False).encode()).hexdigest(),rows=len(t),first=str(t.valid_time.min()),last=str(t.valid_time.max()),missing=int(t.value.isna().sum())))
    i=c['input'];mappingpath=resolve(c,c['weather']['customer_mapping_file']) if c['weather'].get('customer_mapping_file') else resolve(c,i.get('directory','./data'))/'customer_mapping.csv'
    mapping=dataset_frame(mappingpath,resolve(c,c['db_path']))
    mapping=mapping.rename(columns={'客户ID':'entity_id','城市':'city_name','客户名称':'customer_name'})
    if mapping.entity_id.duplicated().any():raise ValueError('客户地区映射：客户ID重复')
    missing=set(tables['customers'].entity_id)-set(mapping.entity_id)
    if missing:raise ValueError('缺少客户地区映射：'+str(sorted(missing)))
    tables['mapping']=mapping
    sums=tables['customers'].groupby('valid_time').value.sum(min_count=1)
    p=tables['portfolio'].set_index('valid_time').value
    diff=(p-sums).dropna();sources.append({'check':'客户组合对账','max_difference':float(diff.abs().max()) if len(diff) else None,'mismatch_hours':int((diff.abs()>1e-6).sum()),'meaning':'保留组合直接预测；明细覆盖变化单独报告'})
    return tables,native,sources

def readiness(c,tables,day):
    issues=[];last=day-pd.Timedelta(days=c['run']['observation_lag_days']);idx=pd.date_range(last,periods=24,freq='h')
    for key in SPECS:
        if key not in tables or tables[key].empty:
            issues.append({'object':key,'required_date':str(last.date()),'reason':'输入表为空或缺失，不能当作零负荷或通过就绪检查'})
            continue
        for entity,t in tables[key].groupby('entity_id'):
            s=t.set_index('valid_time').value.reindex(idx)
            if s.isna().any():issues.append({'object':entity,'required_date':str(last.date()),'available_through':str(t.valid_time.max().date()),'missing_hours':int(s.isna().sum())})
    if c['run']['strict_availability']:
        if not c['input'].get('availability_certified',False):issues.append({'reason':'来源原始发布时点尚未核验；strict_availability禁止把研究快照当严格回放'})
        for key in SPECS:
            if key not in tables or tables[key].empty:continue
            t=tables[key];past=t[t.valid_time.dt.normalize()<=last]
            if past.available_time.isna().any():issues.append({'object':key,'reason':'存在缺失available_time'})
            # A single materialized snapshot supports strict replay only if no revision
            # and every observation was available by its earliest eligible feature use.
            earliest=past.valid_time.dt.normalize()+pd.Timedelta(days=c['run']['observation_lag_days']-1,hours=c['run']['decision_cutoff_hour'],minutes=c['run'].get('decision_cutoff_minute',0))
            if (past.available_time>earliest).any():issues.append({'object':key,'reason':'延迟发布数据需要逐截点版本快照，不支持直接用最终快照训练'})
    return issues
