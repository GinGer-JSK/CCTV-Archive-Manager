
"""CCTV Archive Manager Lite - Windows GUI."""

import os
import sys
import time
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .catalog_reader import (
    get_cached_files,
    get_catalog_snapshot,
)
from .file_scanner import scan_recordings
from .filename_parser import parse_recording_filename
from .merge_planner import plan_merge_groups


DEFAULT_SOURCE = r"\\nvr\NVR_Record\Door\video\WSGXN"
DEFAULT_DATABASE = (
    r"C:\CCTV-Archive-Manager\data\catalog.sqlite3"
)
DEFAULT_OUTPUT = (
    r"C:\CCTV-Archive-Manager\data\merge-output"
)


class BaseWorker(QThread):
    result_ready = Signal(object)
    error_occurred = Signal(str)


class ScanWorker(BaseWorker):
    """NVR SMB 원본 폴더를 직접 검색."""

    def __init__(self, source, parent=None):
        super().__init__(parent)
        self.source = source

    def run(self):
        try:
            started = time.perf_counter()
            result = scan_recordings(self.source)
            elapsed = time.perf_counter() - started

            self.result_ready.emit((result, elapsed))
        except Exception as error:
            self.error_occurred.emit(str(error))


class CacheWorker(BaseWorker):
    """로컬 SQLite DB에서 날짜별 목록 조회."""

    def __init__(self, database, source, parent=None):
        super().__init__(parent)
        self.database = database
        self.source = source

    def run(self):
        try:
            started = time.perf_counter()

            snapshot = get_catalog_snapshot(
                self.database,
                self.source,
            )

            elapsed = time.perf_counter() - started

            self.result_ready.emit((snapshot, elapsed))
        except Exception as error:
            self.error_occurred.emit(str(error))


