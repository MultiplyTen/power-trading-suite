# -*- coding: utf-8 -*-
"""填表后直接运行；双击启动器负责等待回车，流水线永不等待键盘。"""
import argparse,csv,json,math,sys,datetime,logging,sqlite3,io
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import yaml
from database.store import suite_lock,SCHEMA
from database.trading import open_db,create_tables
from data_ingest.validation import validate,SHEETS,parse_date
from data_ingest.importer import commit
from data_ingest.exporter import write_export
ROOT=Path(__file__).resolve().parent

def config(root=ROOT):
    c=yaml.safe_load((root/'config.yaml').read_text(encoding='utf-8'))
    if c['load_unit'] not in ['MWh','kWh']:raise ValueError('load_unit只能是MWh或kWh；程序会把kWh换算为库内MWh')
    for key in ['retail_unit','organization_unit']:
        if c.get(key,'MWh') not in ['MWh','kWh']:raise ValueError(key+'只能是MWh或kWh')
    if c.get('disclosure_rows',['统调负荷(MW)'])!=['统调负荷(MW)']:raise ValueError('新增披露通道前须扩展数据库通道主键；当前只支持统调负荷(MW)')
    p=c['price_range']
    if len(p)!=2 or not all(isinstance(x,(int,float)) and math.isfinite(x) for x in p) or p[0]>p[1]:raise ValueError('price_range须为两个有限数字，下限不大于上限')
    if c.get('timezone')!='Asia/Shanghai' or c.get('price_96_aggregation')!='arithmetic_mean_proxy' or c.get('partial_quarter_hour')!='reject':raise ValueError('当前接入契约要求北京时间、96点价格四点均值、不完整小时拒绝')
    for k in ['template_path','db_path','log_path','export_dir','inbox_dir','processed_dir']:c[k]=(root/c[k]).resolve()
    return c

def write_log(c,status,stats,message):
    p=c['log_path'];p.parent.mkdir(parents=True,exist_ok=True);new=not p.exists()
    with p.open('a',newline='',encoding='utf-8-sig') as f:
        writer=csv.writer(f)
        if new:writer.writerow(['时间','sheet','新增点','覆盖点','同值点','空置格','结果','说明'])
        for s in SHEETS:
            v=stats.get(s,{})
            writer.writerow([datetime.datetime.now().isoformat(),s,v.get('new',0),v.get('updated',0),v.get('unchanged',0),v.get('blank_cells',0),status,message])

def import_template(c):
    # 先检查数据库，避免以“创建模板”为由静默新建空库。
    with open_db(c['db_path']):pass
    if not c['template_path'].exists():
        write_export(c['db_path'],c['template_path'],c['load_unit'],example=True)
        print('【模板已生成】'+str(c['template_path'])+'\n示例实际值来自库内已有记录；预测为空。请检查填写后重新运行。本次未导入。')
        return {'status':'template_created'}
    today=datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).date()
    try:
        raw=c['template_path'].read_bytes()
        errors,changes,stats=validate(io.BytesIO(raw),c,today)
    except Exception as e:
        write_log(c,'失败',{},'模板读取失败：'+str(e));raise ValueError('模板无法读取，请检查文件完整性和.xlsx格式：'+str(e)) from e
    if errors:
        lines=[f'【导入失败】共发现{len(errors)}处错误，本次未导入任何数据：']
        lines += [f'{i}. sheet“{e["sheet"]}”第{e["row"]}行 第{e["column"]}列：{e["message"]}' for i,e in enumerate(errors,1)]
        message='\n'.join(lines);write_log(c,'失败',stats,message);print(message);logging.error(message)
        return {'status':'rejected','errors':errors}
    try:result=commit(c['db_path'],c['template_path'],changes,stats,raw=raw)
    except Exception as e:
        write_log(c,'失败',{},'事务回滚，未写入任何业务数据：'+str(e));raise ValueError('导入事务失败，已全部回滚：'+str(e)) from e
    write_log(c,'成功',result['sheets'],result['batch_id'])
    print('【导入完成】')
    for s,v in result['sheets'].items():
        print(f'{s}：新增{len(v["new_days"])}天{v["new"]}点，覆盖{len(v["updated_days"])}天{v["updated"]}点，同值不改{v["unchanged"]}点，空置跳过{v["blank_cells"]}格')
    result['status']='completed';return result

