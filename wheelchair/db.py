"""SQLite schema and connection helpers."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_registry (
    source TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'operator'
        CHECK (kind IN ('operator','census','survey','other')),
    name TEXT
);

CREATE TABLE IF NOT EXISTS import_batch (
    batch_id TEXT PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS site (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    stop_id TEXT NOT NULL,
    UNIQUE(source, stop_id)
);

CREATE TABLE IF NOT EXISTS site_version (
    source TEXT NOT NULL,
    stop_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    source_kind TEXT NOT NULL DEFAULT 'operator',
    name TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    accessible INTEGER,
    observed_at TEXT,
    payload_hash TEXT NOT NULL,
    batch_id TEXT,
    imported_at TEXT NOT NULL,
    PRIMARY KEY (source, stop_id, version)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS site_current (
    site_pk INTEGER PRIMARY KEY REFERENCES site(id),
    source TEXT NOT NULL,
    stop_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    source_kind TEXT NOT NULL DEFAULT 'operator',
    name TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    accessible INTEGER,
    observed_at TEXT,
    payload_hash TEXT NOT NULL,
    batch_id TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(source, stop_id)
);

CREATE TABLE IF NOT EXISTS trip_version (
    source TEXT NOT NULL,
    trip_code TEXT NOT NULL,
    version INTEGER NOT NULL,
    service_date TEXT NOT NULL,
    route_code TEXT,
    headsign TEXT,
    payload_hash TEXT NOT NULL,
    batch_id TEXT,
    imported_at TEXT NOT NULL,
    PRIMARY KEY (source, trip_code, version)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS trip (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    trip_code TEXT NOT NULL,
    version INTEGER NOT NULL,
    service_date TEXT NOT NULL,
    route_code TEXT,
    headsign TEXT,
    payload_hash TEXT NOT NULL,
    batch_id TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(source, trip_code)
);

CREATE TABLE IF NOT EXISTS stop_time (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_pk INTEGER NOT NULL REFERENCES trip(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    trip_code TEXT NOT NULL,
    seq INTEGER NOT NULL,
    stop_source TEXT NOT NULL,
    stop_id TEXT NOT NULL,
    arrival_s INTEGER NOT NULL,
    departure_s INTEGER NOT NULL,
    UNIQUE(trip_pk, seq)
);

-- This composite index is the planner's departure lookup.  It lets a late
-- query skip 19,000 earlier hub calls instead of scanning the whole table.
CREATE INDEX IF NOT EXISTS idx_stop_time_stop_dep
    ON stop_time(stop_source, stop_id, departure_s, arrival_s);
CREATE INDEX IF NOT EXISTS idx_stop_time_trip_seq
    ON stop_time(trip_pk, seq);
CREATE INDEX IF NOT EXISTS idx_stop_time_source_trip
    ON stop_time(source, trip_code);

CREATE TABLE IF NOT EXISTS same_stop_edge (
    a_source TEXT NOT NULL,
    a_stop_id TEXT NOT NULL,
    b_source TEXT NOT NULL,
    b_stop_id TEXT NOT NULL,
    distance_m REAL NOT NULL,
    name_score REAL NOT NULL,
    rule_version TEXT NOT NULL,
    PRIMARY KEY (a_source, a_stop_id, b_source, b_stop_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS adjudication (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    a_source TEXT NOT NULL,
    a_stop_id TEXT NOT NULL,
    b_source TEXT NOT NULL,
    b_stop_id TEXT NOT NULL,
    pair_key TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('confirm','reject')),
    reason TEXT NOT NULL,
    decided_by TEXT,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    revoke_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_adjudication_active
    ON adjudication(revoked_at, id);
CREATE INDEX IF NOT EXISTS idx_adjudication_pair
    ON adjudication(pair_key, id);

CREATE TABLE IF NOT EXISTS cluster_current (
    cluster_id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    accessible INTEGER,
    status TEXT NOT NULL,
    basis TEXT,
    selected_source TEXT,
    selected_stop_id TEXT,
    selected_observed_at TEXT,
    selected_version INTEGER,
    member_count INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cluster_member (
    cluster_id TEXT NOT NULL REFERENCES cluster_current(cluster_id),
    source TEXT NOT NULL,
    stop_id TEXT NOT NULL,
    PRIMARY KEY (source, stop_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_cluster_member_cluster
    ON cluster_member(cluster_id);

CREATE TABLE IF NOT EXISTS cluster_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_type TEXT NOT NULL,
    operation_ref TEXT,
    adjudication_id INTEGER,
    created_at TEXT NOT NULL,
    reason TEXT,
    cause_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_cluster_event_time ON cluster_event(id);

CREATE TABLE IF NOT EXISTS cluster_change (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES cluster_event(id),
    change_type TEXT NOT NULL,
    cluster_id TEXT,
    old_cluster_ids_json TEXT,
    new_cluster_ids_json TEXT,
    detail_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cluster_change_cluster
    ON cluster_change(cluster_id, id);
CREATE INDEX IF NOT EXISTS idx_cluster_change_event
    ON cluster_change(event_id);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    if str(path) != ":memory:":
        # WAL mode is persisted by init_db(); FULL keeps committed writes
        # durable across process kill / host restart.
        conn.execute("PRAGMA synchronous=FULL")
    else:
        conn.execute("PRAGMA synchronous=FULL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key,value) VALUES ('schema_version','1')"
    )
