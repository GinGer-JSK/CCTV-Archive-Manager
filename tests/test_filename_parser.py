
"""MKV 파일명 분석 기능 테스트."""

import unittest

from cctv_archive_manager.filename_parser import (
    parse_recording_filename,
)


class TestFilenameParser(unittest.TestCase):

    def test_valid_filename(self):
        result = parse_recording_filename(
            "2026-09-02_00-06-03_454.mkv"
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.date, "2026-09-02")
        self.assertEqual(result.time, "00:06:03")
        self.assertEqual(result.month, "2026-09")
        self.assertEqual(
            result.recorded_at.microsecond,
            454000,
        )

    def test_invalid_filename(self):
        result = parse_recording_filename(
            "camera_recording.mkv"
        )

        self.assertIsNone(result)

    def test_invalid_date(self):
        result = parse_recording_filename(
            "2026-02-30_12-00-00_123.mkv"
        )

        self.assertIsNone(result)

    def test_invalid_time(self):
        result = parse_recording_filename(
            "2026-09-02_25-00-00_123.mkv"
        )

        self.assertIsNone(result)

    def test_uppercase_extension(self):
        result = parse_recording_filename(
            "2026-09-02_00-06-03_454.MKV"
        )

        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main()
