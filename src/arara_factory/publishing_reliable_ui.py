from __future__ import annotations

import time
import traceback
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtWidgets import QApplication, QMessageBox

from .process_utils import set_system_awake
from .publishing import (
    PLATFORM_LABELS,
    Platform,
    PublishJob,
    PublishQueue,
    retry_one_failed_delivery,
)
from .publishing_journal import append_publish_log, tail_publish_log
from .publishing_reliable import publish_platform_reliable
from .publishing_targets_ui import TargetAwareSmartWindow


class ReliablePublishWorker(QThread):
    progressed = Signal(int, str)
    logged = Signal(str)
    completed = Signal(object, object)
    failed = Signal(str)

    def __init__(
        self,
        queue: PublishQueue,
        job: PublishJob,
        allowed_platforms: set[Platform] | None = None,
    ) -> None:
        super().__init__()
        self.queue = queue
        self.job = job
        # A snapshot of the active checkboxes protects the queue from uploading
        # to a platform that was not selected when this worker was started.
        # ``None`` keeps the constructor backward compatible for non-UI callers.
        self.allowed_platforms = (
            None if allowed_platforms is None else frozenset(allowed_platforms)
        )

    def _log(self, message: str) -> None:
        line = append_publish_log(message)
        if line:
            self.logged.emit(line)

    def run(self) -> None:
        try:
            video = Path(self.job.video)
            if not video.is_file():
                raise RuntimeError(f"Файл для публикации не найден: {video}")

            pending: list[Platform] = []
            for platform in self.job.pending_platforms:
                if (
                    self.allowed_platforms is not None
                    and platform not in self.allowed_platforms
                ):
                    continue
                state = self.job.deliveries.get(platform.value)
                if state and state.status == "pending":
                    pending.append(platform)

            if not pending:
                self.completed.emit(self.job, {})
                return

            self._log(
                f"JOB {self.job.id[:8]} · {video.name} · старт · "
                + ", ".join(PLATFORM_LABELS[p] for p in pending)
            )
            errors: dict[str, str] = {}
            total = len(pending)

            for position, platform in enumerate(pending):
                if self.isInterruptionRequested():
                    break
                base = int(position * 100 / total)
                span = max(1, int(100 / total))
                self.queue.update_delivery(self.job, platform, status="uploading")
                self._log(
                    f"{PLATFORM_LABELS[platform]} · {video.name} · попытка "
                    f"{self.job.deliveries[platform.value].attempts}"
                )
                last_log_key = ""

                def mapped(value: int, text: str) -> None:
                    nonlocal last_log_key
                    value = max(0, min(100, int(value)))
                    overall = min(99, base + int(span * value / 100))
                    self.progressed.emit(overall, text)
                    # Keep the persistent journal useful without writing the same
                    # progress line dozens of times.
                    key = f"{value // 5}:{text}"
                    if key != last_log_key:
                        last_log_key = key
                        self._log(f"{video.name} · {text}")

                try:
                    remote_id = publish_platform_reliable(
                        platform,
                        video,
                        self.job.caption,
                        mapped,
                    )
                    self.queue.update_delivery(
                        self.job,
                        platform,
                        status="success",
                        remote_id=remote_id,
                    )
                    self._log(
                        f"{PLATFORM_LABELS[platform]} · УСПЕХ · {video.name} · ID {remote_id}"
                    )
                except Exception as exc:
                    message = str(exc) or exc.__class__.__name__
                    errors[platform.value] = message
                    self.queue.update_delivery(
                        self.job,
                        platform,
                        status="failed",
                        error=message,
                    )
                    self._log(
                        f"{PLATFORM_LABELS[platform]} · ОШИБКА · {video.name} · {message}"
                    )
                    details = traceback.format_exc().strip()
                    if details:
                        self._log(details)

            self.progressed.emit(100, "Публикация обработана")
            self.completed.emit(self.job, errors)
        except Exception:
            details = traceback.format_exc()
            self._log(details)
            self.failed.emit(details)


