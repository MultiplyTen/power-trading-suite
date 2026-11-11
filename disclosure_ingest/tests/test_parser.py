# -*- coding: utf-8 -*-
import io
import os
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from openpyxl import load_workbook

from disclosure_ingest.parser import (
    availability,
    classify_workbook,
    decode_name,
    insert_workbook,
    now_string,
    sha256_bytes,
    workbook_target_date,
)
from disclosure_ingest.schema import create_tables


ROOT = Path(__file__).resolve().parents[2]
INPUTS = ROOT / "inputs" / "disclosure"


class DisclosureParserTest(unittest.TestCase):
    def parse_member(self, zip_name, target_date):
        with zipfile.ZipFile(INPUTS / zip_name) as archive:
            info = next(
                item for item in archive.infolist()
                if target_date in decode_name(item)
            )
            raw = archive.read(info)
            member = decode_name(info)
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        connection = sqlite3.connect(path)
        self.addCleanup(lambda: (connection.close(), os.remove(path)))
        create_tables(connection)
        kind = classify_workbook(zip_name, member)
        target = workbook_target_date(member)
        available, basis = availability(kind, target, now_string())
        workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        insert_workbook(
            connection,
            workbook,
            source_id=sha256_bytes(raw),
            snapshot_id="unit-test",
            workbook_kind=kind,
            target_date=target,
            available_time=available,
            available_time_basis=basis,
            source_version="unit-test",
            ingested_at=now_string(),
        )
        workbook.close()
        return connection

    def test_forecast_workbook_preserves_curves_and_header_only_sheets(self):
        connection = self.parse_member(
            "披露中心-中调信息披露-预测(2026-10-01~2026-10-15).zip",
            "2026-10-14",
        )
        self.assertEqual(
            connection.execute("SELECT count(*) FROM disclosure_sheet_audit").fetchone()[0],
            23,
        )
        self.assertEqual(
            connection.execute(
                "SELECT count(*) FROM disclosure_points WHERE metric='system_load'"
            ).fetchone()[0],
            96,
        )
        self.assertEqual(
            connection.execute(
                "SELECT count(*) FROM disclosure_hourly WHERE metric='system_load' AND quality_status='complete'"
            ).fetchone()[0],
            24,
        )
        self.assertEqual(
            connection.execute(
                "SELECT status FROM disclosure_sheet_audit WHERE sheet_name LIKE '日前阻塞断面信息%'"
            ).fetchone()[0],
            "not_provided",
        )

    def test_actual_workbook_curves_and_events_are_separate(self):
        connection = self.parse_member(
            "披露中心-中调信息披露-实际(2026-10-01~2026-10-15).zip",
            "2026-10-01",
        )
        self.assertGreater(
            connection.execute(
                "SELECT count(*) FROM disclosure_points WHERE record_kind='actual'"
            ).fetchone()[0],
            0,
        )
        self.assertGreater(
            connection.execute(
                "SELECT count(*) FROM disclosure_events WHERE record_kind IN ('actual','event')"
            ).fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
