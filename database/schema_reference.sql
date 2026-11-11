-- 结构参考，禁止用作覆盖现有库的初始化脚本。
CREATE TABLE actuals(target TEXT, metric TEXT, value REAL, available_at TEXT, evidence TEXT, source_sha TEXT, ingested_at TEXT, PRIMARY KEY(target,metric,source_sha));

CREATE TABLE audit_events(time TEXT, action TEXT, detail TEXT);

CREATE TABLE "coef_actual_load" (_version INTEGER NOT NULL REFERENCES input_versions(id), _row INTEGER NOT NULL,"timestamp" TEXT,"actual_load" REAL, PRIMARY KEY(_version,_row));

CREATE TABLE "coef_price_c24aa5" (_version INTEGER NOT NULL REFERENCES input_versions(id), _row INTEGER NOT NULL,"目标时刻" TEXT,"日前价格" REAL,"实时价格" REAL, PRIMARY KEY(_version,_row));

CREATE TABLE "coef_weather_history" (_version INTEGER NOT NULL REFERENCES input_versions(id),_row INTEGER NOT NULL,"location_id" TEXT,"city_name" TEXT,"region" TEXT,"valid_time" TEXT,"temperature_2m" REAL,"relative_humidity_2m" INTEGER,"precipitation" REAL,"shortwave_radiation" REAL,"wind_speed_100m" REAL,"cloud_cover" INTEGER,"assumed_available_time" TEXT,"mode" TEXT,"availability_basis" TEXT,"source_sha256" TEXT,"retrieved_at" TEXT,"issue_time" TEXT,"source_url" TEXT,PRIMARY KEY(_version,_row));

CREATE TABLE input_files(
 path TEXT PRIMARY KEY, role TEXT NOT NULL, active_version INTEGER);

CREATE TABLE input_quality(
 version INTEGER NOT NULL, table_name TEXT NOT NULL, rows INTEGER NOT NULL,
 first_time TEXT, last_time TEXT, missing_json TEXT NOT NULL,
 PRIMARY KEY(version,table_name));

CREATE TABLE input_tables(
 name TEXT PRIMARY KEY, path TEXT NOT NULL, sheet TEXT NOT NULL,
 columns_json TEXT NOT NULL, keys_json TEXT NOT NULL, UNIQUE(path,sheet));

CREATE TABLE input_versions(
 id INTEGER PRIMARY KEY, path TEXT NOT NULL REFERENCES input_files(path),
 sha256 TEXT NOT NULL, imported_at TEXT NOT NULL, original_name TEXT NOT NULL,
 note TEXT NOT NULL, batch_id TEXT NOT NULL, payload BLOB NOT NULL,
 UNIQUE(path,sha256));

CREATE TABLE "load_customer_mapping" (_version INTEGER NOT NULL REFERENCES input_versions(id), _row INTEGER NOT NULL,"entity_id" TEXT,"customer_name" TEXT,"city_name" TEXT,"mapping_status" TEXT,"evidence" TEXT,"meter_address_verified" INTEGER, PRIMARY KEY(_version,_row));

CREATE TABLE "load_customers" (_version INTEGER NOT NULL REFERENCES input_versions(id),_row INTEGER NOT NULL,"entity_id" TEXT,"date" TEXT,"hour" INTEGER,"value" REAL,"unit" TEXT,"available_time" TEXT,"version" TEXT,PRIMARY KEY(_version,_row));

CREATE TABLE "load_day_ahead" (_version INTEGER NOT NULL REFERENCES input_versions(id),_row INTEGER NOT NULL,"entity_id" TEXT,"date" TEXT,"hour" INTEGER,"value" REAL,"unit" TEXT,"available_time" TEXT,"version" TEXT,PRIMARY KEY(_version,_row));

CREATE TABLE "load_guangdong_cities" (_version INTEGER NOT NULL REFERENCES input_versions(id), _row INTEGER NOT NULL,"location_id" TEXT,"city_name" TEXT,"latitude" REAL,"longitude" REAL,"region" TEXT, PRIMARY KEY(_version,_row));

CREATE TABLE "load_portfolio" (_version INTEGER NOT NULL REFERENCES input_versions(id),_row INTEGER NOT NULL,"entity_id" TEXT,"date" TEXT,"hour" INTEGER,"value" REAL,"unit" TEXT,"available_time" TEXT,"version" TEXT,PRIMARY KEY(_version,_row));