class ReliablePublishingWindow(TargetAwareSmartWindow):
    """Publishing UX where errors are visible, persistent and never silently loop."""

    def __init__(self) -> None:
        self._confirmed_close = False
        super().__init__()
        self._recover_interrupted_uploads()
        old_log = tail_publish_log()
        if old_log:
            self.log.setPlainText(old_log)
        self.refresh_publish_status()

    def _append_ui_log(self, line: str) -> None:
        if not line:
            return
        self.log.append(line)
        scrollbar = self.log.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _recover_interrupted_uploads(self) -> int:
        recovered = 0
        now = time.time()
        for job in self.publish_queue.jobs:
            for state in job.deliveries.values():
                if state.status == "uploading":
                    state.status = "failed"
                    state.error = "Предыдущая загрузка была прервана закрытием программы."
                    state.updated_at = now
                    recovered += 1
        if recovered:
            self.publish_queue.save()
            line = append_publish_log(
                f"Восстановление очереди: {recovered} незавершённых загрузок помечено ошибкой."
            )
            self._append_ui_log(line)
        return recovered

    def _next_runnable_job(self) -> PublishJob | None:
        selected_names = {
            platform.value for platform in self.selected_platforms()
        }
        if not selected_names:
            return None

        now = time.time()
        candidates: list[PublishJob] = []
        for job in self.publish_queue.jobs:
            if job.due_at > now:
                continue
            if any(
                name in selected_names and state.status == "pending"
                for name, state in job.deliveries.items()
            ):
                candidates.append(job)
        if not candidates:
            return None
        return min(candidates, key=lambda item: (item.due_at, item.created_at))

    def process_publish_queue(self) -> None:
        if not self.publish_timer.isActive():
            return
        if self.publish_worker and self.publish_worker.isRunning():
            return
        if self.batch_worker and self.batch_worker.isRunning():
            return
        if self.render_worker and self.render_worker.isRunning():
            return

        allowed_platforms = set(self.selected_platforms())
        if not allowed_platforms:
            self.refresh_publish_status()
            return
        job = self._next_runnable_job()
        if job is None:
            self.refresh_publish_status()
            return

        self.publish_worker = ReliablePublishWorker(
            self.publish_queue,
            job,
            allowed_platforms,
        )
        self.publish_worker.progressed.connect(self.on_publish_progress)
        self.publish_worker.logged.connect(self._append_ui_log)
        self.publish_worker.completed.connect(self.publish_done)
        self.publish_worker.failed.connect(self.publish_failed)
        self.publish_worker.start()
        self.refresh_publish_status()

    def on_publish_progress(self, value: int, text: str) -> None:
        self.progress.setValue(max(0, min(100, int(value))))
        self.status.setText("Публикация · " + text)

    def publish_done(self, job: PublishJob, errors: dict[str, str]) -> None:
        self.publish_worker = None
        selected_names = {platform.value for platform in self.selected_platforms()}
        visible_errors = {
            name: message
            for name, message in errors.items()
            if name in selected_names
        }
        if visible_errors:
            parts = []
            for name, message in visible_errors.items():
                try:
                    label = PLATFORM_LABELS[Platform(name)]
                except ValueError:
                    label = name
                parts.append(f"{label}: {message}")
            self.status.setText("Ошибка публикации · " + " | ".join(parts))
            self.log.setVisible(True)
            if hasattr(self, "log_button"):
                self.log_button.setText("Скрыть технический журнал")
        else:
            successful = []
            for platform in self.selected_platforms():
                delivery = job.deliveries.get(platform.value)
                if delivery and delivery.status == "success":
                    successful.append(PLATFORM_LABELS[platform])
            target_text = ", ".join(successful) if successful else "выбранной платформе"
            self.status.setText(
                f"Опубликовано: {Path(job.video).name} · {target_text}"
            )

        self.progress.setValue(100)
        self.refresh_publish_status()
        QTimer.singleShot(700, self.process_publish_queue)

    def publish_failed(self, error: str) -> None:
        self.publish_worker = None
        line = append_publish_log("Критическая ошибка worker:\n" + error)
        self._append_ui_log(line)
        self.status.setText("Критическая ошибка очереди публикации")
        self.log.setVisible(True)
        if hasattr(self, "log_button"):
            self.log_button.setText("Скрыть технический журнал")
        self.refresh_publish_status()

    def retry_failed(self) -> None:
        selected = {platform.value for platform in self.selected_platforms()}
        retried = retry_one_failed_delivery(
            self.publish_queue,
            platform_names=selected,
        )
        if retried is None:
            line = append_publish_log(
                "Пользователь запросил повтор: подходящих ошибок нет."
            )
            self._append_ui_log(line)
            self.status.setText("Нет ошибок для повтора на выбранных платформах")
            self.refresh_publish_status()
            return

        job, name = retried
        try:
            label = PLATFORM_LABELS[Platform(name)]
        except ValueError:
            label = name
        line = append_publish_log(
            f"Пользователь повторяет 1 ошибку: {Path(job.video).name} · {label}"
        )
        self._append_ui_log(line)
        self.status.setText(
            f"Повторяется 1 ошибка: {Path(job.video).name} · {label}"
        )
        self.refresh_publish_status()
        self.process_publish_queue()

    def refresh_publish_status(self) -> None:
        super().refresh_publish_status()
        if not hasattr(self, "publish_queue"):
            return
        failed = sum(
            1
            for job in self.publish_queue.jobs
            if any(state.status == "failed" for state in job.deliveries.values())
        )
        if failed and hasattr(self, "publish_status"):
            self.publish_status.setText(
                self.publish_status.text() + f" · ошибок {failed}"
            )

    def _stop_thread(self, worker) -> None:
        if worker is None or not worker.isRunning():
            return
        try:
            worker.requestInterruption()
            worker.quit()
            if not worker.wait(250):
                worker.terminate()
                worker.wait(1250)
        except Exception:
            pass

    def _save_before_forced_close(self) -> None:
        try:
            self.save_publish_preferences()
        except Exception:
            pass
        try:
            self.save_preferences()
        except Exception:
            pass

        if hasattr(self, "publish_timer"):
            self.publish_timer.stop()

        # Stop workers first, then persist the last truthful queue state so a
        # background thread cannot overwrite it after we mark interruption.
        self._stop_thread(getattr(self, "publish_worker", None))

        now = time.time()
        interrupted = 0
        for job in self.publish_queue.jobs:
            for state in job.deliveries.values():
                if state.status == "uploading":
                    state.status = "failed"
                    state.error = "Загрузка прервана при закрытии ARARA Factory."
                    state.updated_at = now
                    interrupted += 1
        self.publish_queue.save()
        if interrupted:
            append_publish_log(
                f"Закрытие программы: {interrupted} активных загрузок сохранено как ошибка."
            )

        for name in (
            "batch_worker",
            "render_worker",
            "index_worker",
            "update_check_worker",
            "update_download_worker",
        ):
            self._stop_thread(getattr(self, name, None))

        # Transactional renders are never considered complete while *.part.mp4
        # exists, so cleaning them here cannot remove a valid final Reel.
        try:
            output = Path(self.output_picker.path)
            for partial in output.glob("*.part.mp4"):
                partial.unlink(missing_ok=True)
        except Exception:
            pass

        try:
            self.publish_settings.sync()
        except Exception:
            pass
        try:
            self.settings.sync()
        except Exception:
            pass
        set_system_awake(False)

    def closeEvent(self, event) -> None:  # noqa: N802
        if not self._confirmed_close:
            answer = QMessageBox.question(
                self,
                "Закрыть ARARA Factory?",
                "Ты уверен, что хочешь закрыть программу? Все очереди и настройки сохранятся.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._confirmed_close = True

        self._save_before_forced_close()
        event.accept()
        QTimer.singleShot(0, QApplication.quit)


def install(app_module) -> None:
    app_module.MainWindow = ReliablePublishingWindow
