"""Isolated real MKV + PySide6 daily-GUI worker integration test.

Creates its own 8 synthetic MKVs, SQLite database, and output directory under
Windows TEMP. Does not access production sources, DB, or output folders.
Only the test process monkeypatches the daily_gui.run_daily_resume dispatcher
so that cancellation happens deterministically after one successful merge.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication, QMessageBox

from cctv_archive_manager import daily_gui
from cctv_archive_manager.catalog import index_recordings
from cctv_archive_manager.daily_resume import CONTROLLER_LOCK, MANIFEST_NAME, run_daily_resume
from cctv_archive_manager.batch_merger import merge_groups


def executable(name: str) -> str:
    env = "FFMPEG_EXE" if name == "ffmpeg" else "MKVMERGE_EXE"
    choices = [os.environ.get(env), shutil.which(name)]
    if name == "mkvmerge":
        choices += [r"C:\Program Files\MKVToolNix\mkvmerge.exe",
                    r"C:\Program Files (x86)\MKVToolNix\mkvmerge.exe"]
    for candidate in choices:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    raise RuntimeError(f"{name} not found. Set $env:{env} to the full executable path.")


def run_command(args: list[str], timeout: int = 90) -> None:
    proc = subprocess.run(args, text=True, encoding="utf-8", errors="replace",
                          capture_output=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(args[:3])}: {proc.stderr[-900:]}")


def await_worker(app: QApplication, dialog: daily_gui.DailyBatchDialog, phase: str,
                 errors: list[str], timeout: float = 90) -> None:
    started = time.monotonic()
    while dialog.worker is not None:
        app.processEvents()
        if time.monotonic() - started > timeout:
            # A running QThread must not be destroyed. Exit the process instead
            # of trying to close/delete an active worker.
            print(f"TIMEOUT: GUI worker in {phase}; test abandoned safely", flush=True)
            os._exit(3)
        time.sleep(0.01)
    app.processEvents()
    if errors:
        raise RuntimeError(f"{phase} GUI error: {errors[-1]}")


def verify_outputs(out: Path, groups: tuple, mkvmerge: str, expected_count: int) -> None:
    found = sorted(out.glob("*.mkv"))
    assert len(found) == expected_count, (len(found), expected_count)
    for output in found:
        assert output.stat().st_size > 0
        run_command([mkvmerge, "-J", str(output)])
    assert not list(out.glob("*.partial.mkv")), "unresolved partial MKV"
    assert not list(out.glob("*.mkv.lock")), "unresolved per-group lock"
    assert not (out / CONTROLLER_LOCK).exists(), "controller lock not released"
    manifest = json.loads((out / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert len(manifest["completed"]) == expected_count
    assert all(output.name in manifest["completed"] for output in found)
    # Step 9: GUI must save verified SHA-256 values and use them for resume.
    for output in found:
        record = manifest["completed"][output.name]
        assert len(record.get("sha256", "")) == 64, f"missing SHA-256 checkpoint: {output.name}"
        assert record.get("integrity_method") == "identify+sha256", record


def main() -> None:
    ffmpeg, mkvmerge = executable("ffmpeg"), executable("mkvmerge")
    base = Path(tempfile.mkdtemp(prefix="cctv-part5-gui-real-"))
    source, out = base / "synthetic_mkv", base / "output"
    source.mkdir()
    out.mkdir()
    db = base / "test-catalog.sqlite3"
    print("FFmpeg:", ffmpeg, flush=True)
    print("MKVToolNix:", mkvmerge, flush=True)
    print("ISOLATED TEST DIR:", base, flush=True)
    print("No NVR files, production SQLite, or project output files are touched.", flush=True)

    fingerprints = {}
    for day_idx, day in enumerate(("2026-09-03", "2026-09-04")):
        for i in range(4):
            fname = source / f"{day}_00-{i:02d}-00_000.mkv"
            run_command([ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                         "-f", "lavfi", "-i", f"color=c={'red' if day_idx == 0 else 'blue'}:s=160x90:r=10",
                         "-t", "1", "-an", "-c:v", "mpeg4", "-q:v", "5", str(fname)])
            st = fname.stat()
            fingerprints[str(fname)] = (st.st_size, st.st_mtime_ns)
    assert index_recordings(source, db) == 8

    app = QApplication.instance() or QApplication([])
    errors: list[str] = []
    real_merge_calls: list[str] = []
    phases = ["first", "resume", "all-skipped"]
    mode = ["first"]

    def synthetic_controlled_run(groups, output_dir, *, cancel_event=None, on_progress=None,
                                 integrity_verifier=None):
        def merge_one(group, destination, event, callback):
            real_merge_calls.append(group.output_name)
            merge_groups([group], destination.parent, mkvmerge=mkvmerge, cancel_event=event,
                         on_progress=lambda _i, _t, progress: callback(progress))
            if mode[0] == "first" and len(real_merge_calls) == 1:
                # Deterministic cancellation only after the first output was
                # successfully written. Allows run_daily_resume to checkpoint it.
                cancel_event.set()
        return run_daily_resume(groups, output_dir, cancel_event=cancel_event,
                                on_progress=on_progress, merge_one=merge_one,
                                integrity_verifier=integrity_verifier)

    dialog = daily_gui.DailyBatchDialog(str(source), str(db), str(out), 2, "count")
    dialog.start_edit.setText("2026-09-03")
    dialog.end_edit.setText("2026-09-04")
    dialog.parts_edit.setText("2")
    dialog.failed_messages = errors
    original_show_failure = dialog.show_failure

    def record_failure(msg):
        errors.append(str(msg))
        original_show_failure(msg)

    dialog.show_failure = record_failure
    # Modal calls are replaced only in the test harness; real GUI stays unchanged.
    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes), \
         patch.object(QMessageBox, "information", return_value=QMessageBox.StandardButton.Ok), \
         patch.object(QMessageBox, "critical", return_value=QMessageBox.StandardButton.Ok), \
         patch.object(daily_gui, "run_daily_resume", side_effect=synthetic_controlled_run):
        print("\nPHASE 0: GUI preview", flush=True)
        dialog.preview_button.click()
        await_worker(app, dialog, "preview", errors)
        assert len(dialog.groups) == 4, len(dialog.groups)
        assert [len(g.files) for g in dialog.groups] == [2, 2, 2, 2]
        assert dialog.table.rowCount() == 4
        assert dialog.execute_button.isEnabled()
        groups = tuple(dialog.groups)
        print("PASS: 2 days, 4 groups, 8 synthetic MKVs", flush=True)

        print("\nPHASE 1: GUI starts real merge, cancel after first completed group", flush=True)
        mode[0] = "first"
        dialog.execute_button.click()
        await_worker(app, dialog, "first execution", errors)
        assert len(real_merge_calls) == 1
        verify_outputs(out, groups, mkvmerge, 1)
        assert "취소" in dialog.status.text(), dialog.status.text()
        first_file = out / groups[0].output_name
        first_stat = (first_file.stat().st_size, first_file.stat().st_mtime_ns)
        print("PASS: first output saved/checkpointed; remaining 3 unstarted", flush=True)

        print("\nPHASE 2: GUI re-preview and resume the remaining 3 groups", flush=True)
        mode[0] = "resume"
        dialog.preview_button.click()
        await_worker(app, dialog, "re-preview", errors)
        assert len(dialog.groups) == 4
        dialog.execute_button.click()
        await_worker(app, dialog, "resume", errors)
        assert len(real_merge_calls) == 4, real_merge_calls
        verify_outputs(out, groups, mkvmerge, 4)
        assert (first_file.stat().st_size, first_file.stat().st_mtime_ns) == first_stat
        assert "기존 완료 1개" in dialog.status.text(), dialog.status.text()
        print("PASS: first output untouched, three outputs created", flush=True)

        print("\nPHASE 3: GUI third run skips all completed groups", flush=True)
        mode[0] = "all-skipped"
        dialog.preview_button.click()
        await_worker(app, dialog, "third preview", errors)
        dialog.execute_button.click()
        await_worker(app, dialog, "third execution", errors)
        assert len(real_merge_calls) == 4
        assert "기존 완료 4개" in dialog.status.text(), dialog.status.text()
        verify_outputs(out, groups, mkvmerge, 4)
        print("PASS: 4 groups skipped; zero new MKV merges", flush=True)

    for path, before in fingerprints.items():
        st = Path(path).stat()
        assert (st.st_size, st.st_mtime_ns) == before, f"source modified: {path}"
    print("\nSOURCE METADATA: UNCHANGED", flush=True)
    print("FINAL RESULT: PASS", flush=True)
    print("Test artifacts preserved at:", base, flush=True)
    dialog.close()
    app.quit()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("\nFINAL RESULT: FAIL", file=sys.stderr)
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise
