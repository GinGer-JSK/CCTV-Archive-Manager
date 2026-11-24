"""Conservative, opt-in, resumable multi-day MKV batch execution.

Does not read/decode the source video; only stat() metadata. An output is
skippable ONLY if this manager recorded a successful merge and the recorded
size/mtime still match. This is NOT a full media integrity verification.

Never removes pre-existing files, locks or partial outputs. The controller
lock is intentionally left on abnormal process termination for manual review.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from .merge_planner import MergeGroup, plan_merge_groups

MANIFEST_NAME = '.cctv-daily-resume.json'
CONTROLLER_LOCK = '.cctv-daily-resume.lock'


@dataclass(frozen=True)
class DailyResumeResult:
    completed: tuple[str, ...]
    skipped: tuple[str, ...]
    total: int
    cancelled: bool


class ResumeConflict(RuntimeError):
    """Unexpected artifact, changed plan, or invalid prior completion."""


def plan_cached_days(database: str | Path, source: str | Path,
                     dates: Sequence[str], parts: int, *,
                     mode: str = 'count') -> tuple[MergeGroup, ...]:
    """Plan only explicitly named dates (no automatic full-catalog execution).

    Duration mode may update the local SQLite cache; count mode reads it only.
    Neither mode executes a merge.
    """
    from .catalog_reader import get_cached_files
    if mode not in ('count', 'duration'):
        raise ValueError('mode must be count or duration')
    if not dates or len(dates) != len(set(dates)) or tuple(dates) != tuple(sorted(dates)):
        raise ValueError('dates must be unique and in chronological order')
    all_groups = []
    for date in dates:
        if mode == 'count':
            groups = plan_merge_groups(get_cached_files(database, source, date), parts)
        else:
            from .duration_workflow import prepare_duration_batch
            groups = prepare_duration_batch(database, source, date, parts).groups
        if len(groups) != parts or any(group.date != date for group in groups):
            raise ResumeConflict('daily group count/date mismatch')
        all_groups.extend(groups)
    return tuple(all_groups)


def _signature(groups: Sequence[MergeGroup]) -> str:
    """Plan fingerprint incorporates original file metadata and group boundaries."""
    data = []
    seen_inputs = set()
    for group in groups:
        if not group.files or not group.output_name or Path(group.output_name).name != group.output_name:
            raise ValueError('invalid group')
        inputs = []
        for file in group.files:
            path = Path(file)
            key = str(path.resolve()).casefold()
            if key in seen_inputs:
                raise ValueError(f'duplicate source input: {path}')
            seen_inputs.add(key)
            stat = path.stat()
            if stat.st_size <= 0:
                raise ResumeConflict(f'empty source recording: {path}')
            inputs.append([key, stat.st_size, stat.st_mtime_ns])
        data.append([group.date, group.part_number, group.output_name, inputs])
    blob = json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(blob).hexdigest()


def _read_manifest(path: Path) -> dict:
    try:
        manifest = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError) as exc:
        raise ResumeConflict(f'manifest unreadable: {path}: {exc}') from exc
    if not isinstance(manifest, dict) or manifest.get('version') != 1 or not isinstance(manifest.get('completed'), dict):
        raise ResumeConflict('invalid resume manifest version or structure')
    return manifest


def _write_manifest(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + '.tmp')
    # Any pre-existing staging file indicates a possible interrupted writer.
    with temporary.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _artifact_paths(path: Path) -> tuple[Path, Path]:
    return (path.with_name(path.stem + '.partial.mkv'),
            path.with_name(path.name + '.lock'))


def _check_artifacts(groups: Sequence[MergeGroup], output_dir: Path,
                     completed: dict) -> tuple[list[MergeGroup], list[str]]:
    pending, skipped = [], []
    seen_outputs = set()
    for group in groups:
        name = group.output_name
        if not name or Path(name).name != name or not name.lower().endswith('.mkv'):
            raise ValueError(f'invalid output name: {name!r}')
        key = name.casefold()
        if key in seen_outputs:
            raise ValueError(f'duplicate output: {name}')
        seen_outputs.add(key)
        dest = output_dir / name
        partial, lock = _artifact_paths(dest)
        if partial.exists() or lock.exists():
            raise ResumeConflict(f'partial or lock requires inspection: {dest}')
        saved = completed.get(name)
        if saved is not None:
            if not isinstance(saved, dict) or not dest.is_file():
                raise ResumeConflict(f'recorded completion missing: {dest}')
            stat = dest.stat()
            if (stat.st_size <= 0 or saved.get('bytes') != stat.st_size or
                    saved.get('modified_ns') != stat.st_mtime_ns):
                raise ResumeConflict(f'completed file changed: {dest}')
            skipped.append(name)
        elif dest.exists():
            raise ResumeConflict(f'untracked output, will not overwrite: {dest}')
        else:
            pending.append(group)
    if set(completed) - {group.output_name for group in groups}:
        raise ResumeConflict('manifest includes unexpected output names')
    return pending, skipped


def run_daily_resume(
    groups: Sequence[MergeGroup], output_dir: str | Path, *,
    cancel_event: threading.Event | None = None,
    on_progress: Callable[[str, int, int], None] | None = None,
    merge_one: Callable[[MergeGroup, Path, threading.Event | None, Callable[[int], None]], object] | None = None,
) -> DailyResumeResult:
    """Run all groups sequentially, checkpointing each verified-by-metadata output.

    A preflight failure never starts a merge. On interruption a completed
    checkpoint is kept and is safe to skip only if source and output metadata
    still match. Leftover lock/partial => halt for manual intervention.

    merge_one allows a synthetic merger for unit tests; default uses existing
    batch_merger.merge_groups to retain its collision and cancellation policy.
    """
    groups = tuple(groups)
    if not groups:
        raise ValueError('no groups')
    destdir = Path(output_dir)
    if not destdir.is_dir():
        raise NotADirectoryError(destdir)
    controller = destdir / CONTROLLER_LOCK
    manifest_path = destdir / MANIFEST_NAME
    # Controller lock is acquired before inspecting/writing the journal.
    try:
        fd = os.open(controller, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ResumeConflict(f'existing controller lock: {controller}') from exc
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(f'pid={os.getpid()}\ncreated_at={datetime.now(timezone.utc).isoformat()}\n')
        sig = _signature(groups)
        if manifest_path.exists():
            manifest = _read_manifest(manifest_path)
            if manifest.get('signature') != sig:
                raise ResumeConflict('source metadata or group plan changed; cannot resume')
        else:
            manifest = {'version': 1, 'signature': sig, 'completed': {}}
        pending, skipped = _check_artifacts(groups, destdir, manifest['completed'])
        if not manifest_path.exists():
            _write_manifest(manifest_path, manifest)
        finished = []
        for group in pending:
            if cancel_event is not None and cancel_event.is_set():
                return DailyResumeResult(tuple(finished), tuple(skipped), len(groups), True)
            # Recheck the immutable plan before starting the next output.
            if _signature(groups) != sig:
                raise ResumeConflict('source changed before a pending merge')
            out = destdir / group.output_name
            def callback(percent: int, name: str = group.output_name) -> None:
                if on_progress is not None:
                    on_progress(name, len(finished) + len(skipped), percent)
            if merge_one is None:
                from .batch_merger import merge_groups
                try:
                    merge_groups([group], destdir,
                                 on_progress=lambda _i, _t, p: callback(p),
                                 cancel_event=cancel_event)
                except Exception:
                    # No checkpoint if merger failed/cancelled; artifacts left untouched.
                    raise
            else:
                merge_one(group, out, cancel_event, callback)
            partial, lock = _artifact_paths(out)
            if not out.is_file() or out.stat().st_size <= 0 or partial.exists() or lock.exists():
                raise ResumeConflict(f'output not clean and nonempty: {out}')
            # Do not checkpoint if any source changed during this operation.
            if _signature(groups) != sig:
                raise ResumeConflict('source changed while merging; checkpoint withheld')
            stat = out.stat()
            manifest['completed'][group.output_name] = {
                'bytes': stat.st_size,
                'modified_ns': stat.st_mtime_ns,
                'saved_at': datetime.now(timezone.utc).isoformat(),
            }
            _write_manifest(manifest_path, manifest)
            finished.append(group.output_name)
        return DailyResumeResult(tuple(finished), tuple(skipped), len(groups), False)
    finally:
        # Controlled exit only. Hard termination leaves the lock for diagnosis.
        controller.unlink(missing_ok=True)
