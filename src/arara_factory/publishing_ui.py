from __future__ import annotations

import time
import traceback
from pathlib import Path

from PySide6.QtCore import QSettings, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .integrated_batch import IntegratedBatchWindow
from .publishing import (
    PLATFORM_LABELS,
    Platform,
    PublishJob,
    PublishQueue,
    connect_youtube,
    platform_connected,
    publish_platform,
    retry_one_failed_delivery,
    spread_overdue_runnable_jobs,
    startup_autopause_count,
)
from .secure_store import load_credentials, update_platform_credentials


class PublishWorker(QThread):
    progressed = Signal(int, str)
    completed = Signal(object, object)
    failed = Signal(str)

    def __init__(self, queue: PublishQueue, job: PublishJob) -> None:
        super().__init__()
        self.queue = queue
        self.job = job

    def run(self) -> None:
        try:
            video = Path(self.job.video)
            if not video.is_file():
                raise RuntimeError(f"Файл для публикации не найден: {video}")
            pending = self.job.pending_platforms
            if not pending:
                self.completed.emit(self.job, {})
                return

            errors: dict[str, str] = {}
            total = len(pending)
            for position, platform in enumerate(pending):
                base = int(position * 100 / total)
                span = max(1, int(100 / total))
                self.queue.update_delivery(self.job, platform, status="uploading")

                def mapped(value: int, text: str) -> None:
                    overall = min(99, base + int(span * max(0, min(100, value)) / 100))
                    self.progressed.emit(overall, f"{PLATFORM_LABELS[platform]} · {text}")

                try:
                    remote_id = publish_platform(
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
                except Exception as exc:
                    message = str(exc) or exc.__class__.__name__
                    errors[platform.value] = message
                    self.queue.update_delivery(
                        self.job,
                        platform,
                        status="failed",
                        error=message,
                    )

            if errors:
                self.job.due_at = time.time() + 10 * 60
                self.queue.save()
            self.progressed.emit(100, "Публикация обработана")
            self.completed.emit(self.job, errors)
        except Exception:
            self.failed.emit(traceback.format_exc())


class YouTubeConnectWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path

    def run(self) -> None:
        try:
            self.completed.emit(connect_youtube(self.path))
        except Exception:
            self.failed.emit(traceback.format_exc())


class CredentialsDialog(QDialog):
    changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Подключения публикации")
        self.resize(650, 470)
        self.youtube_worker: YouTubeConnectWorker | None = None
        credentials = load_credentials()

        outer = QVBoxLayout(self)
        tabs = QTabWidget()
        outer.addWidget(tabs, 1)

        # TikTok
        tiktok = QWidget()
        tiktok_form = QFormLayout(tiktok)
        tiktok_data = credentials.get(Platform.TIKTOK.value) or {}
        self.tt_access = QLineEdit(str(tiktok_data.get("access_token") or ""))
        self.tt_access.setEchoMode(QLineEdit.EchoMode.Password)
        self.tt_refresh = QLineEdit(str(tiktok_data.get("refresh_token") or ""))
        self.tt_refresh.setEchoMode(QLineEdit.EchoMode.Password)
        self.tt_key = QLineEdit(str(tiktok_data.get("client_key") or ""))
        self.tt_secret = QLineEdit(str(tiktok_data.get("client_secret") or ""))
        self.tt_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.tt_privacy = QComboBox()
        self.tt_privacy.addItem("Публично", "PUBLIC_TO_EVERYONE")
        self.tt_privacy.addItem("Только я", "SELF_ONLY")
        index = self.tt_privacy.findData(str(tiktok_data.get("privacy_level") or "PUBLIC_TO_EVERYONE"))
        self.tt_privacy.setCurrentIndex(max(0, index))
        tt_note = QLabel(
            "Нужен TikTok Developer App со scope video.publish. До аудита TikTok может разрешать "
            "только приватные публикации."
        )
        tt_note.setWordWrap(True)
        tiktok_form.addRow("Access token", self.tt_access)
        tiktok_form.addRow("Refresh token", self.tt_refresh)
        tiktok_form.addRow("Client key", self.tt_key)
        tiktok_form.addRow("Client secret", self.tt_secret)
        tiktok_form.addRow("Видимость", self.tt_privacy)
        tiktok_form.addRow(tt_note)
        tabs.addTab(tiktok, "TikTok")

        # Instagram
        instagram = QWidget()
        instagram_form = QFormLayout(instagram)
        instagram_data = credentials.get(Platform.INSTAGRAM.value) or {}
        self.ig_access = QLineEdit(str(instagram_data.get("access_token") or ""))
        self.ig_access.setEchoMode(QLineEdit.EchoMode.Password)
        self.ig_user = QLineEdit(str(instagram_data.get("ig_user_id") or ""))
        self.ig_version = QLineEdit(str(instagram_data.get("api_version") or "v24.0"))
        self.ig_host = QComboBox()
        self.ig_host.addItem("Instagram Login", "graph.instagram.com")
        self.ig_host.addItem("Facebook Login", "graph.facebook.com")
        host_index = self.ig_host.findData(str(instagram_data.get("graph_host") or "graph.instagram.com"))
        self.ig_host.setCurrentIndex(max(0, host_index))
        ig_note = QLabel(
            "Публикация работает только для профессионального Instagram-аккаунта "
            "Business или Creator с разрешением на публикацию контента."
        )
        ig_note.setWordWrap(True)
        instagram_form.addRow("Access token", self.ig_access)
        instagram_form.addRow("IG User ID", self.ig_user)
        instagram_form.addRow("Graph API", self.ig_version)
        instagram_form.addRow("Тип входа", self.ig_host)
        instagram_form.addRow(ig_note)
        tabs.addTab(instagram, "Instagram")

        # YouTube
        youtube = QWidget()
        youtube_layout = QVBoxLayout(youtube)
        youtube_data = credentials.get(Platform.YOUTUBE.value) or {}
        youtube_form = QFormLayout()
        self.yt_path = QLineEdit(str(youtube_data.get("client_secret_path") or ""))
        choose_row = QHBoxLayout()
        choose_row.addWidget(self.yt_path, 1)
        choose = QPushButton("Выбрать JSON")
        choose.clicked.connect(self.choose_youtube_json)
        choose_row.addWidget(choose)
        youtube_form.addRow("OAuth client JSON", choose_row)
        youtube_layout.addLayout(youtube_form)
        self.yt_status = QLabel(
            "YouTube подключён" if platform_connected(Platform.YOUTUBE) else "YouTube не подключён"
        )
        youtube_layout.addWidget(self.yt_status)
        self.yt_connect = QPushButton("Подключить YouTube через браузер")
        self.yt_connect.clicked.connect(self.connect_youtube_clicked)
        youtube_layout.addWidget(self.yt_connect)
        yt_note = QLabel(
            "Создай Desktop OAuth Client в Google Cloud, скачай client_secret.json и выбери его здесь."
        )
        yt_note.setWordWrap(True)
        youtube_layout.addWidget(yt_note)
        youtube_layout.addStretch(1)
        tabs.addTab(youtube, "YouTube")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.save_and_accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def choose_youtube_json(self) -> None:
        value = QFileDialog.getOpenFileName(
            self,
            "Выбрать Google OAuth client JSON",
            self.yt_path.text() or str(Path.home() / "Downloads"),
            "JSON (*.json)",
        )[0]
        if value:
            self.yt_path.setText(value)

    def connect_youtube_clicked(self) -> None:
        path = Path(self.yt_path.text().strip())
        if not path.is_file():
            QMessageBox.warning(self, "Нет JSON", "Сначала выбери client_secret.json.")
            return
        self.yt_connect.setEnabled(False)
        self.yt_connect.setText("Ожидаю вход в браузере…")
        self.youtube_worker = YouTubeConnectWorker(path)
        self.youtube_worker.completed.connect(self.youtube_connected)
        self.youtube_worker.failed.connect(self.youtube_failed)
        self.youtube_worker.start()

    def youtube_connected(self, credentials: object) -> None:
        self.yt_connect.setEnabled(True)
        self.yt_connect.setText("Переподключить YouTube")
        self.yt_status.setText("YouTube подключён")
        self.changed.emit()

    def youtube_failed(self, error: str) -> None:
        self.yt_connect.setEnabled(True)
        self.yt_connect.setText("Подключить YouTube через браузер")
        QMessageBox.critical(self, "YouTube OAuth", error.splitlines()[-1])

    def save_and_accept(self) -> None:
        update_platform_credentials(
            Platform.TIKTOK.value,
            {
                "access_token": self.tt_access.text().strip(),
                "refresh_token": self.tt_refresh.text().strip(),
                "client_key": self.tt_key.text().strip(),
                "client_secret": self.tt_secret.text().strip(),
                "privacy_level": self.tt_privacy.currentData(),
                "expires_at": time.time() + 23 * 60 * 60,
            },
        )
        update_platform_credentials(
            Platform.INSTAGRAM.value,
            {
                "access_token": self.ig_access.text().strip(),
                "ig_user_id": self.ig_user.text().strip(),
                "api_version": self.ig_version.text().strip() or "v24.0",
                "graph_host": self.ig_host.currentData(),
            },
        )
        self.changed.emit()
        self.accept()


class PublishingWindow(IntegratedBatchWindow):
    def __init__(self) -> None:
        self.publish_queue = PublishQueue()
        self.publish_worker: PublishWorker | None = None
        self.publish_settings = QSettings("ARARA", "ARARA Factory")
        self._startup_overdue_pause_count = 0
        super().__init__()

        self.publish_frame = QFrame()
        self.publish_frame.setObjectName("settingsPanel")
        layout = QVBoxLayout(self.publish_frame)
        layout.setContentsMargins(16, 13, 16, 13)
        layout.setSpacing(9)

        title_row = QHBoxLayout()
        title = QLabel("4. ПУБЛИКАЦИЯ")
        title.setObjectName("cardTitle")
        self.connections_button = QPushButton("Подключения")
        self.connections_button.setMaximumWidth(125)
        self.connections_button.clicked.connect(self.open_connections)
        title_row.addWidget(title)
        title_row.addStretch(1)
        title_row.addWidget(self.connections_button)
        layout.addLayout(title_row)

        platforms = QGridLayout()
        self.platform_boxes: dict[Platform, QCheckBox] = {}
        self.platform_labels: dict[Platform, QLabel] = {}
        for column, platform in enumerate(Platform):
            box = QCheckBox(PLATFORM_LABELS[platform])
            box.setChecked(
                self.publish_settings.value(f"publish_{platform.value}", True, type=bool)
            )
            box.toggled.connect(self.save_publish_preferences)
            state = QLabel()
            self.platform_boxes[platform] = box
            self.platform_labels[platform] = state
            platforms.addWidget(box, 0, column)
            platforms.addWidget(state, 1, column)
        layout.addLayout(platforms)

        self.caption = QTextEdit()
        self.caption.setMaximumHeight(78)
        self.caption.setPlaceholderText("Подпись и хэштеги для трёх платформ")
        self.caption.setPlainText(
            str(
                self.publish_settings.value(
                    "publish_caption",
                    "ARARA RARA RARARA\n#arara #gaming #shorts",
                )
            )
        )
        self.caption.textChanged.connect(self.save_publish_preferences)
        layout.addWidget(self.caption)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Интервал:"))
        self.interval = QComboBox()
        for minutes in (15, 30, 60):
            self.interval.addItem(f"{minutes} минут", minutes)
        saved_interval = int(self.publish_settings.value("publish_interval", 60) or 60)
        index = self.interval.findData(saved_interval)
        self.interval.setCurrentIndex(index if index >= 0 else 2)
        self.interval.currentIndexChanged.connect(self.save_publish_preferences)
        self.auto_queue = QCheckBox("Добавлять новые Reels в очередь")
        self.auto_queue.setChecked(
            self.publish_settings.value("publish_auto_queue", True, type=bool)
        )
        self.auto_queue.toggled.connect(self.save_publish_preferences)
        controls.addWidget(self.interval)
        controls.addWidget(self.auto_queue)
        controls.addStretch(1)
        layout.addLayout(controls)

        actions = QHBoxLayout()
        self.add_ready_button = QPushButton("Добавить готовые")
        self.add_ready_button.clicked.connect(self.enqueue_output_folder)
        self.publish_toggle = QPushButton("ЗАПУСТИТЬ ОЧЕРЕДЬ")
        self.publish_toggle.setObjectName("generate")
        self.publish_toggle.clicked.connect(self.toggle_publish_queue)
        self.retry_button = QPushButton("Повторить 1 ошибку")
        self.retry_button.clicked.connect(self.retry_failed)
        actions.addWidget(self.add_ready_button)
        actions.addWidget(self.publish_toggle)
        actions.addWidget(self.retry_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.publish_status = QLabel()
        self.publish_status.setObjectName("libraryStatus")
        self.publish_status.setWordWrap(True)
        layout.addWidget(self.publish_status)

        main_layout = self.progress.parentWidget().layout()
        progress_index = main_layout.indexOf(self.progress)
        main_layout.insertWidget(progress_index, self.publish_frame)

        self.publish_timer = QTimer(self)
        self.publish_timer.setInterval(30_000)
        self.publish_timer.timeout.connect(self.process_publish_queue)
        saved_active = self.publish_settings.value(
            "publish_queue_active", False, type=bool
        )
        self._startup_overdue_pause_count = startup_autopause_count(
            self.publish_queue,
            saved_active=saved_active,
        )
        if self._startup_overdue_pause_count:
            self.publish_settings.setValue("publish_queue_active", False)
            self.publish_settings.sync()
        elif saved_active:
            self.publish_timer.start()
        self.refresh_connections()
        self.refresh_publish_status()
        if self._startup_overdue_pause_count:
            self.status.setText(
                "Автозапуск очереди приостановлен: просрочено "
                f"{self._startup_overdue_pause_count} заданий. "
                "Проверь очередь и нажми «Запустить очередь»."
            )
        QTimer.singleShot(1500, self.process_publish_queue)

    def selected_platforms(self) -> list[Platform]:
        return [platform for platform, box in self.platform_boxes.items() if box.isChecked()]

    def save_publish_preferences(self, *args) -> None:
        for platform, box in self.platform_boxes.items():
            self.publish_settings.setValue(f"publish_{platform.value}", box.isChecked())
        self.publish_settings.setValue("publish_caption", self.caption.toPlainText())
        self.publish_settings.setValue("publish_interval", self.interval.currentData())
        self.publish_settings.setValue("publish_auto_queue", self.auto_queue.isChecked())
        self.publish_settings.sync()

    def refresh_connections(self) -> None:
        for platform, label in self.platform_labels.items():
            connected = platform_connected(platform)
            label.setText("● подключён" if connected else "○ не подключён")
            label.setStyleSheet("color: #77d57a" if connected else "color: #8f857a")

    def open_connections(self) -> None:
        dialog = CredentialsDialog(self)
        dialog.changed.connect(self.refresh_connections)
        dialog.exec()
        self.refresh_connections()

    def enqueue_files(self, files: list[str | Path]) -> int:
        platforms = self.selected_platforms()
        if not platforms:
            return 0
        paths = [Path(item) for item in files if Path(item).is_file()]
        added = self.publish_queue.enqueue(
            paths,
            platforms,
            self.caption.toPlainText(),
            int(self.interval.currentData()),
        )
        self.refresh_publish_status()
        return len(added)

    def enqueue_output_folder(self) -> None:
        output = Path(self.output_picker.path)
        files = sorted(
            (
                path
                for path in output.glob("*.mp4")
                if ".part." not in path.name and "_preview_" not in path.name
            ),
            key=lambda path: path.stat().st_mtime,
        )
        added = self.enqueue_files(files)
        self.status.setText(f"Добавлено в очередь: {added}")

    def toggle_publish_queue(self) -> None:
        active = self.publish_timer.isActive()
        if active:
            self.publish_timer.stop()
            self.publish_settings.setValue("publish_queue_active", False)
        else:
            disconnected = [
                PLATFORM_LABELS[platform]
                for platform in self.selected_platforms()
                if not platform_connected(platform)
            ]
            if disconnected:
                QMessageBox.warning(
                    self,
                    "Не все платформы подключены",
                    "Открой «Подключения»: " + ", ".join(disconnected),
                )
                return
            if not self.selected_platforms():
                QMessageBox.warning(self, "Нет платформ", "Отметь хотя бы одну платформу.")
                return
            spread = spread_overdue_runnable_jobs(
                self.publish_queue,
                interval_minutes=int(self.interval.currentData() or 60),
            )
            self._startup_overdue_pause_count = 0
            self.publish_timer.start()
            self.publish_settings.setValue("publish_queue_active", True)
            if spread > 1:
                self.status.setText(
                    f"Очередь запущена · {spread} просроченных заданий "
                    "разнесены по выбранному интервалу"
                )
            self.process_publish_queue()
        self.publish_settings.sync()
        self.refresh_publish_status()

    def process_publish_queue(self) -> None:
        if not self.publish_timer.isActive():
            return
        if self.publish_worker and self.publish_worker.isRunning():
            return
        if self.batch_worker and self.batch_worker.isRunning():
            return
        if self.render_worker and self.render_worker.isRunning():
            return
        job = self.publish_queue.next_due()
        if job is None:
            self.refresh_publish_status()
            return
        self.publish_worker = PublishWorker(self.publish_queue, job)
        self.publish_worker.progressed.connect(self.on_publish_progress)
        self.publish_worker.completed.connect(self.publish_done)
        self.publish_worker.failed.connect(self.publish_failed)
        self.publish_worker.start()
        self.refresh_publish_status()

    def on_publish_progress(self, value: int, text: str) -> None:
        self.progress.setValue(value)
        self.status.setText("Публикация · " + text)

    def publish_done(self, job: PublishJob, errors: dict[str, str]) -> None:
        self.publish_worker = None
        if errors:
            names = ", ".join(PLATFORM_LABELS[Platform(name)] for name in errors)
            self.status.setText(f"Часть платформ не приняла Reel: {names} · повтор через 10 минут")
        else:
            self.status.setText(f"Опубликовано на выбранных платформах: {Path(job.video).name}")
        self.refresh_publish_status()
        QTimer.singleShot(1000, self.process_publish_queue)

    def publish_failed(self, error: str) -> None:
        self.publish_worker = None
        self.status.setText("Ошибка очереди публикации")
        self.log.setPlainText(error)
        self.log.setVisible(True)
        self.refresh_publish_status()

    def retry_failed(self) -> None:
        selected = {platform.value for platform in self.selected_platforms()}
        retried = retry_one_failed_delivery(
            self.publish_queue,
            platform_names=selected,
        )
        if retried is None:
            self.status.setText("Нет ошибок для повтора на выбранных платформах")
        else:
            job, name = retried
            try:
                label = PLATFORM_LABELS[Platform(name)]
            except ValueError:
                label = name
            self.status.setText(
                f"Повторяется 1 ошибка: {Path(job.video).name} · {label}"
            )
        self.refresh_publish_status()
        self.process_publish_queue()

    def refresh_publish_status(self) -> None:
        active = self.publish_timer.isActive()
        self.publish_toggle.setText("ОСТАНОВИТЬ ОЧЕРЕДЬ" if active else "ЗАПУСТИТЬ ОЧЕРЕДЬ")
        next_job = self.publish_queue.next_scheduled()
        if next_job is None:
            next_text = "очередь пуста"
        else:
            wait = max(0, int(next_job.due_at - time.time()))
            minutes = (wait + 59) // 60
            next_text = f"следующий через {minutes} мин · {Path(next_job.video).name}"
        self.publish_status.setText(
            f"Осталось {self.publish_queue.remaining} · успешно {self.publish_queue.completed} · {next_text}"
        )
        if self._startup_overdue_pause_count:
            self.publish_status.setText(
                self.publish_status.text()
                + " · автозапуск на паузе: просрочено "
                + str(self._startup_overdue_pause_count)
            )

    def render_done(self, files: list[str]) -> None:
        preview = self.last_render_was_preview
        super().render_done(files)
        if files and not preview and self.auto_queue.isChecked():
            added = self.enqueue_files(files)
            if added:
                self.status.setText(self.status.text() + " · добавлен в публикацию")

    def batch_done(self, files: list[str], plan) -> None:
        super().batch_done(files, plan)
        if files and self.auto_queue.isChecked():
            added = self.enqueue_files(files)
            if added:
                self.status.setText(self.status.text() + f" · в очереди публикации {added}")

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.publish_worker and self.publish_worker.isRunning():
            QMessageBox.information(
                self,
                "Идёт публикация",
                "Дождись завершения текущей загрузки. Результаты платформ сохраняются отдельно.",
            )
            event.ignore()
            return
        self.save_publish_preferences()
        super().closeEvent(event)


def install(app_module) -> None:
    app_module.MainWindow = PublishingWindow
