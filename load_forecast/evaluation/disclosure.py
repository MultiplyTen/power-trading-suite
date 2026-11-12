# -*- coding: utf-8 -*-
"""Read versioned forecast disclosure curves as price-model features."""

import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

try:
    from configuration import resolve
except ModuleNotFoundError:
    def resolve(config, value):
        return Path(config["_root"]) / value


def _connect(config):
    return sqlite3.connect(resolve(config, config["db_path"]))


def _eligible_receipts(frame, config):
    """Select the last received complete version known by each target's cutoff.

    Callers supply forecast rows with complete quality. Feature generation and
    target coverage must use this same receipt and version selection.
    """
    if frame.empty:
        return frame.copy()
    frame = frame.copy()
    frame["target_start"] = pd.to_datetime(frame["target_start"], format="mixed").dt.tz_localize(None)
    # Historical backfills and later revisions were not known at the target's
    # decision origin. Receipt time is evidence of receipt, not source issue time.
    # Naive database timestamps use the project's local clock, not UTC.
    def local_receipt(value):
        try:
            ts = pd.Timestamp(value)
            if pd.isna(ts):
                return pd.NaT
            return ts.tz_convert("Asia/Shanghai").tz_localize(None) if ts.tzinfo else ts
        except (TypeError, ValueError):
            return pd.NaT
    frame["received_time"] = frame["ingested_at"].map(local_receipt)
    run = config.get("run", {})
    origins = (frame["target_start"].dt.normalize() - pd.Timedelta(days=1)
               + pd.Timedelta(hours=run.get("decision_cutoff_hour", 12),
                              minutes=run.get("decision_cutoff_minute", 30)))
    frame = frame[frame["received_time"].notna() & (frame["received_time"] <= origins)].copy()
    if frame.empty:
        return frame
    frame = frame.sort_values(["received_time", "source_id"])
    return frame.drop_duplicates(["metric", "target_start"], keep="last")


def read_disclosure_features(config):
    """Return complete hourly forecast curves, one canonical metric per column."""
    with _connect(config) as connection:
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='disclosure_hourly'"
        ).fetchone():
            return pd.DataFrame()
        frame = pd.read_sql_query(
            """
            SELECT target_start, metric, value, ingested_at, source_id
            FROM disclosure_hourly
            WHERE record_kind='forecast' AND quality_status='complete'
            """,
            connection,
        )
    frame = _eligible_receipts(frame, config)
    if frame.empty:
        return pd.DataFrame()
    pivot = frame.pivot(index="target_start", columns="metric", values="value")
    result = pd.DataFrame(index=pivot.index)
    for metric in pivot.columns:
        name = f"disc_{metric}"
        series = pivot[metric].astype(float).sort_index()
        result[name] = series
        result[f"{name}_hour_change"] = series.diff()
        day = series.index.normalize()
        result[f"{name}_day_mean"] = series.groupby(day).transform("mean")
        result[f"{name}_day_range"] = (
            series.groupby(day).transform("max") - series.groupby(day).transform("min")
        )
    return result.sort_index()


def disclosure_status(config):
    with _connect(config) as connection:
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='disclosure_hourly'"
        ).fetchone():
            return {"status": "unavailable", "message": "披露表尚未导入"}
        snapshot = connection.execute(
            """SELECT snapshot_id, created_at, status, workbook_count, new_workbook_count
               FROM disclosure_snapshots ORDER BY created_at DESC LIMIT 1"""
        ).fetchone()
        coverage = connection.execute(
            """SELECT record_kind, count(DISTINCT target_date), count(*)
               FROM disclosure_hourly GROUP BY record_kind ORDER BY record_kind"""
        ).fetchall()
        dates = connection.execute(
            "SELECT min(target_date), max(target_date) FROM disclosure_files WHERE record_kind='forecast'"
        ).fetchone()
    return {
        "status": "ready" if snapshot and snapshot[2] == "completed" else "degraded",
        "snapshot_id": snapshot[0] if snapshot else None,
        "created_at": snapshot[1] if snapshot else None,
        "workbook_count": snapshot[3] if snapshot else 0,
        "new_workbook_count": snapshot[4] if snapshot else 0,
        "forecast_target_dates": (
            {"first": dates[0], "last": dates[1]} if dates and dates[0] else None
        ),
        "hourly_coverage": [
            {"record_kind": row[0], "days": row[1], "rows": row[2]}
            for row in coverage
        ],
        "availability_basis": "features require receipt by configured D-1 decision cutoff; backfills excluded; original source issue times remain uncertified",
    }


def disclosure_target_coverage(config, target_day):
    target_date = str(pd.Timestamp(target_day).date())
    frame = pd.DataFrame()
    with _connect(config) as connection:
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='disclosure_hourly'"
        ).fetchone():
            frame = pd.read_sql_query(
                """SELECT target_start, metric, value, ingested_at, source_id, quality_status
                   FROM disclosure_hourly
                   WHERE record_kind='forecast' AND target_date=?""",
                connection,
                params=(target_date,),
            )
    metrics = []
    if not frame.empty:
        selected = _eligible_receipts(frame[frame.quality_status.eq('complete')], config)
        expected = pd.date_range(target_date, periods=24, freq='h')
        # Duplicate versions or off-grid times must not manufacture 24-hour
        # coverage. Only the model's expected unique hourly targets count.
        selected = selected[selected.target_start.isin(expected)]
        for metric in sorted(frame.metric.unique()):
            values = selected[selected.metric.eq(metric)]
            finite = np.isfinite(pd.to_numeric(values.value, errors='coerce'))
            metrics.append({
                "metric": metric,
                "hours": int(values.target_start.nunique()),
                "complete_hours": int(values.loc[finite, 'target_start'].nunique()),
            })
    complete = [item for item in metrics if item["hours"] == 24 and item["complete_hours"] == 24]
    status = (
        "unavailable" if not metrics
        else "complete" if len(complete) == len(metrics) and len(complete) >= 10
        else "degraded_partial" if complete
        else "unavailable"
    )
    return {
        "target_date": target_date,
        "status": status,
        "metric_count": len(metrics),
        "complete_metric_count": len(complete),
        "metrics": metrics,
        "availability_basis": "latest complete version received by configured D-1 cutoff; unique hourly targets only",
        "policy": "missing target disclosure features remain NaN and are median-imputed inside the selected model; the run is explicitly degraded, not treated as full coverage",
    }
