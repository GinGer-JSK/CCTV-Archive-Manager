
"""MKV 병합 엔진 안전성 회귀 테스트."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cctv_archive_manager.merger import (
    merge_recordings,
    prepare_merge,
)


class TestMergeSafety(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        root = Path(self.temp.name)

        self.source = root / "source"
        self.output = root / "output"

        self.source.mkdir()
        self.output.mkdir()

        self.first = self.source / (
            "2026-09-02_00-00-00_000.mkv"
        )
        self.second = self.source / (
            "2026-09-02_00-10-00_000.mkv"
        )

        self.first.write_bytes(b"FIRST-ORIGINAL")
        self.second.write_bytes(b"SECOND-ORIGINAL")

        self.destination = (
            self.output / "2026-09-02_part01.mkv"
        )

        # 실제 MKVToolNix 설치 여부에 관계없이
        # 입력·출력 안전성 검사를 수행하기 위한 파일
        self.fake_mkvmerge = root / "mkvmerge.exe"
        self.fake_mkvmerge.write_bytes(b"FAKE")

    def test_existing_output_is_protected(self):
        """기존 결과물을 덮어쓰지 않는다."""

        self.destination.write_bytes(b"EXISTING")

        with self.assertRaises(FileExistsError):
            prepare_merge(
                [self.first, self.second],
                self.destination,
                self.fake_mkvmerge,
            )

        self.assertEqual(
            self.destination.read_bytes(),
            b"EXISTING",
        )

    def test_missing_source_is_rejected(self):
        """원본이 사라졌으면 병합을 거부한다."""

        self.second.unlink()

        with self.assertRaises(FileNotFoundError):
            prepare_merge(
                [self.first, self.second],
                self.destination,
                self.fake_mkvmerge,
            )

        self.assertFalse(self.destination.exists())

    def test_output_inside_source_is_rejected(self):
        """원본 폴더 안에 결과물을 저장하지 않는다."""

        destination = (
            self.source / "2026-09-02_part01.mkv"
        )

        with self.assertRaises(ValueError):
            prepare_merge(
                [self.first, self.second],
                destination,
                self.fake_mkvmerge,
            )

        self.assertFalse(destination.exists())

    def test_existing_partial_is_protected(self):
        """미완료 임시 파일을 덮어쓰지 않는다."""

        partial = self.destination.with_name(
            self.destination.stem + ".partial.mkv"
        )

        partial.write_bytes(b"KEEP-PARTIAL")

        with patch(
            "cctv_archive_manager.merger.subprocess.run"
        ) as mocked_run:

            with self.assertRaises(FileExistsError):
                merge_recordings(
                    [self.first, self.second],
                    self.destination,
                    self.fake_mkvmerge,
                )

            mocked_run.assert_not_called()

        self.assertEqual(
            partial.read_bytes(),
            b"KEEP-PARTIAL",
        )

    def test_sources_remain_unchanged(self):
        """입력 검증 시 원본 데이터는 변경되지 않는다."""

        original_first = self.first.read_bytes()
        original_second = self.second.read_bytes()

        prepare_merge(
            [self.first, self.second],
            self.destination,
            self.fake_mkvmerge,
        )

        self.assertEqual(
            self.first.read_bytes(),
            original_first,
        )
        self.assertEqual(
            self.second.read_bytes(),
            original_second,
        )


if __name__ == "__main__":
    unittest.main()
