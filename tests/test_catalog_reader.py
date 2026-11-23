
"""SQLite 캐시 조회 모듈 테스트."""

import tempfile
import unittest
from pathlib import Path

from cctv_archive_manager.catalog import index_recordings
from cctv_archive_manager.catalog_reader import (
    get_cached_files,
    get_catalog_snapshot,
)


class TestCatalogReader(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        root = Path(self.temp.name)

        self.source = root / "source"
        self.source.mkdir()

        self.other_source = root / "other"
        self.other_source.mkdir()

        self.database = root / "catalog.sqlite3"

        filenames = (
            "2026-09-02_00-00-00_000.mkv",
            "2026-09-02_00-10-00_000.mkv",
            "2026-09-03_00-00-00_000.mkv",
        )

        for index, filename in enumerate(filenames):
            (self.source / filename).write_bytes(
                b"x" * ((index + 1) * 100)
            )

        (self.other_source /
         "2026-09-02_00-00-00_000.mkv").write_bytes(
            b"x" * 900
        )

        index_recordings(self.source, self.database)
        index_recordings(self.other_source, self.database)

    def test_snapshot(self):
        result = get_catalog_snapshot(
            self.database,
            self.source,
        )

        self.assertEqual(result.total_files, 3)
        self.assertEqual(result.total_bytes, 600)
        self.assertEqual(len(result.days), 2)
        self.assertIsNotNone(result.last_seen_at)

        self.assertEqual(result.days[0].date, "2026-09-02")
        self.assertEqual(result.days[0].file_count, 2)
        self.assertEqual(result.days[0].total_bytes, 300)

    def test_cached_files_sorted(self):
        files = get_cached_files(
            self.database,
            self.source,
            "2026-09-02",
        )

        self.assertEqual(len(files), 2)
        self.assertEqual(
            files[0].name,
            "2026-09-02_00-00-00_000.mkv",
        )

    def test_sources_are_separated(self):
        result = get_catalog_snapshot(
            self.database,
            self.other_source,
        )

        self.assertEqual(result.total_files, 1)
        self.assertEqual(result.total_bytes, 900)

    def test_cache_does_not_access_originals(self):
        # 원본 파일을 삭제한 후에도
        # SQLite에 기록된 목록은 조회할 수 있어야 한다.
        for file in self.source.glob("*.mkv"):
            file.unlink()

        result = get_catalog_snapshot(
            self.database,
            self.source,
        )

        self.assertEqual(result.total_files, 3)

        files = get_cached_files(
            self.database,
            self.source,
            "2026-09-02",
        )

        self.assertEqual(len(files), 2)
        self.assertFalse(files[0].exists())

    def test_missing_database(self):
        with self.assertRaises(FileNotFoundError):
            get_catalog_snapshot(
                self.source / "missing.sqlite3",
                self.source,
            )


if __name__ == "__main__":
    unittest.main()