def initialize_empty(c,confirmed):
    p=c['db_path']
    if p.exists():raise ValueError('库文件已存在，禁止覆盖或清空；损坏时请先保留原文件并恢复备份。')
    if not confirmed:raise ValueError('数据库缺失。优先恢复备份；确需新建空库，请明确执行 init --confirm-empty。')
    p.parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(p) as db:
        db.executescript(SCHEMA)
        db.executescript('CREATE TABLE runs(id TEXT PRIMARY KEY,kind TEXT,published_at TEXT,model_version TEXT,config_json TEXT,source TEXT,sha TEXT,status TEXT);CREATE TABLE predictions(run_id TEXT,target TEXT,load REAL,da REAL,rt REAL,k REAL,research_k REAL,PRIMARY KEY(run_id,target));CREATE TABLE actuals(target TEXT,metric TEXT,value REAL,available_at TEXT,evidence TEXT,source_sha TEXT,ingested_at TEXT,PRIMARY KEY(target,metric,source_sha));CREATE TABLE audit_events(time TEXT,action TEXT,detail TEXT);')
        create_tables(db)
    print('已按明确指令创建空库；天气、客户明细、映射和日历仍需迁移，空库不能直接完成预测。')

def main(argv=None):
    p=argparse.ArgumentParser(description='四类原始文件自动清洗入库；template为手填兜底')
    p.add_argument('command',nargs='?',default='import',choices=['import','template','export','init','weather'])
    p.add_argument('--force',action='store_true');p.add_argument('--from',dest='start');p.add_argument('--to',dest='end');p.add_argument('--pause',action='store_true');p.add_argument('--confirm-empty',action='store_true')
    args=p.parse_args(argv);c=config();c['export_dir'].mkdir(parents=True,exist_ok=True)
    logging.basicConfig(filename=c['export_dir']/'run.log',encoding='utf-8',level=logging.INFO,format='%(asctime)s %(message)s')
    with suite_lock():
        if args.command=='export':
            start=str(parse_date(args.start)) if args.start else None;end=str(parse_date(args.end)) if args.end else None
            if start and end and start>end:raise ValueError('导出开始日期不能晚于结束日期')
            path=c['export_dir']/('master_backup_'+datetime.date.today().strftime('%Y%m%d')+datetime.datetime.now().strftime('_%H%M%S_%f')+'.xlsx')
            print('【导出完成】'+write_export(c['db_path'],path,c['load_unit'],start,end));return 0
        if args.command=='init':initialize_empty(c,args.confirm_empty);return 0
        for key in ['inbox_dir','processed_dir']:c[key].mkdir(parents=True,exist_ok=True)
        if not c['template_path'].exists():
            # 主入口不预填旧实际值，防止误用模板覆盖新原始文件。
            from data_ingest.exporter import workbook
            w=workbook();w.save(c['template_path']);w.close()
        if args.command=='weather':result={'status':'completed','files':[]}
        elif args.command=='template':result=import_template(c)
        else:
            from data_ingest.inbox import run
            result=run(c)
        if args.command in ['import','weather']:
            from database.weather import refresh
            try:weather=refresh(c['db_path'],c,force=args.force)
            except Exception as exc:weather={'status':'failed','error':str(exc),'message':'天气更新失败，已导入的电力数据不受影响'}
            result['weather']=weather
            print('【天气更新】'+weather.get('message','')+'；状态='+weather['status'])
            if weather.get('error'):print('  原因：'+weather['error'])
            (c['export_dir']/'天气更新结果.json').write_text(json.dumps(weather,ensure_ascii=False,indent=2),encoding='utf-8')
            log=c['log_path'].with_name('weather_import_log.csv');new=not log.exists()
            with log.open('a',encoding='utf-8-sig',newline='') as f:
                writer=csv.writer(f)
                if new:writer.writerow(['时间','状态','城市数','小时点数','接收时间','错误'])
                writer.writerow([datetime.datetime.now().isoformat(),weather['status'],weather.get('cities'),weather.get('rows'),weather.get('received_at'),weather.get('error','')])
            if weather['status']=='failed':result['status']='rejected'
        from data_ingest.readiness import write
        write(c)
        (c['export_dir']/'last_import.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
        return 2 if result['status']=='rejected' else 0

if __name__=='__main__':
    code=1
    try:code=main()
    except Exception as e:print('【操作失败】'+str(e)+'\n数据库缺失/损坏时，请停止操作，保留故障文件并从database/backups恢复，禁止直接清空。');logging.exception('操作失败')
    finally:
        if '--pause' in sys.argv:
            try:input('按回车退出……')
            except EOFError:pass
    sys.exit(code)
