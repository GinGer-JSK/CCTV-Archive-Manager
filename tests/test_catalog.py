
"""SQLite 파일 목록 관리 테스트."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing

from cctv_archive_manager.catalog import (
    index_recordings,
)


class TestCatalog(unittest.TestCase):

    def test_index_and_update(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            source = root / "recordings"
            source.mkdir()

            database = root / "catalog.sqlite3"

            file1 = source / (
                "2026-09-02_00-00-00_000.mkv"
            )
            file2 = source / (
                "2026-09-02_00-10-00_000.mkv"
            )

            file1.write_bytes(b"A" * 100)
            file2.write_bytes(b"B" * 200)

            # 첫 번째 등록
            count = index_recordings(
                source,
                database,
            )
            self.assertEqual(count, 2)

            # 두 번째 등록: 중복 행이 없어야 함
            count = index_recordings(
                source,
                database,
            )
            self.assertEqual(count, 2)

            with closing(sqlite3.connect(database)) as conn:
                rows = conn.execute(
                    "SELECT COUNT(*) FROM recordings"
                ).fetchone()[0]

            self.assertEqual(rows, 2)

            # 파일 크기 변경 시 DB 갱신 확인
            file1.write_bytes(b"C" * 300)

            index_recordings(source, database)

            with closing(sqlite3.connect(database)) as conn:
                size = conn.execute(
                    """
                    SELECT size_bytes
                    FROM recordings
                    WHERE filename = ?
                    """,
                    (file1.name,),
                ).fetchone()[0]

            self.assertEqual(size, 300)

    def test_reject_database_inside_source(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)

            with self.assertRaises(ValueError):
                index_recordings(
                    source,
                    source / "catalog.sqlite3",
                )


if __name__ == "__main__":
    unittest.main()