CREATE TABLE "load_real_time" (_version INTEGER NOT NULL REFERENCES input_versions(id),_row INTEGER NOT NULL,"entity_id" TEXT,"date" TEXT,"hour" INTEGER,"value" REAL,"unit" TEXT,"available_time" TEXT,"version" TEXT,PRIMARY KEY(_version,_row));

CREATE TABLE "load_system_load" (_version INTEGER NOT NULL REFERENCES input_versions(id),_row INTEGER NOT NULL,"entity_id" TEXT,"date" TEXT,"hour" INTEGER,"value" REAL,"unit" TEXT,"available_time" TEXT,"version" TEXT,PRIMARY KEY(_version,_row));

CREATE TABLE "load_weather_history" (_version INTEGER NOT NULL REFERENCES input_versions(id),_row INTEGER NOT NULL,"location_id" TEXT,"city_name" TEXT,"region" TEXT,"valid_time" TEXT,"temperature_2m" REAL,"relative_humidity_2m" INTEGER,"precipitation" REAL,"shortwave_radiation" REAL,"wind_speed_100m" REAL,"cloud_cover" INTEGER,"assumed_available_time" TEXT,"mode" TEXT,"availability_basis" TEXT,"source_sha256" TEXT,"retrieved_at" TEXT,"issue_time" TEXT,"source_url" TEXT,PRIMARY KEY(_version,_row));

CREATE TABLE "load_weather_live" (_version INTEGER NOT NULL REFERENCES input_versions(id),_row INTEGER NOT NULL,"valid_time" TEXT,"temperature_2m" REAL,"relative_humidity_2m" INTEGER,"precipitation" REAL,"shortwave_radiation" REAL,"wind_speed_100m" REAL,"cloud_cover" INTEGER,"location_id" TEXT,"city_name" TEXT,"region" TEXT,"retrieved_at" TEXT,"issue_time" TEXT,"assumed_available_time" TEXT,"availability_basis" TEXT,"mode" TEXT,"source_sha256" TEXT,"source_url" TEXT,PRIMARY KEY(_version,_row));

CREATE TABLE maintenance_log(
 id INTEGER PRIMARY KEY, time TEXT NOT NULL, action TEXT NOT NULL,
 batch_id TEXT, detail_json TEXT NOT NULL);

CREATE TABLE predictions(run_id TEXT, target TEXT, load REAL, da REAL, rt REAL, k REAL, research_k REAL, PRIMARY KEY(run_id,target));

CREATE TABLE prices(source TEXT,t TEXT,da REAL,rt REAL,PRIMARY KEY(source,t));

CREATE TABLE "review_load_c24aa5" (_version INTEGER NOT NULL REFERENCES input_versions(id), _row INTEGER NOT NULL,"目标时刻" TEXT,"实际负荷" REAL, PRIMARY KEY(_version,_row));

CREATE TABLE "review_price_c24aa5" (_version INTEGER NOT NULL REFERENCES input_versions(id), _row INTEGER NOT NULL,"目标时刻" TEXT,"日前价格" REAL,"实时价格" REAL, PRIMARY KEY(_version,_row));

CREATE TABLE runs(id TEXT PRIMARY KEY, kind TEXT, published_at TEXT, model_version TEXT, config_json TEXT, source TEXT, sha TEXT, status TEXT);

CREATE TABLE "shared_calendar" (_version INTEGER NOT NULL REFERENCES input_versions(id),_row INTEGER NOT NULL,"date" TEXT,"is_workday" INTEGER,"has_holiday_name" INTEGER,"holiday_name" TEXT,"source" TEXT,"available_time" TEXT,"evidence_status" TEXT,PRIMARY KEY(_version,_row));

CREATE TABLE sources(path TEXT PRIMARY KEY, sha TEXT);

CREATE TABLE "实时价" (date TEXT NOT NULL,hour INTEGER NOT NULL CHECK(hour>=0 AND hour<24),value REAL,updated_at TEXT NOT NULL,source TEXT NOT NULL,available_at TEXT,evidence TEXT,source_sha TEXT,source_cell TEXT,PRIMARY KEY(date,hour));