class PlanWorker(BaseWorker):
    """파일 목록을 이용해 병합 계획 생성."""

    def __init__(
        self,
        source,
        database,
        date,
        parts,
        output,
        use_cache,
        parent=None,
    ):
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
                raise NotADirectoryError(
                    f"출력 폴더가 없습니다: {self.output}"
                )

            source = self.source.resolve()
            output = self.output.resolve()

            if output.is_relative_to(source):
                raise ValueError(
                    "출력 폴더는 원본 폴더 밖에 있어야 합니다."
                )

            if self.use_cache:
                # 원격 SMB 조회 없이 로컬 DB만 읽음
                files = get_cached_files(
                    self.database,
                    self.source,
                    self.date,
                )
            else:
                # NVR 직접 검색 모드: 파일명만 조회
                files = []

                with os.scandir(self.source) as entries:
                    for entry in entries:
                        if not entry.name.lower().endswith(".mkv"):
                            continue

                        info = parse_recording_filename(
                            entry.name
                        )

                        if info and info.date == self.date:
                            files.append(Path(entry.path))

            groups = plan_merge_groups(files, self.parts)

            # 기존 결과물은 덮어쓰지 않음
            for group in groups:
                destination = self.output / group.output_name

                partial = destination.with_name(
                    destination.stem + ".partial.mkv"
                )

                if destination.exists() or partial.exists():
                    raise FileExistsError(
                        "기존 결과물 또는 임시 파일이 있습니다:\n"
                        f"{destination}"
                    )

            elapsed = time.perf_counter() - started

            self.result_ready.emit((groups, elapsed))

        except Exception as error:
            self.error_occurred.emit(str(error))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.worker = None
        self.current_groups = []
        self.view_mode = None

        self.setWindowTitle(
            f"CCTV Archive Manager Lite - {__version__}"
        )
        self.resize(1100, 820)
        self.setMinimumSize(850, 600)

        self.setup_ui()

    def setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)

        layout = QVBoxLayout(central)
        layout.setSpacing(10)

        title = QLabel("CCTV Archive Manager Lite")
        title.setStyleSheet(
            "font-size: 22px; font-weight: bold;"
        )
        layout.addWidget(title)

        layout.addWidget(
            QLabel("MKV 검색 · SQLite 캐시 · 병합 계획")
        )

        # 원본 폴더
        layout.addWidget(QLabel("원본 MKV 폴더"))

        source_row = QHBoxLayout()

        self.source_edit = QLineEdit(DEFAULT_SOURCE)
        self.source_edit.setMinimumHeight(30)

        self.source_button = QPushButton("폴더 선택")
        self.source_button.clicked.connect(
            self.browse_source
        )

        source_row.addWidget(self.source_edit, 1)
        source_row.addWidget(self.source_button)

        layout.addLayout(source_row)

        # SQLite DB 경로
        layout.addWidget(QLabel("SQLite 캐시 파일"))

        database_row = QHBoxLayout()

        self.database_edit = QLineEdit(DEFAULT_DATABASE)
        self.database_edit.setMinimumHeight(30)

        self.database_button = QPushButton("DB 선택")
        self.database_button.clicked.connect(
            self.browse_database
        )

        database_row.addWidget(self.database_edit, 1)
        database_row.addWidget(self.database_button)

        layout.addLayout(database_row)

        # 캐시와 원본 검색 버튼 분리
        search_row = QHBoxLayout()

        self.cache_button = QPushButton(
            "SQLite 캐시 불러오기"
        )
        self.cache_button.setMinimumHeight(38)
        self.cache_button.clicked.connect(
            self.start_cache
        )

        self.scan_button = QPushButton(
            "NVR 직접 검색 (SMB)"
        )
        self.scan_button.setMinimumHeight(38)
        self.scan_button.clicked.connect(
            self.start_scan
        )

        search_row.addWidget(self.cache_button)
        search_row.addWidget(self.scan_button)

        layout.addLayout(search_row)

        self.mode_label = QLabel(
            "조회 방식: 선택되지 않음"
        )
        layout.addWidget(self.mode_label)

        self.summary_label = QLabel(
            "검색된 파일이 없습니다."
        )
        layout.addWidget(self.summary_label)

        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels([
            "녹화 날짜",
            "파일 수",
            "용량 (GiB)",
        ])

        self.table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self.table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QTableWidget.SelectionMode.SingleSelection
        )
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)

        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )

        layout.addWidget(self.table, 3)

        # 병합 계획
        group_box = QGroupBox("병합 계획 미리보기")
        group_layout = QVBoxLayout(group_box)

        self.selected_date_label = QLabel(
            "선택한 날짜: 없음"
        )
        group_layout.addWidget(self.selected_date_label)

        output_row = QHBoxLayout()
        output_row.addWidget(QLabel("출력 폴더"))

        self.output_edit = QLineEdit(DEFAULT_OUTPUT)

        self.output_button = QPushButton("폴더 선택")
        self.output_button.clicked.connect(
            self.browse_output
        )

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

        self.plan_button = QPushButton(
            "선택한 날짜의 병합 계획 생성"
        )
        self.plan_button.setMinimumHeight(36)
        self.plan_button.clicked.connect(
            self.start_plan
        )

        group_layout.addWidget(self.plan_button)

        self.plan_table = QTableWidget()
        self.plan_table.setColumnCount(4)
        self.plan_table.setHorizontalHeaderLabels([
            "출력 파일명",
            "입력 개수",
            "첫 번째 원본",
            "마지막 원본",
        ])

        self.plan_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )

        self.plan_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )

        group_layout.addWidget(self.plan_table)
        layout.addWidget(group_box, 2)

        self.status_label = QLabel("준비 완료")
        layout.addWidget(self.status_label)

        self.statusBar().showMessage(
            "원본 영상은 변경하지 않습니다."
        )

        # 설정 변경 시 이전 결과 무효화
        self.table.itemSelectionChanged.connect(
            self.on_date_changed
        )
        self.source_edit.textChanged.connect(
            self.on_source_changed
        )
        self.database_edit.textChanged.connect(
            self.on_database_changed
        )
        self.output_edit.textChanged.connect(
            self.clear_plan
        )
        self.parts_spin.valueChanged.connect(
            self.clear_plan
        )

    def selected_date(self):
        row = self.table.currentRow()

        if row < 0:
            return None

        item = self.table.item(row, 0)
        return item.text() if item else None

    def clear_plan(self):
        self.current_groups = []
        self.plan_table.setRowCount(0)

    def clear_results(self):
        self.view_mode = None
        self.clear_plan()
        self.table.setRowCount(0)
        self.summary_label.setText(
            "다시 검색하거나 캐시를 불러와 주세요."
        )
        self.mode_label.setText(
            "조회 방식: 선택되지 않음"
        )
        self.selected_date_label.setText(
            "선택한 날짜: 없음"
        )

    def on_source_changed(self):
        self.clear_results()

    def on_database_changed(self):
        self.clear_results()

    def on_date_changed(self):
        self.clear_plan()
        self.selected_date_label.setText(
            f"선택한 날짜: {self.selected_date() or '없음'}"
        )

    def browse_source(self):
        folder = QFileDialog.getExistingDirectory(
            self,
            "원본 MKV 폴더 선택",
            self.source_edit.text(),
        )

        if folder:
            self.source_edit.setText(folder)

    def browse_database(self):
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "SQLite 캐시 파일 선택",
            str(Path(self.database_edit.text()).parent),
            "SQLite Files (*.sqlite3 *.db);;All Files (*)",
        )

        if filename:
            self.database_edit.setText(filename)

    def browse_output(self):
        folder = QFileDialog.getExistingDirectory(
            self,
            "출력 폴더 선택",
            self.output_edit.text(),
        )

        if folder:
            self.output_edit.setText(folder)

    def set_busy(self, busy):
        widgets = (
            self.source_edit,
            self.source_button,
            self.database_edit,
            self.database_button,
            self.cache_button,
            self.scan_button,
            self.output_edit,
            self.output_button,
            self.parts_spin,
            self.plan_button,
            self.table,
        )

        for widget in widgets:
            widget.setEnabled(not busy)

    def start_worker(self, worker):
        if self.worker is not None:
            return

        self.worker = worker
        self.set_busy(True)

        worker.error_occurred.connect(self.show_error)
        worker.finished.connect(self.finish_work)
        worker.start()

    def finish_work(self):
        self.set_busy(False)

        if self.worker is not None:
            self.worker.deleteLater()
            self.worker = None

    def start_cache(self):
        database = self.database_edit.text().strip()
        source = self.source_edit.text().strip()

        self.clear_results()
        self.status_label.setText("SQLite 캐시 조회 중...")

        worker = CacheWorker(
            database, source, self
        )

        worker.result_ready.connect(
            self.show_cache_result
        )

        self.start_worker(worker)

    def start_scan(self):
        source = self.source_edit.text().strip()

        if not Path(source).is_dir():
            QMessageBox.warning(
                self,
                "원본 폴더 오류",
                "NVR 원본 폴더에 접근할 수 없습니다.",
            )
            return

        self.clear_results()
        self.status_label.setText(
            "NVR SMB 파일 검색 중..."
        )

        worker = ScanWorker(source, self)

        worker.result_ready.connect(
            self.show_scan_result
        )

        self.start_worker(worker)

    def populate_days(self, days):
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(days))

        for row, day in enumerate(days):
            date_item = QTableWidgetItem(day.date)

            count_item = QTableWidgetItem()
            count_item.setData(
                Qt.ItemDataRole.DisplayRole,
                day.file_count,
            )
            count_item.setTextAlignment(
                Qt.AlignmentFlag.AlignRight
                | Qt.AlignmentFlag.AlignVCenter
            )

            size_item = QTableWidgetItem()
            size_item.setData(
                Qt.ItemDataRole.DisplayRole,
                round(day.total_bytes / (1024 ** 3), 2),
            )
            size_item.setTextAlignment(
                Qt.AlignmentFlag.AlignRight
                | Qt.AlignmentFlag.AlignVCenter
            )

            self.table.setItem(row, 0, date_item)
            self.table.setItem(row, 1, count_item)
            self.table.setItem(row, 2, size_item)

        self.table.setSortingEnabled(True)

    def show_cache_result(self, result):
        snapshot, elapsed = result

        self.view_mode = "cache"
        self.populate_days(snapshot.days)

        self.summary_label.setText(
            f"캐시 MKV: {snapshot.total_files:,}개 | "
            f"용량: {snapshot.total_bytes / 1024**3:,.2f} GiB | "
            f"녹화 날짜: {len(snapshot.days)}일"
        )

        self.mode_label.setText(
            "조회 방식: SQLite 캐시 "
            "(원본 파일의 현재 존재 여부 미확인)"
        )

        self.status_label.setText(
            f"캐시 조회 완료: {elapsed:.4f}초"
        )

        self.statusBar().showMessage(
            "마지막 파일 확인: "
            f"{snapshot.last_seen_at or '기록 없음'} "
            "(UTC, 전체 동기화 시각을 뜻하지 않음)"
        )

    def show_scan_result(self, result):
        scan, elapsed = result

        self.view_mode = "direct"
        self.populate_days(scan.days)

        self.summary_label.setText(
            f"전체 MKV: {scan.total_mkv:,}개 | "
            f"정상: {scan.valid_mkv:,}개 | "
            f"규칙 불일치: {scan.invalid_mkv:,}개 | "
            f"용량: {scan.total_bytes / 1024**3:,.2f} GiB | "
            f"녹화 날짜: {len(scan.days)}일"
        )

        self.mode_label.setText(
            "조회 방식: NVR 직접 검색 "
            "(SQLite 캐시에는 반영하지 않음)"
        )

        self.status_label.setText(
            f"NVR 검색 완료: {elapsed:.3f}초"
        )

        self.statusBar().showMessage(
            "NVR 직접 검색 완료. 원본 파일 변경 없음."
        )

    def start_plan(self):
        date = self.selected_date()

        if date is None or self.view_mode is None:
            QMessageBox.warning(
                self,
                "날짜 선택",
                "먼저 파일 목록을 불러오고 날짜를 선택해 주세요.",
            )
            return

        self.clear_plan()

        worker = PlanWorker(
            source=self.source_edit.text().strip(),
            database=self.database_edit.text().strip(),
            date=date,
            parts=self.parts_spin.value(),
            output=self.output_edit.text().strip(),
            use_cache=(self.view_mode == "cache"),
            parent=self,
        )

        self.status_label.setText(
            f"{date} 병합 계획 생성 중..."
        )

        worker.result_ready.connect(self.show_plan)
        self.start_worker(worker)

    def show_plan(self, result):
        groups, elapsed = result
        self.current_groups = groups

        self.plan_table.setRowCount(len(groups))

        for row, group in enumerate(groups):
            values = [
                group.output_name,
                len(group.files),
                group.files[0].name,
                group.files[-1].name,
            ]

            for col, value in enumerate(values):
                item = QTableWidgetItem()

                if col == 1:
                    item.setData(
                        Qt.ItemDataRole.DisplayRole,
                        value,
                    )
                else:
                    item.setText(str(value))

                self.plan_table.setItem(row, col, item)

        self.status_label.setText(
            f"병합 계획 완료: {len(groups)}개 | "
            f"소요시간: {elapsed:.4f}초"
        )

        self.statusBar().showMessage(
            "미리보기만 생성했습니다. "
            "실제 MKV 병합은 실행하지 않았습니다."
        )

    def show_error(self, message):
        self.status_label.setText("작업 실패")
        QMessageBox.critical(
            self,
            "작업 오류",
            message,
        )

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(
                self,
                "작업 진행 중",
                "현재 작업이 끝난 후 종료해 주세요.",
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
