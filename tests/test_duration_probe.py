import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cctv_archive_manager.duration_probe import (
    DurationProbeError,
    duration_ms_from_identification,
    probe_recording_duration,
)


class TestDurationProbe(unittest.TestCase):
    def test_parse_duration_nanoseconds(self):
        payload = {"container": {"properties": {"duration": 600_123_456_789}}}
        self.assertEqual(duration_ms_from_identification(payload), 600_123)

    def test_reject_missing_duration(self):
        with self.assertRaises(DurationProbeError):
            duration_ms_from_identification({"container": {"properties": {}}})

    def test_reject_invalid_duration(self):
        for value in (0, -5, "100", True, 1.5):
            with self.subTest(value=value), self.assertRaises(DurationProbeError):
                duration_ms_from_identification({"container": {"properties": {"duration": value}}})

    def test_probe_success(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video, tool = root / "a.mkv", root / "mkvmerge.exe"
            video.write_bytes(b"stub")
            tool.write_bytes(b"stub")
            payload = {"container": {"properties": {"duration": 3_600_000_000_000}}}
            completed = SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
            with patch("cctv_archive_manager.duration_probe.subprocess.run", return_value=completed) as run:
                result = probe_recording_duration(video, mkvmerge=tool)
            self.assertEqual(result.duration_ms, 3_600_000)
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0], [str(tool), "-J", str(video)])

    def test_probe_failure_preserves_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video, tool = root / "a.mkv", root / "mkvmerge.exe"
            video.write_bytes(b"original")
            tool.write_bytes(b"stub")
            completed = SimpleNamespace(returncode=2, stdout="", stderr="bad mkv")
            with patch("cctv_archive_manager.duration_probe.subprocess.run", return_value=completed):
                with self.assertRaises(DurationProbeError):
                    probe_recording_duration(video, mkvmerge=tool)
            self.assertEqual(video.read_bytes(), b"original")

    def test_missing_source_does_not_launch_tool(self):
        with tempfile.TemporaryDirectory() as temp:
            tool = Path(temp) / "mkvmerge.exe"
            tool.write_bytes(b"stub")
            with patch("cctv_archive_manager.duration_probe.subprocess.run") as run:
                with self.assertRaises(FileNotFoundError):
                    probe_recording_duration(Path(temp) / "missing.mkv", mkvmerge=tool)
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
