import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cctv_archive_manager.lock_owner_inspector import (
    LockOwnerStatus, ProcessInfo, inspect_lock_owner,
)


class LockOwnerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.output = Path(tmp.name) / "2026-09-02_part01.mkv"
        self.lock = self.output.with_name(self.output.name + ".lock")
        self.started = datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc)

    def make_lock(self):
        self.lock.write_text(
            f"pid=1234\nstarted_at={self.started.isoformat()}\noutput={self.output}\n",
            encoding="utf-8",
        )

    def test_no_lock_does_not_query_processes(self):
        report = inspect_lock_owner(self.output, lambda: self.fail("queried"))
        self.assertEqual(report.status, LockOwnerStatus.NO_LOCK)

    def test_matching_owner_is_only_possibly_active(self):
        self.make_lock()
        processes = [ProcessInfo(1234, "python.exe", self.started-timedelta(minutes=2), "python app.py")]
        report = inspect_lock_owner(self.output, lambda: processes)
        self.assertEqual(report.status, LockOwnerStatus.OWNER_POSSIBLY_ACTIVE)
        self.assertEqual(report.matching_pids, (1234,))
        self.assertTrue(self.lock.exists())

    def test_reused_pid_does_not_count_as_owner(self):
        self.make_lock()
        processes = [ProcessInfo(1234, "python.exe", self.started+timedelta(minutes=3), "python other.py")]
        report = inspect_lock_owner(self.output, lambda: processes)
        self.assertEqual(report.status, LockOwnerStatus.NO_MATCH_FOUND)
        self.assertTrue(self.lock.exists())

    def test_orphan_mkvmerge_referencing_partial_is_possible_active(self):
        self.make_lock()
        partial = self.output.with_name(self.output.stem + ".partial.mkv")
        process = ProcessInfo(555, "mkvmerge.exe", self.started, f'mkvmerge --output "{partial}" file.mkv')
        report = inspect_lock_owner(self.output, lambda: [process])
        self.assertEqual(report.status, LockOwnerStatus.MERGER_POSSIBLY_ACTIVE)
        self.assertEqual(report.matching_pids, (555,))

    def test_missing_metadata_is_unknown(self):
        self.lock.write_text("pid=bad\nstarted_at=nope\n", encoding="utf-8")
        report = inspect_lock_owner(self.output, lambda: [])
        self.assertEqual(report.status, LockOwnerStatus.UNKNOWN)

    def test_lookup_failure_is_unknown_and_preserves_lock(self):
        self.make_lock()
        def unavailable():
            raise OSError("CIM permission denied")
        report = inspect_lock_owner(self.output, unavailable)
        self.assertEqual(report.status, LockOwnerStatus.UNKNOWN)
        self.assertTrue(self.lock.exists())

    def test_no_confirmed_match_does_not_delete_lock(self):
        self.make_lock()
        report = inspect_lock_owner(self.output, lambda: [])
        self.assertEqual(report.status, LockOwnerStatus.NO_MATCH_FOUND)
        self.assertTrue(self.lock.exists())


if __name__ == "__main__":
    unittest.main()