CREATE TABLE 接入修订(id INTEGER PRIMARY KEY, batch_id TEXT, kind TEXT, date TEXT, hour INTEGER, old_value REAL, new_value REAL, available_at TEXT, source_cell TEXT);

CREATE TABLE 接入批次(id TEXT PRIMARY KEY, imported_at TEXT, source TEXT, sha256 TEXT, payload BLOB, summary_json TEXT);

CREATE TABLE "日前价" (date TEXT NOT NULL,hour INTEGER NOT NULL CHECK(hour>=0 AND hour<24),value REAL,updated_at TEXT NOT NULL,source TEXT NOT NULL,available_at TEXT,evidence TEXT,source_sha TEXT,source_cell TEXT,PRIMARY KEY(date,hour));

CREATE TABLE "负荷" (date TEXT NOT NULL,hour INTEGER NOT NULL CHECK(hour>=0 AND hour<24),value REAL,updated_at TEXT NOT NULL,source TEXT NOT NULL,available_at TEXT,evidence TEXT,source_sha TEXT,source_cell TEXT,PRIMARY KEY(date,hour));

CREATE TABLE "负荷预测" (date TEXT NOT NULL,hour INTEGER NOT NULL CHECK(hour>=0 AND hour<24),value REAL,updated_at TEXT NOT NULL,source TEXT NOT NULL,available_at TEXT,evidence TEXT,source_sha TEXT,source_cell TEXT,PRIMARY KEY(date,hour));

CREATE VIEW "current_coef_actual_load" AS SELECT t.* FROM "coef_actual_load" t JOIN input_files f ON t._version=f.active_version;

CREATE VIEW "current_coef_price_c24aa5" AS SELECT t.* FROM "coef_price_c24aa5" t JOIN input_files f ON t._version=f.active_version;

CREATE VIEW "current_coef_weather_history" AS SELECT t.* FROM "coef_weather_history" t JOIN input_files f ON t._version=f.active_version;

CREATE VIEW "current_load_customer_mapping" AS SELECT t.* FROM "load_customer_mapping" t JOIN input_files f ON t._version=f.active_version;

CREATE VIEW "current_load_customers" AS SELECT t.* FROM "load_customers" t JOIN input_files f ON t._version=f.active_version;

CREATE VIEW "current_load_day_ahead" AS SELECT t.* FROM "load_day_ahead" t JOIN input_files f ON t._version=f.active_version;

CREATE VIEW "current_load_guangdong_cities" AS SELECT t.* FROM "load_guangdong_cities" t JOIN input_files f ON t._version=f.active_version;

CREATE VIEW "current_load_portfolio" AS SELECT t.* FROM "load_portfolio" t JOIN input_files f ON t._version=f.active_version;

CREATE VIEW "current_load_real_time" AS SELECT t.* FROM "load_real_time" t JOIN input_files f ON t._version=f.active_version;

CREATE VIEW "current_load_system_load" AS SELECT t.* FROM "load_system_load" t JOIN input_files f ON t._version=f.active_version;

CREATE VIEW "current_load_weather_history" AS SELECT t.* FROM "load_weather_history" t JOIN input_files f ON t._version=f.active_version;

CREATE VIEW "current_load_weather_live" AS SELECT t.* FROM "load_weather_live" t JOIN input_files f ON t._version=f.active_version;

CREATE VIEW "current_review_load_c24aa5" AS SELECT t.* FROM "review_load_c24aa5" t JOIN input_files f ON t._version=f.active_version;

CREATE VIEW "current_review_price_c24aa5" AS SELECT t.* FROM "review_price_c24aa5" t JOIN input_files f ON t._version=f.active_version;

CREATE VIEW "current_shared_calendar" AS SELECT t.* FROM "shared_calendar" t JOIN input_files f ON t._version=f.active_version;

CREATE VIEW "系数决策记录" AS SELECT p.*,r.published_at,r.model_version,r.config_json,r.status,r.source,p.load*p.k AS declaration_quantity,p.load*p.research_k AS research_declaration_quantity FROM predictions p JOIN runs r ON p.run_id=r.id WHERE r.kind='coefficient';

CREATE VIEW "负荷预测记录" AS SELECT p.*,r.published_at,r.model_version,r.config_json,r.status,r.source FROM predictions p JOIN runs r ON p.run_id=r.id WHERE r.kind='load';