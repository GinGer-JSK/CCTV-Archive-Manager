"""Read-only inspection of MKV merge outputs, partial files and locks.

This module deliberately does not attempt to establish whether an owner PID is
still alive. A PID alone is not a reliable ownership check (PID reuse), and a
Python parent may have exited while mkvmerge remains alive.
"""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class ArtifactStatus(str, Enum):
    CLEAN = "clean"
    COMPLETE = "complete"
    PARTIAL_ONLY = "partial_only"
    LOCK_PRESENT = "lock_present"
    OUTPUT_CONFLICT = "output_conflict"


@dataclass(frozen=True)
class MergeArtifactReport:
    output: Path
    partial: Path
    lock: Path
    status: ArtifactStatus
    output_bytes: int | None
    partial_bytes: int | None
    lock_contents: str | None
    notes: tuple[str, ...]


def inspect_merge_output(output: str | Path) -> MergeArtifactReport:
    """Inspect one expected final .mkv, without changing any files.

    Status precedence: lock, completed+partial conflict, complete, partial, clean.
    A lock always requires manual ownership verification; it is never called
    stale just because a PID is absent or a timestamp is old.
    """
    output = Path(output)
    if output.suffix.lower() != ".mkv" or output.name.lower().endswith(".partial.mkv"):
        raise ValueError("Expected a final .mkv path, not a partial filename")

    partial = output.with_name(output.stem + ".partial.mkv")
    lock = output.with_name(output.name + ".lock")

    output_exists = output.is_file()
    partial_exists = partial.is_file()
    lock_exists = lock.is_file()

    output_bytes = output.stat().st_size if output_exists else None
    partial_bytes = partial.stat().st_size if partial_exists else None
    lock_contents = None
    if lock_exists:
        try:
            lock_contents = lock.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            lock_contents = f"<unreadable: {type(error).__name__}: {error}>"

    if lock_exists:
        status = ArtifactStatus.LOCK_PRESENT
        notes = (
            "Lock exists; check Python and mkvmerge process ownership before any action.",
            "Do not automatically remove lock or partial files.",
        )
    elif output_exists and partial_exists:
        status = ArtifactStatus.OUTPUT_CONFLICT
        notes = ("Both final and partial MKVs exist; inspect manually.",)
    elif output_exists:
        status = ArtifactStatus.COMPLETE
        notes = ("Final MKV exists; file validity and playback are not verified.",)
    elif partial_exists:
        status = ArtifactStatus.PARTIAL_ONLY
        notes = ("Incomplete MKV remains; playback is not guaranteed.",)
    else:
        status = ArtifactStatus.CLEAN
        notes = ("No merge artifacts found for this output.",)

    return MergeArtifactReport(
        output=output,
        partial=partial,
        lock=lock,
        status=status,
        output_bytes=output_bytes,
        partial_bytes=partial_bytes,
        lock_contents=lock_contents,
        notes=notes,
    )
