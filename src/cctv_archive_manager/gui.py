"""CCTV Archive Manager Lite GUI.

Features: local SQLite catalogue, SMB scan, incremental sync, merge plan,
and execution of one selected merge group with mkvmerge progress.
Does not modify source MKVs.
"""

import os
import sys
import time
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QGroupBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
    QProgressBar, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import __version__
from .catalog_reader import get_cached_files, get_catalog_snapshot
from .file_scanner import scan_recordings
from .filename_parser import parse_recording_filename
from .incremental_sync import sync_new_recordings
from .merge_planner import plan_merge_groups
from .progress_merger import merge_with_progress

DEFAULT_SOURCE = r"\\nvr\NVR_Record\Door\video\WSGXN"
DEFAULT_DATABASE = r"C:\CCTV-Archive-Manager\data\catalog.sqlite3"
DEFAULT_OUTPUT = r"C:\CCTV-Archive-Manager\data\merge-output"


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
    def __init__(self, source, database, date, parts, output, use_cache, parent=None):
        super().__init__(parent)
        self.source = Path(source)
        self.database = Path(database)
        self.date = date
        self.parts = parts
        self.output = Path(output)
        self.use_cache = use_cache

    def run(self):
        try:
            started = time.perf_counter()
            if not self.output.is_dir():
                raise NotADirectoryError(f"출력 폴더가 없습니다: {self.output}")
            if self.output.resolve().is_relative_to(self.source.resolve()):
                raise ValueError("출력 폴더는 원본 폴더 밖에 있어야 합니다.")

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
            for group in groups:
                destination = self.output / group.output_name
                partial = destination.with_name(
                    destination.stem + ".partial.mkv"
                )
                if destination.exists() or partial.exists():
                    raise FileExistsError(
                        f"병합 결과물 또는 임시 파일이 이미 존재합니다: {destination}"
                    )
            self.result_ready.emit((groups, time.perf_counter() - started))
        except Exception as exc:
            self.error_occurred.emit(str(exc))


class MergeWorker(BaseWorker):
    """Execute one merge and forward mkvmerge progress to the GUI."""

    progress_changed = Signal(int)

    def __init__(self, files, output, parent=None):
        super().__init__(parent)
        self.files = tuple(files)
        self.output = Path(output)

    def run(self):
        try:
            started = time.perf_counter()
            result = merge_with_progress(
                self.files, self.output,
                on_progress=self.progress_changed.emit,
            )
            self.result_ready.emit((result, time.perf_counter() - started))
        except Exception as exc:
            self.error_occurred.emit(str(exc))


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
        layout.addWidget(QLabel("MKV 검색 · SQLite 캐시 · 증분 동기화 · 병합 계획 · 단일 그룹 병합"))

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

        group_box = QGroupBox("병합 계획 및 단일 그룹 실행")
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

        self.plan_button = QPushButton("선택한 날짜의 병합 계획 생성")
        self.plan_button.setMinimumHeight(36)
        self.plan_button.clicked.connect(self.start_plan)
        group_layout.addWidget(self.plan_button)

        self.plan_table = QTableWidget()
        self.plan_table.setColumnCount(4)
        self.plan_table.setHorizontalHeaderLabels(
            ["출력 파일명", "입력 개수", "첫 번째 원본", "마지막 원본"]
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
        group_layout.addWidget(self.merge_button)

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

    def selected_date(self):
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        return item.text() if item is not None else None

    def clear_plan(self, *_args):
        self.current_groups = []
        self.plan_table.setRowCount(0)
        self.merge_button.setEnabled(False)

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
            self.parts_spin, self.plan_button, self.table, self.plan_table,
        )
        for widget in widgets:
            widget.setEnabled(not busy)
        self.update_merge_button()

    def update_merge_button(self):
        row = self.plan_table.currentRow()
        valid = (
            self.worker is None
            and bool(self.current_groups)
            and 0 <= row < len(self.current_groups)
            and bool(self.plan_table.selectedItems())
        )
        self.merge_button.setEnabled(valid)

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
            f"병합 계획 완료: {len(groups)}개 | 소요시간: {elapsed:.4f}초"
        )
        self.statusBar().showMessage(
            "미리보기 생성 완료. 실행하려면 아래에서 그룹 1개를 선택하세요."
        )

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
            "mkvmerge 진행률을 수집 중입니다. 작업 취소 기능은 아직 없습니다."
        )
        worker = MergeWorker(inputs, destination, self)
        worker.progress_changed.connect(self.show_merge_progress)
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

    def show_error(self, message):
        if isinstance(self.worker, MergeWorker):
            self.progress_label.setText(
                f"병합 실패 (최종 표시 진행률: {self.progress_bar.value()}%)"
            )
        self.status_label.setText("작업 실패")
        QMessageBox.critical(self, "작업 오류", message)

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
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
