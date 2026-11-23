
"""SQLite 기반 CCTV 녹화 파일 목록 관리."""

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .filename_parser import parse_recording_filename


def index_recordings(
    source: str | Path,
    database: str | Path,
) -> int:
    """원본 MKV의 메타데이터를 SQLite에 기록한다.

    원본 파일의 내용을 수정하거나 이동하지 않는다.
    동일 경로는 새로 삽입하지 않고 정보를 갱신한다.
    """

    source = Path(source).resolve()
    database = Path(database).resolve()

    if not source.is_dir():
        raise NotADirectoryError(
            f"원본 폴더가 없습니다: {source}"
        )

    if database.is_relative_to(source):
        raise ValueError(
            "SQLite DB는 원본 녹화 폴더 밖에 저장해야 합니다."
        )

    database.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    scanned_at = datetime.now(
        timezone.utc
    ).isoformat()

    count = 0

    with closing(sqlite3.connect(database)) as conn:
        with conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS recordings (
                path TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                filename TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                record_date TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                modified_ns INTEGER NOT NULL,
                last_seen_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_recordings_date
            ON recordings(record_date)
        """)

        for entry in source.iterdir():
            if not entry.is_file():
                continue

            if entry.suffix.lower() != ".mkv":
                continue

            info = parse_recording_filename(
                entry.name
            )

            if info is None:
                continue

            stat = entry.stat()

            conn.execute("""
                INSERT INTO recordings (
                    path,
                    source,
                    filename,
                    recorded_at,
                    record_date,
                    size_bytes,
                    modified_ns,
                    last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)

                ON CONFLICT(path) DO UPDATE SET
                    source = excluded.source,
                    filename = excluded.filename,
                    recorded_at = excluded.recorded_at,
                    record_date = excluded.record_date,
                    size_bytes = excluded.size_bytes,
                    modified_ns = excluded.modified_ns,
                    last_seen_at = excluded.last_seen_at
            """, (
                str(entry),
                str(source),
                entry.name,
                info.recorded_at.isoformat(),
                info.date,
                stat.st_size,
                stat.st_mtime_ns,
                scanned_at,
            ))

            count += 1

        conn.commit()

    return count
