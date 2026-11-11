"""更新后给操作人一份可读的数据齐备检查；不把导入成功等同预测可用。"""
import datetime,json
from database.trading import open_db
from database.raw_store import EXTRA

def write(c):
    report={'checked_at':datetime.datetime.now().isoformat(),'tables':{}}
    with open_db(c['db_path']) as db:
        for name in ['负荷','日前价','实时价','负荷预测']+list(EXTRA):
            if not db.execute('SELECT 1 FROM sqlite_master WHERE name=?',(name,)).fetchone():continue
            where=' WHERE value IS NOT NULL'+(' AND value<>0' if name in ['日前价','实时价'] else '')
            v=db.execute('SELECT count(*),min(date),max(date) FROM "'+name+'"'+where).fetchone()
            report['tables'][name]={'points':v[0],'first':v[1],'last':v[2]}
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='weather_batches'").fetchone():
            v=db.execute('SELECT received_at,start,end,rows FROM weather_batches ORDER BY received_at DESC LIMIT 1').fetchone()
            if v:report['automatic_weather']={'received_at':v[0],'first':v[1],'last':v[2],'rows':v[3]}
        for name in ['load_weather_history','load_weather_live']:
            try:
                v=db.execute('SELECT min(valid_time),max(valid_time) FROM current_'+name).fetchone()
                report[name]={'first':v[0],'last':v[1]}
            except Exception:report[name]={'status':'缺少天气表'}
        try:
            report['unmapped_customers']=[r[0] for r in db.execute('SELECT DISTINCT entity FROM 用户明细 EXCEPT SELECT entity_id FROM current_load_customer_mapping')]
        except Exception:report['unmapped_customers']=['客户地区映射表缺失']
    report['notes']=['四类文件提供电力业务实际值及已成交计划，不含天气预报。一键导入现在会自动抓取并入库未来天气；历史天气由原版本化接口维护。','历史气象可用时间未经原始发布认证，不能据此声称严格实盘回测。','本包模型目标日保持历史示例2026-09-02；数据导入不会自动改日期、运行预测或apply。','原始零售和组织文件未标注单位，当前按既有数据库MWh约定；客户与组织实际电量可做交叉核验。','2025价格1—24按结束时段转0—23，负价保留，价格0按业务要求视作缺失。','预测需要目标日天气、足够历史、客户地区映射和完整24小时输出；请先核对配置与气象齐备。']
    (c['export_dir']/'数据齐备检查.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=['# 数据齐备检查','', '更新时间：'+report['checked_at'],'','| 数据 | 点数 | 首日 | 末日 |','|---|---:|---|---|']
    for k,v in report['tables'].items():lines.append(f'| {k} | {v["points"]} | {v["first"]} | {v["last"]} |')
    lines+=['','自动天气版本：'+str(report.get('automatic_weather',{})),'','未匹配地区的客户：'+str(report['unmapped_customers']),'','历史天气：'+str(report['load_weather_history']),'','存档预报：'+str(report['load_weather_live']),'']+['- '+s for s in report['notes']]
    (c['export_dir']/'数据齐备检查.md').write_text('\n'.join(lines),encoding='utf-8')
    return report
