from __future__ import annotations

from PySide6.QtWidgets import QMessageBox

from .process_utils import set_system_awake
from .publishing_oauth_ui import PublishingOAuthWindow


class PublishingRuntimeWindow(PublishingOAuthWindow):
    def __init__(self) -> None:
        super().__init__()
        set_system_awake(self.publish_timer.isActive(), keep_display_on=False)

    def toggle_publish_queue(self) -> None:
        super().toggle_publish_queue()
        set_system_awake(self.publish_timer.isActive(), keep_display_on=False)

    def closeEvent(self, event) -> None:  # noqa: N802
        if (
            self.publish_timer.isActive()
            and self.publish_queue.remaining > 0
            and not (self.publish_worker and self.publish_worker.isRunning())
        ):
            answer = QMessageBox.question(
                self,
                "Очередь публикации включена",
                (
                    "После закрытия расписание остановится. Очередь сохранится и продолжится "
                    "после следующего запуска Verticliv. Закрыть программу?"
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        super().closeEvent(event)
        if event.isAccepted():
            set_system_awake(False)


def install(app_module) -> None:
    app_module.MainWindow = PublishingRuntimeWindow
