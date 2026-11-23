
"""CCTV Archive Manager Lite - Windows GUI."""

import sys
from pathlib import Path

from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .file_scanner import ScanResult, scan_recordings


class ScanWorker(QThread):
    """GUI를 멈추지 않고 MKV 파일을 검색한다."""

    result_ready = Signal(object)
    error_occurred = Signal(str)

    def __init__(self, source: str):
        super().__init__()
        self.source = source

    def run(self):
        try:
            result = scan_recordings(self.source)
            self.result_ready.emit(result)
        except Exception as error:
            self.error_occurred.emit(str(error))


class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()

        self.worker = None

        self.setWindowTitle(
            f"CCTV Archive Manager Lite - {__version__}"
        )
        self.resize(950, 650)

        self.setup_ui()

    def setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)

        layout = QVBoxLayout(central)
        layout.setSpacing(12)

        # 제목
        title = QLabel("CCTV Archive Manager Lite")
        title.setStyleSheet(
            "font-size: 22px; font-weight: bold;"
        )
        layout.addWidget(title)

        subtitle = QLabel(
            "CCTV MKV 녹화 파일 검색 및 날짜별 분류"
        )
        layout.addWidget(subtitle)

        # 원본 폴더 선택
        layout.addWidget(QLabel("원본 MKV 폴더"))

        source_row = QHBoxLayout()

        self.source_edit = QLineEdit()
        self.source_edit.setText(
            r"\\nvr\NVR_Record\Door\video\WSGXN"
        )

        self.browse_button = QPushButton("폴더 선택")
        self.browse_button.clicked.connect(
            self.browse_source
        )

        source_row.addWidget(self.source_edit)
        source_row.addWidget(self.browse_button)

        layout.addLayout(source_row)

        # 검색 실행
        self.scan_button = QPushButton(
            "MKV 파일 검색"
        )
        self.scan_button.setMinimumHeight(40)
        self.scan_button.clicked.connect(
            self.start_scan
        )

        layout.addWidget(self.scan_button)

        # 검색 결과 요약
        self.summary_label = QLabel(
            "검색된 파일이 없습니다."
        )
        self.summary_label.setStyleSheet(
            "font-size: 13px; padding: 8px;"
        )

        layout.addWidget(self.summary_label)

        # 날짜별 녹화 파일 목록
        self.table = QTableWidget()
        self.table.setColumnCount(3)

        self.table.setHorizontalHeaderLabels([
            "녹화 날짜",
            "파일 수",
            "용량 (GiB)",
        ])

        self.table.horizontalHeader().setStretchLastSection(
            True
        )

        self.table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )

        self.table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )

        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(False)

        layout.addWidget(self.table)

        # 상태 표시
        self.status_label = QLabel("준비 완료")
        layout.addWidget(self.status_label)

        self.statusBar().showMessage(
            "원본 영상은 변경하지 않습니다."
        )

    def browse_source(self):
        folder = QFileDialog.getExistingDirectory(
            self,
            "MKV 원본 폴더 선택",
            self.source_edit.text(),
        )

        if folder:
            self.source_edit.setText(folder)

    def start_scan(self):
        source = self.source_edit.text().strip()

        if not source:
            QMessageBox.warning(
                self,
                "경로 오류",
                "원본 폴더를 지정해 주세요.",
            )
            return

        if not Path(source).is_dir():
            QMessageBox.warning(
                self,
                "경로 오류",
                f"원본 폴더에 접근할 수 없습니다:\n{source}",
            )
            return

        self.scan_button.setEnabled(False)
        self.browse_button.setEnabled(False)
        self.source_edit.setEnabled(False)

        self.status_label.setText(
            "MKV 파일 검색 중..."
        )

        self.worker = ScanWorker(source)

        self.worker.result_ready.connect(
            self.show_scan_result
        )

        self.worker.error_occurred.connect(
            self.show_scan_error
        )

        self.worker.finished.connect(
            self.scan_finished
        )

        self.worker.start()

    def show_scan_result(self, result: ScanResult):
        size_gib = result.total_bytes / (1024 ** 3)

        self.summary_label.setText(
            f"전체 MKV: {result.total_mkv:,}개   |   "
            f"정상: {result.valid_mkv:,}개   |   "
            f"규칙 불일치: {result.invalid_mkv:,}개   |   "
            f"총용량: {size_gib:,.2f} GiB   |   "
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

            size_item = QTableWidgetItem()
            size_item.setData(
                Qt.ItemDataRole.DisplayRole,
                round(day.total_bytes / (1024 ** 3), 2),
            )

            self.table.setItem(row, 0, date_item)
            self.table.setItem(row, 1, count_item)
            self.table.setItem(row, 2, size_item)

        self.table.setSortingEnabled(True)
        self.table.resizeColumnsToContents()

        self.status_label.setText(
            f"검색 완료: {result.total_mkv:,}개"
        )

        self.statusBar().showMessage(
            "검색 완료. 원본 파일은 변경되지 않았습니다."
        )

    def show_scan_error(self, message: str):
        self.status_label.setText("검색 실패")

        QMessageBox.critical(
            self,
            "MKV 검색 오류",
            message,
        )

    def scan_finished(self):
        self.scan_button.setEnabled(True)
        self.browse_button.setEnabled(True)
        self.source_edit.setEnabled(True)

    def closeEvent(self, event):
        # 검색 작업이 실행 중이면 종료를 막는다.
        if self.worker and self.worker.isRunning():
            QMessageBox.information(
                self,
                "검색 진행 중",
                "검색이 완료된 후 창을 닫아 주세요.",
            )
            event.ignore()
            return

        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)

    window = MainWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
