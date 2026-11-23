
"""CCTV Archive Manager Lite - Windows GUI.

지원 기능:
- SQLite 캐시 조회
- NVR SMB 직접 검색
- NVR 증분 동기화
- 날짜별 MKV 목록
- MKV 병합 계획 미리보기

주의:
GUI에서 실제 MKV 병합 실행은 아직 구현하지 않았다.
"""

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
from .incremental_sync import sync_new_recordings
from .merge_planner import plan_merge_groups


# ============================================================
# 기본 경로
# ============================================================

DEFAULT_SOURCE = (
    r"\\nvr\NVR_Record\Door\video\WSGXN"
)

DEFAULT_DATABASE = (
    r"C:\CCTV-Archive-Manager\data\catalog.sqlite3"
)

DEFAULT_OUTPUT = (
    r"C:\CCTV-Archive-Manager\data\merge-output"
)


# ============================================================
# 공통 작업 스레드
# ============================================================

class BaseWorker(QThread):
    """GUI를 차단하지 않고 작업을 실행하는 기본 스레드."""

    result_ready = Signal(object)
    error_occurred = Signal(str)


# ============================================================
# NVR 직접 검색
# ============================================================

class ScanWorker(BaseWorker):
    """원본 SMB 폴더를 직접 검색한다."""

    def __init__(self, source, parent=None):
        super().__init__(parent)
        self.source = source

    def run(self):
        try:
            started = time.perf_counter()

            result = scan_recordings(self.source)

            elapsed = time.perf_counter() - started

            self.result_ready.emit(
                (result, elapsed)
            )

        except Exception as error:
            self.error_occurred.emit(str(error))


# ============================================================
# SQLite 캐시 조회
# ============================================================

class CacheWorker(BaseWorker):
    """로컬 SQLite DB의 기존 녹화 정보를 조회한다."""

    def __init__(
        self,
        database,
        source,
        parent=None,
    ):
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

            self.result_ready.emit(
                (snapshot, elapsed)
            )

        except Exception as error:
            self.error_occurred.emit(str(error))


# ============================================================
# NVR 증분 동기화
# ============================================================

class SyncWorker(BaseWorker):
    """새로운 MKV 파일만 SQLite에 추가한다."""

    def __init__(
        self,
        source,
        database,
        parent=None,
    ):
        super().__init__(parent)

        self.source = source
        self.database = database

    def run(self):
        try:
            result = sync_new_recordings(
                source=self.source,
                database=self.database,
                minimum_age_seconds=900,
            )

            # 동기화 완료 후 최신 캐시도 조회한다.
            snapshot = get_catalog_snapshot(
                self.database,
                self.source,
            )

            self.result_ready.emit(
                (result, snapshot)
            )

        except Exception as error:
            self.error_occurred.emit(str(error))


# ============================================================
# 병합 계획 생성
# ============================================================

class PlanWorker(BaseWorker):
    """선택한 날짜의 MKV 병합 그룹을 계산한다."""

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

            # 출력 폴더 존재 확인
            if not self.output.is_dir():
                raise NotADirectoryError(
                    "출력 폴더가 존재하지 않습니다:\n"
                    f"{self.output}"
                )

            source = self.source.resolve()
            output = self.output.resolve()

            # 원본 폴더 내부로 출력하지 않도록 보호
            if output.is_relative_to(source):
                raise ValueError(
                    "출력 폴더는 원본 폴더 밖에 있어야 합니다."
                )

            # --------------------------------------------
            # SQLite 캐시 모드
            # --------------------------------------------

            if self.use_cache:
                files = get_cached_files(
                    self.database,
                    self.source,
                    self.date,
                )

            # --------------------------------------------
            # SMB 직접 검색 모드
            # --------------------------------------------

            else:
                files = []

                with os.scandir(self.source) as entries:
                    for entry in entries:
                        if not entry.name.lower().endswith(
                            ".mkv"
                        ):
                            continue

                        info = parse_recording_filename(
                            entry.name
                        )

                        if info is None:
                            continue

                        if info.date != self.date:
                            continue

                        files.append(
                            Path(entry.path)
                        )

            # 병합 그룹 생성
            groups = plan_merge_groups(
                files,
                self.parts,
            )

            # 기존 결과물 덮어쓰기 방지
            for group in groups:
                destination = (
                    self.output / group.output_name
                )

                partial = destination.with_name(
                    destination.stem + ".partial.mkv"
                )

                if destination.exists():
                    raise FileExistsError(
                        "이미 출력 파일이 존재합니다:\n"
                        f"{destination}"
                    )

                if partial.exists():
                    raise FileExistsError(
                        "미완료 병합 파일이 존재합니다:\n"
                        f"{partial}"
                    )

            elapsed = (
                time.perf_counter() - started
            )

            self.result_ready.emit(
                (groups, elapsed)
            )

        except Exception as error:
            self.error_occurred.emit(str(error))


