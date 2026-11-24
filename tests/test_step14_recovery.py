"""Safety-focused unit tests for Step 14 isolated recovery."""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cctv_archive_manager.daily_resume import _signature
from cctv_archive_manager.lock_owner_inspector import ProcessInfo
from cctv_archive_manager.merge_planner import plan_merge_groups

MODULE = Path(__file__).resolve().parents[1] / 'scripts' / 'repair_step12_crash.py'
spec = importlib.util.spec_from_file_location('repair_step12_crash', MODULE)
recovery = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = recovery
spec.loader.exec_module(recovery)


def no_owners():
    return (ProcessInfo(os.getpid(), 'python.exe', None, 'repair_step12_crash.py'),
            ProcessInfo(987654, 'explorer.exe', None, ''))


class Step14RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix='cctv-step12-real-mkvmerge-'))
        (self.temp / 'source').mkdir()
        (self.temp / 'output').mkdir()
        self.sources = []
        for index, start in enumerate(('00-00-00', '00-02-00')):
            source = self.temp / 'source' / f'2026-09-03_{start}_000.mkv'
            source.write_bytes(bytes([index + 7]) * 128)
            self.sources.append(source)
        self.out = self.temp / 'output'
        self.partial = self.out / '2026-09-03_part01.partial.mkv'
        self.partial.write_bytes(b'partial-media' * 11)
        self.merge_lock = self.out / '2026-09-03_part01.mkv.lock'
        self.merge_lock.write_text('pid=1234567\nstarted_at=2026-11-24T00:00:00+00:00\n')
        self.controller = self.out / '.cctv-daily-resume.lock'
        self.controller.write_text('pid=1234567\ncreated_at=2026-11-24T00:00:00+00:00\n')
        self.manifest = self.out / '.cctv-daily-resume.json'
        sig = _signature(plan_merge_groups(self.sources, 1))
        self.manifest.write_text(json.dumps({'version': 1, 'signature': sig, 'completed': {}}))
        self.before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.sources}

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_readonly_preflight(self):
        plan = recovery.preflight(self.temp, snapshot_provider=no_owners)
        self.assertEqual(len(plan.artifacts), 3)
        self.assertTrue(self.partial.exists())
        self.assertTrue(self.controller.exists())

    def test_quarantine_preserves_everything_and_allows_replanning(self):
        plan = recovery.preflight(self.temp, snapshot_provider=no_owners)
        evidence = recovery.quarantine(plan, snapshot_provider=no_owners)
        self.assertEqual(len(list(evidence.glob('*'))), 5)
        self.assertEqual((evidence / self.manifest.name).read_bytes(), self.manifest.read_bytes())
        self.assertTrue((evidence / self.partial.name).is_file())
        self.assertTrue((evidence / self.controller.name).is_file())
        self.assertTrue((evidence / self.merge_lock.name).is_file())
        self.assertTrue(self.manifest.exists())
        self.assertFalse(self.partial.exists())
        self.assertFalse(self.controller.exists())
        self.assertFalse(self.merge_lock.exists())
        ledger = json.loads((evidence / 'evidence-ledger.json').read_text())
        self.assertEqual(len(ledger['files']), 3)
        for path, prior in self.before.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), prior)

    def test_reject_live_owner_pid(self):
        def snapshot():
            return no_owners() + (ProcessInfo(1234567, 'python.exe', None, None),)
        with self.assertRaisesRegex(recovery.RecoveryBlocked, 'PID still exists'):
            recovery.preflight(self.temp, snapshot_provider=snapshot)
        self.assertTrue(self.partial.exists())

    def test_reject_active_mkvmerge_referencing_root(self):
        def snapshot():
            return no_owners() + (ProcessInfo(42, 'mkvmerge.exe', None,
                                'mkvmerge.exe -o "' + str(self.partial) + '"'),)
        with self.assertRaisesRegex(recovery.RecoveryBlocked, 'Active mkvmerge'):
            recovery.preflight(self.temp, snapshot_provider=snapshot)

    def test_allows_only_own_python_launcher_ancestor(self):
        parent_pid = 852001
        def snapshot():
            return (
                ProcessInfo(os.getpid(), 'python.exe', None,
                            'python repair_step12_crash.py --dir TEST', parent_pid=parent_pid),
                ProcessInfo(parent_pid, 'python.exe', None,
                            'python repair_step12_crash.py --dir TEST', parent_pid=987654),
                ProcessInfo(987654, 'powershell.exe', None, 'powershell'),
            )
        self.assertEqual(recovery.preflight(self.temp, snapshot_provider=snapshot).root, self.temp)

    def test_blocks_unrelated_python_same_command(self):
        def snapshot():
            return no_owners() + (ProcessInfo(852002, 'python.exe', None,
                  'python repair_step12_crash.py --dir ' + str(self.temp)),)
        with self.assertRaisesRegex(recovery.RecoveryBlocked, 'Active Python'):
            recovery.preflight(self.temp, snapshot_provider=snapshot)

    def test_blocks_python_launcher_without_parent_relationship(self):
        def snapshot():
            return no_owners() + (ProcessInfo(852003, 'python.exe', None,
                'python repair_step12_crash.py --dir ' + str(self.temp), parent_pid=999),)
        with self.assertRaisesRegex(recovery.RecoveryBlocked, 'Active Python'):
            recovery.preflight(self.temp, snapshot_provider=snapshot)

    def test_reject_nonempty_checkpoint(self):
        doc = json.loads(self.manifest.read_text())
        doc['completed'] = {'2026-09-03_part01.mkv': {'bytes': 1}}
        self.manifest.write_text(json.dumps(doc))
        with self.assertRaisesRegex(recovery.RecoveryBlocked, 'signature or checkpoint'):
            recovery.preflight(self.temp, snapshot_provider=no_owners)

    def test_reject_unrelated_output_and_root(self):
        (self.out / 'surprise.mkv').write_bytes(b'x')
        with self.assertRaisesRegex(recovery.RecoveryBlocked, 'Unexpected output'):
            recovery.preflight(self.temp, snapshot_provider=no_owners)
        with self.assertRaises(recovery.RecoveryBlocked):
            recovery.preflight(self.temp / 'output', snapshot_provider=no_owners)

    def test_reject_unknown_snapshot(self):
        with self.assertRaisesRegex(recovery.RecoveryBlocked, 'snapshot missing'):
            recovery.preflight(self.temp, snapshot_provider=lambda: ())

    def test_reject_source_change_before_archive(self):
        plan = recovery.preflight(self.temp, snapshot_provider=no_owners)
        self.sources[0].write_bytes(b'changed')
        with self.assertRaises(recovery.RecoveryBlocked):
            recovery.quarantine(plan, snapshot_provider=no_owners)
        self.assertTrue(self.partial.exists())


if __name__ == '__main__':
    unittest.main()
