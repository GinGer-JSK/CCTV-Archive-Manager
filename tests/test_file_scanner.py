
"""MKV 파일 검색 및 날짜별 분류 테스트."""

import tempfile
import unittest
from pathlib import Path

from cctv_archive_manager.file_scanner import scan_recordings


class TestFileScanner(unittest.TestCase):

    def test_daily_grouping(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            files = {
                "2026-09-01_00-00-00_000.mkv": 100,
                "2026-09-01_00-10-00_000.mkv": 200,
                "2026-09-02_00-00-00_000.mkv": 300,
                "invalid.mkv": 50,
            }

            for name, size in files.items():
                (root / name).write_bytes(b"x" * size)

            # MKV가 아닌 파일과 하위 폴더는 제외
            (root / "README.txt").write_text("test")
            (root / "previews").mkdir()
            (root / "previews" / "other.mkv").write_bytes(b"x")

            result = scan_recordings(root)

            self.assertEqual(result.total_mkv, 4)
            self.assertEqual(result.valid_mkv, 3)
            self.assertEqual(result.invalid_mkv, 1)
            self.assertEqual(result.total_bytes, 650)
            self.assertEqual(len(result.days), 2)

            self.assertEqual(result.days[0].date, "2026-09-01")
            self.assertEqual(result.days[0].file_count, 2)
            self.assertEqual(result.days[0].total_bytes, 300)

            self.assertEqual(result.days[1].date, "2026-09-02")
            self.assertEqual(result.days[1].file_count, 1)

    def test_missing_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing"

            with self.assertRaises(NotADirectoryError):
                scan_recordings(missing)


if __name__ == "__main__":
    unittest.main()
