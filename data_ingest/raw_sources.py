"""四类源文件→带单元格证据的标准记录；先全文件校验，再由调用方提交。"""
import datetime as dt
import io, math, re, zipfile
from collections import defaultdict
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter


def blank(v): return v is None or (isinstance(v,str) and not v.strip())
def label(v): return '' if v is None else str(v).strip()
def rows(ws):
    ws.reset_dimensions()
    return list(ws.values)
def at(row,col): return row[col] if col<len(row) else None

def decode_name(info):
    if info.flag_bits & 0x800: return info.filename
    try: return info.filename.encode('cp437').decode('gb18030')
    except (UnicodeError,LookupError): return info.filename

class Cleaner:
    def __init__(self,name,config,today=None):
        self.name=name; self.c=config; self.today=today or dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()
        self.numeric_cells={};self.records=[];self.errors=[];self.notes=[];self.missing=defaultdict(int);self.seen=set()
    def error(self,s,r,c,msg): self.errors.append({'file':self.name,'sheet':s,'row':r,'column':c,'message':msg})
    def number(self,v,s,r,c):
        if blank(v): return None
        if isinstance(v,(dt.datetime,dt.time,dt.timedelta)) and (s,r,c) in self.numeric_cells:
            self.notes.append(f'{s}!{get_column_letter(c)}{r}：误设日期格式，从XML恢复原始数字')
            v=self.numeric_cells[(s,r,c)]
        try:
            if isinstance(v,bool):raise ValueError()
            n=float(v.strip() if isinstance(v,str) else v)
            if not math.isfinite(n):raise ValueError()
            return n
        except (TypeError,ValueError):self.error(s,r,c,'不是有限数值：'+repr(v));return None
    def date(self,v,s,r,c):
        try:
            if isinstance(v,dt.datetime):
                if v.time()!=dt.time():raise ValueError()
                v=v.date()
            return dt.date.fromisoformat(str(v))
        except (ValueError,TypeError):self.error(s,r,c,'日期非法：'+repr(v));return None
    def add(self,kind,date,hour,value,s,r,c,entity='',name=''):
        if value is None:
            self.missing[kind]+=1;return
        if '价' in kind and value==0:
            self.missing[kind+'零值']+=1;return
        if not date:return
        # 次日已成交申报是已知计划，不是未来实测；只允许到明日。
        limit=self.today+dt.timedelta(days=1) if kind=='申报记录' else self.today
        if date>limit:self.error(s,r,c,'未来日期的实际值（申报记录仅允许已成交次日计划）');return
        bounds=self.c.get('system_load_range',[0,200000]) if kind=='统调负荷' else self.c['price_range'] if '价' in kind else [0,float('inf')]
        if not bounds[0]<=value<=bounds[1] or (kind in ('负荷','运行用电量对照') and value<=0):
            self.error(s,r,c,'数值超合理范围：'+str(value));return
        key=(kind,str(date),hour,entity)
        if key in self.seen:self.error(s,r,c,'业务主键重复：'+str(key));return
        self.seen.add(key)
        self.records.append(dict(kind=kind,date=str(date),hour=hour,value=value,entity=entity,name=name,cell=f'{self.name}/{s}!{get_column_letter(c)}{r}'))
    def retail(self,w):
        totals=defaultdict(float);sources=defaultdict(list);companydays=set();found=False
        factor=.001 if self.c.get('retail_unit','MWh')=='kWh' else 1
        for ws in w:
            a=rows(ws)
            if not a:continue
            header=[label(v) for v in a[0]]
            required=['日期','用电企业编码','用电企业名称','合计']+[f'{h}时' for h in range(24)]
            if not all(v in header for v in required):self.error(ws.title,1,1,'零售表缺少日期、企业、合计或完整24小时表头');continue
            found=True;ix={v:header.index(v) for v in required}
            for ri,row in enumerate(a[1:],2):
                if all(blank(v) for v in row):continue
                date=self.date(at(row,ix['日期']),ws.title,ri,ix['日期']+1);entity=label(at(row,ix['用电企业编码']));name=label(at(row,ix['用电企业名称']))
                if not entity or not name:self.error(ws.title,ri,ix['用电企业编码']+1,'企业编码/名称不能为空')
                if (date,entity) in companydays:self.error(ws.title,ri,1,'同企业同日期重复行')
                companydays.add((date,entity))
                nums=[self.number(at(row,ix[f'{h}时']),ws.title,ri,ix[f'{h}时']+1) for h in range(24)]
                total=self.number(at(row,ix['合计']),ws.title,ri,ix['合计']+1)
                if total is None or any(v is None for v in nums):self.error(ws.title,ri,1,'企业行须有合计和完整24小时，防止少计组合负荷');continue
                if abs(total-sum(nums))>.010000001:self.error(ws.title,ri,ix['合计']+1,'合计与24小时求和不一致（源单位容差0.01）')
                for h,v in enumerate(nums):
                    self.add('用户明细',date,h,v*factor,ws.title,ri,ix[f'{h}时']+1,entity,name)
                    if date:totals[(date,h)]+=v*factor;sources[(date,h)].append(f'{ws.title}!{get_column_letter(ix[f"{h}时"]+1)}{ri}')
        if not found:self.error('',1,1,'未找到零售业务表')
        for (date,h),v in totals.items():
            self.add('负荷',date,h,v,'组合汇总',1,h+1)
            if self.records and self.records[-1]['kind']=='负荷':self.records[-1]['cell']=self.name+'/'+','.join(sources[(date,h)])
        self.notes.append('客户单户零电量保留；组合负荷必须大于0。电量单位按配置 '+self.c.get('retail_unit','MWh')+'，源文件未标单位；须由提供方确认。')
    def price(self,w):
        found=False
        for ws in w:
            match=re.fullmatch(r'(\d{2}|\d{4})年价格',ws.title.strip())
            if not match:continue
            found=True;a=rows(ws);year=int(match[1]);year=year+2000 if year<100 else year
            if len(a)<4:self.error(ws.title,1,1,'价格表不足日期表头');continue
            month=None;dates={}
            for j in range(2,max(map(len,a))):
                m=re.fullmatch(r'(\d{1,2})月',label(at(a[0],j)))
                if m:month=int(m[1])
                day=at(a[2],j)
                if blank(day):continue
                try: dates[j]=dt.date(year,month,int(day)) if float(day)==int(day) else None
                except (ValueError,TypeError):self.error(ws.title,3,j+1,'月份/日号非法或缺少月份标签')
            if not dates:self.error(ws.title,3,3,'未识别任何日期列')
            markers=[(i,label(at(r,0))) for i,r in enumerate(a) if label(at(r,0)) in ('日前','实时','价差')]
            for tag,kind in [('日前','日前价'),('实时','实时价')]:
                starts=[i for i,t in markers if t==tag]
                if len(starts)!=1:self.error(ws.title,1,1,'应恰有一个'+tag+'区块');continue
                start=starts[0];end=min([i for i,t in markers if i>start]+[len(a)])
                points=[(i,at(a[i],1)) for i in range(start,end) if isinstance(at(a[i],1),(int,float)) or label(at(a[i],1)).isdigit()]
                hours=[float(h) for _,h in points]
                if hours==list(range(24)):offset=0
                elif hours==list(range(1,25)):
                    offset=1;self.notes.append(ws.title+'/'+tag+'：1—24时按结束时段编号转为0—23，配置说明列明该约定。')
                else:self.error(ws.title,start+1,2,'小时必须完整、有序0—23或1—24');continue
                for i,h in points:
                    for j,date in dates.items():self.add(kind,date,int(h)-offset,self.number(at(a[i],j),ws.title,i+1,j+1),ws.title,i+1,j+1)
        if not found:self.error('',1,1,'未发现年份价格sheet')
    def daily(self,w,member,disclosure):
        name_dates=re.findall(r'\d{4}-\d{2}-\d{2}',member)
        if len(name_dates)!=1:self.error(member,1,1,'成员文件日期无法唯一确定');return
        date=self.date(name_dates[0],member,1,1)
        if not w.sheetnames:
            self.notes.append(member+'：工作簿无sheet，无业务数据');return
        prefix='负荷实际信息' if disclosure else '实际运行结果(正式)-用电侧'
        matches=[s for s in w if s.title.startswith(prefix)]
        if len(matches)!=1:self.error(member,1,1,'未找到唯一目标sheet '+prefix);return
        ws=matches[0];s=member+'/'+ws.title
        if re.findall(r'\d{4}-\d{2}-\d{2}',ws.title)!=name_dates:self.error(s,1,1,'文件日期与sheet日期不一致')
        a=rows(ws)
        if not a:self.notes.append(s+'：空sheet');return
        hdr=[label(v) for v in a[0]];n=96 if disclosure else 24
        timecols=[f'{(k//4 if disclosure else k):02d}:{(k%4*15 if disclosure else 0):02d}' for k in range(n)]
        expected=['类型','通道名称'] if disclosure else ['企业名称','数据项']
        if not all(h in hdr for h in expected+timecols):self.error(s,1,1,'缺少业务列或完整时间表头');return
        cols=[hdr.index(h) for h in timecols];found=set()
        mappings={'日前统一结算价':('结算价对照','da'),'实时统一结算价':('结算价对照','rt'),'日前成交电量':('申报记录',''),'实际用电量':('运行用电量对照','')}
        for i,row in enumerate(a[1:],2):
            metric=label(at(row,hdr.index(expected[1])))
            if disclosure:
                if label(at(row,hdr.index('类型')))!='实际' or metric not in self.c.get('disclosure_rows',['统调负荷(MW)']):continue
                kind,entity='统调负荷',''
            else:
                if metric not in mappings:continue
                kind,entity=mappings[metric]
            if metric in found:self.error(s,i,2,'重复数据项，当前库只支持一个售电组合')
            found.add(metric)
            for k,j in enumerate(cols):
                v=self.number(at(row,j),s,i,j+1)
                if v is not None and not disclosure and '价' not in kind and self.c.get('organization_unit','MWh')=='kWh':v*=.001
                self.add(kind,date,k+1 if disclosure else k,v,s,i,j+1,entity)
        required=set(self.c.get('disclosure_rows',['统调负荷(MW)'])) if disclosure else set(mappings)
        for absent in required-found:self.error(s,1,1,'缺少目标行：'+absent)
    def parse(self,raw):
        if self.name.lower().endswith('.zip'):
            disclosure='披露中心' in self.name;organization='组织中心' in self.name
            if not disclosure and not organization:self.error('',0,0,'无法识别ZIP文件名');return
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                members=[i for i in z.infolist() if i.filename.lower().endswith('.xlsx') and not i.is_dir()]
                if not members:self.error('',0,0,'ZIP无xlsx文件')
                for info in members:
                    member=decode_name(info)
                    expected='披露中心-中调实际信息披露' if disclosure else '组织中心-实际运行结果'
                    if expected not in member:self.error(member,0,0,'ZIP成员文件名不符合类型');continue
                    if info.file_size>100_000_000:self.error(member,0,0,'成员文件过大');continue
                    try:
                        w=load_workbook(io.BytesIO(z.read(info)),read_only=True,data_only=False)
                        try:self.daily(w,member,disclosure)
                        finally:w.close()
                    except Exception as e:self.error(member,0,0,'工作簿读取失败：'+str(e))
        elif self.name.lower().endswith('.xlsx'):
            w=load_workbook(io.BytesIO(raw),read_only=True,data_only=False)
            try:
                if '零售用户电量' in self.name:self.retail(w)
                elif '现货价格' in self.name:
                    self.read_numeric_xml(raw);self.price(w)
                else:self.error('',0,0,'无法识别xlsx文件名，请使用零售用户电量/现货价格')
            finally:w.close()
        else:self.error('',0,0,'无法识别文件类型')
    def read_numeric_xml(self,raw):
        import xml.etree.ElementTree as ET
        import posixpath
        from openpyxl.utils.cell import coordinate_to_tuple
        ns={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            rel=ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
            links={r.attrib['Id']:r.attrib['Target'] for r in rel}
            for sheet in ET.fromstring(z.read('xl/workbook.xml')).find('s:sheets',ns):
                rid=sheet.attrib['{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id']
                target=links[rid];target=target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/'+target)
                for cell in ET.fromstring(z.read(target)).findall('.//s:sheetData/s:row/s:c',ns):
                    value=cell.find('s:v',ns)
                    if cell.attrib.get('t','n')=='n' and value is not None and cell.find('s:f',ns) is None:
                        r,c=coordinate_to_tuple(cell.attrib['r']);self.numeric_cells[(sheet.attrib['name'],r,c)]=value.text
    def result(self):
        coverage={}
        for r in self.records:
            k=r['kind']+('/'+r['entity'] if r['kind']=='结算价对照' else '')
            v=coverage.setdefault(k,{'points':0,'dates':set(),'first':r['date'],'last':r['date']})
            v['points']+=1;v['dates'].add(r['date']);v['first']=min(v['first'],r['date']);v['last']=max(v['last'],r['date'])
        for v in coverage.values():v['days']=len(v.pop('dates'))
        return dict(file=self.name,errors=self.errors,notes=self.notes,missing=dict(self.missing),coverage=coverage,records=self.records)

def clean(name,raw,config,today=None):
    c=Cleaner(name,config,today)
    try:c.parse(raw)
    except Exception as e:c.error('',0,0,'文件读取失败：'+str(e))
    return c.result()