# ============================================================
# 메인 GUI
# ============================================================

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

    # ========================================================
    # UI 구성
    # ========================================================

    def setup_ui(self):

        central = QWidget()
        self.setCentralWidget(central)

        layout = QVBoxLayout(central)
        layout.setSpacing(10)

        # ----------------------------------------------------
        # 제목
        # ----------------------------------------------------

        title = QLabel(
            "CCTV Archive Manager Lite"
        )

        title.setStyleSheet(
            "font-size: 22px; font-weight: bold;"
        )

        layout.addWidget(title)

        subtitle = QLabel(
            "MKV 검색 · SQLite 캐시 · "
            "증분 동기화 · 병합 계획"
        )

        layout.addWidget(subtitle)

        # ----------------------------------------------------
        # 원본 폴더
        # ----------------------------------------------------

        layout.addWidget(
            QLabel("원본 MKV 폴더")
        )

        source_row = QHBoxLayout()

        self.source_edit = QLineEdit(
            DEFAULT_SOURCE
        )

        self.source_edit.setMinimumHeight(30)

        self.source_button = QPushButton(
            "폴더 선택"
        )

        self.source_button.clicked.connect(
            self.browse_source
        )

        source_row.addWidget(
            self.source_edit, 1
        )

        source_row.addWidget(
            self.source_button
        )

        layout.addLayout(source_row)

        # ----------------------------------------------------
        # SQLite DB 경로
        # ----------------------------------------------------

        layout.addWidget(
            QLabel("SQLite 캐시 파일")
        )

        database_row = QHBoxLayout()

        self.database_edit = QLineEdit(
            DEFAULT_DATABASE
        )

        self.database_edit.setMinimumHeight(30)

        self.database_button = QPushButton(
            "DB 선택"
        )

        self.database_button.clicked.connect(
            self.browse_database
        )

        database_row.addWidget(
            self.database_edit, 1
        )

        database_row.addWidget(
            self.database_button
        )

        layout.addLayout(database_row)

        # ----------------------------------------------------
        # 검색 / 동기화 버튼
        # ----------------------------------------------------

        search_row = QHBoxLayout()

        self.cache_button = QPushButton(
            "SQLite 캐시 불러오기"
        )

        self.cache_button.setMinimumHeight(38)

        self.cache_button.clicked.connect(
            self.start_cache
        )

        self.sync_button = QPushButton(
            "NVR 증분 동기화"
        )

        self.sync_button.setMinimumHeight(38)

        self.sync_button.clicked.connect(
            self.start_sync
        )

        self.scan_button = QPushButton(
            "NVR 직접 검색 (SMB)"
        )

        self.scan_button.setMinimumHeight(38)

        self.scan_button.clicked.connect(
            self.start_scan
        )

        search_row.addWidget(
            self.cache_button
        )

        search_row.addWidget(
            self.sync_button
        )

        search_row.addWidget(
            self.scan_button
        )

        layout.addLayout(search_row)

        # ----------------------------------------------------
        # 조회 방식 및 전체 통계
        # ----------------------------------------------------

        self.mode_label = QLabel(
            "조회 방식: 선택되지 않음"
        )

        layout.addWidget(
            self.mode_label
        )

        self.summary_label = QLabel(
            "검색된 파일이 없습니다."
        )

        layout.addWidget(
            self.summary_label
        )

        # ----------------------------------------------------
        # 날짜별 파일 목록
        # ----------------------------------------------------

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

        layout.addWidget(
            self.table, 3
        )

        # ----------------------------------------------------
        # 병합 계획 그룹
        # ----------------------------------------------------

        group_box = QGroupBox(
            "병합 계획 미리보기"
        )

        group_layout = QVBoxLayout(
            group_box
        )

        self.selected_date_label = QLabel(
            "선택한 날짜: 없음"
        )

        group_layout.addWidget(
            self.selected_date_label
        )

        # ----------------------------------------------------
        # 출력 폴더
        # ----------------------------------------------------

        output_row = QHBoxLayout()

        output_row.addWidget(
            QLabel("출력 폴더")
        )

        self.output_edit = QLineEdit(
            DEFAULT_OUTPUT
        )

        self.output_button = QPushButton(
            "폴더 선택"
        )

        self.output_button.clicked.connect(
            self.browse_output
        )

        output_row.addWidget(
            self.output_edit, 1
        )

        output_row.addWidget(
            self.output_button
        )

        group_layout.addLayout(
            output_row
        )

        # ----------------------------------------------------
        # 출력 파일 개수
        # ----------------------------------------------------

        parts_row = QHBoxLayout()

        parts_row.addWidget(
            QLabel("출력 MKV 파일 개수")
        )

        self.parts_spin = QSpinBox()

        self.parts_spin.setRange(
            1, 9999
        )

        self.parts_spin.setValue(3)

        parts_row.addWidget(
            self.parts_spin
        )

        parts_row.addStretch()

        group_layout.addLayout(
            parts_row
        )

        # ----------------------------------------------------
        # 병합 계획 버튼
        # ----------------------------------------------------

        self.plan_button = QPushButton(
            "선택한 날짜의 병합 계획 생성"
        )

        self.plan_button.setMinimumHeight(36)

        self.plan_button.clicked.connect(
            self.start_plan
        )

        group_layout.addWidget(
            self.plan_button
        )

        # ----------------------------------------------------
        # 병합 계획 미리보기 테이블
        # ----------------------------------------------------

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

        self.plan_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )

        self.plan_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )

        group_layout.addWidget(
            self.plan_table
        )

        layout.addWidget(
            group_box, 2
        )

        # ----------------------------------------------------
        # 상태 표시
        # ----------------------------------------------------

        self.status_label = QLabel(
            "준비 완료"
        )

        layout.addWidget(
            self.status_label
        )

        self.statusBar().showMessage(
            "원본 영상은 변경하지 않습니다."
        )

        # ----------------------------------------------------
        # Signal 연결
        # ----------------------------------------------------

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

    # ========================================================
    # 현재 선택한 날짜
    # ========================================================

    def selected_date(self):

        row = self.table.currentRow()

        if row < 0:
            return None

        item = self.table.item(
            row, 0
        )

        if item is None:
            return None

        return item.text()

    # ========================================================
    # 결과 초기화
    # ========================================================

    def clear_plan(self, *args):

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

    def on_source_changed(self, *args):
        self.clear_results()

    def on_database_changed(self, *args):
        self.clear_results()

    def on_date_changed(self):

        self.clear_plan()

        date = self.selected_date()

        self.selected_date_label.setText(
            f"선택한 날짜: {date or '없음'}"
        )

    # ========================================================
    # 폴더 선택
    # ========================================================

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
            str(
                Path(
                    self.database_edit.text()
                ).parent
            ),
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

    # ========================================================
    # 작업 중 GUI 제어
    # ========================================================

    def set_busy(self, busy):

        widgets = (
            self.source_edit,
            self.source_button,
            self.database_edit,
            self.database_button,
            self.cache_button,
            self.sync_button,
            self.scan_button,
            self.output_edit,
            self.output_button,
            self.parts_spin,
            self.plan_button,
            self.table,
        )

        for widget in widgets:
            widget.setEnabled(
                not busy
            )

    def start_worker(self, worker):

        if self.worker is not None:
            return

        self.worker = worker

        self.set_busy(True)

        worker.error_occurred.connect(
            self.show_error
        )

        worker.finished.connect(
            self.finish_work
        )

        worker.start()

    def finish_work(self):

        self.set_busy(False)

        if self.worker is not None:
            self.worker.deleteLater()
            self.worker = None

    # ========================================================
    # SQLite 캐시 조회 시작
    # ========================================================

    def start_cache(self):

        if self.worker is not None:
            return

        database = (
            self.database_edit.text().strip()
        )

        source = (
            self.source_edit.text().strip()
        )

        if not database or not source:
            QMessageBox.warning(
                self,
                "경로 확인",
                "원본 경로와 DB 경로를 입력해 주세요.",
            )
            return

        self.clear_results()

        self.status_label.setText(
            "SQLite 캐시 조회 중..."
        )

        worker = CacheWorker(
            database,
            source,
            self,
        )

        worker.result_ready.connect(
            self.show_cache_result
        )

        self.start_worker(worker)

    # ========================================================
    # NVR 증분 동기화 시작
    # ========================================================

    def start_sync(self):

        if self.worker is not None:
            return

        source = (
            self.source_edit.text().strip()
        )

        database = (
            self.database_edit.text().strip()
        )

        if not source or not database:
            QMessageBox.warning(
                self,
                "경로 확인",
                "원본 폴더와 SQLite DB 경로를 확인해 주세요.",
            )
            return

        # 날짜 선택은 그대로 유지한다.
        # 병합 계획만 초기화한다.
        self.clear_plan()

        self.status_label.setText(
            "NVR 증분 동기화 진행 중..."
        )

        self.statusBar().showMessage(
            "기존 MKV 메타데이터는 재검사하지 않습니다."
        )

        worker = SyncWorker(
            source=source,
            database=database,
            parent=self,
        )

        worker.result_ready.connect(
            self.show_sync_result
        )

        self.start_worker(worker)

    # ========================================================
    # NVR 전체 직접 검색 시작
    # ========================================================

    def start_scan(self):

        if self.worker is not None:
            return

        source = (
            self.source_edit.text().strip()
        )

        if not source:
            QMessageBox.warning(
                self,
                "원본 폴더 오류",
                "원본 MKV 폴더를 입력해 주세요.",
            )
            return

        self.clear_results()

        self.status_label.setText(
            "NVR SMB 파일 검색 중..."
        )

        worker = ScanWorker(
            source,
            self,
        )

        worker.result_ready.connect(
            self.show_scan_result
        )

        self.start_worker(worker)

    # ========================================================
    # 날짜별 목록 채우기
    # ========================================================

    def populate_days(self, days):

        self.table.setSortingEnabled(False)

        # 기존 선택을 확실하게 초기화
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)

        self.table.setRowCount(
            len(days)
        )

        for row, day in enumerate(days):

            date_item = QTableWidgetItem(
                day.date
            )

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
                round(
                    day.total_bytes / (1024 ** 3),
                    2,
                ),
            )

            size_item.setTextAlignment(
                Qt.AlignmentFlag.AlignRight
                | Qt.AlignmentFlag.AlignVCenter
            )

            self.table.setItem(
                row, 0, date_item
            )

            self.table.setItem(
                row, 1, count_item
            )

            self.table.setItem(
                row, 2, size_item
            )

        self.table.setSortingEnabled(True)

    # ========================================================
    # SQLite 캐시 조회 결과
    # ========================================================

    def show_cache_result(self, result):

        snapshot, elapsed = result

        self.view_mode = "cache"

        self.populate_days(
            snapshot.days
        )

        self.summary_label.setText(
            f"캐시 MKV: {snapshot.total_files:,}개 | "
            f"용량: {snapshot.total_bytes / 1024**3:,.2f} GiB | "
            f"녹화 날짜: {len(snapshot.days)}일"
        )

        self.mode_label.setText(
            "조회 방식: SQLite 캐시 "
            "(원본 파일 현재 상태 미확인)"
        )

        self.status_label.setText(
            f"캐시 조회 완료: {elapsed:.4f}초"
        )

        self.statusBar().showMessage(
            "마지막 파일 확인: "
            f"{snapshot.last_seen_at or '기록 없음'} "
            "(UTC, 전체 동기화 시각 아님)"
        )

    # ========================================================
    # 증분 동기화 완료 결과
    # ========================================================

    def show_sync_result(self, payload):

        result, snapshot = payload

        # 목록 변경 전에 기존 선택 날짜 보관
        previous_date = self.selected_date()

        self.view_mode = "cache"

        self.clear_plan()

        self.populate_days(
            snapshot.days
        )

        self.summary_label.setText(
            f"캐시 MKV: {snapshot.total_files:,}개 | "
            f"용량: {snapshot.total_bytes / 1024**3:,.2f} GiB | "
            f"녹화 날짜: {len(snapshot.days)}일"
        )

        self.mode_label.setText(
            "조회 방식: SQLite 캐시 "
            "(증분 동기화 완료, 기존 파일 미재검증)"
        )

        # 기존 선택 날짜 복원
        if previous_date:

            for row in range(
                self.table.rowCount()
            ):
                item = self.table.item(
                    row, 0
                )

                if (
                    item is not None
                    and item.text() == previous_date
                ):
                    self.table.selectRow(row)
                    break

        self.status_label.setText(
            f"동기화 완료: {result.elapsed_seconds:.3f}초 | "
            f"신규 {result.added}개 | "
            f"기존 {result.already_indexed}개 | "
            f"보류 {result.skipped_recent}개"
        )

        self.statusBar().showMessage(
            f"발견: {result.total_mkv:,}개 | "
            f"신규: {result.added}개 | "
            f"원본 미발견: {result.missing_from_source}개 | "
            f"파일명 불일치: {result.invalid_names}개 | "
            f"조회 오류: {result.metadata_errors}개"
        )

        if result.metadata_errors:

            QMessageBox.warning(
                self,
                "일부 파일 조회 실패",
                "신규 파일 메타데이터 조회 오류가 "
                f"{result.metadata_errors}건 발생했습니다.\n"
                "해당 파일은 이번 동기화에서 제외됐습니다.",
            )

    # ========================================================
    # NVR 직접 검색 결과
    # ========================================================

    def show_scan_result(self, payload):

        scan, elapsed = payload

        self.view_mode = "direct"

        self.populate_days(
            scan.days
        )

        self.summary_label.setText(
            f"전체 MKV: {scan.total_mkv:,}개 | "
            f"정상: {scan.valid_mkv:,}개 | "
            f"규칙 불일치: {scan.invalid_mkv:,}개 | "
            f"용량: {scan.total_bytes / 1024**3:,.2f} GiB | "
            f"녹화 날짜: {len(scan.days)}일"
        )

        self.mode_label.setText(
            "조회 방식: NVR 직접 검색 "
            "(SQLite 캐시에 반영하지 않음)"
        )

        self.status_label.setText(
            f"NVR 검색 완료: {elapsed:.3f}초"
        )

        self.statusBar().showMessage(
            "NVR 직접 검색 완료. "
            "원본 파일 변경 없음."
        )

    # ========================================================
    # 병합 계획 생성 시작
    # ========================================================

    def start_plan(self):

        if self.worker is not None:
            return

        date = self.selected_date()

        if date is None or self.view_mode is None:
            QMessageBox.warning(
                self,
                "날짜 선택",
                "먼저 목록을 불러오고 날짜를 선택해 주세요.",
            )
            return

        self.clear_plan()

        worker = PlanWorker(
            source=self.source_edit.text().strip(),
            database=self.database_edit.text().strip(),
            date=date,
            parts=self.parts_spin.value(),
            output=self.output_edit.text().strip(),
            use_cache=(
                self.view_mode == "cache"
            ),
            parent=self,
        )

        self.status_label.setText(
            f"{date} 병합 계획 생성 중..."
        )

        worker.result_ready.connect(
            self.show_plan
        )

        self.start_worker(worker)

    # ========================================================
    # 병합 계획 결과 표시
    # ========================================================

    def show_plan(self, payload):

        groups, elapsed = payload

        self.current_groups = groups

        self.plan_table.setRowCount(
            len(groups)
        )

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
                    item.setText(
                        str(value)
                    )

                self.plan_table.setItem(
                    row, col, item
                )

        self.status_label.setText(
            f"병합 계획 완료: {len(groups)}개 | "
            f"소요시간: {elapsed:.4f}초"
        )

        self.statusBar().showMessage(
            "미리보기만 생성했습니다. "
            "실제 MKV 병합은 실행하지 않았습니다."
        )

    # ========================================================
    # 작업 오류
    # ========================================================

    def show_error(self, message):

        self.status_label.setText(
            "작업 실패"
        )

        QMessageBox.critical(
            self,
            "작업 오류",
            message,
        )

    # ========================================================
    # 종료 처리
    # ========================================================

    def closeEvent(self, event):

        if (
            self.worker is not None
            and self.worker.isRunning()
        ):
            QMessageBox.information(
                self,
                "작업 진행 중",
                "현재 작업이 끝난 후 종료해 주세요.",
            )

            event.ignore()
            return

        super().closeEvent(event)


# ============================================================
# 프로그램 시작
# ============================================================

def main():

    app = QApplication(sys.argv)

    window = MainWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
