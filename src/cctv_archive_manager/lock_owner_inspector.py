"""Read-only diagnostics for mkvmerge lock ownership on Windows.

This module never removes a lock, kills a process or modifies a merge output.
Its findings are advisory, NOT an authorization to delete a lock.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import json
import os
from pathlib import Path
import subprocess
from typing import Callable, Sequence

from .recovery_inspector import inspect_merge_output


class LockOwnerStatus(str, Enum):
    NO_LOCK = "no_lock"
    OWNER_POSSIBLY_ACTIVE = "owner_possibly_active"
    MERGER_POSSIBLY_ACTIVE = "merger_possibly_active"
    NO_MATCH_FOUND = "no_match_found"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    name: str
    creation_time: datetime | None
    command_line: str | None


@dataclass(frozen=True)
class LockOwnerReport:
    output: Path
    lock: Path
    status: LockOwnerStatus
    recorded_pid: int | None
    recorded_started_at: datetime | None
    matching_pids: tuple[int, ...]
    notes: tuple[str, ...]


def _parse_utc(value: str) -> datetime | None:
    try:
        result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if result.tzinfo is None:
            return None
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def _metadata(contents: str | None) -> tuple[int | None, datetime | None]:
    values = {}
    for line in (contents or "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    try:
        pid = int(values.get("pid", ""))
        if pid <= 0:
            pid = None
    except ValueError:
        pid = None
    return pid, _parse_utc(values.get("started_at", ""))


def windows_process_snapshot() -> tuple[ProcessInfo, ...]:
    """Read process details via Windows CIM. Raise if inspection is unavailable."""
    if os.name != "nt":
        raise OSError("Windows CIM process inspection is only available on Windows")

    script = (
        "@(Get-CimInstance Win32_Process | "
        "Select-Object ProcessId, Name, CreationDate, CommandLine) "
        "| ConvertTo-Json -Depth 3 -Compress"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        check=True, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=15,
    )
    raw = json.loads(completed.stdout)
    if not isinstance(raw, list):
        raise ValueError("Unexpected process snapshot format")

    result = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        pid = item.get("ProcessId")
        if not isinstance(pid, int):
            continue
        timestamp = item.get("CreationDate")
        created = _parse_utc(timestamp) if isinstance(timestamp, str) else None
        result.append(ProcessInfo(
            pid=pid,
            name=str(item.get("Name") or ""),
            creation_time=created,
            command_line=item.get("CommandLine"),
        ))
    return tuple(result)


def inspect_lock_owner(
    output: str | Path,
    snapshot_provider: Callable[[], Sequence[ProcessInfo]] = windows_process_snapshot,
) -> LockOwnerReport:
    """Diagnose a lock without changing any files.

    NO_MATCH_FOUND is only a snapshot observation, never proof that a lock can
    be deleted. The caller must recheck ongoing work before manual recovery.
    """
    artifact = inspect_merge_output(output)
    if not artifact.lock.is_file():
        return LockOwnerReport(
            artifact.output, artifact.lock, LockOwnerStatus.NO_LOCK,
            None, None, (), ("No lock file exists.",),
        )

    pid, started = _metadata(artifact.lock_contents)
    try:
        processes = tuple(snapshot_provider())
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return LockOwnerReport(
            artifact.output, artifact.lock, LockOwnerStatus.UNKNOWN,
            pid, started, (), (f"Process lookup unavailable: {error}",
                                "Do not delete the lock automatically."),
        )

    # The PID is for Python's locking process, not necessarily mkvmerge.
    # Its creation must precede the lock timestamp to avoid common PID-reuse cases.
    owner_pids = []
    for proc in processes:
        if pid is None or started is None or proc.pid != pid:
            continue
        if proc.creation_time is None:
            continue
        if proc.creation_time <= started + timedelta(seconds=2):
            owner_pids.append(proc.pid)

    # An orphan mkvmerge may continue after its Python owner exits.
    # A process mentioning this particular partial path is treated as possibly active.
    partial_path = os.path.normcase(str(artifact.partial)).casefold().replace("/", "\\")
    merger_pids = []
    for proc in processes:
        if Path(proc.name).name.casefold() != "mkvmerge.exe":
            continue
        cmd = (proc.command_line or "").casefold().replace("/", "\\")
        if partial_path in cmd:
            merger_pids.append(proc.pid)

    if merger_pids:
        status = LockOwnerStatus.MERGER_POSSIBLY_ACTIVE
        matching = tuple(sorted(set(merger_pids + owner_pids)))
        notes = ("A running mkvmerge process references this partial output.",
                 "Do not change lock or partial files while it may be running.")
    elif owner_pids:
        status = LockOwnerStatus.OWNER_POSSIBLY_ACTIVE
        matching = tuple(owner_pids)
        notes = ("The recorded PID exists with a compatible process start time.",
                 "PID matching alone cannot prove lock ownership; do not auto-unlock.")
    elif pid is None or started is None:
        status = LockOwnerStatus.UNKNOWN
        matching = ()
        notes = ("Lock metadata is missing or invalid.",
                 "Ownership cannot be determined; inspect manually.")
    else:
        # We cannot reliably match a child if its command line is not readable.
        uncertain_merger = any(
            Path(p.name).name.casefold() == "mkvmerge.exe" and not p.command_line
            for p in processes
        )
        status = LockOwnerStatus.UNKNOWN if uncertain_merger else LockOwnerStatus.NO_MATCH_FOUND
        matching = ()
        notes = (
            "Process snapshot did not confirm an active owner for this lock.",
            "This is NOT proof that the lock is safe to remove; inspect manually.",
        )
    return LockOwnerReport(
        artifact.output, artifact.lock, status, pid, started, matching, notes,
    )
