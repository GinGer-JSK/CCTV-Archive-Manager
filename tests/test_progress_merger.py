
"""MKVToolNix 진행률 메시지 해석 테스트."""

import unittest

from cctv_archive_manager.progress_merger import (
    parse_progress,
)


class TestProgressParser(unittest.TestCase):

    def test_zero_percent(self):
        self.assertEqual(
            parse_progress("#GUI#progress 0%"),
            0,
        )

    def test_middle_percent(self):
        self.assertEqual(
            parse_progress("#GUI#progress 57%"),
            57,
        )

    def test_complete_percent(self):
        self.assertEqual(
            parse_progress("#GUI#progress 100%"),
            100,
        )

    def test_invalid_percent(self):
        self.assertIsNone(
            parse_progress("#GUI#progress 101%")
        )

    def test_non_progress_message(self):
        self.assertIsNone(
            parse_progress("#GUI#info Processing")
        )

    def test_line_endings(self):
        self.assertEqual(
            parse_progress("#GUI#progress 25%\r\n"),
            25,
        )


if __name__ == "__main__":
    unittest.main()
