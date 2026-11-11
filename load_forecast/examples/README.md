# 示例隔离

真实业务数据在 `data/`，不与模拟数据混合。`tests/test_contracts.py` 在临时目录生成合成24点/96点、零值、负价格和手算策略场景，并在测试后清理。

要演示真实历史数据，使用 `config.historical.yaml`。要填入新数据，使用 `data/market_input_template.xlsx`。
