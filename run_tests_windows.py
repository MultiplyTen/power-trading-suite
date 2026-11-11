# -*- coding: utf-8 -*-
"""Windows 测试入口：python run_tests_windows.py

交付的测试在 macOS/Linux 上可直接用 unittest discover 运行。Windows 下有两点差异：
1. 测试用 tempfile.TemporaryDirectory 建临时库，但 SQLite 连接未显式关闭，
   cleanup() 删不掉仍被占用的文件（POSIX 允许，Windows 报 WinError 32）。
   这里只忽略清理错误，不影响任何断言。
2. coefficient_opt/tests 与 review/tests 没有 __init__.py，
   discover 的 top_level_dir 必须指向 tests 目录本身。
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYTHON = sys.executable

# suite -> (cwd, start_dir, top_level_dir)，均相对 ROOT
SUITES = [
    ("data_ingest", "data_ingest", "data_ingest", "data_ingest"),
    ("database", "database", "database", "database"),
    ("load_forecast", "load_forecast", "load_forecast/tests", "load_forecast"),
    ("coefficient", "coefficient_opt", "coefficient_opt/tests", "coefficient_opt/tests"),
    ("review", "review", "review/tests", "review/tests"),
]


def patch_tempfile() -> None:
    original = tempfile.TemporaryDirectory

    if sys.version_info >= (3, 10):
        class TolerantTemporaryDirectory(original):  # type: ignore[misc,valid-type]
            def __init__(self, *args, **kwargs):
                kwargs.setdefault("ignore_cleanup_errors", True)
                super().__init__(*args, **kwargs)
    else:
        # Python 3.9 无 ignore_cleanup_errors（3.10+ 才有）；
        # 等效做法：清理阶段（__exit__）吞掉 OSError（Windows WinError 32 文件占用）。
        class TolerantTemporaryDirectory(original):  # type: ignore[misc,valid-type]
            def __exit__(self, exc_type, exc_val, exc_tb):
                try:
                    return super().__exit__(exc_type, exc_val, exc_tb)
                except OSError:
                    return False

    tempfile.TemporaryDirectory = TolerantTemporaryDirectory


def run_child(name: str, cwd_rel: str, start_rel: str, top_rel: str) -> int:
    os.chdir(ROOT / cwd_rel)
    for entry in (str(ROOT / start_rel), str(ROOT / cwd_rel), str(ROOT)):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    patch_tempfile()
    suite = unittest.TestLoader().discover(
        start_dir=str(ROOT / start_rel), pattern="test_*.py", top_level_dir=str(ROOT / top_rel)
    )
    result = unittest.TextTestRunner(verbosity=1, stream=sys.stdout).run(suite)
    print(f"[{name}] ran={result.testsRun} failures={len(result.failures)} "
          f"errors={len(result.errors)} skipped={len(result.skipped)}")
    return 0 if result.wasSuccessful() else 1


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        return run_child(*sys.argv[2:6])
    total = failed = 0
    for name, cwd_rel, start_rel, top_rel in SUITES:
        # 每套测试独立进程，避免模块间 sys.path 与同名模块互相污染。
        proc = subprocess.run(
            [PYTHON, str(Path(__file__).resolve()), "--child", name, cwd_rel, start_rel, top_rel],
            cwd=str(ROOT),
        )
        total += 1
        failed += 1 if proc.returncode else 0
    print(f"合计 {total} 套，失败 {failed} 套")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
