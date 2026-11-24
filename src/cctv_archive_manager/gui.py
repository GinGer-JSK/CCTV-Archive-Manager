"""CCTV Archive Manager Lite GUI.

Features: local SQLite catalogue, SMB scan, incremental sync, merge plan,
single-group merging and sequential batch merging with mkvmerge progress.
Does not modify source MKVs.
"""

import os
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QGroupBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
    QProgressBar, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import __version__
from .catalog_reader import get_cached_files, get_catalog_snapshot
from .file_scanner import scan_recordings
from .filename_parser import parse_recording_filename
from .incremental_sync import sync_new_recordings
from .merge_planner import plan_merge_groups
from .duration_workflow import prepare_duration_batch
from .progress_merger import MergeCancelled, merge_with_progress
from .batch_merger import BatchMergeCancelled, GroupMergeError, merge_groups

DEFAULT_SOURCE = r"\\nvr\NVR_Record\Door\video\WSGXN"
DEFAULT_DATABASE = r"C:\CCTV-Archive-Manager\data\catalog.sqlite3"
DEFAULT_OUTPUT = r"C:\CCTV-Archive-Manager\data\merge-output"


def format_duration_ms(value: int) -> str:
    """Display a cached approximate playback duration without modifying video."""
    seconds = max(0, round(value / 1000))
    hours, remain = divmod(seconds, 3600)
    minutes, seconds = divmod(remain, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


class BaseWorker(QThread):
    result_ready = Signal(object)
    error_occurred = Signal(str)


class ScanWorker(BaseWorker):
    def __init__(self, source, parent=None):
        super().__init__(parent)
        self.source = source

    def run(self):
        try:
            started = time.perf_counter()
            result = scan_recordings(self.source)
            self.result_ready.emit((result, time.perf_counter() - started))
        except Exception as exc:
            self.error_occurred.emit(str(exc))


class CacheWorker(BaseWorker):
    def __init__(self, database, source, parent=None):
        super().__init__(parent)
        self.database = database
        self.source = source

    def run(self):
        try:
            started = time.perf_counter()
            snapshot = get_catalog_snapshot(self.database, self.source)
            self.result_ready.emit((snapshot, time.perf_counter() - started))
        except Exception as exc:
            self.error_occurred.emit(str(exc))


class SyncWorker(BaseWorker):
    def __init__(self, source, database, parent=None):
        super().__init__(parent)
        self.source = source
        self.database = database

    def run(self):
        try:
            result = sync_new_recordings(
                self.source, self.database, minimum_age_seconds=900
            )
            snapshot = get_catalog_snapshot(self.database, self.source)
            self.result_ready.emit((result, snapshot))
        except Exception as exc:
            self.error_occurred.emit(str(exc))


class PlanWorker(BaseWorker):
    def __init__(self, source, database, date, parts, output, use_cache, mode="count", parent=None):
        super().__init__(parent)
        self.source = Path(source)
        self.database = Path(database)
        self.date = date
        self.parts = parts
        self.output = Path(output)
        self.use_cache = use_cache
        self.mode = mode

    def run(self):
        try:
            started = time.perf_counter()
            if not self.output.is_dir():
                raise NotADirectoryError(f"출력 폴더가 없습니다: {self.output}")
            if self.output.resolve().is_relative_to(self.source.resolve()):
                raise ValueError("출력 폴더는 원본 폴더 밖에 있어야 합니다.")

            if self.mode == "duration":
                if not self.use_cache:
                    raise ValueError("B 방식은 SQLite 캐시 목록에서만 사용할 수 있습니다. 먼저 SQLite 캐시를 불러오세요.")
                # This may update duration columns in the *selected* local DB.
                # Never migrate a catalog automatically when its source key differs.
                indexed = get_cached_files(self.database, self.source, self.date)
                if not indexed:
                    raise ValueError(
                        "선택한 SQLite DB의 원본 경로가 현재 NVR 경로와 일치하지 않습니다. "
                        "IP 경로로 매핑한 테스트 DB를 선택하고 캐시 목록을 다시 불러오세요."
                    )
                batch = prepare_duration_batch(self.database, self.source, self.date, self.parts)
                groups = list(batch.groups)
            elif self.mode == "count":
                if self.use_cache:
                    files = get_cached_files(self.database, self.source, self.date)
                else:
                    files = []
                    with os.scandir(self.source) as entries:
                        for entry in entries:
                            if not entry.name.lower().endswith(".mkv"):
                                continue
                            info = parse_recording_filename(entry.name)
                            if info is not None and info.date == self.date:
                                files.append(Path(entry.path))
                groups = plan_merge_groups(files, self.parts)
            else:
                raise ValueError(f"지원하지 않는 분할 방식: {self.mode}")

            for group in groups:
                destination = self.output / group.output_name
                partial = destination.with_name(destination.stem + ".partial.mkv")
                lock = destination.with_name(destination.name + ".lock")
                for existing in (destination, partial, lock):
                    if existing.exists():
                        raise FileExistsError(
                            f"병합 결과물·임시 파일·잠금 파일이 이미 존재합니다: {existing}"
                        )
            self.result_ready.emit((groups, time.perf_counter() - started))
        except Exception as exc:
            self.error_occurred.emit(str(exc))


class MergeWorker(BaseWorker):
    """Execute one merge and forward mkvmerge progress to the GUI."""

    progress_changed = Signal(int)
    cancelled = Signal(str)

    def __init__(self, files, output, parent=None):
        super().__init__(parent)
        self.files = tuple(files)
        self.output = Path(output)
        self.cancel_event = threading.Event()

    def request_cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            started = time.perf_counter()
            result = merge_with_progress(
                self.files, self.output,
                on_progress=self.progress_changed.emit,
                cancel_event=self.cancel_event,
            )
            self.result_ready.emit((result, time.perf_counter() - started))
        except MergeCancelled as exc:
            self.cancelled.emit(str(exc))
        except Exception as exc:
            self.error_occurred.emit(str(exc))


class BatchMergeWorker(BaseWorker):
    """Run all planned groups sequentially, leaving the GUI responsive."""

    group_progress_changed = Signal(int, int, int)
    batch_failed = Signal(object)
    batch_cancelled = Signal(object)

    def __init__(self, groups, output_dir, parent=None):
        super().__init__(parent)
        # Snapshot the immutable plan before the background task begins.
        self.groups = tuple(groups)
        self.output_dir = Path(output_dir)
        self.cancel_event = threading.Event()

    def request_cancel(self):
        self.cancel_event.set()

    def run(self):
        started = time.perf_counter()
        try:
            result = merge_groups(
                self.groups,
                self.output_dir,
                on_progress=self.group_progress_changed.emit,
                cancel_event=self.cancel_event,
            )
            self.result_ready.emit((result, time.perf_counter() - started))
        except BatchMergeCancelled as exc:
            self.batch_cancelled.emit((exc, time.perf_counter() - started))
        except Exception as exc:
            self.batch_failed.emit((exc, time.perf_counter() - started))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.worker = None
        self.current_groups = []
        self.view_mode = None
        self.setWindowTitle(f"CCTV Archive Manager Lite - {__version__}")
        self.resize(1100, 860)
        self.setMinimumSize(850, 640)
        self.setup_ui()

    def setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(10)

        title = QLabel("CCTV Archive Manager Lite")
        title.setStyleSheet("font-size: 22px; font-weight: bold;")
        layout.addWidget(title)
        layout.addWidget(QLabel("MKV 검색 · SQLite 캐시 · 증분 동기화 · 병합 계획 · 단일·전체 그룹 병합"))

        layout.addWidget(QLabel("원본 MKV 폴더"))
        source_row = QHBoxLayout()
        self.source_edit = QLineEdit(DEFAULT_SOURCE)
        self.source_button = QPushButton("폴더 선택")
        self.source_button.clicked.connect(self.browse_source)
        source_row.addWidget(self.source_edit, 1)
        source_row.addWidget(self.source_button)
        layout.addLayout(source_row)

        layout.addWidget(QLabel("SQLite 캐시 파일"))
        database_row = QHBoxLayout()
        self.database_edit = QLineEdit(DEFAULT_DATABASE)
        self.database_button = QPushButton("DB 선택")
        self.database_button.clicked.connect(self.browse_database)
        database_row.addWidget(self.database_edit, 1)
        database_row.addWidget(self.database_button)
        layout.addLayout(database_row)

        search_row = QHBoxLayout()
        self.cache_button = QPushButton("SQLite 캐시 불러오기")
        self.sync_button = QPushButton("NVR 증분 동기화")
        self.scan_button = QPushButton("NVR 직접 검색 (SMB)")
        for button in (self.cache_button, self.sync_button, self.scan_button):
            button.setMinimumHeight(38)
            search_row.addWidget(button)
        self.cache_button.clicked.connect(self.start_cache)
        self.sync_button.clicked.connect(self.start_sync)
        self.scan_button.clicked.connect(self.start_scan)
        layout.addLayout(search_row)

        self.mode_label = QLabel("조회 방식: 선택되지 않음")
        self.summary_label = QLabel("검색된 파일이 없습니다.")
        layout.addWidget(self.mode_label)
        layout.addWidget(self.summary_label)

        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["녹화 날짜", "파일 수", "용량 (GiB)"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 3)

        group_box = QGroupBox("병합 계획 및 실행")
        group_layout = QVBoxLayout(group_box)
        self.selected_date_label = QLabel("선택한 날짜: 없음")
        group_layout.addWidget(self.selected_date_label)

        output_row = QHBoxLayout()
        output_row.addWidget(QLabel("출력 폴더"))
        self.output_edit = QLineEdit(DEFAULT_OUTPUT)
        self.output_button = QPushButton("폴더 선택")
        self.output_button.clicked.connect(self.browse_output)
        output_row.addWidget(self.output_edit, 1)
        output_row.addWidget(self.output_button)
        group_layout.addLayout(output_row)

        parts_row = QHBoxLayout()
        parts_row.addWidget(QLabel("출력 MKV 파일 개수"))
        self.parts_spin = QSpinBox()
        self.parts_spin.setRange(1, 9999)
        self.parts_spin.setValue(3)
        parts_row.addWidget(self.parts_spin)
        parts_row.addStretch()
        group_layout.addLayout(parts_row)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("분할 방식"))
        self.split_mode = QComboBox()
        self.split_mode.addItem("A · 파일 개수 균등 (기존)", "count")
        self.split_mode.addItem("B · 실제 재생시간 균등 (SQLite)", "duration")
        self.split_mode.setToolTip("B 방식은 선택한 SQLite DB에 MKV 재생시간을 저장할 수 있습니다.")
        mode_row.addWidget(self.split_mode, 1)
        group_layout.addLayout(mode_row)

        self.plan_button = QPushButton("선택한 날짜의 병합 계획 생성")
        self.plan_button.setMinimumHeight(36)
        self.plan_button.clicked.connect(self.start_plan)
        group_layout.addWidget(self.plan_button)

        self.plan_table = QTableWidget()
        self.plan_table.setColumnCount(5)
        self.plan_table.setHorizontalHeaderLabels(
            ["출력 파일명", "입력 개수", "예상 재생시간", "첫 번째 원본", "마지막 원본"]
        )
        self.plan_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.plan_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.plan_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.plan_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        group_layout.addWidget(self.plan_table)

        self.merge_button = QPushButton("선택한 그룹 실제 MKV 병합 실행")
        self.merge_button.setMinimumHeight(38)
        self.merge_button.setEnabled(False)
        self.merge_button.clicked.connect(self.start_merge)
        self.merge_all_button = QPushButton("전체 그룹 순차 병합 실행")
        self.merge_all_button.setMinimumHeight(38)
        self.merge_all_button.setEnabled(False)
        self.merge_all_button.clicked.connect(self.start_batch_merge)
        group_layout.addWidget(self.merge_button)
        group_layout.addWidget(self.merge_all_button)

        self.cancel_button = QPushButton("현재 병합 작업 취소")
        self.cancel_button.setMinimumHeight(38)
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.request_merge_cancel)
        group_layout.addWidget(self.cancel_button)

        self.batch_progress_label = QLabel("전체 그룹 진행률: 대기 중")
        group_layout.addWidget(self.batch_progress_label)
        self.batch_progress_bar = QProgressBar()
        self.batch_progress_bar.setRange(0, 100)
        self.batch_progress_bar.setValue(0)
        group_layout.addWidget(self.batch_progress_bar)

        self.progress_label = QLabel("병합 진행률: 대기 중")
        group_layout.addWidget(self.progress_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")
        group_layout.addWidget(self.progress_bar)
        layout.addWidget(group_box, 2)

        self.status_label = QLabel("준비 완료")
        layout.addWidget(self.status_label)
        self.statusBar().showMessage("원본 영상은 변경하지 않습니다.")

        self.table.itemSelectionChanged.connect(self.on_date_changed)
        self.plan_table.itemSelectionChanged.connect(self.update_merge_button)
        self.source_edit.textChanged.connect(self.clear_results)
        self.database_edit.textChanged.connect(self.clear_results)
        self.output_edit.textChanged.connect(self.clear_plan)
        self.parts_spin.valueChanged.connect(self.clear_plan)
        self.split_mode.currentIndexChanged.connect(self.clear_plan)

    def selected_date(self):
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        return item.text() if item is not None else None

    def clear_plan(self, *_args):
        self.current_groups = []
        self.plan_table.setRowCount(0)
        self.merge_button.setEnabled(False)
        self.merge_all_button.setEnabled(False)

    def clear_results(self, *_args):
        self.view_mode = None
        self.clear_plan()
        self.table.setRowCount(0)
        self.summary_label.setText("다시 검색하거나 캐시를 불러와 주세요.")
        self.mode_label.setText("조회 방식: 선택되지 않음")
        self.selected_date_label.setText("선택한 날짜: 없음")

    def on_date_changed(self):
        self.clear_plan()
        self.selected_date_label.setText(
            f"선택한 날짜: {self.selected_date() or '없음'}"
        )

    def browse_source(self):
        folder = QFileDialog.getExistingDirectory(
            self, "원본 MKV 폴더 선택", self.source_edit.text()
        )
        if folder:
            self.source_edit.setText(folder)

    def browse_database(self):
        filename, _ = QFileDialog.getOpenFileName(
            self, "SQLite 캐시 파일 선택",
            str(Path(self.database_edit.text()).parent),
            "SQLite Files (*.sqlite3 *.db);;All Files (*)",
        )
        if filename:
            self.database_edit.setText(filename)

    def browse_output(self):
        folder = QFileDialog.getExistingDirectory(
            self, "출력 폴더 선택", self.output_edit.text()
        )
        if folder:
            self.output_edit.setText(folder)

    def set_busy(self, busy):
        widgets = (
            self.source_edit, self.source_button, self.database_edit,
            self.database_button, self.cache_button, self.sync_button,
            self.scan_button, self.output_edit, self.output_button,
            self.parts_spin, self.split_mode, self.plan_button, self.table, self.plan_table,
        )
        for widget in widgets:
            widget.setEnabled(not busy)
        self.update_merge_button()
        self.cancel_button.setEnabled(
            busy
            and isinstance(self.worker, (MergeWorker, BatchMergeWorker))
            and not self.worker.cancel_event.is_set()
        )

    def update_merge_button(self):
        row = self.plan_table.currentRow()
        valid = (
            self.worker is None
            and bool(self.current_groups)
            and 0 <= row < len(self.current_groups)
            and bool(self.plan_table.selectedItems())
        )
        self.merge_button.setEnabled(valid)
        self.merge_all_button.setEnabled(
            self.worker is None and bool(self.current_groups)
        )

    def request_merge_cancel(self):
        """Non-blocking cancellation; never wait on the GUI thread."""
        worker = self.worker
        if not isinstance(worker, (MergeWorker, BatchMergeWorker)):
            return
        if worker.cancel_event.is_set():
            return
        worker.request_cancel()
        self.cancel_button.setEnabled(False)
        self.status_label.setText("병합 취소 요청됨 — 프로세스 종료를 확인 중...")
        self.statusBar().showMessage(
            "취소를 처리하고 있습니다. 작업이 종료될 때까지 창을 유지하세요. "
            "SMB 사전 검증 중에는 취소 반영이 지연될 수 있습니다."
        )

    def show_merge_cancelled(self, message):
        self.progress_label.setText("병합 취소됨")
        self.status_label.setText("병합 취소 완료")
        self.statusBar().showMessage(
            "미완료 .partial.mkv는 보존됩니다. 재시도 전에 확인하세요."
        )
        self.clear_plan()
        QMessageBox.information(
            self, "MKV 병합 취소",
            f"{message}\n\n최종 출력 파일은 생성되지 않았습니다. "
            "미완료 파일은 보존될 수 있습니다.",
        )

    def show_batch_cancelled(self, payload):
        exc, elapsed = payload
        completed_paths = "\n".join(str(item.output) for item in exc.completed)
        self.progress_label.setText("현재 그룹 병합 취소됨")
        self.batch_progress_label.setText(
            f"전체 그룹 취소: {len(exc.completed)}/{exc.total_groups}개 완료"
        )
        self.status_label.setText("전체 그룹 병합 취소 완료")
        self.statusBar().showMessage(
            "완료된 MKV는 보존됩니다. 미완료 .partial.mkv는 재시도 전에 확인하세요."
        )
        self.clear_plan()
        QMessageBox.information(
            self, "전체 MKV 병합 취소",
            f"취소 그룹: {exc.cancelled_group}/{exc.total_groups}\n"
            f"완료된 그룹: {len(exc.completed)}개\n"
            f"소요시간: {elapsed:.2f}초\n\n"
            f"보존된 결과물:\n{completed_paths or '없음'}\n\n"
            "취소된 그룹의 .partial.mkv는 재생되지 않을 수 있습니다.",
        )

    def start_worker(self, worker):
        if self.worker is not None:
            return
        self.worker = worker
        self.set_busy(True)
        worker.error_occurred.connect(self.show_error)
        worker.finished.connect(self.finish_work)
        worker.start()

    def finish_work(self):
        worker = self.worker
        if worker is not None:
            worker.deleteLater()
        self.worker = None
        self.set_busy(False)

    def start_cache(self):
        if self.worker is not None:
            return
        database = self.database_edit.text().strip()
        source = self.source_edit.text().strip()
        if not database or not source:
            QMessageBox.warning(self, "경로 확인", "원본 경로와 DB 경로를 입력해 주세요.")
            return
        self.clear_results()
        self.status_label.setText("SQLite 캐시 조회 중...")
        worker = CacheWorker(database, source, self)
        worker.result_ready.connect(self.show_cache_result)
        self.start_worker(worker)

    def start_sync(self):
        if self.worker is not None:
            return
        source = self.source_edit.text().strip()
        database = self.database_edit.text().strip()
        if not source or not database:
            QMessageBox.warning(self, "경로 확인", "원본 폴더와 SQLite DB를 입력해 주세요.")
            return
        self.clear_plan()
        self.status_label.setText("NVR 증분 동기화 진행 중...")
        worker = SyncWorker(source, database, self)
        worker.result_ready.connect(self.show_sync_result)
        self.start_worker(worker)

    def start_scan(self):
        if self.worker is not None:
            return
        source = self.source_edit.text().strip()
        if not source:
            QMessageBox.warning(self, "원본 폴더 오류", "원본 MKV 폴더를 입력해 주세요.")
            return
        self.clear_results()
        self.status_label.setText("NVR SMB 파일 검색 중...")
        worker = ScanWorker(source, self)
        worker.result_ready.connect(self.show_scan_result)
        self.start_worker(worker)

    def populate_days(self, days):
        self.table.setSortingEnabled(False)
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        self.table.setRowCount(len(days))
        for row, day in enumerate(days):
            date_item = QTableWidgetItem(day.date)
            count_item = QTableWidgetItem()
            count_item.setData(Qt.ItemDataRole.DisplayRole, day.file_count)
            count_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            size_item = QTableWidgetItem()
            size_item.setData(
                Qt.ItemDataRole.DisplayRole, round(day.total_bytes / 1024 ** 3, 2)
            )
            size_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(row, 0, date_item)
            self.table.setItem(row, 1, count_item)
            self.table.setItem(row, 2, size_item)
        self.table.setSortingEnabled(True)

    def show_cache_result(self, payload):
        snapshot, elapsed = payload
        self.view_mode = "cache"
        self.populate_days(snapshot.days)
        self.summary_label.setText(
            f"캐시 MKV: {snapshot.total_files:,}개 | "
            f"용량: {snapshot.total_bytes / 1024**3:,.2f} GiB | "
            f"녹화 날짜: {len(snapshot.days)}일"
        )
        self.mode_label.setText("조회 방식: SQLite 캐시 (원본 상태 미확인)")
        self.status_label.setText(f"캐시 조회 완료: {elapsed:.4f}초")
        self.statusBar().showMessage(
            f"마지막 파일 확인: {snapshot.last_seen_at or '기록 없음'} "
            "(UTC, 전체 동기화 시각 아님)"
        )

    def show_sync_result(self, payload):
        result, snapshot = payload
        previous_date = self.selected_date()
        self.view_mode = "cache"
        self.clear_plan()
        self.populate_days(snapshot.days)
        self.summary_label.setText(
            f"캐시 MKV: {snapshot.total_files:,}개 | "
            f"용량: {snapshot.total_bytes / 1024**3:,.2f} GiB | "
            f"녹화 날짜: {len(snapshot.days)}일"
        )
        self.mode_label.setText("조회 방식: SQLite 캐시 (증분 동기화 완료, 기존 파일 미재검증)")
        if previous_date:
            for row in range(self.table.rowCount()):
                item = self.table.item(row, 0)
                if item is not None and item.text() == previous_date:
                    self.table.selectRow(row)
                    break
        self.status_label.setText(
            f"동기화 완료: {result.elapsed_seconds:.3f}초 | "
            f"신규 {result.added}개 | 기존 {result.already_indexed}개 | "
            f"보류 {result.skipped_recent}개"
        )
        self.statusBar().showMessage(
            f"발견: {result.total_mkv:,}개 | 신규: {result.added}개 | "
            f"원본 미발견: {result.missing_from_source}개 | "
            f"파일명 불일치: {result.invalid_names}개 | "
            f"조회 오류: {result.metadata_errors}개"
        )
        if result.metadata_errors:
            QMessageBox.warning(
                self, "일부 파일 조회 실패",
                f"신규 파일 메타데이터 조회 오류 {result.metadata_errors}건. "
                "해당 파일은 이번 동기화에서 제외됐습니다.",
            )

    def show_scan_result(self, payload):
        scan, elapsed = payload
        self.view_mode = "direct"
        self.populate_days(scan.days)
        self.summary_label.setText(
            f"전체 MKV: {scan.total_mkv:,}개 | 정상: {scan.valid_mkv:,}개 | "
            f"규칙 불일치: {scan.invalid_mkv:,}개 | "
            f"용량: {scan.total_bytes / 1024**3:,.2f} GiB | "
            f"녹화 날짜: {len(scan.days)}일"
        )
        self.mode_label.setText("조회 방식: NVR 직접 검색 (SQLite 캐시 미반영)")
        self.status_label.setText(f"NVR 검색 완료: {elapsed:.3f}초")
        self.statusBar().showMessage("NVR 직접 검색 완료. 원본 변경 없음.")

    def start_plan(self):
        if self.worker is not None:
            return
        date = self.selected_date()
        if date is None or self.view_mode is None:
            QMessageBox.warning(self, "날짜 선택", "먼저 목록을 불러오고 날짜를 선택해 주세요.")
            return
        self.clear_plan()
        self.status_label.setText(f"{date} 병합 계획 생성 중...")
        worker = PlanWorker(
            source=self.source_edit.text().strip(),
            database=self.database_edit.text().strip(),
            date=date,
            parts=self.parts_spin.value(),
            output=self.output_edit.text().strip(),
            use_cache=self.view_mode == "cache",
            mode=self.split_mode.currentData(),
            parent=self,
        )
        worker.result_ready.connect(self.show_plan)
        self.start_worker(worker)

    def show_plan(self, payload):
        groups, elapsed = payload
        self.current_groups = groups
        self.plan_table.setRowCount(len(groups))
        for row, group in enumerate(groups):
            values = [
                group.output_name, len(group.files),
                format_duration_ms(group.total_duration_ms)
                if hasattr(group, "total_duration_ms") else "—",
                group.files[0].name, group.files[-1].name,
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem()
                if col == 1:
                    item.setData(Qt.ItemDataRole.DisplayRole, value)
                else:
                    item.setText(str(value))
                self.plan_table.setItem(row, col, item)
        self.status_label.setText(
            f"병합 계획 완료 ({self.split_mode.currentText()}): "
            f"{len(groups)}개 | 소요시간: {elapsed:.4f}초"
        )
        self.statusBar().showMessage(
            "한 그룹은 행을 선택하여 실행하고, 전체 그룹은 순차 병합 버튼으로 실행합니다."
        )
        self.update_merge_button()

    def start_merge(self):
        if self.worker is not None:
            return
        row = self.plan_table.currentRow()
        if not (0 <= row < len(self.current_groups)) or not self.plan_table.selectedItems():
            QMessageBox.warning(self, "병합 그룹 선택", "병합할 그룹을 선택해 주세요.")
            return
        group = self.current_groups[row]
        inputs = tuple(group.files)
        destination = Path(self.output_edit.text().strip()) / group.output_name
        partial = destination.with_name(destination.stem + ".partial.mkv")

        # Cheap checks in UI thread. Full input validation is done in merger.py worker.
        if not destination.parent.is_dir():
            QMessageBox.warning(self, "출력 폴더 오류", f"출력 폴더가 없습니다:\n{destination.parent}")
            return
        if destination.exists() or partial.exists():
            QMessageBox.warning(
                self, "덮어쓰기 방지", f"기존 결과물 또는 임시 파일이 있습니다:\n{destination}"
            )
            return
        if len(inputs) < 2:
            QMessageBox.warning(self, "입력 파일 오류", "병합에는 최소 2개 파일이 필요합니다.")
            return

        answer = QMessageBox.question(
            self,
            "실제 MKV 병합 실행 확인",
            f"선택한 그룹 1개만 병합합니다.\n\n"
            f"입력 MKV: {len(inputs)}개\n"
            f"출력 파일: {destination}\n\n"
            "원본을 변경하지 않고 로컬 출력 파일을 생성합니다.\n"
            "계속하시겠습니까?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self.progress_bar.setValue(0)
        self.progress_label.setText("병합 진행률: 0% (시작 중)")
        self.status_label.setText(f"실제 병합 실행 중: {group.output_name}")
        self.statusBar().showMessage(
            "mkvmerge 진행률을 수집 중입니다. 취소 버튼을 사용할 수 있습니다."
        )
        worker = MergeWorker(inputs, destination, self)
        worker.progress_changed.connect(self.show_merge_progress)
        worker.cancelled.connect(self.show_merge_cancelled)
        worker.result_ready.connect(self.show_merge_result)
        self.start_worker(worker)

    def show_merge_progress(self, percent):
        self.progress_bar.setValue(percent)
        self.progress_label.setText(f"병합 진행률: {percent}%")

    def show_merge_result(self, payload):
        result, elapsed = payload
        self.progress_bar.setValue(100)
        self.progress_label.setText("병합 진행률: 100% (완료)")
        self.status_label.setText(
            f"병합 성공: {result.output.name} | "
            f"{result.output_bytes:,} bytes | {elapsed:.2f}초"
        )
        self.statusBar().showMessage(f"최종 결과물: {result.output}")
        self.clear_plan()  # The plan may now contain an existing output file.
        QMessageBox.information(
            self,
            "MKV 병합 완료",
            f"병합이 완료됐습니다.\n\n"
            f"출력 파일: {result.output}\n"
            f"입력 파일: {result.input_count}개\n"
            f"결과 크기: {result.output_bytes:,} bytes\n"
            f"소요시간: {elapsed:.2f}초\n\n"
            "출력 영상은 별도 플레이어에서 재생 검증해 주세요.",
        )

    def start_batch_merge(self):
        """Run all groups in the current preview, sequentially."""
        if self.worker is not None or not self.current_groups:
            return

        groups = tuple(self.current_groups)
        output_dir = Path(self.output_edit.text().strip())
        if not output_dir.is_dir():
            QMessageBox.warning(self, "출력 폴더 오류", f"출력 폴더가 없습니다:\n{output_dir}")
            return

        # Local output-only checks, avoiding slow SMB metadata on GUI thread.
        for group in groups:
            destination = output_dir / group.output_name
            partial = destination.with_name(destination.stem + ".partial.mkv")
            lock = destination.with_name(destination.name + ".lock")
            for path in (destination, partial, lock):
                if path.exists():
                    QMessageBox.warning(
                        self, "병합 실행 불가",
                        f"기존 출력·임시·잠금 파일이 있습니다:\n{path}\n\n"
                        "이번 실행에서는 기존 파일을 덮어쓰지 않습니다.",
                    )
                    return

        total_inputs = sum(len(group.files) for group in groups)
        summary = "\n".join(
            f"{index}. {group.output_name} ({len(group.files)}개"
            + (f", {format_duration_ms(group.total_duration_ms)}" if hasattr(group, "total_duration_ms") else "")
            + ")"
            for index, group in enumerate(groups, 1)
        )
        answer = QMessageBox.question(
            self, "전체 그룹 순차 병합 확인",
            f"출력 파일: {len(groups)}개\n"
            f"입력 MKV: 총 {total_inputs}개\n"
            f"출력 폴더: {output_dir}\n\n"
            f"{summary}\n\n"
            "각 그룹을 순서대로 병합하고 오류가 발생하면 즉시 중단합니다.\n"
            "실행 중 취소할 수 있으며 완료 그룹은 보존됩니다. 계속하시겠습니까?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self.progress_bar.setValue(0)
        self.progress_label.setText("현재 그룹 진행률: 0% (시작 중)")
        self.batch_progress_bar.setValue(0)
        self.batch_progress_label.setText(
            f"전체 그룹 진행률: 0% (0/{len(groups)}개 완료)"
        )
        self.status_label.setText(f"전체 {len(groups)}개 그룹 순차 병합 시작")
        self.statusBar().showMessage(
            "실행 전 전체 원본을 검증합니다. 이 단계는 SMB에서 시간이 걸릴 수 있습니다."
        )

        worker = BatchMergeWorker(groups, output_dir, self)
        worker.group_progress_changed.connect(self.show_batch_progress)
        worker.result_ready.connect(self.show_batch_result)
        worker.batch_failed.connect(self.show_batch_error)
        worker.batch_cancelled.connect(self.show_batch_cancelled)
        self.start_worker(worker)

    def show_batch_progress(self, index, total, percent):
        # MKVToolNix progress=100 means a group is near completion, not that
        # the output was already renamed/validated. Avoid claiming completion.
        self.progress_bar.setValue(percent)
        self.progress_label.setText(
            f"현재 그룹 진행률: {index}/{total} · {percent}%"
        )
        overall = round(((index - 1) + percent / 100) / total * 100)
        self.batch_progress_bar.setValue(overall)
        self.batch_progress_label.setText(
            f"전체 그룹 진행률: 약 {overall}% "
            f"({index}/{total}번째 처리 중)"
        )
        self.status_label.setText(f"MKV 그룹 {index}/{total} 병합 중")

    def show_batch_result(self, payload):
        result, elapsed = payload
        self.progress_bar.setValue(100)
        self.progress_label.setText("현재 그룹 진행률: 100% (완료)")
        self.batch_progress_bar.setValue(100)
        self.batch_progress_label.setText(
            f"전체 그룹 진행률: 100% ({len(result.completed)}/{result.total_groups}개 완료)"
        )
        self.status_label.setText(
            f"전체 그룹 병합 성공: {len(result.completed)}개 | {elapsed:.2f}초"
        )
        completed_paths = "\n".join(str(item.output) for item in result.completed)
        self.statusBar().showMessage("전체 그룹 병합 성공. 결과 파일을 재생 검증하세요.")
        self.clear_plan()  # Prevent reuse of a plan containing completed outputs.
        QMessageBox.information(
            self, "전체 MKV 병합 완료",
            f"{len(result.completed)}개 그룹을 모두 병합했습니다.\n"
            f"실행시간: {elapsed:.2f}초\n\n"
            f"완료 파일:\n{completed_paths}\n\n"
            "영상 연결부는 플레이어로 별도 확인해 주세요.",
        )

    def show_batch_error(self, payload):
        exc, elapsed = payload
        if isinstance(exc, GroupMergeError):
            completed_count = len(exc.completed)
            total = exc.total_groups
            completed_paths = "\n".join(str(item.output) for item in exc.completed)
            description = (
                f"실패 그룹: {exc.failed_group}/{total}\n"
                f"완료 그룹: {completed_count}/{total}\n"
                f"처리시간: {elapsed:.2f}초\n\n"
                f"오류 내용: {exc}\n\n"
                f"완료된 파일(보존됨):\n{completed_paths or '없음'}"
            )
            self.batch_progress_label.setText(
                f"전체 그룹 병합 중단: {completed_count}/{total}개 완료"
            )
        else:
            # Errors in the all-groups preflight happen before any merge starts.
            description = f"사전 검증 또는 실행 오류:\n{exc}"
            self.batch_progress_label.setText("전체 그룹 병합 실패: 사전 검증/실행 오류")
        self.progress_label.setText(
            f"현재 그룹 중단 (마지막 표시: {self.progress_bar.value()}%)"
        )
        self.status_label.setText("전체 그룹 병합 중단")
        self.statusBar().showMessage(
            "완료된 출력 파일은 보존합니다. 재시도 전에 출력·잠금·임시 파일을 확인하세요."
        )
        self.clear_plan()
        QMessageBox.critical(self, "전체 MKV 병합 실패", description)

    def show_error(self, message):
        if isinstance(self.worker, MergeWorker):
            self.progress_label.setText(
                f"병합 실패 (최종 표시 진행률: {self.progress_bar.value()}%)"
            )
        self.status_label.setText("작업 실패")
        QMessageBox.critical(self, "작업 오류", message)

    def closeEvent(self, event):
        worker = self.worker
        if worker is not None and worker.isRunning():
            if isinstance(worker, (MergeWorker, BatchMergeWorker)):
                if not worker.cancel_event.is_set():
                    answer = QMessageBox.question(
                        self, "병합 작업 진행 중",
                        "병합 중에는 바로 창을 닫을 수 없습니다.\n"
                        "현재 병합을 취소하시겠습니까?\n"
                        "취소가 완료되면 창을 다시 닫아 주세요.",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No,
                    )
                    if answer == QMessageBox.StandardButton.Yes:
                        self.request_merge_cancel()
                else:
                    self.statusBar().showMessage(
                        "취소 처리가 진행 중입니다. 완료 후 창을 닫아 주세요."
                    )
            else:
                QMessageBox.information(
                    self, "작업 진행 중", "현재 작업이 끝난 후 종료해 주세요."
                )
            event.ignore()
            return
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
