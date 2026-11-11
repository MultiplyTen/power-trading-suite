# -*- coding: utf-8 -*-
"""One-folder daily import and four-module generation workflow."""

import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DAILY_INBOX = ROOT / "daily_inbox"
DAILY_PROCESSED = ROOT / "daily_processed"
LEGACY_INBOX = ROOT / "data_ingest" / "data" / "inbox"
DISCLOSURE_INBOX = ROOT / "inputs" / "disclosure"


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def copy_if_needed(source, destination_dir):
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / source.name
    if destination.exists():
        if sha256(destination) == sha256(source):
            return {"file": source.name, "destination": str(destination), "status": "already_present"}
        raise ValueError("目标目录存在同名但内容不同的文件：" + str(destination))
    shutil.copy2(source, destination)
    return {"file": source.name, "destination": str(destination), "status": "copied"}


def classify(path):
    name = path.name
    if name.startswith("~$") or name.startswith("."):
        return "ignore"
    if "零售用户电量" in name and path.suffix.lower() == ".xlsx":
        return "legacy"
    if "现货价格" in name and path.suffix.lower() == ".xlsx":
        return "legacy"
    if "组织中心" in name and path.suffix.lower() == ".zip":
        return "legacy"
    if "披露中心" in name and "实际" in name and path.suffix.lower() == ".zip":
        return "both"
    if ("预测信息" in name or "预测披露" in name) and path.suffix.lower() == ".xlsx":
        return "disclosure"
    raise ValueError("无法识别日常文件类型，请检查文件名：" + name)


def dispatch(files):
    actions = []
    for path in files:
        kind = classify(path)
        if kind == "ignore":
            continue
        if kind in {"legacy", "both"}:
            actions.append(copy_if_needed(path, LEGACY_INBOX))
        if kind in {"disclosure", "both"}:
            actions.append(copy_if_needed(path, DISCLOSURE_INBOX))
    return actions


def run_all():
    environment = {
        **dict(__import__("os").environ),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    }
    return subprocess.run(
        [sys.executable, str(ROOT / "main.py"), "all"],
        cwd=ROOT,
        env=environment,
    )


def main():
    DAILY_INBOX.mkdir(parents=True, exist_ok=True)
    files = sorted(
        path for path in DAILY_INBOX.iterdir()
        if path.is_file() and not path.name.startswith((".", "~$"))
    )
    if not files:
        print("daily_inbox 为空：请把零售电量、现货价格、组织中心结果、中调实际披露和次日预测披露放入该目录。")
        return 0
    actions = dispatch(files)
    result = run_all()
    if result.returncode:
        print("日常流程失败：源文件保留在 daily_inbox，已复制文件保留在模块inbox，可修正后重跑。")
        return result.returncode

    archive_dir = DAILY_PROCESSED / datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_dir.mkdir(parents=True, exist_ok=True)
    for path in files:
        if classify(path) != "ignore":
            shutil.move(str(path), str(archive_dir / path.name))
    summary = {
        "status": "completed",
        "archived_to": str(archive_dir),
        "dispatch": actions,
    }
    (archive_dir / "daily_import.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
