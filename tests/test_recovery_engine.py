"""Fail-closed recovery engine regression tests (no actual NVR required)."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from cctv_archive_manager.daily_resume import _signature, CONTROLLER_LOCK, MANIFEST_NAME
from cctv_archive_manager.lock_owner_inspector import ProcessInfo
from cctv_archive_manager.merge_planner import plan_merge_groups
from cctv_archive_manager.recovery_engine import RecoveryBlocked, preflight, quarantine


def snapshot():
    return (ProcessInfo(os.getpid(), 'python.exe', None, 'unittest'),
            ProcessInfo(90001, 'explorer.exe', None, 'explorer.exe'))


class RecoveryEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='step16-general-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.src = self.root / 'source'
        self.out = self.root / 'output'
        self.src.mkdir()
        self.out.mkdir()
        self.inputs = []
        for i in range(4):
            p = self.src / f'2026-09-03_00-{i:02d}-00_000.mkv'
            p.write_bytes(bytes([i + 5]) * 55)
            self.inputs.append(p)
        self.groups = plan_merge_groups(self.inputs, 2)
        self.controller = self.out / CONTROLLER_LOCK
        self.controller.write_text('pid=989898\ncreated_at=2026-11-24T00:00:00+00:00\n')
        self.first = self.out / self.groups[0].output_name
        self.first.write_bytes(b'valid completed output')
        stat = self.first.stat()
        self.manifest = self.out / MANIFEST_NAME
        self.manifest.write_text(json.dumps({
            'version': 1, 'signature': _signature(self.groups),
            'completed': {self.first.name: {
                'bytes': stat.st_size, 'modified_ns': stat.st_mtime_ns,
                'sha256': hashlib.sha256(self.first.read_bytes()).hexdigest(),
            }},
        }))
        name = self.groups[1].output_name
        self.partial = self.out / (name[:-4] + '.partial.mkv')
        self.partial.write_bytes(b'incomplete video' * 5)
        self.lock = self.out / (name + '.lock')
        self.lock.write_text('pid=989898\nstarted_at=2026-11-24T00:00:00+00:00\n')

    def preflight(self):
        return preflight(self.groups, self.out, snapshot_provider=snapshot)

    def test_readonly_inspection(self):
        before = {p: p.read_bytes() for p in (self.controller, self.partial, self.lock, self.first, self.manifest)}
        plan = self.preflight()
        self.assertEqual(plan.completed, (self.first.name,))
        self.assertEqual(len(plan.archives), 3)
        self.assertTrue(all(p.read_bytes() == before[p] for p in before))

    def test_archive_preserves_completed_and_sources(self):
        originals = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.inputs}
        recorded = self.first.read_bytes()
        ledger = self.manifest.read_bytes()
        plan = self.preflight()
        evidence = quarantine(plan, self.groups, snapshot_provider=snapshot)
        self.assertEqual(evidence.parent, self.root)
        self.assertTrue((evidence / 'evidence-ledger.json').is_file())
        for p in (self.controller, self.partial, self.lock):
            self.assertFalse(p.exists())
            self.assertTrue((evidence / p.name).is_file())
        self.assertEqual(self.first.read_bytes(), recorded)
        self.assertEqual(self.manifest.read_bytes(), ledger)
        for path, old in originals.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), old)

    def test_blocks_active_owner_pid(self):
        with self.assertRaisesRegex(RecoveryBlocked, 'PID'):
            preflight(self.groups, self.out, snapshot_provider=lambda: snapshot() +
                      (ProcessInfo(989898, 'python.exe', None, 'python merge.py'),))

    def test_blocks_live_merger(self):
        with self.assertRaisesRegex(RecoveryBlocked, '프로세스'):
            preflight(self.groups, self.out, snapshot_provider=lambda: snapshot() +
                      (ProcessInfo(42, 'mkvmerge.exe', None, 'mkvmerge -o ' + str(self.partial)),))

    def test_blocks_process_snapshot_failure(self):
        with self.assertRaises(RecoveryBlocked):
            preflight(self.groups, self.out, snapshot_provider=lambda: ())

    def test_blocks_missing_owner_pid(self):
        self.lock.write_text('broken')
        with self.assertRaisesRegex(RecoveryBlocked, 'PID'):
            self.preflight()

    def test_blocks_changed_plan(self):
        self.inputs[0].write_bytes(b'changed')
        with self.assertRaisesRegex(RecoveryBlocked, '서명'):
            self.preflight()

    def test_blocks_changed_completed_sha(self):
        self.first.write_bytes(b'changed output')
        with self.assertRaisesRegex(RecoveryBlocked, 'SHA-256'):
            self.preflight()

    def test_blocks_untracked_output(self):
        (self.out / 'surprise.mkv').write_bytes(b'whoops')
        with self.assertRaises(RecoveryBlocked):
            self.preflight()

    def test_blocks_untracked_final_for_pending(self):
        (self.out / self.groups[1].output_name).write_bytes(b'unknown final')
        with self.assertRaisesRegex(RecoveryBlocked, '완료 기록 없는'):
            self.preflight()

    def test_blocks_unknown_staging(self):
        (self.out / (MANIFEST_NAME + '.tmp')).write_text('stale')
        with self.assertRaisesRegex(RecoveryBlocked, '임시 파일'):
            self.preflight()

    def test_blocks_changed_artifact_after_preview(self):
        plan = self.preflight()
        self.partial.write_bytes(b'changed!')
        with self.assertRaises(RecoveryBlocked):
            quarantine(plan, self.groups, snapshot_provider=snapshot)
        self.assertTrue(self.controller.exists())

    def test_blocks_no_controller(self):
        self.controller.unlink()
        with self.assertRaisesRegex(RecoveryBlocked, '일괄 작업 잠금'):
            self.preflight()

    def test_blocks_no_incomplete(self):
        self.partial.unlink()
        self.lock.unlink()
        with self.assertRaisesRegex(RecoveryBlocked, '정리 대상'):
            self.preflight()

    def test_blocks_completed_output_with_partial(self):
        (self.out / (self.first.stem + '.partial.mkv')).write_bytes(b'problem')
        with self.assertRaisesRegex(RecoveryBlocked, '완료된 출력'):
            self.preflight()

    def test_blocks_output_inside_source(self):
        nested = self.src / 'output'
        nested.mkdir()
        with self.assertRaises(RecoveryBlocked):
            preflight(self.groups, nested, snapshot_provider=snapshot)

    def test_blocks_symlink_artifact(self):
        try:
            self.partial.unlink()
            self.partial.symlink_to(self.inputs[0])
        except (OSError, NotImplementedError):
            self.skipTest('symlink creation not allowed')
        with self.assertRaises(RecoveryBlocked):
            self.preflight()


if __name__ == '__main__':
    unittest.main()
