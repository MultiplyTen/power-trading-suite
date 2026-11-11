#!/bin/bash
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo '首次运行：正在建立Python环境并安装依赖……'
  python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
  if [ $? -ne 0 ]; then echo '初始化失败：请安装Python 3.9或以上，检查网络后重试。'; read -r -p '按回车退出……'; exit 1; fi
fi
.venv/bin/python main.py export
result=$?
read -r -p '按回车退出……'
exit "$result"
