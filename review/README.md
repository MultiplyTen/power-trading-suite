# 复盘调参模块

本版配置顶层db_path指向统一的../database/master.db。通过根目录main.py运行，或在本目录运行python main.py。

实际负荷和价格从四张事实表读取，发布结果直接写入runs/predictions；复盘直接读库，不再解析模型输出Excel。天气和其他维表沿用库内版本化结构表。

算法参数和输出报表沿用原功能，运行方法见../README.md，数据库维护见../database/数据库维护说明.md。历史独立版接口资料移入历史算法说明.md，不作为本版的数据接入操作指南。
