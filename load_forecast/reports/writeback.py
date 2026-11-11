import hashlib,json,os,shutil,tempfile,zipfile
from pathlib import Path
from openpyxl import load_workbook
from reports.excel import clean,write_book

def append_record(path,frame,run_id,output_dir):
    path=Path(path);output_dir=Path(output_dir);record=frame.reset_index(names='valid_time');record.insert(0,'run_id',run_id)
    fallback=output_dir/('独立预测记录_'+run_id+'.xlsx')
    try:
        if path.suffix.lower()!='.xlsx':raise ValueError('宏或非xlsx格式不直接写回')
        with zipfile.ZipFile(path) as z:
            unsupported=[n for n in z.namelist() if any(k in n for k in ['vbaProject','xl/drawings/','xl/charts/','xl/externalLinks/','xl/connections','xl/pivot','xl/activeX','xl/embeddings','xl/slicers'])]
            if unsupported:raise ValueError('含复杂图表、宏或连接，需原生Excel保真处理')
        wb=load_workbook(path);original={s.title:list(s.values) for s in wb if s.title!='预测记录'}
        if '预测记录' in wb:
            ws=wb['预测记录']
            if list(next(ws.values))!=list(record.columns):raise ValueError('已有预测记录字段与当前版本不同')
            if run_id in [r[0] for r in list(ws.values)[1:]]:return {'status':'idempotent','run_id':run_id}
        else:ws=wb.create_sheet('预测记录');ws.append(list(record.columns))
        for row in record.itertuples(index=False,name=None):ws.append([clean(v) for v in row])
        digest=hashlib.sha256(path.read_bytes()).hexdigest()[:12];backup=path.with_name(path.stem+'.backup_'+digest+'.xlsx')
        if not backup.exists():shutil.copy2(path,backup)
        fd,temp=tempfile.mkstemp(suffix='.xlsx',dir=path.parent);os.close(fd)
        try:
            wb.save(temp);check=load_workbook(temp)
            if any(list(check[n].values)!=v for n,v in original.items()):raise ValueError('写回改变原始数据，已取消')
            check.close();wb.close();os.replace(temp,path)
        finally:
            if Path(temp).exists():Path(temp).unlink()
        return {'status':'appended','backup':backup.name,'run_id':run_id}
    except (OSError,ValueError,zipfile.BadZipFile) as exc:
        write_book(fallback,{'预测记录':record});return {'status':'separate_file','reason':str(exc),'file':fallback.name}
