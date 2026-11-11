# -*- coding: utf-8 -*-
"""生成可填写模板/导出四张宽表，不把示例数值编造为实际值。"""
import datetime
from pathlib import Path
from openpyxl import Workbook
from openpyxl.styles import Font,PatternFill,Alignment
from database.trading import open_db
from database.store import quote
from data_ingest.validation import SHEETS,headers

def workbook():
    w=Workbook();w.remove(w.active)
    for s in SHEETS:
        ws=w.create_sheet(s);ws.append(headers(24));ws.freeze_panes='B2';ws.column_dimensions['A'].width=15
        for c in ws[1]:c.fill=PatternFill('solid',fgColor='17365D');c.font=Font(color='FFFFFF',bold=True);c.alignment=Alignment(horizontal='center')
        for col in range(2,26):
            ws.column_dimensions[ws.cell(1,col).column_letter].width=12
    return w

def write_export(db_path,path,load_unit='MWh',start=None,end=None,example=False,include_extra=True):
    w=workbook()
    with open_db(db_path) as db:
        for s in SHEETS:
            sql='SELECT date,hour,value FROM '+quote(s)+' WHERE 1=1';params=[]
            if start:sql+=' AND date>=?';params.append(start)
            if end:sql+=' AND date<=?';params.append(end)
            sql+=' ORDER BY date,hour'
            rows=db.execute(sql,params).fetchall()
            if example and rows:
                last=rows[-1][0];rows=[r for r in rows if r[0]==last]
            groups={}
            for date,hour,value in rows:
                if s in ['日前价','实时价'] and value==0:value=None
                if value is not None and load_unit=='kWh' and s in ['负荷','负荷预测']:value*=1000
                groups.setdefault(date,[None]*24)[hour]=value
            for date,values in groups.items():w[s].append([date,*values])
            if example and not rows:w[s].append([str(datetime.date.today()),*([None]*24)])
            for row in w[s].iter_rows(min_row=2,min_col=2):
                for c in row:c.number_format='0.000000';c.font=Font(color='1565C0')
        if not example and include_extra:
            from database.raw_store import EXTRA
            # 数值交换导出包含全部接入事实及发布记录；源文件BLOB/历史天气档案由master.db迁移。
            for name in list(EXTRA)+['手填负荷预测','负荷预测记录','系数决策记录']:
                if not db.execute('SELECT 1 FROM sqlite_master WHERE name=?',(name,)).fetchone():continue
                field='target' if name.endswith('记录') and name!='申报记录' else 'date'
                sql='SELECT * FROM '+quote(name)+' WHERE 1=1';params=[]
                if start:sql+=' AND '+field+'>=?';params.append(start)
                if end:sql+=' AND '+field+'<?';params.append(str(datetime.date.fromisoformat(end)+datetime.timedelta(days=1)))
                cursor=db.execute(sql+' ORDER BY '+field,params);ws=w.create_sheet(name);ws.append([d[0] for d in cursor.description])
                for row in cursor:ws.append(list(row))
                ws.freeze_panes='C2';ws.auto_filter.ref=ws.dimensions
                for cell in ws[1]:
                    cell.fill=PatternFill('solid',fgColor='17365D');cell.font=Font(color='FFFFFF',bold=True)
                    widths={'date':16,'hour':9,'entity':20,'name':38,'value':18,'updated_at':31,'available_at':31,'published_at':31,'source':66,'run_id':58,'target':25,'config_json':80,'source_cell':80,'source_sha':70,'evidence':48}
                    ws.column_dimensions[cell.column_letter].width=widths.get(cell.value,max(18,len(str(cell.value))+2))
                for row in ws.iter_rows(min_row=2):
                    ws.row_dimensions[row[0].row].height=34
                    for cell in row:
                        cell.alignment=Alignment(vertical='center',wrap_text=isinstance(cell.value,str))
                        if isinstance(cell.value,float):cell.number_format='0.000000'
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);w.save(path);w.close()
    return str(path)
