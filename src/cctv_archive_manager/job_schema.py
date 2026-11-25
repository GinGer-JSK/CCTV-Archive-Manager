"""Versioned schema for a dedicated, local merge-job database.

No GUI dependency; never migrate a recording catalog into a job database.
"""
from contextlib import closing
from pathlib import Path
import sqlite3

SCHEMA_VERSION = 1
APPLICATION_ID = 0x43414D4A  # CAMJ
STATUSES = ('PENDING', 'RUNNING', 'VERIFYING', 'COMPLETED', 'INTERRUPTED',
            'PAUSED', 'FAILED', 'BLOCKED', 'CANCELLED')
_STATUS_SQL = ', '.join(repr(s) for s in STATUSES)

DDL = (
    f'''CREATE TABLE merge_jobs (
        job_id TEXT PRIMARY KEY NOT NULL,
        job_type TEXT NOT NULL CHECK(job_type IN ('single','batch','daily')),
        source_root TEXT NOT NULL,
        catalog_path TEXT NOT NULL,
        output_root TEXT NOT NULL,
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL CHECK(end_date >= start_date),
        split_method TEXT NOT NULL CHECK(split_method IN ('count','duration')),
        parts_per_day INTEGER NOT NULL CHECK(parts_per_day > 0),
        plan_signature TEXT NOT NULL CHECK(length(plan_signature) = 64),
        status TEXT NOT NULL CHECK(status IN ({_STATUS_SQL})),
        created_at TEXT NOT NULL,
        started_at TEXT,
        updated_at TEXT NOT NULL,
        completed_at TEXT,
        last_error TEXT
    )''',
    f'''CREATE TABLE merge_parts (
        part_id INTEGER PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES merge_jobs(job_id) ON DELETE RESTRICT,
        record_date TEXT NOT NULL,
        part_number INTEGER NOT NULL CHECK(part_number > 0),
        sequence_no INTEGER NOT NULL CHECK(sequence_no > 0),
        output_name TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ({_STATUS_SQL})),
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
        started_at TEXT,
        completed_at TEXT,
        output_bytes INTEGER CHECK(output_bytes > 0),
        output_modified_ns INTEGER,
        output_sha256 TEXT CHECK(length(output_sha256) = 64),
        integrity_method TEXT CHECK(integrity_method IN ('identify+sha256','decode+sha256')),
        last_error TEXT,
        UNIQUE(job_id, sequence_no),
        UNIQUE(job_id, record_date, part_number),
        UNIQUE(job_id, output_name COLLATE NOCASE),
        UNIQUE(job_id, part_id),
        CHECK(status <> 'COMPLETED' OR (
            completed_at IS NOT NULL AND output_bytes IS NOT NULL
            AND output_modified_ns IS NOT NULL AND output_sha256 IS NOT NULL
            AND integrity_method IS NOT NULL))
    )''',
    '''CREATE TABLE merge_part_files (
        id INTEGER PRIMARY KEY,
        part_id INTEGER NOT NULL REFERENCES merge_parts(part_id) ON DELETE RESTRICT,
        sequence_no INTEGER NOT NULL CHECK(sequence_no > 0),
        source_path TEXT NOT NULL,
        size_bytes INTEGER NOT NULL CHECK(size_bytes > 0),
        modified_ns INTEGER NOT NULL,
        duration_ms INTEGER CHECK(duration_ms > 0),
        UNIQUE(part_id, sequence_no),
        UNIQUE(part_id, source_path)
    )''',
    f'''CREATE TABLE merge_job_events (
        event_id INTEGER PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES merge_jobs(job_id) ON DELETE RESTRICT,
        part_id INTEGER,
        event_type TEXT NOT NULL,
        previous_status TEXT CHECK(previous_status IN ({_STATUS_SQL})),
        new_status TEXT CHECK(new_status IN ({_STATUS_SQL})),
        message TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(job_id, part_id) REFERENCES merge_parts(job_id, part_id) ON DELETE RESTRICT
    )''',
    'CREATE INDEX idx_merge_jobs_status ON merge_jobs(status, updated_at)',
    'CREATE INDEX idx_merge_parts_status ON merge_parts(job_id, status)',
    'CREATE INDEX idx_merge_job_events_job ON merge_job_events(job_id, event_id)',
)


class JobSchemaError(RuntimeError):
    pass


def _configure(conn: sqlite3.Connection) -> None:
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA synchronous = FULL')
    conn.execute('PRAGMA busy_timeout = 5000')


def _validate(conn: sqlite3.Connection) -> None:
    if conn.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID:
        raise JobSchemaError('not a merge-job database')
    if conn.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
        raise JobSchemaError('unsupported merge-job schema version')
    # Compare against canonical definitions, including constraints and indexes.
    actual = {row[0]: row[1] for row in conn.execute(
        "SELECT name, sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'")}
    with closing(sqlite3.connect(':memory:')) as expected:
        for statement in DDL:
            expected.execute(statement)
        canonical = dict(expected.execute(
            "SELECT name, sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"))
    if actual != canonical:
        raise JobSchemaError('unexpected merge-job schema')


def initialize_job_database(database: str | Path) -> None:
    """Create version 1 atomically, or validate an existing job database.

    Parent must exist. Never modifies a nonempty unrelated database.
    """
    path = Path(database).resolve()
    with closing(sqlite3.connect(path, timeout=5)) as conn:
        _configure(conn)
        conn.execute('BEGIN IMMEDIATE')
        try:
            app = conn.execute('PRAGMA application_id').fetchone()[0]
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            objects = conn.execute('SELECT name FROM sqlite_schema').fetchall()
            if app == 0 and version == 0 and not objects:
                for statement in DDL:
                    conn.execute(statement)
                conn.execute(f'PRAGMA application_id = {APPLICATION_ID}')
                conn.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
            _validate(conn)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def connect_job_database(database: str | Path) -> sqlite3.Connection:
    """Open an existing job database; caller must close the connection."""
    uri = Path(database).resolve().as_uri() + '?mode=rw'
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        _configure(conn)
        _validate(conn)
        return conn
    except BaseException:
        conn.close()
        raise
