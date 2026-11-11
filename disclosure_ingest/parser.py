# -*- coding: utf-8 -*-
"""Parse Guangdong dispatch disclosure workbooks into auditable records."""

import datetime as dt
import hashlib
import json
import math
import re
import zipfile
from pathlib import Path

from openpyxl import load_workbook


TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
TZ = dt.timezone(dt.timedelta(hours=8))

METRIC_MAP = [
    ("统调系统实际负荷", "system_load"),
    ("统调负荷", "system_load"),
    ("省内A类电源", "province_a_generation"),
    ("省内B类电源", "province_b_generation"),
    ("粤港联络线", "guangdong_hong_kong_link"),
    ("水电（含抽蓄）总出力", "hydro_including_pumped_storage"),
    ("抽蓄电站出力计划", "pumped_storage_plan"),
    ("预测出力", "pumped_storage_plan"),
    ("光伏出力预测", "system_solar_forecast"),
    ("风电出力预测", "system_wind_forecast"),
    ("发电总出力", "total_generation"),
    ("D+2日", "spot_renewable_d2"),
    ("D+1日", "spot_renewable_d1"),
    ("D日", "spot_renewable_d0"),
    ("正备用", "positive_reserve"),
    ("负备用", "negative_reserve"),
    ("一次调频备用", "primary_frequency_reserve"),
    ("光伏", "local_solar"),
    ("风电", "local_wind"),
    ("火电", "local_thermal"),
    ("水电", "local_hydro"),
]


def now_string():
    return dt.datetime.now(TZ).isoformat(timespec="seconds")


