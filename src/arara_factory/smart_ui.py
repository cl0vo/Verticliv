from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .publishing import PLATFORM_LABELS, Platform, platform_connected
from .publishing_library_ui import PublishingLibraryWindow
from .version import __version__


class SmartMainWindow(PublishingLibraryWindow):
    """User-facing shell built around two simple step-by-step jobs."""

    CREATE_STEPS = ("Исходник", "Brainrot", "Порция", "Проверка")
    PUBLISH_STEPS = ("Reels", "Платформы", "Подпись", "Расписание")

    def __init__(self) -> None:
        super().__init__()
        self._create_step = 0
        self._publish_step = 0
        self.create_step_buttons: list[QPushButton] = []
        self.publish_step_buttons: list[QPushButton] = []
        self.create_next_buttons: list[QPushButton] = []
        self.publish_next_buttons: list[QPushButton] = []

        self.setWindowTitle(f"Verticliv {__version__}")
        self.resize(1380, 900)
        self.setMinimumSize(1080, 720)

        self._rebuild_shell()
        self._wire_smart_state()
        self._apply_copy()
        self._apply_style()
        self._sync_create_workflow()
        self._sync_publish_workflow()
        self.refresh_connections()
        self.refresh_publish_status()

    # ------------------------------------------------------------------
    # Shell
    # ------------------------------------------------------------------
    def _rebuild_shell(self) -> None:
        root = self.centralWidget()
        root_layout = root.layout()
        splitter = self.splitter
        left_scroll = splitter.widget(0)
        old_left = left_scroll.takeWidget()

        activity = self._build_activity_bar()
        publish_page = self._build_publish_workflow()
        creator = self._build_create_workflow()

        left_scroll.setWidget(creator)
        left_scroll.setMinimumWidth(560)
        left_scroll.setMaximumWidth(700)

        # Keep engine-only controls alive without exposing them to the main flow.
        technical_host = QWidget(creator)
        technical_host.hide()
        hidden_layout = QVBoxLayout(technical_host)
        hidden_layout.setContentsMargins(0, 0, 0, 0)
        for widget in (
            self.prepare_button,
            self.reset_button,
            self.next_button,
            self.add_ready_button,
        ):
            widget.hide()
            hidden_layout.addWidget(widget)

        if old_left is not None:
            old_left.deleteLater()

        root_layout.removeWidget(splitter)
        self.mode_tabs = QTabWidget()
        self.mode_tabs.setObjectName("modeTabs")
        self.mode_tabs.setDocumentMode(True)
        self.mode_tabs.setMovable(False)
        self.mode_tabs.addTab(splitter, "Создать Reels")
        self.mode_tabs.addTab(publish_page, "Опубликовать")
        saved_tab = int(self.settings.value("smart_tab", 0) or 0)
        self.mode_tabs.setCurrentIndex(1 if saved_tab == 1 else 0)
        self.mode_tabs.currentChanged.connect(self._mode_changed)

        root_layout.insertWidget(1, self.mode_tabs, 1)
        root_layout.addWidget(activity)

        splitter.setSizes([650, 540])
        self.preview_panel.setMinimumWidth(350)
        self.preview_panel.setMaximumWidth(540)
        self.preview_panel.title.setText("СЦЕНА")
        self.preview_panel.source_label.setText("brainrot можно двигать и растягивать мышью")

    def _build_activity_bar(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("activityBar")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(6)

        top = QHBoxLayout()
        self.status.setObjectName("globalStatus")
        top.addWidget(self.status, 1)
        self.log_button.setText("Технический журнал")
        self.log_button.setMaximumWidth(170)
        top.addWidget(self.log_button)
        layout.addLayout(top)
        layout.addWidget(self.progress)
        layout.addWidget(self.log)
        self.log.setMaximumHeight(120)
        return frame

    def _workflow_header(self, title: str, hint: str) -> QFrame:
        frame = QFrame()
        frame.setObjectName("workflowHeader")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(18, 15, 18, 15)
        layout.setSpacing(4)
        title_label = QLabel(title)
        title_label.setObjectName("workflowTitle")
        hint_label = QLabel(hint)
        hint_label.setObjectName("workflowHint")
        hint_label.setWordWrap(True)
        layout.addWidget(title_label)
        layout.addWidget(hint_label)
        return frame

    def _step_bar(self, labels: tuple[str, ...], kind: str) -> QWidget:
        host = QWidget()
        host.setObjectName("stepBar")
        layout = QHBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(7)
        buttons: list[QPushButton] = []
        for index, label in enumerate(labels):
            button = QPushButton(f"{index + 1}. {label}")
            button.setObjectName("stepButton")
            button.setProperty("stepState", "future")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            if kind == "create":
                button.clicked.connect(
                    lambda checked=False, idx=index: self._set_create_step(idx)
                )
            else:
                button.clicked.connect(
                    lambda checked=False, idx=index: self._set_publish_step(idx)
                )
            buttons.append(button)
            layout.addWidget(button, 1)
        if kind == "create":
            self.create_step_buttons = buttons
        else:
            self.publish_step_buttons = buttons
        return host

    def _page(self, title: str, hint: str) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        page.setObjectName("stepPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(2, 4, 2, 4)
        layout.setSpacing(12)

        heading = QLabel(title)
        heading.setObjectName("stepTitle")
        sub = QLabel(hint)
        sub.setObjectName("stepHint")
        sub.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(sub)
        return page, layout

    def _nav_row(
        self,
        *,
        kind: str,
        step: int,
        back: bool = True,
        next_text: str = "Дальше",
    ) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(9)
        if back:
            back_button = QPushButton("Назад")
            back_button.setObjectName("secondaryButton")
            back_button.setMinimumWidth(100)
            back_button.setMaximumWidth(120)
            if kind == "create":
                back_button.clicked.connect(
                    lambda checked=False, idx=step - 1: self._set_create_step(idx)
                )
            else:
                back_button.clicked.connect(
                    lambda checked=False, idx=step - 1: self._set_publish_step(idx)
                )
            row.addWidget(back_button)

        next_button = QPushButton(next_text)
        next_button.setObjectName("nextButton")
        next_button.setMinimumWidth(130)
        next_button.setMaximumWidth(180)
        if kind == "create":
            next_button.clicked.connect(
                lambda checked=False, idx=step + 1: self._set_create_step(idx)
            )
            self.create_next_buttons.append(next_button)
        else:
            next_button.clicked.connect(
                lambda checked=False, idx=step + 1: self._set_publish_step(idx)
            )
            self.publish_next_buttons.append(next_button)
        row.addStretch(1)
        row.addWidget(next_button)
        return row

    # ------------------------------------------------------------------
    # Create workflow
    # ------------------------------------------------------------------
    def _build_create_workflow(self) -> QWidget:
        creator = QWidget()
        creator.setObjectName("createWorkflow")
        main = QVBoxLayout(creator)
        main.setContentsMargins(8, 6, 18, 14)
        main.setSpacing(12)

        main.addWidget(
            self._workflow_header(
                "СОЗДАТЬ REELS",
                "Четыре коротких шага. Выбираешь два видео, задаёшь порцию и запускаешь сборку.",
            )
        )
        main.addWidget(self._step_bar(self.CREATE_STEPS, "create"))

        self.create_stack = QStackedWidget()
        self.create_stack.setObjectName("workflowStack")
        main.addWidget(self.create_stack, 1)

        # Step 1: source
        page, layout = self._page(
            "Выбери запись ARARA",
            "Подойдёт один готовый Reel или длинная вертикальная запись. Длинное видео программа сама разрежет на 9–15 секунд.",
        )
        layout.addWidget(self.reel_card)
        self.source_help = QLabel("После выбора программа сама проверит формат, звук и длительность.")
        self.source_help.setObjectName("infoNote")
        self.source_help.setWordWrap(True)
        layout.addWidget(self.source_help)
        layout.addStretch(1)
        layout.addLayout(self._nav_row(kind="create", step=0, back=False))
        self.create_stack.addWidget(page)

        # Step 2: brainrot
        page, layout = self._page(
            "Выбери brainrot",
            "Один длинный файл используется для всей порции. Свежие участки выбираются автоматически без повторов.",
        )
        layout.addWidget(self.brainrot_card)
        self.library_status.setObjectName("infoNote")
        layout.addWidget(self.library_status)
        scene_note = QLabel(
            "Справа уже находится редактор сцены: перетащи brainrot мышью или потяни за углы. Эти координаты применятся ко всем роликам."
        )
        scene_note.setObjectName("accentNote")
        scene_note.setWordWrap(True)
        layout.addWidget(scene_note)
        layout.addStretch(1)
        layout.addLayout(self._nav_row(kind="create", step=1))
        self.create_stack.addWidget(page)

        # Step 3: batch / output
        page, layout = self._page(
            "Настрой результат",
            "Для длинной записи выбери размер порции. Для короткой будет создан один Reel.",
        )
        self.single_mode_info = QFrame()
        self.single_mode_info.setObjectName("summaryCard")
        single_layout = QVBoxLayout(self.single_mode_info)
        single_layout.setContentsMargins(14, 12, 14, 12)
        single_title = QLabel("ОДИН REEL")
        single_title.setObjectName("summaryTitle")
        single_text = QLabel("Исходник короткий — будет создан один готовый ролик.")
        single_text.setObjectName("summaryText")
        single_layout.addWidget(single_title)
        single_layout.addWidget(single_text)
        layout.addWidget(self.single_mode_info)
        layout.addWidget(self.batch_frame)

        output_card = QFrame()
        output_card.setObjectName("summaryCard")
        output_layout = QVBoxLayout(output_card)
        output_layout.setContentsMargins(14, 12, 14, 12)
        output_title = QLabel("ПАПКА РЕЗУЛЬТАТА")
        output_title.setObjectName("summaryTitle")
        self.output_summary = QLabel()
        self.output_summary.setObjectName("summaryText")
        self.output_summary.setWordWrap(True)
        choose_output = QPushButton("Изменить папку")
        choose_output.setObjectName("secondaryButton")
        choose_output.setMaximumWidth(150)
        choose_output.clicked.connect(self.output_picker.choose)
        output_layout.addWidget(output_title)
        output_layout.addWidget(self.output_summary)
        output_layout.addWidget(choose_output, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(output_card)
        layout.addStretch(1)
        layout.addLayout(self._nav_row(kind="create", step=2))
        self.create_stack.addWidget(page)

        # Step 4: review and run
        page, layout = self._page(
            "Проверь и запускай",
            "Сцена видна справа. Можно сделать короткое превью или сразу собрать выбранную порцию.",
        )
        summary = QFrame()
        summary.setObjectName("summaryCardStrong")
        summary_layout = QVBoxLayout(summary)
        summary_layout.setContentsMargins(16, 13, 16, 13)
        summary_title = QLabel("ПЕРЕД ЗАПУСКОМ")
        summary_title.setObjectName("summaryTitle")
        self.create_summary = QLabel()
        self.create_summary.setObjectName("summaryText")
        self.create_summary.setWordWrap(True)
        summary_layout.addWidget(summary_title)
        summary_layout.addWidget(self.create_summary)
        layout.addWidget(summary)

        action_row = QHBoxLayout()
        action_row.setSpacing(10)
        self.preview_button.setText("Превью 5 сек")
        self.preview_button.setObjectName("secondaryAction")
        self.preview_button.setMinimumWidth(150)
        self.preview_button.setMaximumWidth(170)
        self.render_button.setObjectName("primaryAction")
        self.render_button.setMinimumWidth(230)
        self.render_button.setMaximumWidth(340)
        action_row.addWidget(self.preview_button)
        action_row.addWidget(self.render_button)
        action_row.addStretch(1)
        layout.addLayout(action_row)

        utility = QHBoxLayout()
        self.settings_button.setText("Доп. настройки")
        self.settings_button.setMaximumWidth(150)
        self.open_button.setText("Открыть папку")
        self.open_button.setMaximumWidth(140)
        utility.addWidget(self.settings_button)
        utility.addWidget(self.open_button)
        utility.addStretch(1)
        layout.addLayout(utility)
        layout.addWidget(self.settings_panel)
        layout.addStretch(1)

        back_row = QHBoxLayout()
        back = QPushButton("Назад")
        back.setObjectName("secondaryButton")
        back.setMaximumWidth(120)
        back.clicked.connect(lambda: self._set_create_step(2))
        back_row.addWidget(back)
        back_row.addStretch(1)
        layout.addLayout(back_row)
        self.create_stack.addWidget(page)

        return creator

    # ------------------------------------------------------------------
    # Publish workflow
    # ------------------------------------------------------------------
    def _build_publish_workflow(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setObjectName("publishScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        page = QWidget()
        page.setObjectName("publishWorkflow")
        outer = QVBoxLayout(page)
        outer.setContentsMargins(28, 18, 28, 24)
        outer.setSpacing(12)

        content = QWidget()
        content.setObjectName("publishContent")
        content.setMaximumWidth(940)
        content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(12)

        content_layout.addWidget(
            self._workflow_header(
                "ОПУБЛИКОВАТЬ",
                "Выбери пачку готовых Reels, аккаунты и расписание. Программа сама ведёт очередь и защищает от дублей.",
            )
        )
        content_layout.addWidget(self._step_bar(self.PUBLISH_STEPS, "publish"))

        self.publish_stack = QStackedWidget()
        self.publish_stack.setObjectName("workflowStack")
        content_layout.addWidget(self.publish_stack, 1)

        # Step 1: files
        step, layout = self._page(
            "Выбери готовые Reels",
            "Можно выделить много файлов в Проводнике или выбрать целую папку.",
        )
        file_card = QFrame()
        file_card.setObjectName("largeCard")
        file_layout = QVBoxLayout(file_card)
        file_layout.setContentsMargins(18, 16, 18, 16)
        file_layout.setSpacing(12)

        buttons = QHBoxLayout()
        self.choose_reels_button.setText("Выбрать файлы")
        self.choose_reels_button.setObjectName("primarySoft")
        self.choose_reels_button.setMinimumWidth(150)
        self.choose_reels_button.setMaximumWidth(180)
        self.choose_folder_button.setText("Выбрать папку")
        self.choose_folder_button.setObjectName("secondaryButton")
        self.choose_folder_button.setMinimumWidth(150)
        self.choose_folder_button.setMaximumWidth(180)
        self.clear_selection_button.setText("Очистить")
        self.clear_selection_button.setObjectName("linkButton")
        self.clear_selection_button.setMaximumWidth(90)
        buttons.addWidget(self.choose_reels_button)
        buttons.addWidget(self.choose_folder_button)
        buttons.addWidget(self.clear_selection_button)
        buttons.addStretch(1)
        file_layout.addLayout(buttons)

        options = QHBoxLayout()
        options.addWidget(QLabel("Порядок"))
        self.order_combo.setMaximumWidth(190)
        options.addWidget(self.order_combo)
        options.addWidget(self.recursive_box)
        options.addStretch(1)
        file_layout.addLayout(options)
        self.selection_status.setObjectName("selectionStatus")
        file_layout.addWidget(self.selection_status)
        layout.addWidget(file_card)
        layout.addStretch(1)
        layout.addLayout(self._nav_row(kind="publish", step=0, back=False))
        self.publish_stack.addWidget(step)

        # Step 2: platforms
        step, layout = self._page(
            "Выбери платформы",
            "Отмечаются только подключённые аккаунты. Можно спокойно использовать один YouTube, пока Meta не подключена.",
        )
        platform_card = QFrame()
        platform_card.setObjectName("largeCard")
        platform_layout = QVBoxLayout(platform_card)
        platform_layout.setContentsMargins(18, 16, 18, 16)
        platform_layout.setSpacing(13)

        top = QHBoxLayout()
        top.addWidget(QLabel("Подключённые аккаунты"))
        top.addStretch(1)
        self.connections_button.setText("Подключения")
        self.connections_button.setObjectName("secondaryButton")
        self.connections_button.setMaximumWidth(140)
        top.addWidget(self.connections_button)
        platform_layout.addLayout(top)

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(6)
        for column, platform in enumerate(Platform):
            box = self.platform_boxes[platform]
            state = self.platform_labels[platform]
            box.setText(PLATFORM_LABELS[platform])
            box.setMinimumWidth(160)
            state.setObjectName("connectionState")
            grid.addWidget(box, 0, column)
            grid.addWidget(state, 1, column)
            grid.setColumnStretch(column, 1)
        platform_layout.addLayout(grid)
        layout.addWidget(platform_card)
        layout.addStretch(1)
        layout.addLayout(self._nav_row(kind="publish", step=1))
        self.publish_stack.addWidget(step)

        # Step 3: caption
        step, layout = self._page(
            "Подпись и хэштеги",
            "Одна подпись применяется ко всей выбранной пачке. Можно использовать {n}, {filename} и {file}.",
        )
        caption_card = QFrame()
        caption_card.setObjectName("largeCard")
        caption_layout = QVBoxLayout(caption_card)
        caption_layout.setContentsMargins(18, 16, 18, 16)
        caption_layout.setSpacing(9)
        caption_label = QLabel("Текст публикации")
        caption_label.setObjectName("fieldLabel")
        self.caption.setMinimumHeight(140)
        self.caption.setMaximumHeight(190)
        self.caption.setPlaceholderText("ARARA RARA RARARA\n#arara #gaming #shorts")
        caption_layout.addWidget(caption_label)
        caption_layout.addWidget(self.caption)
        caption_help = QLabel("Оставь пустым, если подпись не нужна.")
        caption_help.setObjectName("mutedText")
        caption_layout.addWidget(caption_help)
        layout.addWidget(caption_card)
        layout.addStretch(1)
        layout.addLayout(self._nav_row(kind="publish", step=2))
        self.publish_stack.addWidget(step)

        # Step 4: schedule + queue
        step, layout = self._page(
            "Настрой расписание",
            "Задержка относится к первому ролику. Остальные выходят через выбранный интервал.",
        )
        schedule_card = QFrame()
        schedule_card.setObjectName("largeCard")
        schedule_layout = QVBoxLayout(schedule_card)
        schedule_layout.setContentsMargins(18, 16, 18, 16)
        schedule_layout.setSpacing(12)

        schedule_grid = QGridLayout()
        schedule_grid.setHorizontalSpacing(16)
        schedule_grid.setVerticalSpacing(7)
        schedule_grid.addWidget(QLabel("Первый пост через"), 0, 0)
        self.delay_minutes.setMinimumWidth(130)
        self.delay_minutes.setMaximumWidth(150)
        schedule_grid.addWidget(self.delay_minutes, 1, 0)
        schedule_grid.addWidget(QLabel("Интервал между Reels"), 0, 1)
        self.interval.setMinimumWidth(130)
        self.interval.setMaximumWidth(150)
        schedule_grid.addWidget(self.interval, 1, 1)
        schedule_grid.setColumnStretch(2, 1)
        schedule_layout.addLayout(schedule_grid)

        self.auto_queue.setText("Новые Reels после рендера автоматически добавлять в очередь")
        schedule_layout.addWidget(self.auto_queue)

        self.publish_summary = QLabel()
        self.publish_summary.setObjectName("summaryTextLarge")
        self.publish_summary.setWordWrap(True)
        schedule_layout.addWidget(self.publish_summary)

        self.schedule_selected_button.setText("Добавить в очередь")
        self.schedule_selected_button.setObjectName("primaryAction")
        self.schedule_selected_button.setMinimumWidth(200)
        self.schedule_selected_button.setMaximumWidth(230)
        schedule_layout.addWidget(
            self.schedule_selected_button,
            alignment=Qt.AlignmentFlag.AlignLeft,
        )
        layout.addWidget(schedule_card)

        queue_card = QFrame()
        queue_card.setObjectName("queueCard")
        queue_layout = QVBoxLayout(queue_card)
        queue_layout.setContentsMargins(16, 13, 16, 13)
        queue_layout.setSpacing(9)
        queue_head = QHBoxLayout()
        queue_title = QLabel("ОЧЕРЕДЬ")
        queue_title.setObjectName("summaryTitle")
        queue_head.addWidget(queue_title)
        queue_head.addStretch(1)
        self.publish_toggle.setMinimumWidth(165)
        self.publish_toggle.setMaximumWidth(200)
        self.retry_button.setText("Повторить 1 ошибку")
        self.retry_button.setMaximumWidth(150)
        queue_head.addWidget(self.publish_toggle)
        queue_head.addWidget(self.retry_button)
        queue_layout.addLayout(queue_head)
        queue_layout.addWidget(self.publish_status)
        layout.addWidget(queue_card)
        layout.addStretch(1)

        back = QPushButton("Назад")
        back.setObjectName("secondaryButton")
        back.setMaximumWidth(120)
        back.clicked.connect(lambda: self._set_publish_step(2))
        layout.addWidget(back, alignment=Qt.AlignmentFlag.AlignLeft)
        self.publish_stack.addWidget(step)

        center = QHBoxLayout()
        center.addStretch(1)
        center.addWidget(content, 1)
        center.addStretch(1)
        outer.addLayout(center)
        scroll.setWidget(page)
        return scroll

    # ------------------------------------------------------------------
    # State and navigation
    # ------------------------------------------------------------------
    def _wire_smart_state(self) -> None:
        self.reel_card.changed.connect(lambda *_: self._sync_create_workflow())
        self.brainrot_card.changed.connect(lambda *_: self._sync_create_workflow())
        self.batch_size.currentIndexChanged.connect(lambda *_: self._sync_create_workflow())
        self.output_picker.changed.connect(lambda *_: self._sync_create_workflow())

        self.order_combo.currentIndexChanged.connect(lambda *_: self._sync_publish_workflow())
        self.recursive_box.toggled.connect(lambda *_: self._sync_publish_workflow())
        self.delay_minutes.valueChanged.connect(lambda *_: self._sync_publish_workflow())
        self.interval.currentIndexChanged.connect(lambda *_: self._sync_publish_workflow())
        self.caption.textChanged.connect(lambda: self._sync_publish_workflow())
        for box in self.platform_boxes.values():
            box.toggled.connect(lambda *_: self._sync_publish_workflow())

    def _mode_changed(self, index: int) -> None:
        self.settings.setValue("smart_tab", int(index))
        self.settings.sync()
        if index == 1:
            self.refresh_connections()
            self.refresh_publish_status()
            self._sync_publish_workflow()

    def _create_max_step(self) -> int:
        if not self.reel_valid:
            return 0
        if not self.brainrot_valid:
            return 1
        return 3

    def _publish_max_step(self) -> int:
        if not self.selected_publish_files:
            return 0
        if not self.selected_platforms():
            return 1
        return 3

    def _set_create_step(self, index: int) -> None:
        index = max(0, min(3, int(index)))
        maximum = self._create_max_step()
        if index > maximum:
            self.status.setText(
                "Сначала закончи текущий шаг — программа подсветит, чего не хватает."
            )
            return
        self._create_step = index
        self.create_stack.setCurrentIndex(index)
        self._sync_create_workflow()

    def _set_publish_step(self, index: int) -> None:
        index = max(0, min(3, int(index)))
        maximum = self._publish_max_step()
        if index > maximum:
            self.status.setText("Сначала выбери Reels и хотя бы одну подключённую платформу.")
            return
        self._publish_step = index
        self.publish_stack.setCurrentIndex(index)
        self._sync_publish_workflow()

    def _sync_step_buttons(
        self,
        buttons: list[QPushButton],
        active: int,
        maximum: int,
    ) -> None:
        for index, button in enumerate(buttons):
            if index == active:
                state = "active"
            elif index < active:
                state = "done"
            else:
                state = "future"
            button.setProperty("stepState", state)
            button.setEnabled(index <= maximum)
            button.style().unpolish(button)
            button.style().polish(button)

    def _sync_create_workflow(self) -> None:
        if not hasattr(self, "create_stack"):
            return
        maximum = self._create_max_step()
        if self._create_step > maximum:
            self._create_step = maximum
            self.create_stack.setCurrentIndex(maximum)
        self._sync_step_buttons(self.create_step_buttons, self._create_step, maximum)

        if self.create_next_buttons:
            self.create_next_buttons[0].setEnabled(self.reel_valid)
        if len(self.create_next_buttons) > 1:
            self.create_next_buttons[1].setEnabled(self.brainrot_valid)
        if len(self.create_next_buttons) > 2:
            self.create_next_buttons[2].setEnabled(self.reel_valid and self.brainrot_valid)

        if hasattr(self, "single_mode_info"):
            self.single_mode_info.setVisible(not self.batch_mode)
        self.batch_frame.setVisible(self.batch_mode)

        if hasattr(self, "output_summary"):
            output = self.output_picker.path or "Папка не выбрана"
            self.output_summary.setText(output)

        if hasattr(self, "create_summary"):
            if self.batch_mode:
                amount = self.batch_size.currentText()
                source_mode = f"Длинная запись · порция: {amount}"
            else:
                source_mode = "Короткий исходник · 1 Reel"
            subtitles = "с субтитрами" if self.subtitles_enabled.isChecked() else "без субтитров"
            self.create_summary.setText(
                f"{source_mode}\nBrainrot: {'готов' if self.brainrot_valid else 'не выбран'} · "
                f"{subtitles}\nРезультат: {self.output_picker.path or 'папка не выбрана'}"
            )

        self._sync_primary_button()

    def _sync_publish_workflow(self) -> None:
        if not hasattr(self, "publish_stack"):
            return
        maximum = self._publish_max_step()
        if self._publish_step > maximum:
            self._publish_step = maximum
            self.publish_stack.setCurrentIndex(maximum)
        self._sync_step_buttons(self.publish_step_buttons, self._publish_step, maximum)

        if self.publish_next_buttons:
            self.publish_next_buttons[0].setEnabled(bool(self.selected_publish_files))
        if len(self.publish_next_buttons) > 1:
            self.publish_next_buttons[1].setEnabled(bool(self.selected_platforms()))
        if len(self.publish_next_buttons) > 2:
            self.publish_next_buttons[2].setEnabled(bool(self.selected_platforms()))

        self._update_publish_summary()

    def _update_publish_summary(self) -> None:
        if not hasattr(self, "publish_summary"):
            return
        count = len(self.selected_publish_files)
        platforms = [
            PLATFORM_LABELS[platform]
            for platform in self.selected_platforms()
        ]
        if not count:
            self.publish_summary.setText("Сначала выбери Reels — расписание посчитается автоматически.")
            return
        if not platforms:
            self.publish_summary.setText(
                f"Выбрано {count} Reels · выбери хотя бы одну подключённую платформу."
            )
            return

        interval = int(self.interval.currentData() or 60)
        start_at = self._effective_start(interval)
        start = datetime.fromtimestamp(start_at)
        finish = start + timedelta(minutes=interval * max(0, count - 1))
        platform_text = ", ".join(platforms)
        self.publish_summary.setText(
            f"{count} Reels → {platform_text}\n"
            f"Первый: {start:%d.%m %H:%M} · последний: {finish:%d.%m %H:%M} · "
            f"интервал {interval} мин"
        )

    def _apply_copy(self) -> None:
        self.reel_card.title_label.setText("ЗАПИСЬ ARARA")
        self.reel_card.hint_label.setText(
            "Короткий Reel или длинная запись. Формат 9:16, обязательно со звуком."
        )
        self.reel_card.choose_button.setText("Выбрать видео")
        self.reel_card.choose_button.setMinimumWidth(130)
        self.reel_card.choose_button.setMaximumWidth(145)

        self.brainrot_card.title_label.setText("BRAINROT")
        self.brainrot_card.hint_label.setText(
            "Длинный фон. Программа сама выбирает свежие отрезки нужной длины."
        )
        self.brainrot_card.choose_button.setText("Выбрать видео")
        self.brainrot_card.choose_button.setMinimumWidth(130)
        self.brainrot_card.choose_button.setMaximumWidth(145)

        self.batch_reset.setText("Начать заново")
        self.batch_reset.setMaximumWidth(130)
        self.stop_button.setText("Стоп после текущего")
        self.stop_button.setMaximumWidth(175)

    def _sync_primary_button(self) -> None:
        super()._sync_primary_button()
        if not hasattr(self, "render_button"):
            return
        if self.batch_mode:
            size = self._selected_batch_size()
            label = "все Reels" if size == 0 else f"{size} Reels"
            self.render_button.setText(f"Создать {label}")
        else:
            self.render_button.setText("Создать Reel")
        self.render_button.setMinimumWidth(230)
        self.render_button.setMaximumWidth(340)

    def _sync_mode_ui(self) -> None:
        super()._sync_mode_ui()
        if hasattr(self, "single_mode_info"):
            self.single_mode_info.setVisible(not self.batch_mode)
        self._sync_create_workflow()

    def refresh_ready_state(self) -> None:
        super().refresh_ready_state()
        self._sync_create_workflow()

    def update_selection_status(self) -> None:
        super().update_selection_status()
        self._sync_publish_workflow()

    def refresh_connections(self) -> None:
        super().refresh_connections()
        if not hasattr(self, "platform_boxes"):
            return
        for platform, box in self.platform_boxes.items():
            connected = platform_connected(platform)
            box.setEnabled(connected)
            if not connected:
                box.setChecked(False)
            box.setToolTip(
                "Аккаунт подключён" if connected else "Сначала открой «Подключения»"
            )
        self._sync_publish_workflow()

    def refresh_publish_status(self) -> None:
        super().refresh_publish_status()
        if hasattr(self, "mode_tabs"):
            remaining = self.publish_queue.remaining
            self.mode_tabs.setTabText(
                1,
                f"Опубликовать · {remaining}" if remaining else "Опубликовать",
            )
        if hasattr(self, "publish_toggle"):
            self.publish_toggle.setText(
                "Остановить очередь" if self.publish_timer.isActive() else "Запустить очередь"
            )
        self._sync_publish_workflow()

    # ------------------------------------------------------------------
    # Styling
    # ------------------------------------------------------------------
    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
* {
    font-family: "Segoe UI";
    font-size: 13px;
}
QMainWindow, QWidget {
    background: #0b0e12;
    color: #eef1f4;
}
QLabel#title {
    color: #f3b75b;
    font-size: 25px;
    font-weight: 800;
}
QLabel#version, QLabel#mutedText {
    color: #7f8994;
    font-size: 11px;
}
QPushButton {
    background: #1a2028;
    color: #eef1f4;
    border: 1px solid #313a45;
    border-radius: 8px;
    min-height: 38px;
    padding: 0 15px;
    font-size: 13px;
    font-weight: 600;
}
QPushButton:hover {
    background: #222a34;
    border-color: #566271;
}
QPushButton:pressed { background: #141a21; }
QPushButton:disabled {
    background: #11161c;
    color: #5f6872;
    border-color: #252c34;
}
QPushButton#primaryAction {
    background: #e6a84c;
    color: #15100a;
    border: 1px solid #f0bd6c;
    min-height: 44px;
    padding: 0 20px;
    font-size: 14px;
    font-weight: 800;
}
QPushButton#primaryAction:hover { background: #f0b75e; }
QPushButton#primarySoft, QPushButton#nextButton {
    background: #2a2118;
    color: #f2c477;
    border-color: #72542e;
}
QPushButton#primarySoft:hover, QPushButton#nextButton:hover {
    background: #36291d;
    border-color: #9a7139;
}
QPushButton#secondaryAction, QPushButton#secondaryButton {
    background: #161c23;
    color: #dce2e8;
}
QPushButton#linkButton {
    background: transparent;
    border: none;
    color: #9ca8b4;
    padding: 0 8px;
    min-height: 32px;
}
QPushButton#linkButton:hover { color: #f0bd6c; }
QPushButton#updateButton {
    min-height: 34px;
    padding: 0 12px;
}
QLineEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background: #10151b;
    color: #eef1f4;
    border: 1px solid #303944;
    border-radius: 8px;
    min-height: 36px;
    padding: 0 10px;
    selection-background-color: #9a6a2c;
}
QTextEdit { padding: 10px; }
QLineEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {
    border-color: #b27c36;
}
QComboBox::drop-down { border: none; width: 26px; }
QCheckBox { spacing: 8px; color: #dce2e8; }
QCheckBox::indicator {
    width: 17px;
    height: 17px;
}
QCheckBox::indicator:unchecked {
    background: #10151b;
    border: 1px solid #46515d;
    border-radius: 4px;
}
QCheckBox::indicator:checked {
    background: #e6a84c;
    border: 1px solid #e6a84c;
    border-radius: 4px;
}
QProgressBar {
    background: #10151b;
    border: 1px solid #2d3640;
    border-radius: 6px;
    min-height: 17px;
    max-height: 17px;
    text-align: center;
    color: #d9dee4;
    font-size: 10px;
}
QProgressBar::chunk {
    background: #d99a42;
    border-radius: 5px;
}
QTabWidget#modeTabs::pane {
    border: 1px solid #242c35;
    border-radius: 12px;
    background: #090c10;
    top: -1px;
}
QTabWidget#modeTabs QTabBar::tab {
    background: #11161c;
    color: #89939e;
    border: 1px solid #28313b;
    border-bottom: none;
    min-width: 180px;
    min-height: 42px;
    padding: 0 24px;
    font-size: 14px;
    font-weight: 700;
    margin-right: 6px;
    border-top-left-radius: 9px;
    border-top-right-radius: 9px;
}
QTabWidget#modeTabs QTabBar::tab:selected {
    background: #171c23;
    color: #f0bd6c;
    border-color: #604629;
}
QWidget#createWorkflow, QScrollArea#publishScroll, QWidget#publishWorkflow,
QWidget#publishContent, QWidget#stepPage, QWidget#stepBar {
    background: transparent;
}
QFrame#workflowHeader {
    background: #10151b;
    border: 1px solid #252e38;
    border-radius: 12px;
}
QLabel#workflowTitle {
    color: #f1b75d;
    font-size: 22px;
    font-weight: 800;
}
QLabel#workflowHint {
    color: #98a3ae;
    font-size: 12px;
}
QPushButton#stepButton {
    background: #10151b;
    color: #717c87;
    border: 1px solid #262f39;
    border-radius: 8px;
    min-height: 36px;
    padding: 0 10px;
    font-size: 12px;
    font-weight: 650;
}
QPushButton#stepButton[stepState="active"] {
    background: #2b2118;
    color: #f2bf6b;
    border-color: #825f32;
}
QPushButton#stepButton[stepState="done"] {
    background: #141b20;
    color: #aeb8c2;
    border-color: #33404b;
}
QLabel#stepTitle {
    color: #f1f3f5;
    font-size: 20px;
    font-weight: 750;
    padding-top: 4px;
}
QLabel#stepHint {
    color: #98a3ae;
    font-size: 12px;
    padding-bottom: 3px;
}
QFrame#dropCard, QFrame#settingsPanel, QFrame#largeCard,
QFrame#summaryCard, QFrame#summaryCardStrong, QFrame#queueCard {
    background: #12171d;
    border: 1px solid #2a333e;
    border-radius: 11px;
}
QFrame#summaryCardStrong {
    background: #171a1e;
    border-color: #5d452b;
}
QFrame#queueCard { background: #0f1419; }
QLabel#cardTitle {
    color: #f0bb68;
    font-size: 14px;
    font-weight: 750;
}
QLabel#cardHint {
    color: #8f9aa5;
    font-size: 11px;
}
QLabel#fileStatus {
    color: #8f9aa5;
    font-size: 11px;
}
QLabel#fileStatus[state="ok"] { color: #6fcf8b; }
QLabel#fileStatus[state="warning"] { color: #e3af59; }
QLabel#fileStatus[state="error"] { color: #e57878; }
QLabel#infoNote, QLabel#selectionStatus {
    background: #0f1419;
    color: #aeb7c0;
    border: 1px solid #27313b;
    border-radius: 8px;
    padding: 10px 12px;
}
QLabel#accentNote {
    background: #1c1812;
    color: #e5c28a;
    border: 1px solid #574124;
    border-radius: 8px;
    padding: 10px 12px;
}
QLabel#summaryTitle, QLabel#fieldLabel {
    color: #dca95a;
    font-size: 11px;
    font-weight: 800;
}
QLabel#summaryText {
    color: #c4cbd2;
    font-size: 12px;
}
QLabel#summaryTextLarge {
    background: #0e1318;
    color: #e0e5ea;
    border: 1px solid #29333d;
    border-radius: 8px;
    padding: 11px 13px;
    font-size: 13px;
}
QLabel#connectionState {
    color: #7f8994;
    font-size: 11px;
}
QFrame#activityBar {
    background: #10151b;
    border: 1px solid #28313b;
    border-radius: 10px;
}
QLabel#globalStatus {
    color: #c9d0d7;
    font-size: 12px;
    font-weight: 600;
}
QScrollBar:vertical {
    background: transparent;
    width: 9px;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background: #303a45;
    border-radius: 4px;
    min-height: 30px;
}
QSplitter::handle { background: #1b222a; width: 1px; }
"""
        )


def install(app_module) -> None:
    app_module.MainWindow = SmartMainWindow
