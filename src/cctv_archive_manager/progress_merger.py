
"""MKVToolNix 실시간 진행률 및 안전한 작업 취소.

- 기존 입력·출력 검증 재사용
- 독점 잠금 파일 생성
- 실시간 진행률 콜백
- threading.Event 기반 취소 요청
- 종료 확인 후 잠금 파일 정리
- 실패·취소 시 partial MKV 보존
"""

import os
import re
import subprocess
import threading
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


class MergeCancelled(RuntimeError):
    """사용자 요청으로 MKV 병합이 취소됨."""


def parse_progress(line: str) -> int | None:
    match = PROGRESS_PATTERN.fullmatch(line.strip())

    if match is None:
        return None

    percent = int(match.group(1))
    return percent if 0 <= percent <= 100 else None


def _lock_path(destination: Path) -> Path:
    return destination.with_name(
        destination.name + ".lock"
    )


def _create_lock(destination: Path) -> Path:
    lock_file = _lock_path(destination)

    try:
        fd = os.open(
            lock_file,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
    except FileExistsError as error:
        try:
            details = lock_file.read_text(
                encoding="utf-8"
            )
        except OSError:
            details = "잠금 정보를 읽을 수 없습니다."

        raise FileExistsError(
            f"기존 병합 잠금 파일: {lock_file}\n"
            f"{details}\n"
            "다른 병합 작업이 실행 중인지 확인하세요."
        ) from error

    try:
        with os.fdopen(
            fd, "w", encoding="utf-8"
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


def _stop_process(process: subprocess.Popen) -> None:
    """해당 MKVToolNix 프로세스의 종료를 확인한다."""

    if process.poll() is not None:
        return

    try:
        process.terminate()
    except ProcessLookupError:
        return
    except OSError:
        if process.poll() is not None:
            return
        raise

    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)


def merge_with_progress(
    inputs: Sequence[str | Path],
    output: str | Path,
    on_progress: Callable[[int], None] | None = None,
    mkvmerge: str | Path = DEFAULT_MKVMERGE,
    cancel_event: threading.Event | None = None,
) -> MergeResult:
    """진행률과 취소 요청을 처리하며 MKV를 병합한다.

    cancel_event.set()으로 취소를 요청한다.

    취소가 처리되면 MergeCancelled가 발생한다.
    취소와 실패 시 partial MKV는 보존한다.
    """

    # 이미 취소가 요청된 작업은 시작하지 않음
    if cancel_event is not None and cancel_event.is_set():
        raise MergeCancelled(
            "병합 시작 전에 취소됐습니다."
        )

    command, destination = prepare_merge(
        inputs,
        output,
        mkvmerge,
    )

    temporary = destination.with_name(
        destination.stem + ".partial.mkv"
    )

    if temporary.exists():
        raise FileExistsError(
            f"기존 임시 병합 파일이 있습니다: {temporary}"
        )

    lock_file = _create_lock(destination)

    process = None
    monitor_thread = None
    monitor_done = threading.Event()
    cancelled = threading.Event()
    monitor_errors = []

    try:
        if destination.exists():
            raise FileExistsError(
                f"출력 파일이 이미 존재합니다: {destination}"
            )

        if temporary.exists():
            raise FileExistsError(
                f"임시 파일이 이미 존재합니다: {temporary}"
            )

        if cancel_event is not None and cancel_event.is_set():
            raise MergeCancelled(
                "프로세스 실행 전에 취소됐습니다."
            )

        command.insert(1, "--gui-mode")
        output_index = command.index("--output") + 1
        command[output_index] = str(temporary)

        log_lines = deque(maxlen=200)
        last_progress = -1

        started = time.perf_counter()

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        def monitor_cancellation():
            """출력을 기다리는 동안에도 취소를 감지한다."""
            if cancel_event is None:
                return

            while not monitor_done.wait(0.05):
                if not cancel_event.is_set():
                    continue

                if process.poll() is None:
                    cancelled.set()

                    try:
                        _stop_process(process)
                    except Exception as error:
                        monitor_errors.append(error)

                return

        if cancel_event is not None:
            monitor_thread = threading.Thread(
                target=monitor_cancellation,
                name="MKVMergeCancelMonitor",
                daemon=True,
            )
            monitor_thread.start()

        if process.stdout is None:
            raise RuntimeError(
                "MKVToolNix 출력 스트림을 열지 못했습니다."
            )

        for line in process.stdout:
            line = line.rstrip("\r\n")
            log_lines.append(line)

            percent = parse_progress(line)

            if percent is not None and percent > last_progress:
                last_progress = percent

                if on_progress is not None:
                    on_progress(percent)

            if (
                cancel_event is not None
                and cancel_event.is_set()
                and process.poll() is None
            ):
                cancelled.set()
                _stop_process(process)
                break

        return_code = process.wait()

        # 감시 스레드의 처리를 마친 뒤 결과 판정
        monitor_done.set()

        if monitor_thread is not None:
            monitor_thread.join()

        if monitor_errors:
            raise RuntimeError(
                "병합 취소 중 프로세스 종료 오류: "
                f"{monitor_errors[0]}"
            )

        if cancelled.is_set():
            raise MergeCancelled(
                "MKV 병합이 취소됐습니다. "
                "미완료 파일은 보존합니다."
            )

        log = "\n".join(log_lines)

        if return_code not in (0, 1):
            raise RuntimeError(
                f"mkvmerge 실행 실패 "
                f"(종료 코드 {return_code})\n"
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
                "MKVToolNix 경고 발생. "
                "미완료 파일을 보존합니다.\n"
                + log[-3000:]
            )

        if destination.exists():
            raise FileExistsError(
                f"병합 중 출력 파일이 생성됐습니다: {destination}"
            )

        temporary.rename(destination)

        elapsed = time.perf_counter() - started
        print(f"\n병합 처리시간: {elapsed:.3f}초")

        return MergeResult(
            output=destination,
            input_count=len(inputs),
            output_bytes=size,
            return_code=return_code,
            log=log,
        )

    finally:
        # 예외나 콜백 오류가 발생해도 자식 프로세스 정리
        monitor_done.set()

        if process is not None and process.poll() is None:
            _stop_process(process)

        if monitor_thread is not None:
            monitor_thread.join()

        if process is not None and process.stdout is not None:
            process.stdout.close()

        # 프로세스가 실제 종료된 경우에만 잠금 해제
        if process is None or process.poll() is not None:
            lock_file.unlink(missing_ok=True)
