"""Portable openpyxl writer explicitly requested by the project specification."""
import json
from pathlib import Path
import numpy as np
import pandas as pd
from openpyxl import Workbook,load_workbook
from openpyxl.styles import Font,PatternFill,Alignment
from openpyxl.chart import LineChart,Reference
from openpyxl.chart.series import SeriesLabel
from openpyxl.chart.data_source import AxDataSource,StrData,StrVal

LABELS={'valid_time':'目标时刻','decision_cutoff':'信息截点','load_hat':'客户组合预测_MWh','load_baseline':'客户组合基准_MWh','load_p10':'客户负荷P10','load_p90':'客户负荷P90','system_load_hat':'全省统调预测_MW','da_hat':'日前预测_元每MWh','rt_hat':'实时预测_元每MWh','da_p10':'日前P10','da_p90':'日前P90','rt_p10':'实时P10','rt_p90':'实时P90','da_ref':'日前历史参考价','rt_ref':'实时历史参考价','da_direction':'日前方向','rt_direction':'实时方向','spread_hat':'预计实时减日前价差','prob_rt_above_da':'实时高于日前概率','research_k':'研究系数','adopted_k':'采用系数','declaration_quantity':'申报电量_MWh','reason':'采用原因','scenario_count':'联合场景数','expected_increment':'场景平均增量','tail_loss':'场景尾部损失','availability_status':'数据时点状态','price_basis_status':'价格口径状态','prediction':'点预测','baseline':'历史基准','actual':'实际值','object':'预测对象','selected_model':'采用模型'}

def clean(v):
    if isinstance(v,(dict,list,tuple)):return json.dumps(v,ensure_ascii=False,default=str)
    if pd.isna(v):return None
    if isinstance(v,np.generic):return v.item()
    if isinstance(v,pd.Timestamp):return v.to_pydatetime()
    # Input strings are labels, never workbook formula instructions.
    if isinstance(v,str) and v.startswith(('=','+','-','@')):return "'"+v
    return v

def add_table(wb,name,frame):
    f=frame.copy();f=f.reset_index(names='valid_time') if isinstance(f.index,pd.DatetimeIndex) else f.reset_index(drop=True)
    if len(f)>1048575:raise ValueError(f'{name}超过Excel上限；请使用CSV输出')
    ws=wb.create_sheet(name);ws.append([LABELS.get(str(x),str(x)) for x in f.columns])
    for row in f.itertuples(index=False,name=None):ws.append([clean(v) for v in row])
    ws.freeze_panes='B2';ws.auto_filter.ref=ws.dimensions;ws.sheet_view.showGridLines=False
    for cell in ws[1]:cell.fill=PatternFill('solid',fgColor='153D53');cell.font=Font(name='Microsoft YaHei',bold=True,color='FFFFFF',size=11);cell.alignment=Alignment(wrap_text=True,vertical='center')
    ws.row_dimensions[1].height=38
    for col in ws.columns:
        letter=col[0].column_letter;maxlen=max(len(str(cell.value or '')) for cell in list(col)[:100]);ws.column_dimensions[letter].width=min(42,max(16,maxlen*1.3))
        for cell in col[1:]:
            cell.font=Font(name='Microsoft YaHei',size=10,color='172D3C');cell.alignment=Alignment(vertical='center',wrap_text=isinstance(cell.value,str) and len(cell.value)>30)
            if isinstance(cell.value,(int,float)):cell.number_format='#,##0.000;[Red](#,##0.000);–'
            elif isinstance(cell.value,__import__('datetime').datetime):cell.number_format='yyyy-mm-dd hh:mm'
    for row in ws.iter_rows(min_row=2):
        lines=max([1]+[int(np.ceil(sum(2 if ord(ch)>127 else 1 for ch in cell.value)/max(12,ws.column_dimensions[cell.column_letter].width))) for cell in row if isinstance(cell.value,str)])
        ws.row_dimensions[row[0].row].height=min(150,max(22,lines*15))
    return ws

def write_book(path,tables):
    wb=Workbook();wb.remove(wb.active)
    for name,f in tables.items():add_table(wb,name,f)
    # Small native chart on a dedicated readable sheet, data remain in original views.
    if '明日24点总览' in wb and len(tables['明日24点总览']):
        ws=wb['明日24点总览'];headers=[c.value for c in ws[1]]
        chart=LineChart();chart.title='日前与实时预测价格';chart.y_axis.title='元/MWh';chart.x_axis.title='目标时刻';chart.width=26;chart.height=12
        for key in ['da_hat','rt_hat']:
            if LABELS[key] in headers:
                col=headers.index(LABELS[key])+1;chart.add_data(Reference(ws,min_col=col,min_row=1,max_row=ws.max_row),titles_from_data=True);chart.series[-1].tx=SeriesLabel(v=LABELS[key])
        chart.x_axis.tickLblSkip=3
        labels=[t.strftime('%H:%M') if hasattr(t,'strftime') else str(t) for t in tables['明日24点总览'].index]
        for series in chart.series:series.cat=AxDataSource(strLit=StrData(ptCount=len(labels),pt=[StrVal(idx=i,v=v) for i,v in enumerate(labels)]))
        cs=wb.create_sheet('预测价格图');cs.add_chart(chart,'A1')
    Path(path).parent.mkdir(parents=True,exist_ok=True);wb.save(path)
    # Reopen and verify cells rather than trusting an export success.
    check=load_workbook(path,data_only=False)
    for name,f in tables.items():
        if check[name].max_row!=len(f)+1:raise AssertionError('Excel行数不符：'+name)
        for row in check[name]:
            if any(c.data_type=='e' for c in row):raise AssertionError('Excel错误单元格：'+name)
    check.close()

def template(path,sheets):
    names={'portfolio':['日期','时间','实际值','单位','可用时间','版本'],'customers':['客户ID','日期','时间','实际值','单位','可用时间','版本'],'system_load':['日期','时间','统调负荷','单位','可用时间','版本'],'day_ahead':['日期','时间','日前价格','单位','可用时间','版本'],'real_time':['日期','时间','实时价格','单位','可用时间','版本'],'customer_mapping':['客户ID','客户名称','城市','经度','纬度','映射状态'],'supply_demand':['目标时刻','指标','值','单位','可用时间','版本'],'production_plan':['客户ID','目标时刻','计划','可用时间'],'versions':['数据来源','业务时间','发布时间','可用时间','版本']}
    tables={sheets[k]:pd.DataFrame(columns=v) for k,v in names.items()}
    tables['填写说明']=pd.DataFrame([['时间','起点00:00—23:00；终点01:00—24:00须配置interval_end'],['单位','电量MWh/kWh；功率MW/kW；价格元/MWh'],['缺失','留空；零是真实0。严格模式必须提供可用时间'],['地区','填写实际计量城市；城市代理须标明'],['布局','支持长表或日期+24/96时间列'],['数据','模板没有伪造示例值']],columns=['字段','说明']);write_book(path,tables)
