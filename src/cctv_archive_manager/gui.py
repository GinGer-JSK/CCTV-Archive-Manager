
"""CCTV Archive Manager Lite - Windows GUI."""

import sys
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
from .file_scanner import scan_recordings
from .filename_parser import parse_recording_filename
from .merge_planner import plan_merge_groups


DEFAULT_SOURCE = r"\\nvr\NVR_Record\Door\video\WSGXN"
DEFAULT_OUTPUT = (
    r"C:\CCTV-Archive-Manager\data\merge-output"
)


class ScanWorker(QThread):
    """별도 스레드에서 MKV 파일을 검색한다."""

    result_ready = Signal(object)
    error_occurred = Signal(str)

    def __init__(self, source, parent=None):
        super().__init__(parent)
        self.source = source

    def run(self):
        try:
            result = scan_recordings(self.source)
            self.result_ready.emit(result)
        except Exception as error:
            self.error_occurred.emit(str(error))


class PlanWorker(QThread):
    """선택한 날짜의 MKV 병합 계획을 작성한다."""

    result_ready = Signal(object)
    error_occurred = Signal(str)

    def __init__(self, source, date, parts, output, parent=None):
        super().__init__(parent)

        self.source = Path(source)
        self.date = date
        self.parts = parts
        self.output = Path(output)

    def run(self):
        try:
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

            files = []

            for entry in self.source.iterdir():
                if not entry.is_file():
                    continue

                if entry.suffix.lower() != ".mkv":
                    continue

                info = parse_recording_filename(entry.name)

                if info is not None and info.date == self.date:
                    files.append(entry)

            groups = plan_merge_groups(files, self.parts)

            # 기존 병합 결과를 덮어쓰지 않도록 사전 확인
            for group in groups:
                destination = self.output / group.output_name

                partial = destination.with_name(
                    destination.stem + ".partial.mkv"
                )

                if destination.exists():
                    raise FileExistsError(
                        f"이미 출력 파일이 존재합니다:\n{destination}"
                    )

                if partial.exists():
                    raise FileExistsError(
                        f"미완료 병합 파일이 존재합니다:\n{partial}"
                    )

            self.result_ready.emit(groups)

        except Exception as error:
            self.error_occurred.emit(str(error))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.worker = None
        self.current_groups = []

        self.setWindowTitle(
            f"CCTV Archive Manager Lite - {__version__}"
        )
        self.resize(1100, 800)
        self.setMinimumSize(850, 600)

        self.setup_ui()

    def setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)

        layout = QVBoxLayout(central)
        layout.setSpacing(10)

        # 제목
        title = QLabel("CCTV Archive Manager Lite")
        title.setStyleSheet(
            "font-size: 22px; font-weight: bold;"
        )

        layout.addWidget(title)
        layout.addWidget(
            QLabel("MKV 검색 · 날짜별 분류 · 병합 계획 미리보기")
        )

        # 원본 폴더
        layout.addWidget(QLabel("원본 MKV 폴더"))

        source_row = QHBoxLayout()

        self.source_edit = QLineEdit(DEFAULT_SOURCE)
        self.source_edit.setMinimumHeight(30)
        self.source_edit.setToolTip(DEFAULT_SOURCE)

        self.source_button = QPushButton("폴더 선택")
        self.source_button.clicked.connect(
            self.browse_source
        )

        source_row.addWidget(self.source_edit, 1)
        source_row.addWidget(self.source_button)

        layout.addLayout(source_row)

        self.scan_button = QPushButton("MKV 파일 검색")
        self.scan_button.setMinimumHeight(38)
        self.scan_button.clicked.connect(self.start_scan)

        layout.addWidget(self.scan_button)

        self.summary_label = QLabel(
            "검색된 파일이 없습니다."
        )
        self.summary_label.setMinimumHeight(30)
        layout.addWidget(self.summary_label)

        # 날짜별 목록
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

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )

        layout.addWidget(self.table, 3)

        # 병합 계획 영역
        group_box = QGroupBox("병합 계획 미리보기")
        group_layout = QVBoxLayout(group_box)

        self.selected_date_label = QLabel(
            "선택한 날짜: 없음"
        )
        group_layout.addWidget(self.selected_date_label)

        output_row = QHBoxLayout()
        output_row.addWidget(QLabel("출력 폴더"))

        self.output_edit = QLineEdit(DEFAULT_OUTPUT)
        self.output_edit.setMinimumHeight(30)

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
        self.plan_button.clicked.connect(self.start_plan)

        group_layout.addWidget(self.plan_button)

        # 예상 병합 결과
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
            "미리보기 모드: 원본 영상은 변경하지 않습니다."
        )

        # 설정 변경 시 기존 계획 무효화
        self.table.itemSelectionChanged.connect(
            self.on_date_changed
        )
        self.source_edit.textChanged.connect(
            self.on_source_changed
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

    def on_date_changed(self):
        self.clear_plan()

        date = self.selected_date()
        self.selected_date_label.setText(
            f"선택한 날짜: {date or '없음'}"
        )

    def on_source_changed(self):
        # 원본 경로 변경 시 기존 검색 결과를 사용하지 않음
        self.clear_plan()
        self.table.setRowCount(0)
        self.summary_label.setText(
            "원본 폴더가 변경됐습니다. 다시 검색해 주세요."
        )
        self.selected_date_label.setText(
            "선택한 날짜: 없음"
        )

    def browse_source(self):
        folder = QFileDialog.getExistingDirectory(
            self,
            "원본 MKV 폴더 선택",
            self.source_edit.text(),
        )

        if folder:
            self.source_edit.setText(folder)

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
        if self.worker is not None and self.worker.isRunning():
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

    def start_scan(self):
        source = self.source_edit.text().strip()

        if not Path(source).is_dir():
            QMessageBox.warning(
                self,
                "원본 폴더 오류",
                "원본 MKV 폴더에 접근할 수 없습니다.",
            )
            return

        self.clear_plan()
        self.table.setRowCount(0)
        self.summary_label.setText("검색 중...")
        self.status_label.setText("MKV 검색 중...")

        worker = ScanWorker(source, self)

        worker.result_ready.connect(
            self.show_scan_result
        )

        self.start_worker(worker)

    def show_scan_result(self, result):
        size_gib = result.total_bytes / (1024 ** 3)

        self.summary_label.setText(
            f"전체 MKV: {result.total_mkv:,}개  |  "
            f"정상: {result.valid_mkv:,}개  |  "
            f"규칙 불일치: {result.invalid_mkv:,}개  |  "
            f"전체 용량: {size_gib:,.2f} GiB  |  "
            f"녹화 날짜: {len(result.days)}일"
        )

        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(result.days))

        for row, day in enumerate(result.days):
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

        self.status_label.setText(
            f"검색 완료: {result.total_mkv:,}개"
        )

        self.statusBar().showMessage(
            "검색 완료. 원본 영상은 변경되지 않았습니다."
        )

    def start_plan(self):
        date = self.selected_date()

        if date is None:
            QMessageBox.warning(
                self,
                "날짜 선택",
                "병합할 날짜를 먼저 선택해 주세요.",
            )
            return

        source = self.source_edit.text().strip()
        output = self.output_edit.text().strip()
        parts = self.parts_spin.value()

        if not output:
            QMessageBox.warning(
                self,
                "출력 경로 오류",
                "출력 폴더를 지정해 주세요.",
            )
            return

        self.clear_plan()
        self.status_label.setText(
            f"{date} 병합 계획 생성 중..."
        )

        worker = PlanWorker(
            source, date, parts, output, self
        )

        worker.result_ready.connect(self.show_plan)

        self.start_worker(worker)

    def show_plan(self, groups):
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
            f"병합 계획 완료: {len(groups)}개 출력 파일"
        )

        self.statusBar().showMessage(
            "미리보기만 생성했습니다. 실제 병합은 실행하지 않았습니다."
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
                "현재 작업이 완료된 후 종료해 주세요.",
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
