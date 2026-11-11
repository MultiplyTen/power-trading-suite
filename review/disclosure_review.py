# -*- coding: utf-8 -*-
"""M4 disclosure coverage and forecast-versus-actual review."""

import json
import sqlite3
from pathlib import Path

import pandas as pd


def _table_exists(connection, name):
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def run(config, output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    db_path = Path(config["db_path"])
    if not db_path.is_absolute():
        db_path = Path(config["_root"]) / db_path
    with sqlite3.connect(db_path) as connection:
        required = {"disclosure_snapshots", "disclosure_files", "disclosure_sheet_audit", "disclosure_hourly"}
        if not required <= {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}:
            result = {"status": "unavailable", "message": "披露数据尚未导入，无法生成披露复盘"}
            (output_dir / "disclosure_review.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return result

        snapshot = connection.execute(
            """SELECT snapshot_id,created_at,status,zip_count,workbook_count,new_workbook_count
               FROM disclosure_snapshots ORDER BY created_at DESC LIMIT 1"""
        ).fetchone()
        files = pd.read_sql_query(
            """SELECT record_kind,count(*) AS workbooks,count(DISTINCT target_date) AS days,
                      min(target_date) AS first_date,max(target_date) AS last_date
               FROM disclosure_files GROUP BY record_kind""",
            connection,
        )
        sheets = pd.read_sql_query(
            """SELECT record_kind,status,count(*) AS sheets
               FROM disclosure_sheet_audit GROUP BY record_kind,status""",
            connection,
        )
        paired = pd.read_sql_query(
            """
            SELECT f.metric,
                   count(*) AS hours,
                   count(DISTINCT f.target_date) AS days,
                   avg(f.value-a.value) AS bias,
                   avg(abs(f.value-a.value)) AS mae,
                   sqrt(avg((f.value-a.value)*(f.value-a.value))) AS rmse,
                   sum(abs(f.value-a.value))/nullif(sum(abs(a.value)),0) AS wmape
            FROM disclosure_hourly f
            JOIN disclosure_hourly a
              ON a.metric=f.metric AND a.target_start=f.target_start
             AND a.record_kind='actual' AND a.quality_status='complete'
            WHERE f.record_kind='forecast' AND f.quality_status='complete'
            GROUP BY f.metric ORDER BY f.metric
            """,
            connection,
        )

    files.to_csv(output_dir / "disclosure_coverage.csv", index=False, encoding="utf-8-sig")
    sheets.to_csv(output_dir / "disclosure_sheet_audit.csv", index=False, encoding="utf-8-sig")
    paired.to_csv(
        output_dir / "disclosure_forecast_vs_actual.csv",
        index=False,
        encoding="utf-8-sig",
    )
    result = {
        "status": "completed",
        "snapshot_id": snapshot[0] if snapshot else None,
        "snapshot_created_at": snapshot[1] if snapshot else None,
        "snapshot_status": snapshot[2] if snapshot else None,
        "zip_count": snapshot[3] if snapshot else 0,
        "workbook_count": snapshot[4] if snapshot else 0,
        "new_workbook_count": snapshot[5] if snapshot else 0,
        "file_coverage": files.to_dict(orient="records"),
        "sheet_status": sheets.to_dict(orient="records"),
        "paired_metrics": paired.to_dict(orient="records"),
        "limitations": [
            "实际披露的原始发布/接收时点未认证，本表仅作覆盖与误差研究，不构成严格事前回测",
            "备用、条件阻塞和机组群约束语义待来源认证，不能解释为裕度或实际触发",
        ],
    }
    (output_dir / "disclosure_review.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return result
