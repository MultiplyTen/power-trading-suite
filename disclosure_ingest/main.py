# -*- coding: utf-8 -*-
"""Idempotent M1 disclosure ingestion entry point."""

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from database.store import suite_lock
from disclosure_ingest.parser import ingest_file, now_string, sha256_bytes
from disclosure_ingest.schema import create_tables


def read_config(root=ROOT):
    path = root / "disclosure_ingest" / "config.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["db_path"] = (root / "disclosure_ingest" / config["db_path"]).resolve()
    config["inputs_dir"] = (root / "disclosure_ingest" / config["inputs_dir"]).resolve()
    config["output_dir"] = (root / "disclosure_ingest" / config["output_dir"]).resolve()
    config["config_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return config


def run(inputs_dir=None, db_path=None, rebuild=False):
    config = read_config()
    inputs_dir = Path(inputs_dir or config["inputs_dir"]).resolve()
    db_path = Path(db_path or config["db_path"]).resolve()
    output_dir = config["output_dir"]
    output_dir.mkdir(parents=True, exist_ok=True)
    inputs = sorted(
        path for path in inputs_dir.iterdir()
        if path.is_file() and path.suffix.lower() in {".zip", ".xlsx"}
        and not path.name.startswith((".", "~$"))
    )
    if not inputs:
        raise ValueError("披露目录中没有可导入的ZIP/XLSX文件")

    identity = "\n".join(f"{path.name}:{sha256_bytes(path.read_bytes())}" for path in inputs)
    snapshot_id = "disclosure_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    ingested_at = now_string()
    results = []
    with sqlite3.connect(db_path) as connection:
        if rebuild:
            for table in [
                "disclosure_points", "disclosure_hourly", "disclosure_events",
                "disclosure_sheet_audit", "disclosure_files", "disclosure_snapshots",
            ]:
                connection.execute(f"DROP TABLE IF EXISTS {table}")
        create_tables(connection)
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """INSERT OR IGNORE INTO disclosure_snapshots VALUES(?,?,?,?,?,?,?,?)""",
                (
                    snapshot_id, ingested_at, "running",
                    json.dumps(config, ensure_ascii=False, sort_keys=True, default=str),
                    len(inputs), 0, 0, "[]",
                ),
            )
            snapshot_created_at = connection.execute(
                "SELECT created_at FROM disclosure_snapshots WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()[0]
            for path in inputs:
                result = ingest_file(
                    connection, path, snapshot_id=snapshot_id, ingested_at=ingested_at
                )
                results.append(result)
            workbook_count = connection.execute(
                "SELECT count(*) FROM disclosure_files WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()[0]
            new_count = sum(item["new_workbooks"] for item in results)
            connection.execute(
                """UPDATE disclosure_snapshots
                   SET status=?, workbook_count=?,
                       new_workbook_count=CASE WHEN ?>new_workbook_count THEN ? ELSE new_workbook_count END,
                       notes_json=?
                   WHERE snapshot_id=?""",
                (
                    "completed", workbook_count, new_count, new_count,
                    json.dumps(results, ensure_ascii=False), snapshot_id,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    summary = {
        "status": "completed",
        "snapshot_id": snapshot_id,
        "created_at": snapshot_created_at,
        "file_count": len(inputs),
        "workbook_count": workbook_count,
        "new_workbook_count": new_count,
        "files": results,
    }
    path = output_dir / f"disclosure_ingest_{snapshot_id}.json"
    path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description="导入中调预测/实际披露ZIP")
    parser.add_argument("--inputs", help="披露ZIP目录；默认读取config.yaml")
    parser.add_argument("--db", help="SQLite数据库路径；默认读取config.yaml")
    parser.add_argument("--rebuild", action="store_true", help="删除并重建本包新增的披露表；不影响原有业务表")
    args = parser.parse_args(argv)
    result = run(args.inputs, args.db, args.rebuild)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        with suite_lock():
            code = main()
    except Exception as exc:
        print("披露导入失败：" + str(exc), file=sys.stderr)
        code = 1
    sys.exit(code)
