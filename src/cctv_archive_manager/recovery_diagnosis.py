"""Read-only, fail-closed inspection of a user-selected batch output folder.

This module never authorizes removal of a lock or partial MKV. It does not
read or update SQLite, start a merge, alter the manifest or touch NVR files.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

# Names from the daily-resume journal contract. Keeping this inspector free
# of merger imports lets recovery diagnostics run even if the merger is broken.
CONTROLLER_LOCK = '.cctv-daily-resume.lock'
MANIFEST_NAME = '.cctv-daily-resume.json'


@dataclass(frozen=True)
class RecoveryDiagnosis:
    output_dir: Path
    status: str
    messages: tuple[str, ...]
    completed: tuple[str, ...]
    controller_lock: bool
    partials: tuple[str, ...]
    merge_locks: tuple[str, ...]
    unexpected: tuple[str, ...]

    @property
    def can_auto_recover(self) -> bool:
        # Intentionally always false; production recovery has not been authorized.
        return False


def inspect_daily_output(output_dir: str | Path, expected_outputs: Sequence[str] = ()) -> RecoveryDiagnosis:
    """Inspect known output artifacts without modifying anything.

    ``expected_outputs`` should come from a verified GUI plan. If absent,
    the report is advisory and must NOT be used to approve a recovery.
    """
    root = Path(output_dir)
    if not root.is_dir() or root.is_symlink():
        raise NotADirectoryError(f'출력 폴더가 없거나 안전하게 확인할 수 없습니다: {root}')
    expected = tuple(expected_outputs)
    if len(expected) != len(set(n.casefold() for n in expected)):
        raise ValueError('중복된 출력 파일명이 있습니다.')
    if any(Path(n).name != n or not n.lower().endswith('.mkv') for n in expected):
        raise ValueError('잘못된 출력 파일명이 포함돼 있습니다.')
    controller = (root / CONTROLLER_LOCK).is_file()
    manifest_path = root / MANIFEST_NAME
    messages = []
    completed = ()
    manifest_valid = False
    if manifest_path.exists():
        try:
            doc = json.loads(manifest_path.read_text(encoding='utf-8'))
            if not isinstance(doc, dict) or doc.get('version') != 1 or not isinstance(doc.get('completed'), dict):
                raise ValueError('버전 또는 완료 기록 구조가 올바르지 않음')
            if not isinstance(doc.get('signature'), str) or not doc['signature']:
                raise ValueError('계획 서명이 없음')
            completed = tuple(sorted(doc['completed']))
            manifest_valid = True
        except (OSError, ValueError) as exc:
            messages.append(f'체크포인트를 신뢰할 수 없음: {exc}')
    else:
        messages.append('체크포인트가 없습니다. 새로운 작업이거나 이전 기록이 없는 상태입니다.')

    partials = tuple(sorted(p.name for p in root.glob('*.partial.mkv') if p.is_file()))
    merge_locks = tuple(sorted(p.name for p in root.glob('*.mkv.lock') if p.is_file()))
    extra = []
    if (root / (MANIFEST_NAME + '.tmp')).exists():
        extra.append(MANIFEST_NAME + '.tmp')
    if expected:
        allowed = {n.casefold() for n in expected}
        for p in root.glob('*.mkv'):
            if p.is_file() and not p.name.lower().endswith('.partial.mkv') and p.name.casefold() not in allowed:
                extra.append(p.name)
        if any(n.casefold() not in allowed for n in completed):
            messages.append('계획과 관계없는 체크포인트 완료 항목이 있습니다.')
    else:
        messages.append('미리보기 계획이 없어 출력 파일명 및 계획 서명 일치 여부를 확인하지 못했습니다.')
    unexpected = tuple(sorted(extra))
    blocked = bool(controller or partials or merge_locks or unexpected)
    if controller:
        messages.append('일괄 작업 잠금이 남아 있습니다. 현재 작업 실행 여부를 별도로 확인해야 합니다.')
    if partials:
        messages.append('부분 영상이 남아 있습니다. 완료된 MKV로 간주하지 않습니다.')
    if merge_locks:
        messages.append('개별 병합 잠금이 남아 있습니다. 잠금 소유자를 별도로 확인해야 합니다.')
    if unexpected:
        messages.append('예상하지 못한 출력/임시 파일이 있습니다. 삭제하거나 덮어쓰지 마세요.')
    if manifest_valid:
        messages.append(f'체크포인트 구조 확인: 완료 항목 {len(completed)}개. SHA-256/원본 서명 검증은 별도 단계가 필요합니다.')
    status = ('BLOCKED — 수동 확인 필요' if blocked or (manifest_path.exists() and not manifest_valid)
              else 'READ ONLY — 위험 잔여물 미발견 (재개 승인 아님)')
    messages.append('이 진단은 파일을 변경하거나 잠금을 해제하지 않습니다. 자동 복구는 지원하지 않습니다.')
    return RecoveryDiagnosis(root, status, tuple(messages), completed, controller, partials, merge_locks, unexpected)


def format_diagnosis(report: RecoveryDiagnosis) -> str:
    def lines(title, items):
        return title + '\n' + ('\n'.join('  - ' + x for x in items) if items else '  (없음)')
    return '\n\n'.join((
        f'판정: {report.status}\n출력 폴더: {report.output_dir}',
        f'일괄 잠금: {"있음" if report.controller_lock else "없음"}',
        lines('완료 체크포인트 항목:', report.completed),
        lines('부분 MKV:', report.partials),
        lines('개별 병합 잠금:', report.merge_locks),
        lines('예상 외 파일:', report.unexpected),
        lines('진단 및 주의사항:', report.messages),
    ))
