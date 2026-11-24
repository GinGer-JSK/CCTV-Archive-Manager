"""Step 14: conservative, opt-in recovery of one Step-12 test crash.

Default: read-only preflight. --apply requires explicit user confirmation.
Never deletes artifacts: moves incomplete output and both locks into an
isolated evidence folder. Keeps manifest and original source MKVs unchanged.
Not a general-purpose production unlocker or automated crash recovery tool.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from cctv_archive_manager.daily_resume import (
    CONTROLLER_LOCK, MANIFEST_NAME, _signature,
)
from cctv_archive_manager.lock_owner_inspector import (
    ProcessInfo, windows_process_snapshot,
)
from cctv_archive_manager.merge_planner import plan_merge_groups

OUTPUT = '2026-09-03_part01.mkv'
APPROVAL = 'ARCHIVE STEP12 ARTIFACTS'


class RecoveryBlocked(RuntimeError):
    """An unsafe or ambiguous recovery situation requires manual review."""


@dataclass(frozen=True)
class RecoveryPlan:
    root: Path
    output_dir: Path
    files: tuple[Path, ...]
    original_states: tuple[tuple[str, int, int], ...]
    artifacts: tuple[Path, ...]
    manifest: Path
    signature: str


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _read_pid(path: Path) -> int:
    try:
        for line in path.read_text(encoding='utf-8').splitlines():
            if line.startswith('pid='):
                pid = int(line[4:].strip())
                if pid > 0:
                    return pid
    except (OSError, ValueError) as exc:
        raise RecoveryBlocked(f'unreadable lock owner PID: {path}: {exc}') from exc
    raise RecoveryBlocked(f'lock owner PID missing: {path}')


def _require_test_root(root: Path) -> Path:
    root = root.resolve(strict=True)
    temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
    # A single, generated Step-12 directory immediately under system temp.
    if (root.parent != temp_root or
            not root.name.startswith('cctv-step12-real-mkvmerge-') or
            root.is_symlink()):
        raise RecoveryBlocked('Refusing non-isolated Step-12 test directory')
    for name in ('source', 'output'):
        target = root / name
        if not target.is_dir() or target.is_symlink() or target.resolve() != target:
            raise RecoveryBlocked(f'Unsafe or missing {name}/ directory')
    return root


def _own_launcher_pids(snapshot: Sequence[ProcessInfo]) -> frozenset[int]:
    """Ignore only our *own* Windows venv Python launcher ancestors.

    Windows venvs may launch an outer python.exe process and a child Python
    interpreter. os.getpid() identifies the child, but the outer launcher can
    share the same recovery command line and must not be treated as a foreign
    worker. All other Python/mkvmerge instances remain guarded.

    If process parentage or the launcher's identity is ambiguous, fail closed.
    """
    processes = {proc.pid: proc for proc in snapshot}
    current = processes.get(os.getpid())
    if current is None:
        raise RecoveryBlocked('Current Python process absent from process snapshot')
    allowed = {os.getpid()}
    visited = set(allowed)
    parent = current.parent_pid
    script_name = Path(__file__).name.casefold()
    while parent is not None and parent not in visited:
        visited.add(parent)
        proc = processes.get(parent)
        if proc is None:
            break
        name = Path(proc.name).name.casefold()
        cmd = (proc.command_line or '').casefold()
        if name not in ('python.exe', 'pythonw.exe', 'py.exe'):
            break
        # Only the launcher of *this* recovery invocation can be excluded.
        if script_name not in cmd:
            break
        allowed.add(proc.pid)
        parent = proc.parent_pid
    return frozenset(allowed)


def _process_guard(root: Path, locks: Sequence[Path],
                   snapshot: Sequence[ProcessInfo]) -> None:
    """Fail closed if an owner may be alive or process inspection is incomplete."""
    if not isinstance(snapshot, (list, tuple)) or not snapshot:
        raise RecoveryBlocked('Process snapshot missing or empty')
    own_pids = _own_launcher_pids(snapshot)
    pids = {_read_pid(item) for item in locks}
    active = {p.pid for p in snapshot}
    if pids & active:
        raise RecoveryBlocked(f'Lock PID still exists: {sorted(pids & active)}')
    marker = str(root).casefold().replace('/', '\\')
    output_marker = str(root / 'output').casefold().replace('/', '\\')
    for proc in snapshot:
        name = Path(proc.name).name.casefold()
        cmd = proc.command_line
        if name == 'mkvmerge.exe':
            if not cmd:
                raise RecoveryBlocked('mkvmerge command line is unavailable')
            if marker in cmd.casefold().replace('/', '\\'):
                raise RecoveryBlocked(f'Active mkvmerge references test files: PID {proc.pid}')
        if name in ('python.exe', 'pythonw.exe', 'py.exe') and proc.pid not in own_pids:
            if not cmd:
                raise RecoveryBlocked('A Python process command line is unavailable')
            if output_marker in cmd.casefold().replace('/', '\\') or marker in cmd.casefold().replace('/', '\\'):
                raise RecoveryBlocked(f'Active Python process references test files: PID {proc.pid}')


def preflight(root: Path, *,
              snapshot_provider: Callable[[], Sequence[ProcessInfo]] = windows_process_snapshot,
              ) -> RecoveryPlan:
    root = _require_test_root(root)
    src = root / 'source'
    out = root / 'output'
    sources = tuple(sorted(src.glob('*.mkv')))
    if len(sources) != 2 or any(p.is_symlink() or not p.is_file() for p in sources):
        raise RecoveryBlocked('Expected exactly two regular synthetic MKV sources')
    groups = plan_merge_groups(sources, 1)
    if len(groups) != 1 or groups[0].output_name != OUTPUT:
        raise RecoveryBlocked('Unexpected Step-12 group plan')
    signature = _signature(groups)
    manifest = out / MANIFEST_NAME
    controller = out / CONTROLLER_LOCK
    output_lock = out / (OUTPUT + '.lock')
    partial = out / OUTPUT.replace('.mkv', '.partial.mkv')
    final = out / OUTPUT
    artifacts = (partial, output_lock, controller)
    if final.exists():
        raise RecoveryBlocked('A final output exists; do not reset')
    if any(not p.is_file() or p.is_symlink() for p in artifacts):
        raise RecoveryBlocked('Expected partial and both regular lock files')
    if not manifest.is_file() or manifest.is_symlink():
        raise RecoveryBlocked('Missing or unsafe manifest')
    if partial.stat().st_size <= 0:
        raise RecoveryBlocked('Partial is empty; unexpected test state')
    expected_files = {MANIFEST_NAME, CONTROLLER_LOCK, OUTPUT + '.lock', partial.name}
    actual_files = {p.name for p in out.iterdir()}
    if actual_files != expected_files or any(p.is_symlink() for p in out.iterdir()):
        raise RecoveryBlocked(f'Unexpected output artifacts: {sorted(actual_files ^ expected_files)}')
    try:
        doc = json.loads(manifest.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise RecoveryBlocked(f'Manifest cannot be read: {exc}') from exc
    if doc.get('version') != 1 or doc.get('completed') != {} or doc.get('signature') != signature:
        raise RecoveryBlocked('Manifest signature or checkpoint state does not match')
    try:
        snapshot = tuple(snapshot_provider())
    except Exception as exc:
        raise RecoveryBlocked(f'Process inspection unavailable: {exc}') from exc
    _process_guard(root, artifacts[1:], snapshot)
    states = tuple((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in sources)
    return RecoveryPlan(root, out, sources, states, artifacts, manifest, signature)


def quarantine(plan: RecoveryPlan, *,
               snapshot_provider: Callable[[], Sequence[ProcessInfo]] = windows_process_snapshot,
               ) -> Path:
    """After approval only: move artifacts into a new evidence folder, no delete."""
    fresh = preflight(plan.root, snapshot_provider=snapshot_provider)
    if fresh.original_states != plan.original_states or fresh.signature != plan.signature:
        raise RecoveryBlocked('Sources changed after inspection')
    if fresh.artifacts != plan.artifacts:
        raise RecoveryBlocked('Artifact paths changed after inspection')
    evidence = plan.root / ('recovery-evidence-' + uuid.uuid4().hex[:12])
    evidence.mkdir(mode=0o700)
    moved: list[tuple[Path, Path]] = []
    try:
        # Controller lock moved last; incomplete output never becomes 'completed'.
        for item in plan.artifacts:
            target = evidence / item.name
            os.replace(item, target)
            moved.append((item, target))
        if any(p.is_file() for p in plan.artifacts):
            raise RecoveryBlocked('Some blocked artifacts are still present')
        if any((p.stat().st_size, p.stat().st_mtime_ns) != (sz, mt)
               for p, (_, sz, mt) in zip(plan.files, plan.original_states)):
            raise RecoveryBlocked('A source changed while quarantining')
        if json.loads(plan.manifest.read_text(encoding='utf-8')).get('completed') != {}:
            raise RecoveryBlocked('Checkpoint changed during recovery')
        # Preserve an evidentiary copy of the unchanged checkpoint as well.
        manifest_copy = evidence / plan.manifest.name
        shutil.copy2(plan.manifest, manifest_copy)
        if _digest(manifest_copy) != _digest(plan.manifest):
            raise RecoveryBlocked('Manifest evidence copy failed checksum comparison')
        ledger = {
            'status': 'evidence_archived_no_merge_executed',
            'original_output': str(plan.output_dir),
            'manifest_sha256': _digest(manifest_copy),
            'files': [{'name': old.name, 'bytes': new.stat().st_size, 'sha256': _digest(new)}
                      for old, new in moved],
        }
        (evidence / 'evidence-ledger.json').write_text(
            json.dumps(ledger, ensure_ascii=False, indent=2), encoding='utf-8')
    except BaseException:
        # Best-effort restoration if the archive operation fails.
        for original, archived in reversed(moved):
            if archived.exists() and not original.exists():
                os.replace(archived, original)
        raise
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dir', type=Path, required=True)
    parser.add_argument('--apply', action='store_true', help='archive partial and lock files (only after approval)')
    parser.add_argument('--confirm', default='', help=f'must equal {APPROVAL!r} for --apply')
    args = parser.parse_args()
    if args.apply and (sys.platform != 'win32' or args.confirm != APPROVAL):
        raise RecoveryBlocked('Only Windows with explicit --confirm is allowed to apply')
    plan = preflight(args.dir)
    print('===== STEP 14: SAFE RECOVERY PREFLIGHT =====')
    print('Root:', plan.root)
    print('Sources:', *(p.name for p in plan.files), sep='\n - ')
    print('Manifest completed: 0; matching signature: YES')
    print('Active lock owners: NOT SEEN IN PROCESS SNAPSHOT')
    print('Artifacts to preserve:', *(f'{p.name} ({p.stat().st_size:,} bytes)' for p in plan.artifacts), sep='\n - ')
    print('No source or manifest modifications are planned.')
    if not args.apply:
        print('PREVIEW PASS — READ ONLY. No changes made.')
        return 0
    evidence = quarantine(plan)
    print('ARCHIVE PASS:', evidence)
    print('Checkpoint manifest retained, no outputs created, originals unchanged.')
    print('RECOVERY PREPARATION: PASS. Resume is NOT auto-started.')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f'RECOVERY BLOCKED: {type(exc).__name__}: {exc}', file=sys.stderr)
        raise SystemExit(1)
