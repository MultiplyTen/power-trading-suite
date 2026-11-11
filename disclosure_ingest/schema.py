# -*- coding: utf-8 -*-
"""Disclosure store schema. New tables are additive and never replace legacy tables."""

DDL = """
CREATE TABLE IF NOT EXISTS disclosure_snapshots(
  snapshot_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL,
  config_json TEXT NOT NULL,
  zip_count INTEGER NOT NULL,
  workbook_count INTEGER NOT NULL,
  new_workbook_count INTEGER NOT NULL,
  notes_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS disclosure_files(
  source_id TEXT PRIMARY KEY,
  snapshot_id TEXT NOT NULL REFERENCES disclosure_snapshots(snapshot_id),
  zip_name TEXT NOT NULL,
  member_name TEXT NOT NULL,
  zip_sha256 TEXT NOT NULL,
  source_sha256 TEXT NOT NULL,
  record_kind TEXT NOT NULL,
  target_date TEXT NOT NULL,
  file_size INTEGER NOT NULL,
  received_at TEXT NOT NULL,
  available_time TEXT NOT NULL,
  available_time_basis TEXT NOT NULL,
  source_version TEXT NOT NULL,
  ingested_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_disclosure_files_target
  ON disclosure_files(record_kind,target_date,ingested_at);

CREATE TABLE IF NOT EXISTS disclosure_sheet_audit(
  source_id TEXT NOT NULL,
  sheet_name TEXT NOT NULL,
  record_kind TEXT NOT NULL,
  target_date TEXT NOT NULL,
  status TEXT NOT NULL,
  row_count INTEGER NOT NULL,
  point_count INTEGER NOT NULL,
  time_header_count INTEGER NOT NULL,
  notes TEXT,
  PRIMARY KEY(source_id,sheet_name)
);

CREATE TABLE IF NOT EXISTS disclosure_events(
  source_id TEXT NOT NULL,
  sheet_name TEXT NOT NULL,
  row_number INTEGER NOT NULL,
  snapshot_id TEXT NOT NULL,
  record_kind TEXT NOT NULL,
  category TEXT NOT NULL,
  target_date TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  metric TEXT NOT NULL,
  context_json TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  value_count INTEGER NOT NULL,
  numeric_min REAL,
  numeric_max REAL,
  numeric_mean REAL,
  quality_status TEXT NOT NULL,
  available_time TEXT NOT NULL,
  available_time_basis TEXT NOT NULL,
  source_version TEXT NOT NULL,
  ingested_at TEXT NOT NULL,
  PRIMARY KEY(source_id,sheet_name,row_number)
);

CREATE INDEX IF NOT EXISTS idx_disclosure_events_target
  ON disclosure_events(record_kind,category,target_date,entity_id);

CREATE TABLE IF NOT EXISTS disclosure_points(
  source_id TEXT NOT NULL,
  sheet_name TEXT NOT NULL,
  row_number INTEGER NOT NULL,
  point_index INTEGER NOT NULL,
  snapshot_id TEXT NOT NULL,
  target_start TEXT NOT NULL,
  target_end TEXT NOT NULL,
  target_date TEXT NOT NULL,
  hour INTEGER NOT NULL,
  entity_id TEXT NOT NULL,
  metric TEXT NOT NULL,
  record_kind TEXT NOT NULL,
  value REAL,
  unit TEXT NOT NULL,
  time_semantics TEXT NOT NULL,
  available_time TEXT NOT NULL,
  available_time_basis TEXT NOT NULL,
  source_version TEXT NOT NULL,
  quality_status TEXT NOT NULL,
  ingested_at TEXT NOT NULL,
  PRIMARY KEY(source_id,sheet_name,row_number,point_index)
);

CREATE INDEX IF NOT EXISTS idx_disclosure_points_lookup
  ON disclosure_points(record_kind,metric,target_start,ingested_at);

CREATE TABLE IF NOT EXISTS disclosure_hourly(
  source_id TEXT NOT NULL,
  sheet_name TEXT NOT NULL,
  row_number INTEGER NOT NULL,
  hour INTEGER NOT NULL,
  snapshot_id TEXT NOT NULL,
  target_start TEXT NOT NULL,
  target_end TEXT NOT NULL,
  target_date TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  metric TEXT NOT NULL,
  record_kind TEXT NOT NULL,
  value REAL,
  unit TEXT NOT NULL,
  point_count INTEGER NOT NULL,
  missing_count INTEGER NOT NULL,
  available_time TEXT NOT NULL,
  available_time_basis TEXT NOT NULL,
  source_version TEXT NOT NULL,
  quality_status TEXT NOT NULL,
  ingested_at TEXT NOT NULL,
  PRIMARY KEY(source_id,sheet_name,row_number,hour)
);

CREATE INDEX IF NOT EXISTS idx_disclosure_hourly_lookup
  ON disclosure_hourly(record_kind,metric,target_start,ingested_at);
"""


def create_tables(connection):
    connection.executescript(DDL)
