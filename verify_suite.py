# -*- coding: utf-8 -*-
"""Independent checks for the v1.1 disclosure delivery package."""

import json
import sqlite3
import sys
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "review"))
from common import read_config
from reconciliation import connect, reconcile


def main():
    config = read_config(ROOT / "review")
    db = connect(ROOT / "database/master.db")
    try:
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert not db.execute(
            "SELECT run_id,target,count(*) FROM predictions GROUP BY run_id,target HAVING count(*)>1"
        ).fetchall()
        done, pending, cutoff = reconcile(db, config)
        today = pd.Timestamp.now(tz="Asia/Shanghai").tz_localize(None).normalize()
        load_cutoff = today - pd.Timedelta(
            days=config.get("actual_lag_days_by_metric", {}).get("load", config["actual_lag_days"])
        )
        assert done.loc[done.kind.eq("load"), "target"].dt.normalize().le(load_cutoff).all()
        assert done.loc[done.kind.eq("coefficient"), "target"].dt.normalize().le(cutoff).all()

        review_manifest = json.loads(
            (ROOT / "review/output/run_manifest.json").read_text(encoding="utf-8")
        )
        assert review_manifest["status"] == "completed"
        assert review_manifest["reconciled_rows"] == len(done)
        assert review_manifest["pending_rows"] == len(pending)
        assert review_manifest["disclosure_review"]["status"] == "completed"

        load_manifest = json.loads(
            (ROOT / "load_forecast/output/run_manifest.json").read_text(encoding="utf-8")
        )
        assert load_manifest["status"].startswith("completed")
        assert load_manifest["rows"] == 24
        assert load_manifest["model_version"] == "load-suite-v1.1-disclosure"
        assert load_manifest["disclosure_input"]["target_coverage"]["status"] in {
            "complete", "degraded_partial"
        }

        audit = json.loads(
            (ROOT / "coefficient_opt/output/audit_20261015.json").read_text(
                encoding="utf-8"
            )
        )
        assert audit["价格发布来源"]["run_id"] == load_manifest["database_run_id"]
        assert audit["价格发布来源"]["model_version"] == "load-suite-v1.1-disclosure"

        coefficient_path = sorted(
            (ROOT / "coefficient_opt/output").glob("coefficients_*.xlsx")
        )[-1]
        frame = pd.read_excel(coefficient_path, sheet_name="24小时系数")
        assert len(frame) == 24
        assert frame["实际采用系数"].eq(1).all()
        assert frame["研究系数"].between(0.8, 1.2).all()
        np.testing.assert_allclose(
            frame["研究申报电量_MWh"],
            frame["预测负荷_MWh"] * frame["研究系数"],
            rtol=0,
            atol=1e-8,
        )

        snapshot = db.execute(
            """SELECT status,workbook_count,new_workbook_count FROM disclosure_snapshots
               ORDER BY created_at DESC LIMIT 1"""
        ).fetchone()
        assert snapshot[0] == "completed"
        assert snapshot[1] >= 1 and snapshot[2] >= 1
        assert db.execute("SELECT count(*) FROM disclosure_files").fetchone()[0] == 304
        assert db.execute(
            "SELECT count(*) FROM disclosure_files WHERE record_kind='forecast'"
        ).fetchone()[0] == 289
        assert db.execute(
            "SELECT count(DISTINCT target_date) FROM disclosure_hourly WHERE record_kind='actual'"
        ).fetchone()[0] == 12

        for stem in review_manifest["reports"]:
            path = ROOT / "review/output" / (stem + ".xlsx")
            workbook = load_workbook(path, read_only=True)
            for sheet in workbook:
                for row in sheet:
                    assert not any(cell.data_type == "e" for cell in row)
            workbook.close()
            with zipfile.ZipFile(path) as archive:
                assert archive.testzip() is None

        result = {
            "status": "passed",
            "package_version": "v1.2.0-optimized-20261014",
            "prediction_rows": db.execute("SELECT count(*) FROM predictions").fetchone()[0],
            "reconciled_rows": len(done),
            "pending_rows": len(pending),
            "cutoff": str(cutoff.date()),
            "disclosure_snapshot": {
                "status": snapshot[0],
                "latest_snapshot_workbooks": snapshot[1],
                "latest_snapshot_new_workbooks": snapshot[2],
            },
            "coefficient_file": coefficient_path.name,
            "report_workbooks": len(review_manifest["reports"]),
            "xlsx_errors": 0,
            "m3_consumes_m2": True,
        }
    finally:
        db.close()
    (ROOT / "verification.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
