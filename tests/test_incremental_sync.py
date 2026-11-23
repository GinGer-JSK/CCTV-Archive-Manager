
"""SMB MKV 증분 동기화 테스트."""

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from cctv_archive_manager.catalog import index_recordings
from cctv_archive_manager.incremental_sync import (
    sync_new_recordings,
)


class TestIncrementalSync(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        root = Path(self.temp.name)

        self.source = root / "source"
        self.source.mkdir()

        self.database = root / "catalog.sqlite3"

        self.first = self.source / (
            "2026-09-02_00-00-00_000.mkv"
        )
        self.first.write_bytes(b"A" * 100)

        # 기존 SQLite 카탈로그 준비
        index_recordings(self.source, self.database)

    def db_count(self):
        with closing(sqlite3.connect(self.database)) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM recordings"
            ).fetchone()[0]

    def test_add_new_file(self):
        second = self.source / (
            "2026-09-02_00-10-00_000.mkv"
        )
        second.write_bytes(b"B" * 200)

        result = sync_new_recordings(
            self.source,
            self.database,
            minimum_age_seconds=0,
        )

        self.assertEqual(result.total_mkv, 2)
        self.assertEqual(result.already_indexed, 1)
        self.assertEqual(result.added, 1)
        self.assertEqual(self.db_count(), 2)

    def test_existing_file_is_not_updated(self):
        self.first.write_bytes(b"C" * 300)

        result = sync_new_recordings(
            self.source,
            self.database,
            minimum_age_seconds=0,
        )

        self.assertEqual(result.already_indexed, 1)
        self.assertEqual(result.added, 0)

        with closing(sqlite3.connect(self.database)) as conn:
            size = conn.execute(
                """
                SELECT size_bytes
                FROM recordings
                WHERE path = ?
                """,
                (str(self.first),),
            ).fetchone()[0]

        # 기존 기록 100바이트를 그대로 유지
        self.assertEqual(size, 100)

    def test_missing_file_is_not_deleted(self):
        self.first.unlink()

        result = sync_new_recordings(
            self.source,
            self.database,
            minimum_age_seconds=0,
        )

        self.assertEqual(result.missing_from_source, 1)
        self.assertEqual(self.db_count(), 1)

    def test_recent_file_is_skipped(self):
        second = self.source / (
            "2026-09-02_00-10-00_000.mkv"
        )
        second.write_bytes(b"B" * 200)

        result = sync_new_recordings(
            self.source,
            self.database,
            minimum_age_seconds=900,
        )

        self.assertEqual(result.skipped_recent, 1)
        self.assertEqual(result.added, 0)
        self.assertEqual(self.db_count(), 1)

    def test_invalid_filename_not_indexed(self):
        bad = self.source / "invalid.mkv"
        bad.write_bytes(b"bad")

        result = sync_new_recordings(
            self.source,
            self.database,
            minimum_age_seconds=0,
        )

        self.assertEqual(result.invalid_names, 1)
        self.assertEqual(result.added, 0)


if __name__ == "__main__":
    unittest.main()
