"""Opt-in, multi-day resumable merge dialog. No work begins on opening dialog."""
from __future__ import annotations

import threading
import time
from datetime import date
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QProgressBar, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QPlainTextEdit, QInputDialog,
)

from .catalog_reader import get_catalog_snapshot
from .daily_resume import plan_cached_days, run_daily_resume
from .recovery_diagnosis import inspect_daily_output, format_diagnosis
from .recovery_engine import preflight as recovery_preflight, quarantine as recovery_quarantine
from .output_integrity import inspect_mkv_output

MAX_DAYS = 7  # Separate deliberate runs for larger archives.
MAX_OUTPUTS = 28


def selected_cached_dates(database: str | Path, source: str | Path,
                          start: str, end: str) -> tuple[str, ...]:
    """Choose indexed calendar dates, not every date between bounds."""
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    if a > b:
        raise ValueError('시작 날짜가 종료 날짜보다 늦습니다.')
    if (b - a).days + 1 > MAX_DAYS:
        raise ValueError(f'한 번에 최대 {MAX_DAYS}일 범위까지만 선택할 수 있습니다.')
    snapshot = get_catalog_snapshot(database, source)
    dates = tuple(item.date for item in snapshot.days if start <= item.date <= end)
    if not dates:
        raise ValueError('지정한 기간에 해당하는 SQLite 녹화 기록이 없습니다. 원본 경로를 확인하세요.')
    return dates


class DailyPlanWorker(QThread):
    ready = Signal(object)
    failed = Signal(str)

    def __init__(self, source, database, output, start, end, parts, mode):
        super().__init__()
        self.source, self.database, self.output = Path(source), Path(database), Path(output)
        self.start_date, self.end_date, self.parts, self.mode = start, end, parts, mode

    def run(self):
        try:
            if not self.source.is_dir():
                raise NotADirectoryError(f'원본 폴더에 접근할 수 없습니다: {self.source}')
            if not self.output.is_dir():
                raise NotADirectoryError(f'출력 폴더가 없습니다: {self.output}')
            if self.output.resolve().is_relative_to(self.source.resolve()):
                raise ValueError('출력 폴더를 원본 폴더 안에 지정할 수 없습니다.')
            dates = selected_cached_dates(self.database, self.source, self.start_date, self.end_date)
            if len(dates) * self.parts > MAX_OUTPUTS:
                raise ValueError(f'출력 그룹은 한 번에 최대 {MAX_OUTPUTS}개까지 계획할 수 있습니다.')
            groups = plan_cached_days(self.database, self.source, dates, self.parts, mode=self.mode)
            if not groups:
                raise ValueError('병합 가능한 그룹이 없습니다.')
            self.ready.emit((tuple(groups), dates))
        except Exception as exc:
            self.failed.emit(str(exc))


class DailyExecuteWorker(QThread):
    ready = Signal(object)
    failed = Signal(str)
    progress = Signal(str, int, int)

    def __init__(self, groups, output):
        super().__init__()
        self.groups = tuple(groups)
        self.output = Path(output)
        self.cancel_event = threading.Event()

    def request_cancel(self):
        self.cancel_event.set()

    def run(self):
        started = time.perf_counter()
        try:
            result = run_daily_resume(
                self.groups, self.output, cancel_event=self.cancel_event,
                on_progress=lambda name, finished, percent:
                    self.progress.emit(name, finished, percent),
                integrity_verifier=inspect_mkv_output,
            )
            self.ready.emit((result, time.perf_counter() - started))
        except Exception as exc:
            self.failed.emit(str(exc))


class DailyRecoveryInspectWorker(QThread):
    ready = Signal(str)
    failed = Signal(str)

    def __init__(self, output: str, expected: tuple[str, ...]):
        super().__init__()
        self.output, self.expected = output, expected

    def run(self):
        try:
            report = inspect_daily_output(self.output, self.expected)
            self.ready.emit(format_diagnosis(report))
        except Exception as exc:
            self.failed.emit(str(exc))


class DailyRecoveryPreflightWorker(QThread):
    ready = Signal(object)
    failed = Signal(str)

    def __init__(self, groups, output):
        super().__init__()
        self.groups, self.output = tuple(groups), output

    def run(self):
        try:
            self.ready.emit(recovery_preflight(self.groups, self.output))
        except Exception as exc:
            self.failed.emit(str(exc))


class DailyRecoveryApplyWorker(QThread):
    ready = Signal(str)
    failed = Signal(str)

    def __init__(self, plan, groups):
        super().__init__()
        self.plan, self.groups = plan, tuple(groups)

    def run(self):
        try:
            self.ready.emit(str(recovery_quarantine(self.plan, self.groups)))
        except Exception as exc:
            self.failed.emit(str(exc))


