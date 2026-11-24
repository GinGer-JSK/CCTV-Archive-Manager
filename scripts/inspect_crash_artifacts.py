"""Read-only diagnostic of Step 12's interrupted MKV merge.

No deletion, process termination, SQLite access, or source modifications.
This does NOT authorize cleanup or automatic restart.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

CONTROLLER_LOCK = ".cctv-daily-resume.lock"
MANIFEST_NAME = ".cctv-daily-resume.json"
from cctv_archive_manager.lock_owner_inspector import inspect_lock_owner
from cctv_archive_manager.recovery_inspector import inspect_merge_output


def limited_text(path: Path, limit: int = 4096) -> str:
    if not path.is_file():
        return '<absent>'
    if path.stat().st_size > limit:
        return f'<file exceeds {limit} bytes; not printed>'
    return path.read_text(encoding='utf-8', errors='replace')


def diagnose(root: Path) -> tuple[bool, list[str]]:
    root = root.resolve()
    outdir = root / 'output'
    if not root.is_dir() or not outdir.is_dir():
        raise NotADirectoryError(f'Expected existing test directory with output/: {root}')
    if not root.name.startswith('cctv-step12-real-mkvmerge-'):
        raise ValueError('Refusing to inspect a non-Step12 directory; use only the isolated test folder')

    lines = ['===== STEP 13: READ-ONLY CRASH DIAGNOSIS =====', f'Root: {root}']
    controller = outdir / CONTROLLER_LOCK
    manifest_path = outdir / MANIFEST_NAME
    lines.append(f'Controller lock: {"PRESENT" if controller.is_file() else "ABSENT"}')
    if controller.is_file():
        lines.append('Controller lock metadata:\n' + limited_text(controller).strip())

    lines.append(f'Manifest: {"PRESENT" if manifest_path.is_file() else "ABSENT"}')
    completed = None
    if manifest_path.is_file():
        try:
            data = json.loads(manifest_path.read_text(encoding='utf-8'))
            if isinstance(data, dict) and isinstance(data.get('completed'), dict):
                completed = data['completed']
                lines.append('Completed checkpoint names: ' + repr(sorted(completed)))
                lines.append('Manifest version: ' + repr(data.get('version')))
            else:
                lines.append('Manifest format: UNRECOGNIZED')
        except (OSError, ValueError) as exc:
            lines.append('Manifest format: UNREADABLE (' + str(exc) + ')')

    finals = sorted(p for p in outdir.glob('*.mkv') if not p.name.lower().endswith('.partial.mkv'))
    partials = sorted(outdir.glob('*.partial.mkv'))
    merge_locks = sorted(p for p in outdir.glob('*.mkv.lock') if p.is_file())
    expected = sorted(set([p.name.replace('.partial.mkv', '.mkv') for p in partials]
                          + [p.name[:-5] for p in merge_locks]
                          + [p.name for p in finals]
                          + (list(completed) if completed else [])))
    if not expected:
        expected = ['2026-09-03_part01.mkv']

    safe = True
    for name in expected:
        output = outdir / name
        report = inspect_merge_output(output)
        owner = inspect_lock_owner(output)
        lines.extend([
            f'\nOutput: {name}',
            f'Artifact status: {report.status.value}',
            f'Final bytes: {report.output_bytes}',
            f'Partial bytes: {report.partial_bytes}',
            f'Merge lock: {"PRESENT" if report.lock.is_file() else "ABSENT"}',
            f'Lock-owner status: {owner.status.value}',
            f'Matching process PIDs: {list(owner.matching_pids)}',
            f'Checkpoint completed: {name in (completed or {})}',
        ])
        lines.extend('  Note: ' + note for note in owner.notes)
        if report.status.value != 'clean' or name in (completed or {}):
            safe = False

    if controller.exists() or manifest_path.exists():
        safe = False
    lines.append('\n===== ASSESSMENT =====')
    if safe:
        lines.append('No blocking artifacts seen in snapshot; this is NOT authorization to restart.')
    else:
        lines.append('BLOCKED / MANUAL REVIEW REQUIRED. Do not auto-delete locks, partials, or manifest.')
    lines.append('No files were changed; no processes were terminated.')
    return safe, lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dir', type=Path, required=True, help='Full cctv-step12-real-mkvmerge-* directory')
    args = parser.parse_args()
    _safe, lines = diagnose(args.dir)
    print('\n'.join(lines), flush=True)


if __name__ == '__main__':
    main()