def label(value):
    if value is None:
        return ""
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def numeric(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def decode_name(info):
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("gb18030")
    except (UnicodeError, LookupError):
        return info.filename


def sheet_base(sheet_name):
    result = DATE_RE.sub("", sheet_name)
    result = re.sub(r"[（(][^（）()]*[）)]", "", result)
    return result.strip()


def canonical_metric(entity):
    text = label(entity)
    for phrase, name in METRIC_MAP:
        if phrase in text:
            return name
    return None


def target_offset_days(entity):
    text = label(entity)
    if text.startswith("D+1日"):
        return 1
    if text.startswith("D+2日"):
        return 2
    return 0


def classify_record(sheet, workbook_kind):
    text = sheet_base(sheet)
    if workbook_kind == "actual":
        return "actual"
    if any(word in text for word in ["阻塞", "约束"]):
        return "limit"
    if any(word in text for word in ["检修", "停运", "投产", "退役", "调控"]):
        return "event"
    return "forecast" if canonical_metric(text) else "plan"


def category(sheet):
    text = sheet_base(sheet)
    if canonical_metric(text):
        return "supply_demand_curve"
    if any(word in text for word in ["阻塞", "约束"]):
        return "constraint"
    if any(word in text for word in ["检修", "停运", "投产", "退役", "调控"]):
        return "maintenance_or_event"
    return "other"


def availability(workbook_kind, target_date, received_at):
    if workbook_kind == "forecast":
        boundary = dt.datetime.combine(
            target_date - dt.timedelta(days=1), dt.time(12, 0), TZ
        )
        return (
            boundary.isoformat(timespec="seconds"),
            "user_confirmed_before_1200_boundary; exact source issue_time unavailable",
        )
    return (
        received_at,
        "archive_receipt_only; original publication/receipt time not certified",
    )


def json_safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return label(value)


def context_from_row(header, row, context_indexes):
    return {
        header[index] if index < len(header) and header[index] else f"column_{index+1}":
        json_safe(row[index] if index < len(row) else None)
        for index in context_indexes
    }


def entity_from_context(context):
    for key in ["通道名称", "断面名称", "机组名称", "电厂名称", "数据类型", "类型", "内容"]:
        if label(context.get(key)):
            return label(context[key])
    values = [label(v) for v in context.values() if label(v)]
    return values[0] if values else "unnamed"


def insert_workbook(connection, workbook, *, source_id, snapshot_id, workbook_kind,
                    target_date, available_time, available_time_basis, source_version,
                    ingested_at):
    point_count = 0
    for sheet in workbook.worksheets:
        rows = list(sheet.iter_rows(values_only=True))
        header = [label(value) for value in rows[0]] if rows else []
        time_indexes = [
            index for index, value in enumerate(header) if TIME_RE.fullmatch(value or "")
        ]
        status = "parsed"
        notes = None
        if time_indexes and time_indexes != list(range(time_indexes[0], time_indexes[0] + 96)):
            status = "degraded_time_header"
            notes = f"valid_time_headers={len(time_indexes)}"
        elif time_indexes and len(rows) <= 1:
            status = "not_provided"
            notes = "time header only; absence is not zero"

        sheet_row_count = max(0, len(rows) - 1)
        sheet_point_count = 0
        context_indexes = list(range(time_indexes[0])) if time_indexes else list(range(len(header)))

        if not time_indexes:
            if sheet_row_count == 0:
                status = "not_provided"
                notes = "header only; absence is not zero"
            else:
                for row_number, row in enumerate(rows[1:], start=2):
                    if all(value is None or label(value) == "" for value in row):
                        continue
                    context = context_from_row(header, row, context_indexes)
                    payload = {
                        header[index] if index < len(header) and header[index] else f"column_{index+1}":
                        json_safe(row[index] if index < len(row) else None)
                        for index in range(len(header))
                    }
                    numbers = [numeric(value) for value in row]
                    numbers = [value for value in numbers if value is not None]
                    entity = entity_from_context(context)
                    record_kind = classify_record(sheet.title, workbook_kind)
                    connection.execute(
                        """INSERT INTO disclosure_events VALUES(
                           ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            source_id, sheet.title, row_number, snapshot_id, record_kind,
                            category(sheet.title), str(target_date), entity,
                            sheet_base(sheet.title), json.dumps(context, ensure_ascii=False),
                            json.dumps(payload, ensure_ascii=False), len(payload),
                            min(numbers) if numbers else None,
                            max(numbers) if numbers else None,
                            sum(numbers) / len(numbers) if numbers else None,
                            "parsed" if numbers else "no_numeric_value",
                            available_time, available_time_basis, source_version, ingested_at,
                        ),
                    )
        else:
            for row_number, row in enumerate(rows[1:], start=2):
                if all(value is None or label(value) == "" for value in row):
                    continue
                context = context_from_row(header, row, context_indexes)
                entity = entity_from_context(context)
                metric = canonical_metric(entity)
                values = [row[index] if index < len(row) else None for index in time_indexes]
                numbers = [numeric(value) for value in values]
                if metric:
                    offset = target_offset_days(entity)
                    point_target = target_date + dt.timedelta(days=offset)
                    for point_index, value in enumerate(numbers):
                        start = dt.datetime.combine(
                            point_target, dt.time(point_index // 4, point_index % 4 * 15), TZ
                        )
                        end = start + dt.timedelta(minutes=15)
                        connection.execute(
                            """INSERT INTO disclosure_points VALUES(
                               ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                source_id, sheet.title, row_number, point_index, snapshot_id,
                                start.isoformat(), end.isoformat(), str(point_target), point_index // 4,
                                entity, metric, workbook_kind,
                                value, "MW", "source_label_unverified",
                                available_time, available_time_basis, source_version,
                                "complete" if value is not None else "missing", ingested_at,
                            ),
                        )
                    for hour in range(24):
                        hour_values = numbers[hour * 4:(hour + 1) * 4]
                        present = [value for value in hour_values if value is not None]
                        missing = len(hour_values) - len(present)
                        start = dt.datetime.combine(point_target, dt.time(hour), TZ)
                        quality = "complete" if missing == 0 else (
                            "degraded_partial" if present else "unavailable"
                        )
                        connection.execute(
                            """INSERT INTO disclosure_hourly VALUES(
                               ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                source_id, sheet.title, row_number, hour, snapshot_id,
                                start.isoformat(), (start + dt.timedelta(hours=1)).isoformat(),
                                str(point_target), entity, metric, workbook_kind,
                                sum(present) / len(present) if present else None, "MW",
                                len(hour_values), missing, available_time, available_time_basis,
                                source_version, quality, ingested_at,
                            ),
                        )
                    sheet_point_count += len(numbers)
                else:
                    payload = {
                        "context": context,
                        "values": {
                            header[index]: json_safe(row[index] if index < len(row) else None)
                            for index in time_indexes
                        },
                    }
                    record_kind = classify_record(sheet.title, workbook_kind)
                    connection.execute(
                        """INSERT INTO disclosure_events VALUES(
                           ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            source_id, sheet.title, row_number, snapshot_id, record_kind,
                            category(sheet.title), str(target_date), entity,
                            sheet_base(sheet.title), json.dumps(context, ensure_ascii=False),
                            json.dumps(payload, ensure_ascii=False), len(values),
                            min([v for v in numbers if v is not None]) if any(v is not None for v in numbers) else None,
                            max([v for v in numbers if v is not None]) if any(v is not None for v in numbers) else None,
                            sum([v for v in numbers if v is not None]) / len([v for v in numbers if v is not None]) if any(v is not None for v in numbers) else None,
                            "parsed" if any(v is not None for v in numbers) else "no_numeric_value",
                            available_time, available_time_basis, source_version, ingested_at,
                        ),
                    )
        point_count += sheet_point_count
        connection.execute(
            """INSERT INTO disclosure_sheet_audit VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                source_id, sheet.title, workbook_kind, str(target_date), status,
                sheet_row_count, sheet_point_count, len(time_indexes), notes,
            ),
        )
    return point_count


def classify_workbook(zip_name, member_name):
    if "预测" in zip_name and "预测" in member_name:
        return "forecast"
    if "实际" in zip_name and "实际" in member_name:
        return "actual"
    raise ValueError("ZIP/member name does not identify forecast or actual disclosure")


def workbook_target_date(member_name):
    dates = DATE_RE.findall(member_name)
    if len(dates) != 1:
        raise ValueError("member workbook must contain exactly one target date")
    return dt.date.fromisoformat(dates[0])


def ingest_zip(connection, zip_path, *, snapshot_id, ingested_at):
    zip_path = Path(zip_path)
    zip_sha = sha256_bytes(zip_path.read_bytes())
    new_files = 0
    member_count = 0
    with zipfile.ZipFile(zip_path) as archive:
        members = [info for info in archive.infolist() if not info.is_dir() and info.filename.lower().endswith(".xlsx")]
        for info in members:
            member = decode_name(info)
            member_count += 1
            raw = archive.read(info)
            raw_sha256 = sha256_bytes(raw)
            source_id = sha256_bytes(
                f"{zip_sha}\n{member}\n{raw_sha256}".encode("utf-8")
            )
            if connection.execute("SELECT 1 FROM disclosure_files WHERE source_id=?", (source_id,)).fetchone():
                continue
            workbook_kind = classify_workbook(zip_path.name, member)
            target_date = workbook_target_date(member)
            received_at = now_string()
            available_time, available_time_basis = availability(workbook_kind, target_date, received_at)
            source_version = f"zip:{zip_sha[:12]}:xlsx:{raw_sha256[:12]}"
            workbook = load_workbook(__import__("io").BytesIO(raw), read_only=True, data_only=True)
            try:
                insert_workbook(
                    connection,
                    workbook,
                    source_id=source_id,
                    snapshot_id=snapshot_id,
                    workbook_kind=workbook_kind,
                    target_date=target_date,
                    available_time=available_time,
                    available_time_basis=available_time_basis,
                    source_version=source_version,
                    ingested_at=ingested_at,
                )
            finally:
                workbook.close()
            connection.execute(
                """INSERT INTO disclosure_files VALUES(
                   ?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    source_id, snapshot_id, zip_path.name, member, zip_sha, raw_sha256,
                    workbook_kind, str(target_date), info.file_size, received_at,
                    available_time, available_time_basis, source_version, ingested_at,
                ),
            )
            new_files += 1
    return {"zip": zip_path.name, "members": member_count, "new_workbooks": new_files}


def ingest_standalone_workbook(connection, workbook_path, *, snapshot_id, ingested_at):
    """Ingest the single-day forecast workbook exported by the disclosure center."""
    workbook_path = Path(workbook_path)
    raw = workbook_path.read_bytes()
    raw_sha256 = sha256_bytes(raw)
    source_id = sha256_bytes(f"file:{workbook_path.name}\n{raw_sha256}".encode("utf-8"))
    if connection.execute("SELECT 1 FROM disclosure_files WHERE source_id=?", (source_id,)).fetchone():
        return {"file": workbook_path.name, "members": 0, "new_workbooks": 0}

    workbook_kind = "forecast" if "预测" in workbook_path.name else "actual"
    target_date = workbook_target_date(workbook_path.name)
    received_at = now_string()
    available_time, available_time_basis = availability(workbook_kind, target_date, received_at)
    source_version = f"file:{raw_sha256[:16]}"
    workbook = load_workbook(__import__("io").BytesIO(raw), read_only=True, data_only=True)
    try:
        insert_workbook(
            connection,
            workbook,
            source_id=source_id,
            snapshot_id=snapshot_id,
            workbook_kind=workbook_kind,
            target_date=target_date,
            available_time=available_time,
            available_time_basis=available_time_basis,
            source_version=source_version,
            ingested_at=ingested_at,
        )
    finally:
        workbook.close()
    connection.execute(
        """INSERT INTO disclosure_files VALUES(
           ?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            source_id, snapshot_id, "standalone", workbook_path.name, raw_sha256, raw_sha256,
            workbook_kind, str(target_date), len(raw), received_at,
            available_time, available_time_basis, source_version, ingested_at,
        ),
    )
    return {"file": workbook_path.name, "members": 1, "new_workbooks": 1}


def ingest_file(connection, path, *, snapshot_id, ingested_at):
    path = Path(path)
    if path.suffix.lower() == ".zip":
        result = ingest_zip(connection, path, snapshot_id=snapshot_id, ingested_at=ingested_at)
        result["file"] = path.name
        return result
    if path.suffix.lower() == ".xlsx":
        return ingest_standalone_workbook(
            connection, path, snapshot_id=snapshot_id, ingested_at=ingested_at
        )
    raise ValueError("披露目录只支持ZIP或XLSX：" + path.name)
