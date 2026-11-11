# 原始文件数据接入

日常用户先读根目录 `先看这里_三步操作.md`。

四类原文件放 `data/inbox`，运行 `python main.py`；成功移入 `data/processed`，失败留在原地。自动识别文件名和内部结构。调用程序始终与其他三模块共享 `../database/master.db`，不会新建另一套业务库。

```
python main.py                           # 扫描原始文件
python main.py template                  # 手填4sheet模板
python main.py export --from 2026-09-01 --to 2026-09-17
python main.py init --confirm-empty      # 仅真正新项目；已有库禁止覆盖
```

首次缺目录/模板会自动创建；缺数据库时明确报错，优先恢复备份。init创建空库不代表具备预测所需历史数据。

`raw_sources.py`仅解析校验，输出标准记录与单元格证据；`database/raw_store.py`每源文件一个事务，同时写事实、修订、原始文件BLOB与复盘实际值。ZIP整体是一个源文件，一个成员有业务错误则整ZIP拒绝；空工作簿按缺数据报告。ZIP只读取内存字节，不向磁盘解压任意成员路径。

非空数据按主键upsert；同值不重写接收时间。企业编码或价格类型是附加主键，不以名称代替。日期合法/未来值、重复主键、完整表头、合计、数值范围同时校验，错误列出文件/sheet/行/列。不对缺失值补零、插值或生成预测。

`config.yaml`可调整price_range、load_cross_check_tolerance、system_load_range、retail_unit、organization_unit等；具体值与本次源数据匹配。当前统调表仅支持一个通道，白名单固定为统调负荷(MW)；扩展其他通道时须同步增加通道主键与模型适配，不能只加白名单造成主键碰撞。

日志：总日志`data/import_log.csv`，原始文件详细摘要`data/原始文件_import_log.csv`；每次完整报告`output/接入报告_*.json`。`output/数据齐备检查.md`展示库内覆盖、客户映射、天气时效。错误CSV一次列出整文件全部错误；用电量偏差另列警告CSV，绝不替换主源。

自动备份在`database/backups/`；每次原始文件提交前保存SQLite一致备份，维护人员可按保留策略归档。不要在任务运行时直接删除或替换数据库。

导出的Excel含9张接入业务事实表（含额外运行用电量对照）、手填独立快照（存在时）、负荷预测记录与系数决策记录。四类小时事实为宽表，其余长表，保留来源和接收时间；统调表hour字段实际上为1—96时段编号，单位MW。原文件BLOB与天气等版本化档案只在master.db内，完整迁移须复制数据库。
