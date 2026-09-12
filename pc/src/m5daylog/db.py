"""SQLite state initialization for the M5Daylog PoC CLI scaffold.

Contract reference: Spec S-005 (PC sync / local storage / SQLite state).
Scaffold scope (#43) creates the database and tables only; sync / VAD /
STT / diarization logic lives in later Tasks (#51 and follow-ups).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

DEFAULT_DB_RELATIVE = Path("state") / "m5daylog.db"

_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS schema_info (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    model TEXT,
    first_seen_at TEXT,
    last_seen_at TEXT,
    firmware_version TEXT,
    last_manifest_at TEXT
);
CREATE TABLE IF NOT EXISTS recordings (
    recording_id TEXT PRIMARY KEY,
    device_id TEXT,
    started_at TEXT,
    duration_ms INTEGER,
    source_filename TEXT,
    source_sha256 TEXT,
    size_bytes INTEGER,
    local_path TEXT,
    sync_status TEXT,
    vad_status TEXT,
    stt_status TEXT,
    diarization_status TEXT,
    day_status TEXT,
    processed_at TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    job_type TEXT,
    recording_id TEXT,
    target_date TEXT,
    status TEXT,
    attempts INTEGER,
    created_at TEXT,
    started_at TEXT,
    completed_at TEXT,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    recording_id TEXT,
    target_date TEXT,
    artifact_type TEXT,
    path TEXT,
    sha256 TEXT,
    producer TEXT,
    producer_version TEXT,
    created_at TEXT
);
"""

_EXPECTED_TABLES = ("devices", "recordings", "jobs", "artifacts", "schema_info")


def resolve_db_path(
    explicit_db: str | None = None,
    data_dir: str | None = None,
) -> Path:
    """Resolve the SQLite path without reading unrelated configuration.

    Precedence: explicit --db > --data-dir/state/m5daylog.db >
    M5DAYLOG_DATA_DIR/state/m5daylog.db > state/m5daylog.db (CWD-relative).
    """
    if explicit_db:
        return Path(explicit_db).expanduser()
    if data_dir:
        return Path(data_dir).expanduser() / "state" / "m5daylog.db"
    configured = os.environ.get("M5DAYLOG_DATA_DIR")
    if configured:
        return Path(configured).expanduser() / "state" / "m5daylog.db"
    return Path.cwd() / DEFAULT_DB_RELATIVE


def init_db(db_path: Path) -> Path:
    """Create parent directories and initialize the scaffold schema.

    Idempotent: safe to re-run on an existing database. Existing user
    rows are preserved; only missing tables and the schema version
    marker are created/updated.
    """
    db_path = Path(db_path).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path))
    try:
        connection.executescript(_SCHEMA_DDL)
        connection.execute(
            "INSERT INTO schema_info (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()
    finally:
        connection.close()
    return db_path


def list_tables(db_path: Path) -> list[str]:
    """Return sorted user table names for verification output."""
    connection = sqlite3.connect(str(db_path))
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    finally:
        connection.close()
    return sorted(row[0] for row in rows if not row[0].startswith("sqlite_"))


def expected_tables() -> tuple[str, ...]:
    """Table contract created by this scaffold."""
    return _EXPECTED_TABLES