class DailyBatchDialog(QDialog):
    """Explicit 2-step preview/execute dialog for user-selected short date ranges."""

    def __init__(self, source: str, database: str, output: str,
                 parts: int, mode: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle('날짜별 일괄 병합 · 체크포인트 재개')
        self.resize(1000, 630)
        self.groups = ()
        self.worker = None
        self.recovery_plan = None
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            'SQLite에 등록된 날짜만 계획합니다. 미리보기만으로 병합은 시작되지 않습니다.\n'
            '동일한 출력 폴더·동일한 계획으로 재실행하면 검증된 완료 파일만 건너뜁니다.'
        ))
        form = QFormLayout()
        self.source_edit = QLineEdit(source)
        self.database_edit = QLineEdit(database)
        self.output_edit = QLineEdit(output)
        self.start_edit = QLineEdit()
        self.end_edit = QLineEdit()
        self.start_edit.setPlaceholderText('YYYY-MM-DD')
        self.end_edit.setPlaceholderText('YYYY-MM-DD')
        self.parts_edit = QLineEdit(str(parts))
        self.mode_combo = QComboBox()
        self.mode_combo.addItem('A · 파일 개수 균등', 'count')
        self.mode_combo.addItem('B · 실제 재생시간 균등 (DB 갱신 가능)', 'duration')
        idx = self.mode_combo.findData(mode)
        self.mode_combo.setCurrentIndex(max(0, idx))
        for title, widget in (
            ('NVR 원본 경로', self.source_edit), ('SQLite DB', self.database_edit),
            ('로컬 출력 폴더', self.output_edit), ('시작 날짜', self.start_edit),
            ('종료 날짜', self.end_edit), ('하루당 그룹 수', self.parts_edit),
            ('분할 방식', self.mode_combo),
        ):
            form.addRow(title, widget)
        layout.addLayout(form)
        self.preview_button = QPushButton('날짜 범위 병합 계획 미리보기')
        self.execute_button = QPushButton('미완료 그룹 병합 실행 / 중단 후 재개')
        self.execute_button.setEnabled(False)
        self.recovery_button = QPushButton('출력 폴더 중단 상태 진단 (읽기 전용)')
        self.recovery_preflight_button = QPushButton('중단 작업 복구 준비 검사 (미리보기 계획 필요)')
        self.recovery_apply_button = QPushButton('승인 후 미완료 파일 증거 보존')
        self.recovery_preflight_button.setEnabled(False)
        self.recovery_apply_button.setEnabled(False)
        self.cancel_button = QPushButton('진행 중인 병합 취소')
        self.cancel_button.setEnabled(False)
        btns = QHBoxLayout()
        for btn in (self.preview_button, self.execute_button, self.cancel_button):
            btns.addWidget(btn)
        layout.addLayout(btns)
        layout.addWidget(self.recovery_button)
        recovery_row = QHBoxLayout()
        recovery_row.addWidget(self.recovery_preflight_button)
        recovery_row.addWidget(self.recovery_apply_button)
        layout.addLayout(recovery_row)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(('날짜', '출력 파일명', '입력 파일 수', '예상 재생시간'))
        layout.addWidget(self.table)
        self.progress_bar = QProgressBar()
        layout.addWidget(self.progress_bar)
        self.status = QLabel('준비 완료. 최대 7일 범위, 최대 28개 출력 그룹.')
        layout.addWidget(self.status)
        self.preview_button.clicked.connect(self.start_preview)
        self.execute_button.clicked.connect(self.start_execution)
        self.cancel_button.clicked.connect(self.request_cancel)
        self.recovery_button.clicked.connect(self.start_recovery_inspection)
        self.recovery_preflight_button.clicked.connect(self.start_recovery_preflight)
        self.recovery_apply_button.clicked.connect(self.start_recovery_apply)
        for edit in (self.source_edit, self.database_edit, self.output_edit,
                     self.start_edit, self.end_edit, self.parts_edit):
            edit.textChanged.connect(self.invalidate_plan)
        self.mode_combo.currentIndexChanged.connect(self.invalidate_plan)

    def invalidate_plan(self, *_args):
        self.recovery_plan = None
        self.recovery_preflight_button.setEnabled(False)
        self.recovery_apply_button.setEnabled(False)
        self.groups = ()
        self.table.setRowCount(0)
        self.execute_button.setEnabled(False)

    def set_busy(self, busy: bool):
        for item in (self.source_edit, self.database_edit, self.output_edit,
                     self.start_edit, self.end_edit, self.parts_edit, self.mode_combo,
                     self.preview_button, self.recovery_button,
                     self.recovery_preflight_button, self.recovery_apply_button):
            item.setEnabled(not busy)
        self.execute_button.setEnabled(not busy and bool(self.groups))
        self.recovery_preflight_button.setEnabled(not busy and bool(self.groups))
        self.recovery_apply_button.setEnabled(not busy and self.recovery_plan is not None)
        self.cancel_button.setEnabled(
            busy and isinstance(self.worker, DailyExecuteWorker)
            and not self.worker.cancel_event.is_set()
        )

    def start_worker(self, worker):
        if self.worker is not None:
            return
        self.worker = worker
        self.set_busy(True)
        worker.failed.connect(self.show_failure)
        worker.finished.connect(self.worker_finished)
        worker.start()

    def worker_finished(self):
        if self.worker is not None:
            self.worker.deleteLater()
        self.worker = None
        self.set_busy(False)

    def start_recovery_preflight(self):
        if self.worker is not None or not self.groups:
            return
        self.recovery_plan = None
        self.recovery_apply_button.setEnabled(False)
        self.status.setText('복구 준비 검사 중: 원본·체크포인트·잠금 소유자 검증...')
        worker = DailyRecoveryPreflightWorker(self.groups, self.output_edit.text().strip())
        worker.ready.connect(self.show_recovery_plan)
        self.start_worker(worker)

    def show_recovery_plan(self, plan):
        self.recovery_plan = plan
        items = '\n'.join(f'  - {p.name} ({p.stat().st_size:,} bytes)' for p in plan.archives)
        QMessageBox.information(
            self, '복구 준비 검사 완료 — 아직 파일 이동 없음',
            f'병합 계획 서명과 원본이 일치합니다. 완료 그룹 {len(plan.completed)}개는 '
            f'유지합니다.\n\n출력 폴더: {plan.output}\n\n'
            f'보존 대상 ({len(plan.archives)}개):\n{items}\n\n'
            '실행 중인 잠금 소유자가 프로세스 목록에서 발견되지 않았습니다. '
            '이것만으로 안전을 보장하지는 않으므로 실제 실행 때 다시 검사합니다.\n'
            '증거 파일은 출력 폴더 밖의 별도 하위 경로에 보관합니다.'
        )
        self.status.setText('복구 사전 점검 통과. 실행은 별도 승인 필요.')

    def start_recovery_apply(self):
        if self.worker is not None or self.recovery_plan is None or not self.groups:
            return
        plan = self.recovery_plan
        phrase = 'ARCHIVE INCOMPLETE ARTIFACTS'
        entered, ok = QInputDialog.getText(
            self, '복구 승인 문구 확인',
            f'원본 및 완료 MKV는 그대로 둡니다.\n증거 보존만 실행합니다.\n'
            f'승인하려면 다음 문구를 정확히 입력하세요:\n{phrase}'
        )
        if not ok or entered != phrase:
            return
        decision = QMessageBox.warning(
            self, '마지막 확인 — 증거 파일 이동',
            f'출력 폴더: {plan.output}\n'
            f'미완료 파일 및 잠금 {len(plan.archives)}개를 출력 폴더 밖에 보존합니다.\n'
            '병합은 시작되지 않습니다. 계속할까요?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if decision != QMessageBox.StandardButton.Yes:
            return
        self.recovery_apply_button.setEnabled(False)
        self.status.setText('복구 적용 중: 전체 조건 재검사 및 증거 보존...')
        worker = DailyRecoveryApplyWorker(plan, tuple(self.groups))
        worker.ready.connect(self.show_recovery_applied)
        self.start_worker(worker)

    def show_recovery_applied(self, evidence):
        self.recovery_plan = None
        self.invalidate_plan()  # Fresh preview is mandatory before a new merge.
        self.status.setText('증거 보존 완료. 새 병합 계획 미리보기가 필요합니다.')
        QMessageBox.information(
            self, '복구 준비 완료 — 자동 병합하지 않음',
            f'증거 보존 폴더:\n{evidence}\n\n'
            '원본과 완료된 출력, 체크포인트는 유지했습니다.\n'
            '미완료 그룹을 다시 병합하려면 계획을 새로 미리보고 실행하세요.'
        )

    def start_recovery_inspection(self):
        if self.worker is not None:
            return
        # A changed plan is invalidated by existing textChanged handlers.
        expected = tuple(g.output_name for g in self.groups)
        worker = DailyRecoveryInspectWorker(self.output_edit.text().strip(), expected)
        worker.ready.connect(self.show_recovery_inspection)
        self.status.setText('출력 폴더 중단 상태 읽기 전용 진단 중...')
        self.start_worker(worker)

    def show_recovery_inspection(self, body: str):
        dialog = QDialog(self)
        dialog.setWindowTitle('중단 작업 진단 — 읽기 전용 / 복구 실행 불가')
        dialog.resize(760, 510)
        layout = QVBoxLayout(dialog)
        edit = QPlainTextEdit()
        edit.setReadOnly(True)
        edit.setPlainText(body)
        layout.addWidget(edit)
        close_button = QPushButton('닫기')
        close_button.clicked.connect(dialog.accept)
        layout.addWidget(close_button)
        dialog.exec()
        self.status.setText('진단 완료. 출력 파일이나 잠금을 변경하지 않았습니다.')

    def start_preview(self):
        if self.worker is not None:
            return
        self.invalidate_plan()
        try:
            parts = int(self.parts_edit.text().strip())
            if not 1 <= parts <= MAX_OUTPUTS:
                raise ValueError('하루당 그룹 수가 올바르지 않습니다.')
        except ValueError as exc:
            QMessageBox.warning(self, '그룹 수 확인', str(exc))
            return
        self.status.setText('SQLite 날짜 조회 및 병합 계획 생성 중...')
        worker = DailyPlanWorker(
            self.source_edit.text().strip(), self.database_edit.text().strip(),
            self.output_edit.text().strip(), self.start_edit.text().strip(),
            self.end_edit.text().strip(), parts, self.mode_combo.currentData(),
        )
        worker.ready.connect(self.show_preview)
        self.start_worker(worker)

    def show_preview(self, payload):
        groups, dates = payload
        self.groups = groups
        self.table.setRowCount(len(groups))
        for row, group in enumerate(groups):
            ms = getattr(group, 'total_duration_ms', None)
            duration = '—' if ms is None else f'{ms / 60000:.2f} 분'
            for column, value in enumerate((group.date, group.output_name, len(group.files), duration)):
                self.table.setItem(row, column, QTableWidgetItem(str(value)))
        self.status.setText(
            f'미리보기 완료: {len(dates)}일, {len(groups)}개 그룹, '
            f'{sum(len(group.files) for group in groups)}개 MKV. 병합은 실행하지 않았습니다.'
        )
        self.execute_button.setEnabled(True)
        self.recovery_preflight_button.setEnabled(True)

    def start_execution(self):
        if self.worker is not None or not self.groups:
            return
        groups = tuple(self.groups)
        output = self.output_edit.text().strip()
        response = QMessageBox.question(
            self, '날짜별 실제 병합 / 재개 확인',
            f'날짜 {len(set(group.date for group in groups))}일 · 그룹 {len(groups)}개를 '
            f'출력 폴더에 실제로 병합합니다.\n\n{output}\n\n'
            '완료 체크포인트가 일치하는 파일만 건너뛰고, 낯선 결과물이나 잠금·부분 파일이 '
            '발견되면 중단합니다.\n'
            '원본 MKV는 변경하지 않습니다. 실제 병합을 실행할까요?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if response != QMessageBox.StandardButton.Yes:
            return
        self.progress_bar.setValue(0)
        self.status.setText('날짜별 병합 사전 검증/실행 중...')
        worker = DailyExecuteWorker(groups, output)
        worker.progress.connect(self.show_progress)
        worker.ready.connect(self.show_execution_result)
        self.start_worker(worker)

    def show_progress(self, name, finished, percent):
        self.progress_bar.setValue(percent)
        self.status.setText(f'병합 중: {name} / 앞서 완료·건너뛴 그룹 {finished}개 / {percent}%')

    def request_cancel(self):
        if isinstance(self.worker, DailyExecuteWorker):
            self.worker.request_cancel()
            self.cancel_button.setEnabled(False)
            self.status.setText('취소 요청됨. 병합 엔진 응답 확인 중...')

    def show_execution_result(self, payload):
        result, elapsed = payload
        self.status.setText(
            f'{"취소" if result.cancelled else "완료"}: 이번 실행 {len(result.completed)}개, '
            f'기존 완료 {len(result.skipped)}개 건너뜀, 전체 {result.total}개 / {elapsed:.2f}초'
        )
        self.invalidate_plan()  # Re-plan before any subsequent run, even after success.
        QMessageBox.information(self, '날짜별 일괄 병합 결과',
                                self.status.text() + '\n실제 출력 파일을 재생해 검증해 주세요.')

    def show_failure(self, message):
        self.status.setText(f'작업 중단: {message}')
        self.invalidate_plan()
        QMessageBox.critical(self, '날짜별 일괄 병합 오류', message)

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            if isinstance(self.worker, DailyExecuteWorker):
                response = QMessageBox.question(
                    self, '병합 진행 중', '창을 닫기 전에 취소를 요청할까요? 취소 완료 후 다시 닫으세요.',
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if response == QMessageBox.StandardButton.Yes:
                    self.request_cancel()
            else:
                QMessageBox.information(self, '계획 생성 중', '계획 생성이 끝난 후 닫아 주세요.')
            event.ignore()
            return
        super().closeEvent(event)
