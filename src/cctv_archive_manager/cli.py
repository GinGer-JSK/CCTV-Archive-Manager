
"""CCTV Archive Manager 명령줄 실행 모듈."""

import argparse
import sqlite3

from cctv_archive_manager.catalog import index_recordings
from cctv_archive_manager import __version__
from cctv_archive_manager.file_scanner import scan_recordings


def format_gib(size_bytes: int) -> str:
    return f"{size_bytes / (1024 ** 3):,.2f}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="CCTV Archive Manager"
    )

    parser.add_argument(
        "--source",
        type=str,
        help="MKV 원본 폴더 경로",
    )

    parser.add_argument(
    "--index-db",
    type=str,
    help="SQLite 데이터베이스 저장 경로",
    )

    args = parser.parse_args()

    print("=" * 55)
    print("CCTV Archive Manager")
    print(f"Version: {__version__}")
    print("=" * 55)

    if not args.source:
        print("Status: Development environment ready")
        print("사용법: python -m cctv_archive_manager --source 경로")
        return 0

    try:
        result = scan_recordings(args.source)
    except (OSError, ValueError) as error:
        print(f"[ERROR] 파일 검색 실패: {error}")
        return 1

    print(f"\n원본 폴더: {result.source}")
    print(f"전체 MKV: {result.total_mkv:,}개")
    print(f"정상 파일명: {result.valid_mkv:,}개")
    print(f"규칙 불일치: {result.invalid_mkv:,}개")
    print(f"전체 용량: {format_gib(result.total_bytes)} GiB")
    print(f"녹화 날짜: {len(result.days)}일")

    print("\n===== 날짜별 녹화 파일 =====")
    print(f"{'Date':<14} {'Files':>8} {'Size (GiB)':>14}")
    print("-" * 40)

    for day in result.days:
        print(
            f"{day.date:<14} "
            f"{day.file_count:>8,} "
            f"{format_gib(day.total_bytes):>14}"
        )

    print("\n검색 완료. 원본 파일은 변경되지 않았습니다.")

    if args.index_db:
        try:
            indexed = index_recordings(
                args.source,
                args.index_db,
            )
        except (OSError, ValueError, sqlite3.Error) as error:
            print(f"[ERROR] DB 저장 실패: {error}")
            return 1

        print(f"\nSQLite 등록/갱신: {indexed:,}개")
        print(f"DB 저장 경로: {args.index_db}")

    return 0
