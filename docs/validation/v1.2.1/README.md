# v1.2.1离线验收记录

日期：2026-11-12。基线：`69cf1547f062a0edbe02ee329217f4504e3c4778`。范围为源码、合成数据与离线报告；没有读取或发布真实业务数据库，也没有部署或执行申报。

## 回归测试

`python run_tests_windows.py`运行六套：**162项，159通过、3跳过，0失败/错误**。

| 测试组 | 项数 | 通过 | 跳过 |
|---|---:|---:|---:|
| data_ingest | 49 | 49 | 0 |
| database | 29 | 29 | 0 |
| load_forecast | 34 | 34 | 0 |
| coefficient | 18 | 17 | 1 |
| review | 30 | 30 | 0 |
| disclosure | 2 | 0 | 2 |

跳过原因：系数测试的本地数据库/价格工作簿样例缺失；两项披露解析测试依赖未随公开版发布的原始实际/预测ZIP。本次先尝试披露测试，确认缺文件后改为明确skip，保留原测试断言；并将该组纳入统一入口。没有用合成ZIP冒充原始资料通过。

重点覆盖：六类软借常数保持、增强日历及未来负荷不泄漏、空输入拦截、披露接收截点和覆盖状态、固定模型标签、原始预测唯一完整发布/迟发/回放/实际版本/同版本30日门槛、原始误差到网格与精确求解联动、误差诊断、CI正负方向、两段幅度+CI、风险固定基准约束重评分及apply重验。

风险反例已验证：基准5/90场景超限、置信度0.9；候选13/90超限、置信度放宽到0.85时自身仍可行。新门槛按原0.9重评并阻断，候选即使利润改善也不能通过。另覆盖基准SHA变更、场景/约束版本不符及完整worker→CSV→guard链路。

实际环境为Python 3.12.14、numpy 2.5.1、pandas 2.3.3、scipy 1.18.0、scikit-learn 1.9.0。具体信息见[test-summary.json](test-summary.json)。这不是根requirements锁定栈的重复安装验收。新增/修改Python源码34个文件通过Python3.9语法解析；语法检查不代表3.9运行时验收。测试命令局部屏蔽了现有numpy/pandas组合的DeprecationWarning；未屏蔽测试失败。

## 实际跑过的合成示例

M2：固定seed=42、相同744个测试小时、6日观测滞后和Ridge alpha=1000，真实调用make_features及逐日起点walk。basic为34列、enhanced为60列，WMAPE分别3.9267%和3.6778%。该固定合成周内/天气信号只验证程序和候选对照能运行，不能推广成真实业务提升；没有为了结果重调数据参数。见[M2结果](m2-synthetic-comparison.json)。

```bash
python examples/m2_calendar_comparison.py --output /absolute/path/to/new-demo-output
```

M4：336个唯一小时、14完整日、两个合成模型版本，偏差实际−预测为+2MWh，WMAPE=16.67%；故意放入迟发及replay，确认没有混入。输出JSON、5份CSV，以及日/周/月HTML和XLSX。3个工作簿ZIP结构有效、无错误单元格；18个报告内本地链接均存在。见[M4冒烟摘要](m4-synthetic-smoke.json)。没有运行模型、交易或apply；此说明仅指M4冒烟，M2示例确实拟合了Ridge。

```bash
python review/tests/smoke_load_risk.py --output /absolute/path/to/new-empty-smoke-directory
```

输出目录需显式指定；M4示例要求目录尚不存在。测试及示例可丢弃产物在受管临时目录中，公开验收只保留无业务数据的摘要。

## 证据与边界

- [六组结果](suite-results.txt)、[机器可读汇总](test-summary.json)、[变更Python源码SHA256](source-manifest.json)。
- 独立复核发现并修复披露覆盖状态与特征选择不一致，以及候选自身放宽约束的风险口径问题；复核后没有新增阻断项。
- M3的30个同版本原始日是最终场景门槛；现阶段仍另外要求旧90日proxy对照前置完成，原始路径尚不能脱离该前置独立运行。
- 真实M2精度、真实收益、正式结算、获批资金预算、完整历史可得时点和LP/MILP全部数学边界没有在本次验收中确认。
