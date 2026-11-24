"""Step 5-9: integrity verification and opt-in validated checkpoint tests."""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from subprocess import CompletedProcess

from cctv_archive_manager.daily_resume import MANIFEST_NAME, ResumeConflict, run_daily_resume
from cctv_archive_manager.merge_planner import MergeGroup
from cctv_archive_manager.output_integrity import (
    OutputIntegrityError, OutputIntegrityReport, inspect_mkv_output,
)


class OutputIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.src = self.root / 'source'
        self.dst = self.root / 'output'
        self.src.mkdir()
        self.dst.mkdir()
        self.inputs = []
        for i in range(2):
            item = self.src / f'2026-09-03_00-0{i}-00_000.mkv'
            item.write_bytes(b'input ' + bytes([i]))
            self.inputs.append(item)
        self.group = MergeGroup('2026-09-03', 1, tuple(self.inputs))

    @staticmethod
    def valid_identification():
        return CompletedProcess([], 0, json.dumps({
            'tracks': [{'type': 'video'}],
            'container': {'properties': {'duration': 1_000_000_000}},
        }), '')

    def stub_probe(self, data=b'valid data'):
        output = self.root / 'video.mkv'
        output.write_bytes(data)
        return output

    def test_identify_and_hash_without_modifying_output(self):
        output = self.stub_probe()
        before = output.stat()
        with patch('cctv_archive_manager.output_integrity._executable', return_value='mkvmerge'), \
             patch('cctv_archive_manager.output_integrity._run', return_value=self.valid_identification()) as runner:
            report = inspect_mkv_output(output)
        self.assertEqual(report.sha256, hashlib.sha256(b'valid data').hexdigest())
        self.assertFalse(report.decoded)
        self.assertEqual(before.st_mtime_ns, output.stat().st_mtime_ns)
        self.assertEqual(runner.call_count, 1)

    def test_invalid_identification_fails_closed(self):
        output = self.stub_probe()
        with patch('cctv_archive_manager.output_integrity._executable', return_value='mkvmerge'), \
             patch('cctv_archive_manager.output_integrity._run', return_value=CompletedProcess([], 0, '{}', '')):
            with self.assertRaises(OutputIntegrityError):
                inspect_mkv_output(output)

    def test_decode_mode_calls_ffmpeg(self):
        output = self.stub_probe()
        with patch('cctv_archive_manager.output_integrity._executable', side_effect=['mkvmerge', 'ffmpeg']), \
             patch('cctv_archive_manager.output_integrity._run', side_effect=[self.valid_identification(), CompletedProcess([], 0, '', '')]) as runner:
            report = inspect_mkv_output(output, decode=True)
        self.assertTrue(report.decoded)
        self.assertIn('-xerror', runner.call_args_list[1].args[0])
        self.assertIn('-nostdin', runner.call_args_list[1].args[0])

    def test_bad_decode_rejected(self):
        output = self.stub_probe()
        with patch('cctv_archive_manager.output_integrity._executable', side_effect=['mkvmerge', 'ffmpeg']), \
             patch('cctv_archive_manager.output_integrity._run', side_effect=[self.valid_identification(), OutputIntegrityError('decode error')]):
            with self.assertRaisesRegex(OutputIntegrityError, 'decode error'):
                inspect_mkv_output(output, decode=True)

    def test_zero_byte_output_rejected_without_external_process(self):
        output = self.stub_probe(b'')
        with patch('cctv_archive_manager.output_integrity._run') as runner:
            with self.assertRaises(OutputIntegrityError):
                inspect_mkv_output(output)
            runner.assert_not_called()

    def merger(self, _group, target, _cancel, _progress):
        target.write_bytes(b'a valid-looking fake output')

    @staticmethod
    def hash_report(target):
        data = target.read_bytes()
        return OutputIntegrityReport(target, len(data), hashlib.sha256(data).hexdigest(), 1, 1_000_000_000, False)

    def test_verified_checkpoint_saved_and_reused(self):
        first = run_daily_resume([self.group], self.dst, merge_one=self.merger,
                                 integrity_verifier=self.hash_report)
        self.assertEqual(len(first.completed), 1)
        journal = json.loads((self.dst / MANIFEST_NAME).read_text())
        self.assertEqual(len(journal['completed'][self.group.output_name]['sha256']), 64)
        second = run_daily_resume([self.group], self.dst, merge_one=lambda *_: self.fail('should skip'),
                                  integrity_verifier=self.hash_report)
        self.assertEqual(second.skipped, (self.group.output_name,))

    def test_same_metadata_modified_bytes_blocks_verified_resume(self):
        run_daily_resume([self.group], self.dst, merge_one=self.merger,
                         integrity_verifier=self.hash_report)
        output = self.dst / self.group.output_name
        original = output.stat()
        with output.open('r+b') as stream:
            stream.seek(0)
            stream.write(b'X')
        os.utime(output, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.assertEqual(output.stat().st_size, original.st_size)
        self.assertEqual(output.stat().st_mtime_ns, original.st_mtime_ns)
        with self.assertRaisesRegex(ResumeConflict, 'digest mismatch'):
            run_daily_resume([self.group], self.dst, merge_one=self.merger,
                             integrity_verifier=self.hash_report)

    def test_unverified_legacy_checkpoint_not_auto_trusted(self):
        run_daily_resume([self.group], self.dst, merge_one=self.merger)
        with self.assertRaisesRegex(ResumeConflict, 'no verified digest'):
            run_daily_resume([self.group], self.dst, merge_one=self.merger,
                             integrity_verifier=self.hash_report)

    def test_failed_verification_never_checkpoints(self):
        with self.assertRaisesRegex(ResumeConflict, 'integrity verification failed'):
            run_daily_resume([self.group], self.dst, merge_one=self.merger,
                             integrity_verifier=lambda p: (_ for _ in ()).throw(OutputIntegrityError('corrupt')))
        journal = json.loads((self.dst / MANIFEST_NAME).read_text())
        self.assertEqual(journal['completed'], {})
        self.assertTrue((self.dst / self.group.output_name).exists())
        with self.assertRaisesRegex(ResumeConflict, 'untracked output'):
            run_daily_resume([self.group], self.dst, merge_one=self.merger)


if __name__ == '__main__':
    unittest.main()
