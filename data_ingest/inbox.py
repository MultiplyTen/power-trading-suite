"""无需改模板的主入口：逐文件处理，失败文件留在inbox，成功归档。"""
import csv,datetime,json,shutil
from pathlib import Path
from database.trading import open_db
from database.raw_store import ensure,commit_raw,cross_check
from data_ingest.raw_sources import clean

def run(c):
    for key in ['inbox_dir','processed_dir','export_dir']:c[key].mkdir(parents=True,exist_ok=True)
    with open_db(c['db_path']) as db:ensure(db)
    files=sorted(p for p in c['inbox_dir'].iterdir() if p.is_file() and not p.name.startswith(('.', '~$')))
    results=[]
    for p in files:
        try:raw=p.read_bytes();result=clean(p.name,raw,c)
        except OSError as e:
            raw=b'';result=dict(file=p.name,errors=[dict(file=p.name,sheet='',row=0,column=0,message='文件读取失败：'+str(e))],notes=[],missing={},coverage={},records=[])
        if not result['errors']:
            try:
                result.update(commit_raw(c['db_path'],p.name,raw,result));result['status']='completed'
                archived=c['processed_dir']/(p.stem+'_'+datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')+p.suffix)
                # 不归档校验后被外部修改的文件；下一次会重新处理。
                if p.read_bytes()!=raw:result['notes'].append('源文件处理期间变化，保留inbox供下次导入')
                else:
                    try:shutil.move(str(p),str(archived));result['archived']=str(archived.relative_to(c['processed_dir'].parent.parent))
                    except OSError as e:result['notes'].append('已入库但归档失败，重试不会重复累加：'+str(e))
            except Exception as e:result['errors'].append(dict(file=p.name,sheet='',row=0,column=0,message='事务失败，已回滚：'+str(e)))
        result.pop('records',None)
        if result['errors']:result['status']='rejected'
        results.append(result)
        print('【'+('导入完成' if result['status']=='completed' else '文件拒绝')+'】'+p.name)
        for k,v in result['coverage'].items():print(f'  {k}：{v["days"]}天 {v["points"]}点，{v["first"]}～{v["last"]}')
        for e in result['errors'][:8]:print(f'  {e["sheet"]} 第{e["row"]}行 第{e["column"]}列：{e["message"]}')
    warnings=cross_check(c['db_path'],c.get('load_cross_check_tolerance',.01))
    if warnings:print(f'【交叉核对】{len(warnings)}小时偏差超过阈值；已保留两种口径，未替换零售主源。')
    if not files:print('inbox没有待处理文件。请放入原始xlsx/zip后重新运行；手填模板须单独运行“导入手填模板”。')
    errors=[e for r in results for e in r['errors']]
    stamp=datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    output=dict(status='rejected' if errors else 'completed',files=results,cross_check_warnings=warnings,as_of=str(datetime.datetime.now().date()))
    (c['export_dir']/('接入报告_'+stamp+'.json')).write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
    for filename,items in [('错误清单_'+stamp+'.csv',errors),('用电量交叉核对_'+stamp+'.csv',warnings)]:
        if items:
            with (c['export_dir']/filename).open('w',encoding='utf-8-sig',newline='') as f:
                writer=csv.DictWriter(f,fieldnames=list(items[0]));writer.writeheader();writer.writerows(items)
    c['log_path'].parent.mkdir(parents=True,exist_ok=True)
    # 独立格式的新日志避免破坏旧模板日志。
    log=c['log_path'].with_name('原始文件_import_log.csv');new=not log.exists()
    with log.open('a',encoding='utf-8-sig',newline='') as f:
        writer=csv.writer(f)
        if new:writer.writerow(['时间','文件','结果','覆盖范围','新增覆盖统计','错误数','缺失数'])
        for r in results:writer.writerow([stamp,r['file'],r['status'],json.dumps(r['coverage'],ensure_ascii=False),json.dumps(r.get('changes',{}),ensure_ascii=False),len(r['errors']),json.dumps(r['missing'],ensure_ascii=False)])
    # 与手填入口共用8列总日志；原始文件详细摘要另存上表。
    new=not c['log_path'].exists()
    with c['log_path'].open('a',encoding='utf-8-sig',newline='') as f:
        writer=csv.writer(f)
        if new:writer.writerow(['时间','文件/sheet','新增点','覆盖点','同值点','空置格','结果','说明'])
        for r in results:
            counts=r.get('changes',{}).values()
            writer.writerow([stamp,r['file'],sum(x['new'] for x in counts),sum(x['updated'] for x in counts),sum(x['unchanged'] for x in counts),sum(r['missing'].values()),r['status'],json.dumps(r['coverage'],ensure_ascii=False)])
    return output
