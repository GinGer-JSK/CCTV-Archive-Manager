"""Conservative, explicitly approved recovery for the normal multi-day merger.

Preflight never changes files. Apply archives only known stale controller/merge
locks and partial outputs, never completed outputs, sources, or the manifest.
A verified plan and a complete Windows process snapshot are mandatory.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .daily_resume import CONTROLLER_LOCK, MANIFEST_NAME, _read_manifest, _signature
from .lock_owner_inspector import ProcessInfo, windows_process_snapshot
from .merge_planner import MergeGroup


class RecoveryBlocked(RuntimeError):
    """Recovery is unsafe or not sufficiently established."""


@dataclass(frozen=True)
class RecoveryPlan:
    output: Path
    signature: str
    completed: tuple[str, ...]
    archives: tuple[Path, ...]
    fingerprint: tuple[tuple[str, int, int, str], ...]
    manifest_sha256: str


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _pid_of_lock(lock: Path) -> int:
    try:
        content = lock.read_text(encoding='utf-8')
        for line in content.splitlines():
            if line.startswith('pid='):
                pid = int(line.removeprefix('pid=').strip())
                if pid > 0:
                    return pid
    except (OSError, ValueError) as exc:
        raise RecoveryBlocked(f'잠금의 PID를 확인할 수 없습니다: {lock.name}: {exc}') from exc
    raise RecoveryBlocked(f'잠금에 유효한 PID가 없습니다: {lock.name}')


def _guard_processes(snapshot: Sequence[ProcessInfo], locks: Sequence[Path], output: Path) -> None:
    if not isinstance(snapshot, (tuple, list)) or not snapshot:
        raise RecoveryBlocked('프로세스 목록을 신뢰할 수 없습니다.')
    seen = {p.pid for p in snapshot}
    recorded = {_pid_of_lock(lock) for lock in locks}
    if recorded & seen:
        raise RecoveryBlocked(f'잠금 PID가 현재 존재합니다: {sorted(recorded & seen)}')

    # The current GUI process is allowed, but not any separate Python worker.
    path_marker = str(output).casefold().replace('/', '\\')
    for proc in snapshot:
        executable = Path(proc.name).name.casefold()
        if executable not in ('mkvmerge.exe', 'python.exe', 'pythonw.exe', 'py.exe'):
            continue
        cmd = proc.command_line
        if cmd is None:
            raise RecoveryBlocked(f'프로세스 명령줄을 확인할 수 없습니다: {executable} PID {proc.pid}')
        if path_marker in cmd.casefold().replace('/', '\\'):
            if proc.pid == os.getpid():
                continue
            raise RecoveryBlocked(f'작업 폴더를 참조하는 프로세스가 있습니다: {executable} PID {proc.pid}')


def preflight(groups: Sequence[MergeGroup], output_dir: str | Path, *,
              snapshot_provider: Callable[[], Sequence[ProcessInfo]] = windows_process_snapshot,
              ) -> RecoveryPlan:
    groups = tuple(groups)
    if not groups:
        raise RecoveryBlocked('병합 계획 미리보기가 먼저 필요합니다.')
    output = Path(output_dir)
    if not output.is_dir() or output.is_symlink():
        raise RecoveryBlocked('출력 폴더가 없거나 심볼릭 링크입니다.')
    output = output.resolve(strict=True)
    names = [g.output_name for g in groups]
    if (len(set(n.casefold() for n in names)) != len(names) or
        any(Path(n).name != n or not n.lower().endswith('.mkv') or n.lower().endswith('.partial.mkv') for n in names)):
        raise RecoveryBlocked('병합 출력 파일명 중복 또는 잘못된 이름')
    for group in groups:
        for src in group.files:
            source = Path(src)
            if source.is_symlink() or not source.is_file():
                raise RecoveryBlocked('원본 파일을 안전하게 확인할 수 없습니다.')
            if source.resolve().is_relative_to(output):
                raise RecoveryBlocked('원본이 출력 폴더에 포함됩니다.')
            if output.is_relative_to(source.resolve().parent):
                raise RecoveryBlocked('출력 폴더가 원본 폴더 내부입니다.')
    try:
        sig = _signature(groups)
    except (OSError, ValueError) as exc:
        raise RecoveryBlocked(f'원본 병합 계획 확인 실패: {exc}') from exc
    manifest_path = output / MANIFEST_NAME
    if not _file(manifest_path):
        raise RecoveryBlocked('검증 가능한 체크포인트 파일이 없습니다.')
    try:
        manifest = _read_manifest(manifest_path)
    except Exception as exc:
        raise RecoveryBlocked(f'체크포인트를 읽을 수 없습니다: {exc}') from exc
    if manifest.get('signature') != sig:
        raise RecoveryBlocked('원본 또는 병합 계획 서명이 달라졌습니다.')
    saved = manifest['completed']
    if set(saved) - set(names):
        raise RecoveryBlocked('계획 외 완료 항목이 체크포인트에 존재합니다.')
    for name, record in saved.items():
        final = output / name
        if not _file(final) or not isinstance(record, dict):
            raise RecoveryBlocked(f'완료 파일 누락 또는 체크포인트 손상: {name}')
        stat = final.stat()
        digest = record.get('sha256')
        if (stat.st_size <= 0 or stat.st_size != record.get('bytes') or
            stat.st_mtime_ns != record.get('modified_ns') or
            not isinstance(digest, str) or len(digest) != 64 or _sha(final) != digest):
            raise RecoveryBlocked(f'완료 파일의 SHA-256/메타데이터를 신뢰할 수 없습니다: {name}')
    controller = output / CONTROLLER_LOCK
    if not _file(controller):
        raise RecoveryBlocked('중단된 일괄 작업 잠금이 없습니다. 이 복구 경로는 해당하지 않습니다.')
    known = {MANIFEST_NAME, CONTROLLER_LOCK, *saved.keys()}
    artifacts: list[Path] = [controller]
    unfinished = 0
    for name in names:
        partial = output / (name[:-4] + '.partial.mkv')
        lock = output / (name + '.lock')
        if name not in saved and (output / name).exists():
            raise RecoveryBlocked(f'완료 기록 없는 최종 출력이 있습니다: {name}')
        if name in saved and (partial.exists() or lock.exists()):
            raise RecoveryBlocked(f'완료된 출력에 부분 파일 또는 잠금이 있습니다: {name}')
        for artifact in (partial, lock):
            if artifact.exists():
                if not _file(artifact):
                    raise RecoveryBlocked(f'안전하지 않은 잔여 파일: {artifact}')
                artifacts.append(artifact)
                known.add(artifact.name)
                unfinished += 1
    if not unfinished:
        raise RecoveryBlocked('미완료 그룹의 잔여물이 없어 정리 대상이 없습니다.')
    if (output / (MANIFEST_NAME + '.tmp')).exists():
        raise RecoveryBlocked('체크포인트 임시 파일 존재: 수동 점검이 필요합니다.')
    for item in output.iterdir():
        if item.is_symlink() or not item.is_file() or item.name not in known:
            raise RecoveryBlocked(f'예상하지 못한 출력 폴더 항목: {item.name}')
    try:
        snapshot = tuple(snapshot_provider())
    except Exception as exc:
        raise RecoveryBlocked(f'프로세스 조회 실패: {exc}') from exc
    _guard_processes(snapshot, [p for p in artifacts if p.name.endswith('.lock')], output)
    fingerprint = tuple((p.name, p.stat().st_size, p.stat().st_mtime_ns, _sha(p)) for p in artifacts)
    return RecoveryPlan(output, sig, tuple(sorted(saved)), tuple(artifacts), fingerprint, _sha(manifest_path))


def quarantine(plan: RecoveryPlan, groups: Sequence[MergeGroup], *,
               snapshot_provider: Callable[[], Sequence[ProcessInfo]] = windows_process_snapshot,
               ) -> Path:
    """Archive after explicit GUI approval. Never delete, overwrite or merge."""
    fresh = preflight(groups, plan.output, snapshot_provider=snapshot_provider)
    if fresh != plan:
        raise RecoveryBlocked('확인 후 파일 상태가 변경됐습니다. 처음부터 다시 진단하세요.')
    # Keep evidence outside output, so run_daily_resume sees only the manifest
    # and completed files upon the next explicit execution.
    evidence = plan.output.parent / f'.{plan.output.name}-recovery-evidence-{uuid.uuid4().hex[:12]}'
    evidence.mkdir(mode=0o700)
    moved: list[tuple[Path, Path]] = []
    try:
        manifest = plan.output / MANIFEST_NAME
        shutil.copy2(manifest, evidence / MANIFEST_NAME)
        if _sha(evidence / MANIFEST_NAME) != plan.manifest_sha256:
            raise RecoveryBlocked('체크포인트 보존본 검증 실패')
        for artifact in plan.archives:
            dest = evidence / artifact.name
            # In-progress output goes first; controller lock goes last.
            if artifact.name == CONTROLLER_LOCK:
                continue
            os.replace(artifact, dest)
            moved.append((artifact, dest))
        controller = plan.output / CONTROLLER_LOCK
        dest = evidence / controller.name
        os.replace(controller, dest)
        moved.append((controller, dest))
        for old, archived in moved:
            previous = next(item for item in plan.fingerprint if item[0] == old.name)
            actual = archived.stat()
            if (actual.st_size, actual.st_mtime_ns, _sha(archived)) != previous[1:]:
                raise RecoveryBlocked(f'증거 보존 파일 검증 실패: {archived.name}')
        ledger = {
            'status': 'archived_no_merge_executed',
            'original_output': str(plan.output),
            'signature': plan.signature,
            'manifest_sha256': plan.manifest_sha256,
            'archived_files': [{'name': name, 'bytes': size, 'mtime_ns': mtime, 'sha256': digest}
                               for name, size, mtime, digest in plan.fingerprint],
        }
        with (evidence / 'evidence-ledger.json').open('x', encoding='utf-8') as stream:
            json.dump(ledger, stream, ensure_ascii=False, indent=2)
        if _sha(manifest) != plan.manifest_sha256:
            raise RecoveryBlocked('작업 중 체크포인트가 변경됨')
        return evidence
    except BaseException:
        for original, archived in reversed(moved):
            if archived.exists() and not original.exists():
                os.replace(archived, original)
        raise
