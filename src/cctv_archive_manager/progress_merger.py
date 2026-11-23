
"""MKVToolNix 실시간 진행률 수집 병합 엔진.

기존 merger.py의 입력 검증 기능을 재사용한다.
GUI 연결 및 작업 취소는 후속 단계에서 구현한다.
"""

import os
import re
import subprocess
import time

from collections import deque
from pathlib import Path
from typing import Callable, Sequence

from .merger import (
    DEFAULT_MKVMERGE,
    MergeResult,
    prepare_merge,
)


PROGRESS_PATTERN = re.compile(
    r"^#GUI#progress\s+(\d{1,3})%$"
)


def parse_progress(line: str) -> int | None:
    """MKVToolNix GUI 진행률 메시지를 해석한다."""

    match = PROGRESS_PATTERN.fullmatch(line.strip())

    if match is None:
        return None

    percent = int(match.group(1))

    if not 0 <= percent <= 100:
        return None

    return percent


def merge_with_progress(
    inputs: Sequence[str | Path],
    output: str | Path,
    on_progress: Callable[[int], None] | None = None,
    mkvmerge: str | Path = DEFAULT_MKVMERGE,
) -> MergeResult:
    """MKV 파일을 병합하면서 실시간 진행률을 전달한다.

    성공한 경우에만 최종 파일명을 확정한다.
    오류 발생 시 .partial.mkv를 보존한다.
    """

    command, destination = prepare_merge(
        inputs,
        output,
        mkvmerge,
    )

    temporary = destination.with_name(
        destination.stem + ".partial.mkv"
    )

    lock_file = destination.with_name(
        destination.name + ".lock"
    )

    if temporary.exists():
        raise FileExistsError(
            f"임시 병합 파일이 이미 존재합니다: {temporary}"
        )

    # 같은 출력 파일에 대한 프로그램 간 실행 충돌을 줄인다.
    # O_EXCL을 통해 잠금 파일을 독점적으로 생성한다.
    lock_fd = os.open(
        lock_file,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        0o600,
    )

    os.close(lock_fd)

    try:
        # 잠금을 획득한 후 다시 확인
        if destination.exists():
            raise FileExistsError(
                f"출력 파일이 이미 존재합니다: {destination}"
            )

        if temporary.exists():
            raise FileExistsError(
                f"임시 파일이 이미 존재합니다: {temporary}"
            )

        # MKVToolNix GUI 모드 활성화
        command.insert(1, "--gui-mode")

        # 실제 출력 경로를 임시 파일로 변경
        output_index = command.index("--output") + 1
        command[output_index] = str(temporary)

        log_lines = deque(maxlen=200)
        last_progress = -1

        started = time.perf_counter()

        with subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        ) as process:

            if process.stdout is None:
                raise RuntimeError(
                    "MKVToolNix 출력 스트림을 열지 못했습니다."
                )

            for line in process.stdout:
                line = line.rstrip("\r\n")
                log_lines.append(line)

                percent = parse_progress(line)

                if percent is not None:
                    if percent > last_progress:
                        last_progress = percent

                        if on_progress is not None:
                            on_progress(percent)

            return_code = process.wait()

        log = "\n".join(log_lines)

        if return_code not in (0, 1):
            raise RuntimeError(
                f"mkvmerge 실행 실패: {return_code}\n"
                + log[-3000:]
            )

        if not temporary.is_file():
            raise RuntimeError(
                "MKV 병합 결과 파일이 생성되지 않았습니다."
            )

        size = temporary.stat().st_size

        if size == 0:
            raise RuntimeError(
                "MKV 병합 결과 파일이 0바이트입니다."
            )

        if return_code == 1:
            raise RuntimeError(
                "MKVToolNix 경고가 발생했습니다. "
                "임시 파일을 보존합니다.\n"
                + log[-3000:]
            )

        # 병합 중 외부에서 출력 파일을 생성했는지 재확인
        if destination.exists():
            raise FileExistsError(
                f"병합 중 출력 파일이 생성됐습니다: {destination}"
            )

        temporary.rename(destination)

        elapsed = time.perf_counter() - started

        print(
            f"\n병합 처리시간: {elapsed:.3f}초"
        )

        return MergeResult(
            output=destination,
            input_count=len(inputs),
            output_bytes=size,
            return_code=return_code,
            log=log,
        )

    finally:
        # 우리가 생성한 잠금 파일을 정상·오류 종료 시 정리
        lock_file.unlink(missing_ok=True)
