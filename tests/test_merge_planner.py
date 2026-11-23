
"""날짜별 MKV 병합 그룹 생성 테스트."""

import unittest
from pathlib import Path

from cctv_archive_manager.merge_planner import (
    plan_merge_groups,
)


def make_files(count: int, date: str = "2026-09-02"):
    """10분 간격의 가상 MKV 파일명 생성."""

    from datetime import datetime, timedelta

    start = datetime.strptime(date, "%Y-%m-%d")
    result = []

    for index in range(count):
        timestamp = start + timedelta(minutes=index * 10)

        result.append(
            Path(timestamp.strftime(
                "%Y-%m-%d_%H-%M-%S_000.mkv"
            ))
        )

    return result


class TestMergePlanner(unittest.TestCase):

    def test_one_group(self):
        files = make_files(144)
        groups = plan_merge_groups(files, 1)

        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0].files), 144)
        self.assertEqual(
            groups[0].output_name,
            "2026-09-02_part01.mkv",
        )

    def test_three_groups(self):
        files = make_files(144)
        groups = plan_merge_groups(files, 3)

        self.assertEqual(
            [len(group.files) for group in groups],
            [48, 48, 48],
        )

    def test_uneven_group_sizes(self):
        files = make_files(143)
        groups = plan_merge_groups(files, 3)

        self.assertEqual(
            [len(group.files) for group in groups],
            [48, 48, 47],
        )

    def test_sort_by_recording_time(self):
        files = list(reversed(make_files(4)))
        groups = plan_merge_groups(files, 2)

        self.assertEqual(
            groups[0].files[0].name,
            "2026-09-02_00-00-00_000.mkv",
        )

    def test_reject_mixed_dates(self):
        files = (
            make_files(2, "2026-09-02")
            + make_files(2, "2026-09-03")
        )

        with self.assertRaises(ValueError):
            plan_merge_groups(files, 2)

    def test_reject_too_many_parts(self):
        files = make_files(3)

        with self.assertRaises(ValueError):
            plan_merge_groups(files, 2)

    def test_reject_duplicate_files(self):
        files = make_files(2)

        with self.assertRaises(ValueError):
            plan_merge_groups([files[0], files[0]], 1)


if __name__ == "__main__":
    unittest.main()
