
"""MKVToolNix 진행률 병합 엔진.

- 기존 입력 및 출력 경로 검증 재사용
- 실시간 진행률 콜백
- 독점 잠금 파일 및 작업 정보 기록
- 실패 시 임시 MKV 보존
- 기존 출력 파일 덮어쓰기 방지

주의:
프로세스 강제 종료 시 잠금 파일이 남을 수 있다.
잠금 파일은 자동으로 회수하지 않는다.
"""

import os
import re
import subprocess
import time

from collections import deque
from datetime import datetime, timezone
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
    """MKVToolNix GUI 진행률 메시지 해석."""

    match = PROGRESS_PATTERN.fullmatch(
        line.strip()
    )

    if match is None:
        return None

    percent = int(match.group(1))

    if not 0 <= percent <= 100:
        return None

    return percent


def _lock_path(destination: Path) -> Path:
    return destination.with_name(
        destination.name + ".lock"
    )


def _create_lock(
    destination: Path,
) -> Path:
    """같은 출력 파일에 대한 독점 잠금 파일 생성."""

    lock_file = _lock_path(destination)

    try:
        fd = os.open(
            lock_file,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )

    except FileExistsError as error:
        details = ""

        try:
            details = lock_file.read_text(
                encoding="utf-8"
            )
        except OSError:
            details = "잠금 정보 읽기 실패"

        raise FileExistsError(
            "기존 병합 잠금 파일이 발견됐습니다.\n"
            f"잠금 파일: {lock_file}\n"
            f"{details}\n"
            "다른 병합 작업이 실행 중인지 확인하세요.\n"
            "실행 중인 작업이 없는 것이 확인된 경우에만 "
            "잠금 파일을 수동으로 정리하세요."
        ) from error

    try:
        with os.fdopen(
            fd,
            "w",
            encoding="utf-8",
        ) as stream:
            stream.write(
                f"pid={os.getpid()}\n"
                f"started_at="
                f"{datetime.now(timezone.utc).isoformat()}\n"
                f"output={destination}\n"
            )

    except Exception:
        lock_file.unlink(missing_ok=True)
        raise

    return lock_file


def merge_with_progress(
    inputs: Sequence[str | Path],
    output: str | Path,
    on_progress: Callable[[int], None] | None = None,
    mkvmerge: str | Path = DEFAULT_MKVMERGE,
) -> MergeResult:
    """MKV 병합 및 실시간 진행률 전달."""

    # 기존 병합 엔진의 경로·파일 검증 재사용
    command, destination = prepare_merge(
        inputs,
        output,
        mkvmerge,
    )

    temporary = destination.with_name(
        destination.stem + ".partial.mkv"
    )

    # 파일 생성 여부를 먼저 검사한다.
    if temporary.exists():
        raise FileExistsError(
            f"기존 임시 병합 파일이 있습니다: {temporary}"
        )

    # 원자적 생성으로 잠금 획득
    lock_file = _create_lock(destination)

    try:
        # 잠금 획득 후 다시 검사
        if destination.exists():
            raise FileExistsError(
                f"출력 파일이 이미 존재합니다: {destination}"
            )

        if temporary.exists():
            raise FileExistsError(
                f"임시 병합 파일이 이미 존재합니다: {temporary}"
            )

        command.insert(1, "--gui-mode")

        output_index = (
            command.index("--output") + 1
        )
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
                    "MKVToolNix 출력 스트림을 "
                    "열지 못했습니다."
                )

            for line in process.stdout:
                line = line.rstrip("\r\n")
                log_lines.append(line)

                percent = parse_progress(line)

                if (
                    percent is not None
                    and percent > last_progress
                ):
                    last_progress = percent

                    if on_progress is not None:
                        on_progress(percent)

            return_code = process.wait()

        log = "\n".join(log_lines)

        if return_code not in (0, 1):
            raise RuntimeError(
                f"mkvmerge 실행 실패 "
                f"(종료 코드 {return_code})\n"
                + log[-3000:]
            )

        if not temporary.is_file():
            raise RuntimeError(
                "MKV 병합 결과 파일이 "
                "생성되지 않았습니다."
            )

        size = temporary.stat().st_size

        if size == 0:
            raise RuntimeError(
                "MKV 병합 결과 파일이 "
                "0바이트입니다."
            )

        if return_code == 1:
            raise RuntimeError(
                "MKVToolNix 경고 발생.\n"
                "임시 파일을 보존합니다.\n"
                + log[-3000:]
            )

        if destination.exists():
            raise FileExistsError(
                "병합 도중 최종 출력 파일이 "
                f"생성됐습니다: {destination}"
            )

        # Windows에서는 기존 대상이 있으면
        # Path.rename()이 실패하므로 덮어쓰지 않음
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
        # 현재 실행이 획득한 잠금만 정리한다.
        # 프로세스 강제 종료 시에는 finally가
        # 실행되지 않을 수 있다.
        lock_file.unlink(missing_ok=True)
