import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from cctv_archive_manager.lock_owner_inspector import LockOwnerReport, LockOwnerStatus
from scripts.inspect_crash_artifacts import diagnose

class CrashInspectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cctv-step12-real-mkvmerge-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / 'output'
        self.output.mkdir()

    def test_rejects_other_directories(self):
        other = self.root / 'not-step12'
        other.mkdir(exist_ok=True)
        (other/'output').mkdir(exist_ok=True)
        with self.assertRaises(ValueError):
            diagnose(other)

    @patch('scripts.inspect_crash_artifacts.inspect_lock_owner')
    def test_interrupted_run_is_blocked_and_unchanged(self, owner):
        name = '2026-09-03_part01.mkv'
        (self.output / (name + '.lock')).write_text('pid=123\nstarted_at=2026-11-24T14:00:00Z\n')
        (self.output / '2026-09-03_part01.partial.mkv').write_bytes(b'incomplete')
        (self.output / '.cctv-daily-resume.lock').write_text('pid=123\n')
        (self.output / '.cctv-daily-resume.json').write_text(json.dumps({'version':1,'completed':{}}))
        owner.return_value = LockOwnerReport(self.output/name, self.output/(name+'.lock'), LockOwnerStatus.UNKNOWN, 123, None, (), ('Do not delete',))
        before = {p.name:p.read_bytes() for p in self.output.iterdir()}
        safe, lines = diagnose(self.root)
        self.assertFalse(safe)
        self.assertIn('manual review', '\n'.join(lines).lower())
        self.assertEqual(before, {p.name:p.read_bytes() for p in self.output.iterdir()})

    @patch('scripts.inspect_crash_artifacts.inspect_lock_owner')
    def test_clean_folder_read_only(self, owner):
        owner.return_value = LockOwnerReport(self.output/'2026-09-03_part01.mkv', self.output/'a.lock',LockOwnerStatus.NO_LOCK,None,None,(),())
        safe, _ = diagnose(self.root)
        self.assertTrue(safe)
        self.assertEqual(list(self.output.iterdir()),[])

if __name__ == '__main__':
    unittest.main()
