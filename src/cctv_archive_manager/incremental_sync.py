
"""SMB MKV 파일 증분 동기화.

기존 SQLite 기록은 재검사하지 않고,
새로 발견한 MKV 파일의 메타데이터만 수집한다.

원본 영상은 수정하거나 삭제하지 않는다.
"""

import os
import sqlite3
import time

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .filename_parser import parse_recording_filename


@dataclass(frozen=True)
class SyncResult:
    total_mkv: int
    already_indexed: int
    added: int
    skipped_recent: int
    invalid_names: int
    missing_from_source: int
    metadata_errors: int
    elapsed_seconds: float


def sync_new_recordings(
    source: str | Path,
    database: str | Path,
    minimum_age_seconds: int = 900,
) -> SyncResult:
    """SMB에서 신규 MKV만 SQLite에 추가한다.

    기존 DB 행의 크기·수정 시각은 재검증하지 않는다.
    원본에서 사라진 파일의 DB 행도 삭제하지 않는다.
    """

    started = time.perf_counter()

    source = Path(source).resolve()
    database = Path(database).resolve()

    if not source.is_dir():
        raise NotADirectoryError(
            f"원본 폴더에 접근할 수 없습니다: {source}"
        )

    if not database.is_file():
        raise FileNotFoundError(
            f"기존 SQLite DB가 없습니다: {database}"
        )

    if database.is_relative_to(source):
        raise ValueError(
            "SQLite DB는 원본 폴더 밖에 있어야 합니다."
        )

    if minimum_age_seconds < 0:
        raise ValueError(
            "최소 파일 보존 대기시간은 0 이상이어야 합니다."
        )

    source_key = str(source)
    now = time.time()
    scanned_at = datetime.now(timezone.utc).isoformat()

    total_mkv = 0
    already_indexed = 0
    added = 0
    skipped_recent = 0
    invalid_names = 0
    metadata_errors = 0

    with closing(sqlite3.connect(database)) as conn:
        # 해당 원본 폴더에 등록된 파일 경로만 로컬 DB에서 조회
        existing = {
            row[0]
            for row in conn.execute(
                "SELECT path FROM recordings WHERE source = ?",
                (source_key,),
            )
        }

        seen = set()

        # SMB 전체 파일명만 조사
        # 기존 파일에는 stat()을 호출하지 않는다.
        with os.scandir(source) as entries:
            with conn:
                for entry in entries:
                    if not entry.name.lower().endswith(".mkv"):
                        continue

                    total_mkv += 1

                    path = entry.path
                    seen.add(path)

                    if path in existing:
                        already_indexed += 1
                        continue

                    info = parse_recording_filename(entry.name)

                    if info is None:
                        invalid_names += 1
                        continue

                    try:
                        if not entry.is_file():
                            invalid_names += 1
                            continue

                        stat = entry.stat()

                    except OSError:
                        metadata_errors += 1
                        continue

                    # 수정된 지 얼마 지나지 않은 파일은 등록 보류
                    if now - stat.st_mtime < minimum_age_seconds:
                        skipped_recent += 1
                        continue

                    conn.execute(
                        """
                        INSERT OR IGNORE INTO recordings (
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
                        """,
                        (
                            path,
                            source_key,
                            entry.name,
                            info.recorded_at.isoformat(),
                            info.date,
                            stat.st_size,
                            stat.st_mtime_ns,
                            scanned_at,
                        ),
                    )

                    added += 1

    missing_from_source = len(existing - seen)

    return SyncResult(
        total_mkv=total_mkv,
        already_indexed=already_indexed,
        added=added,
        skipped_recent=skipped_recent,
        invalid_names=invalid_names,
        missing_from_source=missing_from_source,
        metadata_errors=metadata_errors,
        elapsed_seconds=time.perf_counter() - started,
    )
