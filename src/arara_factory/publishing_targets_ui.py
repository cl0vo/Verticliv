from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QCheckBox, QFrame, QGridLayout, QLabel, QVBoxLayout

from .publishing import PLATFORM_LABELS, Platform, PublishJob, platform_connected
from .smart_ui import SmartMainWindow


class TargetAwareSmartWindow(SmartMainWindow):
    """Publishing UX with target defaults and a non-destructive active filter."""

    def __init__(self) -> None:
        self.schedule_platform_boxes: dict[Platform, QCheckBox] = {}
        self.schedule_platform_states: dict[Platform, QLabel] = {}
        super().__init__()
        self._install_schedule_targets()
        self._sync_schedule_platforms()
        self._auto_select_single_connected_platform()
        self._sync_publish_workflow()
        self.refresh_publish_status()

    def _install_schedule_targets(self) -> None:
        page = self.publish_stack.widget(3)
        page_layout = page.layout()

        card = QFrame()
        card.setObjectName("largeCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 15, 18, 15)
        layout.setSpacing(9)

        title = QLabel("КУДА ПУБЛИКОВАТЬ")
        title.setObjectName("summaryTitle")
        hint = QLabel(
            "Галочки задают платформы для новых пачек и включают их очередь. "
            "Снятая галочка приостановит публикацию, но сохранит старые задания. "
            "Верни галочку, чтобы продолжить."
        )
        hint.setObjectName("mutedText")
        hint.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(hint)

        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(5)
        for column, platform in enumerate(Platform):
            box = QCheckBox(PLATFORM_LABELS[platform])
            box.setMinimumWidth(170)
            state = QLabel()
            state.setObjectName("connectionState")
            self.schedule_platform_boxes[platform] = box
            self.schedule_platform_states[platform] = state
            grid.addWidget(box, 0, column)
            grid.addWidget(state, 1, column)
            grid.setColumnStretch(column, 1)

            box.toggled.connect(
                lambda checked, p=platform: self._schedule_target_toggled(p, checked)
            )
            self.platform_boxes[platform].toggled.connect(
                lambda checked, p=platform: self._primary_target_toggled(p, checked)
            )

        layout.addLayout(grid)
        page_layout.insertWidget(2, card)
        self.schedule_targets_card = card

    def _auto_select_single_connected_platform(self) -> None:
        connected = [platform for platform in Platform if platform_connected(platform)]
        selected_connected = [
            platform
            for platform in connected
            if self.platform_boxes[platform].isChecked()
        ]
        if len(connected) == 1 and not selected_connected:
            self.platform_boxes[connected[0]].setChecked(True)

    def _schedule_target_toggled(self, platform: Platform, checked: bool) -> None:
        primary = self.platform_boxes[platform]
        if primary.isChecked() != checked:
            primary.setChecked(checked)
        self._sync_publish_workflow()

    def _primary_target_toggled(self, platform: Platform, checked: bool) -> None:
        mirror = self.schedule_platform_boxes.get(platform)
        if mirror is not None and mirror.isChecked() != checked:
            mirror.setChecked(checked)
        self._sync_publish_workflow()

    def _sync_schedule_platforms(self) -> None:
        if not self.schedule_platform_boxes:
            return
        for platform in Platform:
            connected = platform_connected(platform)
            primary = self.platform_boxes[platform]
            mirror = self.schedule_platform_boxes[platform]
            state = self.schedule_platform_states[platform]

            if not connected:
                if primary.isChecked():
                    primary.setChecked(False)
                if mirror.isChecked():
                    mirror.setChecked(False)
            else:
                if mirror.isChecked() != primary.isChecked():
                    mirror.setChecked(primary.isChecked())

            mirror.setEnabled(connected)
            state.setText("● подключено" if connected else "○ не подключено")
            state.setStyleSheet("color: #6fcf8b" if connected else "color: #7f8994")
            mirror.setToolTip(
                "Будет публиковаться в эту платформу"
                if connected
                else "Сначала подключи аккаунт"
            )

    def refresh_connections(self) -> None:
        super().refresh_connections()
        if not hasattr(self, "schedule_platform_boxes"):
            return
        self._sync_schedule_platforms()
        self._auto_select_single_connected_platform()
        self._sync_publish_workflow()

    def publish_done(self, job: PublishJob, errors: dict[str, str]) -> None:
        self.publish_worker = None
        selected_names = {platform.value for platform in self.selected_platforms()}
        visible_errors = {
            name: message
            for name, message in errors.items()
            if name in selected_names
        }

        if visible_errors:
            labels = [PLATFORM_LABELS[Platform(name)] for name in visible_errors]
            if len(labels) == 1:
                self.status.setText(
                    f"{labels[0]} не принял Reel · повтор через 10 минут"
                )
            else:
                self.status.setText(
                    "Не приняли Reel: " + ", ".join(labels) + " · повтор через 10 минут"
                )
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
        QTimer.singleShot(1000, self.process_publish_queue)

    def refresh_publish_status(self) -> None:
        super().refresh_publish_status()
        if not hasattr(self, "schedule_platform_boxes"):
            return
        self._sync_schedule_platforms()
        selected = [PLATFORM_LABELS[p] for p in self.selected_platforms()]
        if hasattr(self, "publish_summary") and selected:
            self.publish_summary.setToolTip(
                "Активные платформы: " + ", ".join(selected)
            )


def install(app_module) -> None:
    app_module.MainWindow = TargetAwareSmartWindow
