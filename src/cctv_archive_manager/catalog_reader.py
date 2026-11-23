
"""SQLite에 저장된 CCTV 녹화 파일 목록 조회.

이 모듈은 원본 NVR에 접근하지 않고
로컬 데이터베이스의 기존 기록만 읽는다.
"""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CachedDay:
    date: str
    file_count: int
    total_bytes: int


@dataclass(frozen=True)
class CatalogSnapshot:
    source: str
    total_files: int
    total_bytes: int
    last_seen_at: str | None
    days: tuple[CachedDay, ...]


def _source_key(source: str | Path) -> str:
    """기존 catalog.py와 동일한 경로 형식 사용."""
    return str(Path(source).resolve())


def _connect(database: str | Path):
    """SQLite 연결을 조회 전용으로 연다."""
    db_path = Path(database)

    if not db_path.is_file():
        raise FileNotFoundError(
            f"SQLite 데이터베이스가 없습니다: {db_path}"
        )

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA query_only = ON")
    except Exception:
        conn.close()
        raise

    return conn


def get_catalog_snapshot(
    database: str | Path,
    source: str | Path,
) -> CatalogSnapshot:
    """특정 원본 폴더의 날짜별 캐시 현황 반환."""

    source_key = _source_key(source)

    with closing(_connect(database)) as conn:
        total_files, total_bytes, last_seen = conn.execute(
            """
            SELECT
                COUNT(*),
                COALESCE(SUM(size_bytes), 0),
                MAX(last_seen_at)
            FROM recordings
            WHERE source = ?
            """,
            (source_key,),
        ).fetchone()

        rows = conn.execute(
            """
            SELECT
                record_date,
                COUNT(*),
                SUM(size_bytes)
            FROM recordings
            WHERE source = ?
            GROUP BY record_date
            ORDER BY record_date
            """,
            (source_key,),
        ).fetchall()

    days = tuple(
        CachedDay(
            date=date,
            file_count=count,
            total_bytes=size,
        )
        for date, count, size in rows
    )

    return CatalogSnapshot(
        source=source_key,
        total_files=total_files,
        total_bytes=total_bytes,
        last_seen_at=last_seen,
        days=days,
    )


def get_cached_files(
    database: str | Path,
    source: str | Path,
    date: str,
) -> list[Path]:
    """해당 날짜의 MKV 경로를 녹화시간 순서로 반환.

    파일이 현재도 존재하는지는 검사하지 않는다.
    """

    source_key = _source_key(source)

    with closing(_connect(database)) as conn:
        rows = conn.execute(
            """
            SELECT path
            FROM recordings
            WHERE source = ?
              AND record_date = ?
            ORDER BY recorded_at, path
            """,
            (source_key, date),
        ).fetchall()

    return [Path(row[0]) for row in rows]
