
"""CCTV MKV 파일 검색 및 날짜별 집계 모듈."""

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .filename_parser import parse_recording_filename


@dataclass(frozen=True)
class DailySummary:
    date: str
    file_count: int
    total_bytes: int


@dataclass(frozen=True)
class ScanResult:
    source: Path
    total_mkv: int
    valid_mkv: int
    invalid_mkv: int
    total_bytes: int
    days: list[DailySummary]


def scan_recordings(source: str | Path) -> ScanResult:
    """폴더 최상위의 MKV 파일을 읽기 전용으로 조사한다."""

    source = Path(source)

    if not source.is_dir():
        raise NotADirectoryError(
            f"원본 폴더를 찾을 수 없습니다: {source}"
        )

    groups = defaultdict(lambda: [0, 0])

    total_mkv = 0
    valid_mkv = 0
    invalid_mkv = 0
    total_bytes = 0

    for entry in source.iterdir():
        if not entry.is_file():
            continue

        if entry.suffix.lower() != ".mkv":
            continue

        total_mkv += 1

        # 영상 내용은 읽지 않고 파일 크기만 조사
        size = entry.stat().st_size
        total_bytes += size

        info = parse_recording_filename(entry.name)

        if info is None:
            invalid_mkv += 1
            continue

        valid_mkv += 1

        groups[info.date][0] += 1
        groups[info.date][1] += size

    days = [
        DailySummary(
            date=date,
            file_count=values[0],
            total_bytes=values[1],
        )
        for date, values in sorted(groups.items())
    ]

    return ScanResult(
        source=source,
        total_mkv=total_mkv,
        valid_mkv=valid_mkv,
        invalid_mkv=invalid_mkv,
        total_bytes=total_bytes,
        days=days,
    )
