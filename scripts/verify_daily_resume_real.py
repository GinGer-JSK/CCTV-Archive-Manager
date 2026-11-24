"""Step 5-6 local real-engine integration smoke test.

Only creates MKVs in a fresh temporary directory. Never opens NVR or SQLite.
Requires ffmpeg and MKVToolNix mkvmerge. No project files are modified.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from cctv_archive_manager.daily_resume import (
    CONTROLLER_LOCK, MANIFEST_NAME, ResumeConflict, run_daily_resume,
)
from cctv_archive_manager.merge_planner import plan_merge_groups
from cctv_archive_manager.batch_merger import merge_groups


def find_executable(name: str) -> str:
    env_var = "FFMPEG_EXE" if name == "ffmpeg" else "MKVMERGE_EXE"
    override = os.environ.get(env_var)
    candidates = ([override] if override else [])
    found = shutil.which(name)
    if found:
        candidates.append(found)
    if os.name == "nt":
        if name == "mkvmerge":
            candidates += [r"C:\Program Files\MKVToolNix\mkvmerge.exe",
                           r"C:\Program Files (x86)\MKVToolNix\mkvmerge.exe"]
        else:
            candidates += [r"C:\ffmpeg\bin\ffmpeg.exe", r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"]
    for candidate in candidates:
        if candidate and (Path(candidate).is_file() or shutil.which(candidate)):
            return str(candidate)
    raise RuntimeError(f"{name} not found. Set ${env_var} in PowerShell to its executable path.")


def call(argv, timeout=60):
    proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if proc.returncode:
        raise RuntimeError(f"Command failed ({proc.returncode}): {argv[0]}\n{proc.stderr[-1500:]}")
    return proc


def main() -> int:
    ffmpeg = find_executable("ffmpeg")
    mkvmerge = find_executable("mkvmerge")
    print("ffmpeg:", ffmpeg, flush=True)
    print("mkvmerge:", mkvmerge, flush=True)
    base = Path(tempfile.mkdtemp(prefix="cctv-part5-resume-integration-"))
    sources = base / "synthetic_sources"
    out = base / "outputs"
    sources.mkdir()
    out.mkdir()
    print("ISOLATED TEST DIR:", base, flush=True)
    print("No NVR or SQLite paths will be accessed.", flush=True)

    groups = []
    source_meta = {}
    for date in ("2026-09-03", "2026-09-04"):
        day_files = []
        for i in range(2):
            f = sources / f"{date}_00-0{i}-00_000.mkv"
            # 1-second synthetic video, no audio, no external media.
            call([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                  "-f", "lavfi", "-i", f"color=c={'red' if i == 0 else 'blue'}:s=160x90:r=10",
                  "-t", "1", "-an", "-c:v", "mpeg4", "-q:v", "5", str(f)])
            day_files.append(f)
            st = f.stat()
            source_meta[f.name] = (st.st_size, st.st_mtime_ns)
        groups.extend(plan_merge_groups(day_files, 1))

    cancel = threading.Event()
    real_merge_calls = []

    def merge_one(group, destination, event, on_progress):
        real_merge_calls.append(group.output_name)
        merge_groups([group], destination.parent, mkvmerge=mkvmerge, cancel_event=event,
                     on_progress=lambda _index, _total, percent: on_progress(percent))
        # Simulate deliberate interruption immediately after first successful
        # real merge. run_daily_resume will checkpoint it, then stop next group.
        if len(real_merge_calls) == 1:
            cancel.set()

    print("\nPHASE 1: Merge first group, then interrupt", flush=True)
    first = run_daily_resume(groups, out, cancel_event=cancel, merge_one=merge_one)
    assert first.cancelled and len(first.completed) == 1 and not first.skipped, first
    assert len(real_merge_calls) == 1
    print("PASS: first completed and safely checkpointed", first, flush=True)

    first_path = out / groups[0].output_name
    first_stat = first_path.stat()
    assert first_stat.st_size > 0
    call([mkvmerge, "-J", str(first_path)])

    print("\nPHASE 2: Resume only second group", flush=True)
    second = run_daily_resume(groups, out, merge_one=merge_one)
    assert not second.cancelled and len(second.completed) == 1 and len(second.skipped) == 1, second
    assert len(real_merge_calls) == 2
    assert first_path.stat().st_size == first_stat.st_size
    assert first_path.stat().st_mtime_ns == first_stat.st_mtime_ns
    print("PASS: first output untouched; second completed", second, flush=True)

    print("\nPHASE 3: Third run skips both groups", flush=True)
    third = run_daily_resume(groups, out, merge_one=merge_one)
    assert not third.completed and len(third.skipped) == 2 and len(real_merge_calls) == 2, third
    print("PASS: no extra merge launched", third, flush=True)

    journal = json.loads((out / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert len(journal["completed"]) == 2
    for group in groups:
        output = out / group.output_name
        assert output.is_file() and output.stat().st_size > 0
        call([mkvmerge, "-J", str(output)])
        assert not output.with_name(output.stem + ".partial.mkv").exists()
        assert not output.with_name(output.name + ".lock").exists()
    assert not (out / CONTROLLER_LOCK).exists()
    for f in sources.iterdir():
        st = f.stat()
        assert source_meta[f.name] == (st.st_size, st.st_mtime_ns)
    print("\nPHASE 4: Verify existing unrelated output is protected", flush=True)
    other = base / "untracked_outputs"
    other.mkdir()
    preexisting = other / groups[0].output_name
    preexisting.write_bytes(b"do not overwrite")
    try:
        run_daily_resume(groups, other, merge_one=merge_one)
    except ResumeConflict:
        pass
    else:
        raise AssertionError("Existing unknown output was not rejected")
    assert preexisting.read_bytes() == b"do not overwrite"
    assert len(real_merge_calls) == 2
    print("PASS: untracked existing file protected", flush=True)
    print("\nFINAL RESULT: PASS", flush=True)
    print("Test artifacts preserved at:", base)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("\nFINAL RESULT: FAIL", file=sys.stderr)
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise
