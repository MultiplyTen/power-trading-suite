# -*- coding: utf-8 -*-
"""收集全部错误；只有整本通过才返回可以提交的小时变更。"""
import datetime,re,math
from numbers import Real
from openpyxl import load_workbook

SHEETS=['负荷','日前价','实时价','负荷预测']
def headers(n):
    return ['日期']+[f'{i*(1440//n)//60:02d}:{i*(1440//n)%60:02d}' for i in range(n)]

def empty(v):return v is None or isinstance(v,str) and not v.strip()

def parse_date(v):
    if isinstance(v,datetime.datetime):
        if v.time()!=datetime.time():raise ValueError('日期中不能带非零时分秒')
        return v.date()
    if isinstance(v,datetime.date):return v
    if not isinstance(v,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',v):raise ValueError('格式须为YYYY-MM-DD')
    return datetime.date.fromisoformat(v)

def validate(path,config,today):
    errors=[];changes=[];stats={s:{'new':0,'updated':0,'unchanged':0,'blank_cells':0,'new_days':set(),'updated_days':set()} for s in SHEETS}
    def error(s,r,c,msg):errors.append({'sheet':s,'row':r,'column':c,'message':msg})
    book=load_workbook(path,data_only=False,read_only=True)
    try:
        for s in set(book.sheetnames)-set(SHEETS):error(s,1,1,'多余或被改名的sheet；必须使用固定四个名称')
        for s in SHEETS:
            if s not in book.sheetnames:
                error(s,1,1,'缺少sheet，请恢复模板原名称');continue
            ws=book[s];ws.reset_dimensions();head=[c.value for c in next(ws.iter_rows(min_row=1,max_row=1))]
            while head and empty(head[-1]):head.pop()
            n=len(head)-1
            if n not in ([24] if s=='负荷预测' else [24,96]):
                error(s,1,1,'数值列数须为'+('24' if s=='负荷预测' else '24或96')+'，检测到'+str(n));continue
            for col,(actual,expected) in enumerate(zip(head,headers(n)),1):
                if actual!=expected:error(s,1,col,f'表头应为“{expected}”，实际为“{actual}”')
            seen={}
            for row in ws.iter_rows(min_row=2):
                values=[c.value for c in row]
                if all(empty(v) for v in values):continue
                r=row[0].row;d=None
                try:d=parse_date(values[0])
                except Exception:error(s,r,1,f'日期“{values[0]}”非法，必须为YYYY-MM-DD且不带时分秒')
                if d:
                    if d in seen:error(s,r,1,f'日期{d}重复出现，首次位于第{seen[d]}行')
                    seen[d]=r
                    if s!='负荷预测' and d>today:error(s,r,1,f'实际值日期{d}晚于今天{today}，疑似把预测填入实际值')
                for col in range(n+1,len(values)):
                    if not empty(values[col]):error(s,r,col+1,'此列超出模板数值范围')
                numbers=[]
                for col in range(1,n+1):
                    v=values[col] if col<len(values) else None
                    if empty(v):stats[s]['blank_cells']+=1;numbers.append(None);continue
                    try:
                        if isinstance(v,bool):raise ValueError()
                        x=float(v)
                        if not math.isfinite(x):raise ValueError()
                    except Exception:error(s,r,col+1,f'值“{v}”不是有限数字（公式也不接受，请粘贴为值）');numbers.append(None);continue
                    if s in ['负荷','负荷预测'] and x<=0:error(s,r,col+1,f'负荷必须大于0，实际为{x}')
                    if s in ['日前价','实时价'] and not config['price_range'][0]<=x<=config['price_range'][1]:error(s,r,col+1,f'价格{x}超出配置范围{config["price_range"]}')
                    if s in ['日前价','实时价'] and x==0:
                        stats[s]['blank_cells']+=1;numbers.append(None);continue
                    numbers.append(x)
                for h in range(24):
                    chunk=numbers[h*(n//24):(h+1)*(n//24)];filled=[v for v in chunk if v is not None]
                    if not filled:continue
                    if n==96 and s in ['日前价','实时价'] and any(str(v).strip() in ['0','0.0'] for v in values[h*4+1:h*4+5]):continue
                    if len(filled)!=len(chunk):
                        error(s,r,h*4+2,f'{h:02d}:00这一小时必须填齐4个15分钟数值，或全部留空');continue
                    value=sum(filled) if s in ['负荷','负荷预测'] else sum(filled)/len(filled)
                    if s in ['负荷','负荷预测'] and config['load_unit']=='kWh':value/=1000
                    if d:changes.append({'kind':s,'date':str(d),'hour':h,'value':value,'cell':f'{s}!R{r}C{h*(n//24)+2}:C{(h+1)*(n//24)+1}','aggregation':'sum_energy' if s in ['负荷','负荷预测'] else 'arithmetic_mean_proxy' if n==96 else 'hourly_input'})
    finally:book.close()
    return errors,changes,stats
