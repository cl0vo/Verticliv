from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from .app import MainWindow
from .batch import BatchPlan, format_timestamp, load_plan, reset_progress
from .batch_worker import BatchRenderWorker
from .render import MAX_REEL_SECONDS, RenderOptions
from .version import __version__


class IntegratedBatchWindow(MainWindow):
    """One-window ARARA workflow for both single Reels and long recordings."""

    def __init__(self) -> None:
        self.batch_mode = False
        self.batch_worker: BatchRenderWorker | None = None
        self._long_source_duration = 0.0
        super().__init__()

        self.setWindowTitle(f"Verticliv {__version__}")
        self.reel_card.title_label.setText("1. ЗАПИСЬ ARARA")
        self.reel_card.hint_label.setText(
            "Выбери готовый короткий Reel или длинную запись. Длинное видео программа "
            "сама разрежет по паузам на фрагменты 9–15 секунд."
        )
        self.brainrot_card.title_label.setText("2. BRAINROT")
        self.brainrot_card.hint_label.setText(
            "Выбери длинное видео один раз. Свежий участок будет подставляться автоматически."
        )

        description = self.reel_card.parentWidget().layout().itemAt(0).widget()
        if isinstance(description, QLabel):
            description.setText(
                "Выбери два видео → настрой рамку brainrot справа → нажми одну золотую кнопку."
            )

        # Technical actions are automatic now. Keep them available only through code/state.
        self.prepare_button.hide()
        self.reset_button.hide()
        self.next_button.hide()
        self.library_status.setText("Участки brainrot подготавливаются автоматически")
        self.settings_button.setText("Дополнительно")
        self.open_button.setText("Открыть готовые")
        self.preview_button.setText("ПРЕВЬЮ 5 СЕК")

        # Batch mode is integrated, so never open a second ARARA window.
        self.preview_panel.batch_button.hide()
        self.preview_panel.edit_box.hide()
        self.preview_panel.canvas.set_edit_enabled(True)
        self.preview_panel.reset_button.setText("Сбросить рамку")

        self.batch_frame = QFrame()
        self.batch_frame.setObjectName("settingsPanel")
        batch_layout = QVBoxLayout(self.batch_frame)
        batch_layout.setContentsMargins(16, 13, 16, 13)
        batch_layout.setSpacing(9)

        title = QLabel("3. СКОЛЬКО REELS СДЕЛАТЬ СЕЙЧАС")
        title.setObjectName("cardTitle")
        batch_layout.addWidget(title)

        row = QHBoxLayout()
        row.addWidget(QLabel("Порция:"))
        self.batch_size = QComboBox()
        self.batch_size.setMaximumWidth(180)
        self.batch_size.addItem("10 роликов", 10)
        self.batch_size.addItem("25 роликов", 25)
        self.batch_size.addItem("50 роликов", 50)
        self.batch_size.addItem("100 роликов", 100)
        self.batch_size.addItem("Все оставшиеся", 0)
        saved_size = int(self.settings.value("batch_size", 10) or 10)
        saved_index = self.batch_size.findData(saved_size)
        self.batch_size.setCurrentIndex(saved_index if saved_index >= 0 else 0)
        self.batch_size.currentIndexChanged.connect(self._batch_size_changed)
        row.addWidget(self.batch_size)
        row.addStretch(1)
        batch_layout.addLayout(row)

        self.batch_status = QLabel(
            "Первый запуск сам найдёт паузы, создаст план и сразу начнёт выбранную порцию."
        )
        self.batch_status.setObjectName("libraryStatus")
        self.batch_status.setWordWrap(True)
        batch_layout.addWidget(self.batch_status)

        tools = QHBoxLayout()
        self.stop_button = QPushButton("Остановить после текущего Reel")
        self.stop_button.setMaximumWidth(245)
        self.stop_button.clicked.connect(self.stop_batch_after_current)
        self.stop_button.hide()
        self.batch_reset = QPushButton("Начать эту запись заново")
        self.batch_reset.setObjectName("linkButton")
        self.batch_reset.setMaximumWidth(190)
        self.batch_reset.clicked.connect(self.reset_batch_progress)
        tools.addWidget(self.stop_button)
        tools.addStretch(1)
        tools.addWidget(self.batch_reset)
        batch_layout.addLayout(tools)

        main_layout = self.progress.parentWidget().layout()
        progress_index = main_layout.indexOf(self.progress)
        main_layout.insertWidget(progress_index, self.batch_frame)
        self.batch_frame.setVisible(False)
        self.refresh_ready_state()

    def inspect_reel(self, value: str) -> None:
        path = Path(value) if value else Path("__missing__")
        if not path.is_file():
            self.batch_mode = False
            self._long_source_duration = 0.0
            super().inspect_reel(value)
            self._sync_mode_ui()
            return

        try:
            info = self._probe(path)
            is_long_valid = (
                info.duration > MAX_REEL_SECONDS + 0.05
                and abs(info.width / info.height - 9 / 16) <= 0.015
                and info.has_audio
            )
        except Exception:
            is_long_valid = False

        if not is_long_valid:
            self.batch_mode = False
            self._long_source_duration = 0.0
            super().inspect_reel(value)
            self._sync_mode_ui()
            return

        self.batch_mode = True
        self._long_source_duration = float(info.duration)
        self.reel_valid = True
        self.reel_card.set_status(
            f"{info.width}×{info.height} · {info.duration / 60:.1f} мин · будет нарезано по паузам",
            "ok",
        )
        self.preview_panel.load_file(path, autoplay=False, title="длинная запись ARARA")
        self._sync_mode_ui()
        self._refresh_batch_plan(path)
        self.refresh_ready_state()

    def _selected_batch_size(self) -> int:
        return int(self.batch_size.currentData() or 0)

    def _batch_size_changed(self, *args) -> None:
        self.settings.setValue("batch_size", self._selected_batch_size())
        self.settings.sync()
        self._sync_primary_button()

    def _sync_primary_button(self) -> None:
        if not self.batch_mode:
            self.render_button.setText("СОЗДАТЬ REEL")
            self.render_button.setMaximumWidth(220)
            return
        size = self._selected_batch_size()
        label = "ВСЕ ОСТАВШИЕ" if size == 0 else str(size)
        self.render_button.setText(f"СОЗДАТЬ {label} REELS")
        self.render_button.setMaximumWidth(285)

    def _sync_mode_ui(self) -> None:
        if not hasattr(self, "batch_frame"):
            return
        self.batch_frame.setVisible(self.batch_mode)
        self._sync_primary_button()
        self.preview_button.setText("ПРЕВЬЮ 5 СЕК")

    def _refresh_batch_plan(self, source: Path | None = None) -> None:
        if not hasattr(self, "batch_status"):
            return
        source = source or Path(self.reel_card.path)
        if not source.is_file():
            self.batch_status.setText("Выбери длинную запись ARARA")
            return
        plan = load_plan(source)
        if plan is None:
            estimated = max(1, int(self._long_source_duration / 12.0))
            self.batch_status.setText(
                f"Ориентировочно {estimated} роликов. Первый клик выполнит анализ и сразу начнёт сборку."
            )
            return
        self._show_batch_plan(plan)

    def _show_batch_plan(self, plan: BatchPlan) -> None:
        next_segment = plan.next_segment
        if next_segment is None:
            next_text = "вся запись обработана"
        else:
            next_text = f"следующий старт {format_timestamp(next_segment.start).replace('-', ':')}"
        self.batch_status.setText(
            f"Готово {plan.completed_count} из {len(plan.segments)} · "
            f"осталось {plan.remaining_count} · {next_text}"
        )

    def refresh_ready_state(self) -> None:
        super().refresh_ready_state()
        self._sync_mode_ui()
        if not self.batch_mode:
            if self.reel_valid and self.brainrot_valid:
                self.status.setText("Всё готово · проверь сцену справа и нажми «СОЗДАТЬ REEL»")
            return

        ready = self.reel_valid and self.brainrot_valid
        busy = bool(self.batch_worker and self.batch_worker.isRunning())
        self.preview_button.setEnabled(ready and not busy)
        self.render_button.setEnabled(ready and not busy)
        if ready and not busy:
            self.status.setText("Всё готово · выбери размер порции и нажми золотую кнопку")

    def start_render(self, preview: bool) -> None:
        if self.batch_mode and not preview:
            self.start_batch()
            return
        super().start_render(preview)

    def start_batch(self) -> None:
        source = Path(self.reel_card.path)
        brainrot = Path(self.brainrot_card.path)
        output = Path(self.output_picker.path)
        if not self.reel_valid or not self.brainrot_valid or not self.batch_mode:
            QMessageBox.warning(
                self,
                "Проверь два видео",
                "Нужны длинная вертикальная запись ARARA со звуком и длинный brainrot.",
            )
            return
        if not self.output_picker.path:
            QMessageBox.warning(self, "Нужна папка", "Открой «Дополнительно» и выбери папку результата.")
            return

        self.save_preferences()
        output.mkdir(parents=True, exist_ok=True)
        limit = self._selected_batch_size()
        transform = self.preview_panel.brain_transform()
        options = RenderOptions(
            variants=1,
            subtitle_y=self.subtitle_y.value(),
            font="Arial Black",
            encoder_preset="veryfast",
            crf=self.quality.value(),
            encoder_mode=str(self.encoder.currentData()),
            brainrot_zoom=self.zoom.value(),
            subtitles_enabled=self.subtitles_enabled.isChecked(),
            banner_enabled=self.banner_enabled.isChecked(),
            banner_path=self.banner_picker.path,
            banner_width_percent=self.banner_width.value(),
            banner_top_percent=self.banner_top.value(),
            brainrot_x=transform.x,
            brainrot_y=transform.y,
            brainrot_width=transform.width,
            brainrot_height=transform.height,
        )

        self.preview_panel.player.pause()
        self.set_busy(True)
        self.log.clear()
        self.progress.setValue(0)
        amount = "все оставшиеся" if limit == 0 else f"{limit} роликов"
        self.status.setText(f"Запускаю порцию: {amount}…")
        self.batch_worker = BatchRenderWorker(source, brainrot, output, limit, options)
        self.batch_worker.progressed.connect(self.on_progress)
        self.batch_worker.logged.connect(self.log.append)
        self.batch_worker.completed.connect(self.batch_done)
        self.batch_worker.stopped.connect(self.batch_stopped)
        self.batch_worker.failed.connect(self.batch_failed)
        self.batch_worker.start()

    def stop_batch_after_current(self) -> None:
        if not self.batch_worker or not self.batch_worker.isRunning():
            return
        self.batch_worker.request_stop_after_current()
        self.stop_button.setEnabled(False)
        self.stop_button.setText("Останавливаю после текущего…")
        self.status.setText("Текущий Reel будет завершён, затем очередь остановится")

    def _finish_batch_ui(self, plan: BatchPlan) -> None:
        self.batch_worker = None
        self.set_busy(False)
        self.stop_button.hide()
        self.stop_button.setEnabled(True)
        self.stop_button.setText("Остановить после текущего Reel")
        self._show_batch_plan(plan)
        self.inspect_brainrot()

    def batch_done(self, files: list[str], plan: BatchPlan) -> None:
        self._finish_batch_ui(plan)
        self.progress.setValue(100)
        if not files:
            self.status.setText("Все фрагменты этой записи уже собраны")
            return
        self._show_last_result(files, f"Порция готова: {len(files)} роликов")

    def batch_stopped(self, files: list[str], plan: BatchPlan) -> None:
        self._finish_batch_ui(plan)
        if files:
            self._show_last_result(
                files,
                f"Остановлено безопасно · создано {len(files)} · продолжишь с этого места",
                open_folder=False,
            )
        else:
            self.status.setText("Очередь остановлена · готовые файлы не потеряны")

    def _show_last_result(
        self,
        files: list[str],
        message: str,
        *,
        open_folder: bool = True,
    ) -> None:
        result = Path(files[-1])
        self.preview_panel.load_file(
            result,
            autoplay=True,
            title="последний готовый Reel",
            editable_source=False,
        )
        self.status.setText(message + " · прогресс сохранён")
        if open_folder and self.auto_open.isChecked():
            self.open_output()

    def batch_failed(self, error: str) -> None:
        self.batch_worker = None
        self.set_busy(False)
        self.stop_button.hide()
        self._refresh_batch_plan()
        self.status.setText("Очередь остановлена · уже готовые ролики сохранены")
        self.log.setPlainText(error)
        self.log.setVisible(True)
        self.log_button.setText("Скрыть технический журнал")
        QMessageBox.critical(self, "Ошибка пакетной сборки", error.splitlines()[-1])

    def reset_batch_progress(self) -> None:
        source = Path(self.reel_card.path)
        if not source.is_file():
            return
        answer = QMessageBox.question(
            self,
            "Начать эту запись заново?",
            (
                "Программа забудет обработанные места. Готовые файлы останутся, "
                "но следующая сборка снова начнётся с начала и создаст повторы."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            reset_progress(source)
            self._refresh_batch_plan(source)
            self.status.setText("Прогресс записи сброшен")

    def save_preferences(self, *args) -> None:
        super().save_preferences(*args)
        if hasattr(self, "batch_size"):
            self.settings.setValue("batch_size", self._selected_batch_size())
            self.settings.sync()

    def set_busy(self, busy: bool) -> None:
        super().set_busy(busy)
        if hasattr(self, "batch_size"):
            self.batch_size.setDisabled(busy)
            self.batch_reset.setDisabled(busy)
            self.stop_button.setVisible(busy and self.batch_mode)
        if busy and self.batch_mode:
            self.render_button.setEnabled(False)
            self.preview_button.setEnabled(False)

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.batch_worker and self.batch_worker.isRunning():
            answer = QMessageBox.question(
                self,
                "Идёт создание порции",
                "Завершить текущий Reel и остановить очередь? Окно можно свернуть, пока Reel заканчивается.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.stop_batch_after_current()
                self.showMinimized()
            event.ignore()
            return
        if self.render_worker and self.render_worker.isRunning():
            QMessageBox.information(self, "Идёт создание Reel", "Дождись завершения текущего Reel.")
            event.ignore()
            return
        super().closeEvent(event)


def install(app_module) -> None:
    app_module.MainWindow = IntegratedBatchWindow
