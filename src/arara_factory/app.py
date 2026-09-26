from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

from PySide6.QtCore import QSettings, QThread, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .brainrot_index import build_index, get_index_info, reset_usage
from .preview import PreviewPanel
from .render import (
    DEFAULT_BANNER_PATH,
    MAX_REEL_SECONDS,
    MIN_REEL_SECONDS,
    RenderOptions,
    _binary,
    output_duration,
    probe_media,
    render_reels,
)
from .updater import UpdateInfo, check_for_update, download_update, launch_installer
from .version import __version__

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}


class FileDropCard(QFrame):
    changed = Signal(str)

    def __init__(self, title: str, hint: str, button_text: str, saved_path: str = ""):
        super().__init__()
        self.setObjectName("dropCard")
        self.setAcceptDrops(True)
        self.setMinimumHeight(132)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(6)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("cardTitle")
        self.hint_label = QLabel(hint)
        self.hint_label.setObjectName("cardHint")
        self.hint_label.setWordWrap(True)

        self.path_edit = QLineEdit(saved_path)
        self.path_edit.setReadOnly(True)
        self.path_edit.setPlaceholderText("Перетащи видео сюда или нажми «Выбрать»")
        self.path_edit.textChanged.connect(self.changed.emit)

        self.choose_button = QPushButton(button_text)
        self.choose_button.setObjectName("chooseButton")
        self.choose_button.setMaximumWidth(138)

        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        bottom.addWidget(self.path_edit, 1)
        bottom.addWidget(self.choose_button)

        self.status_label = QLabel("Файл ещё не выбран")
        self.status_label.setObjectName("fileStatus")
        self.status_label.setWordWrap(True)

        layout.addWidget(self.title_label)
        layout.addWidget(self.hint_label)
        layout.addLayout(bottom)
        layout.addWidget(self.status_label)

    @property
    def path(self) -> str:
        return self.path_edit.text().strip()

    def set_path(self, value: str) -> None:
        self.path_edit.setText(value)

    def clear_path(self) -> None:
        self.path_edit.clear()
        self.set_status("Файл ещё не выбран", "neutral")

    def set_status(self, text: str, state: str = "neutral") -> None:
        self.status_label.setText(text)
        self.status_label.setProperty("state", state)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile() and Path(url.toLocalFile()).suffix.lower() in VIDEO_EXTENSIONS:
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        for url in event.mimeData().urls():
            if not url.isLocalFile():
                continue
            path = Path(url.toLocalFile())
            if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
                self.set_path(str(path))
                event.acceptProposedAction()
                return
        event.ignore()


class FolderPicker(QWidget):
    changed = Signal(str)

    def __init__(self, value: str):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.line = QLineEdit(value)
        self.line.setPlaceholderText("Папка готовых роликов")
        self.line.textChanged.connect(self.changed.emit)
        self.button = QPushButton("Выбрать")
        self.button.setMaximumWidth(95)
        self.button.clicked.connect(self.choose)
        layout.addWidget(self.line, 1)
        layout.addWidget(self.button)

    @property
    def path(self) -> str:
        return self.line.text().strip()

    def choose(self) -> None:
        start = self.path or str(Path.home() / "Videos")
        value = QFileDialog.getExistingDirectory(self, "Куда сохранять готовые ролики", start)
        if value:
            self.line.setText(value)


class VideoFilePicker(QWidget):
    changed = Signal(str)

    def __init__(self, value: str):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.line = QLineEdit(value)
        self.line.setReadOnly(True)
        self.line.setPlaceholderText('Прозрачный анимированный WebM')
        self.line.textChanged.connect(self.changed.emit)
        self.button = QPushButton('Выбрать')
        self.button.setMaximumWidth(95)
        self.button.clicked.connect(self.choose)
        layout.addWidget(self.line, 1)
        layout.addWidget(self.button)

    @property
    def path(self) -> str:
        return self.line.text().strip()

    def choose(self) -> None:
        start = str(Path(self.path).parent) if self.path else str(Path.home() / 'Videos')
        value = QFileDialog.getOpenFileName(
            self,
            'Выбрать прозрачный анимированный баннер',
            start,
            'Прозрачный баннер (*.webm *.mov);;Видео (*.webm *.mov *.mkv *.mp4)',
        )[0]
        if value:
            self.line.setText(value)


class IndexWorker(QThread):
    completed = Signal(str)
    failed = Signal(str)

    def __init__(self, video: Path):
        super().__init__()
        self.video = video

    def run(self) -> None:
        try:
            ffprobe = _binary("ffprobe")
            if not ffprobe:
                raise RuntimeError("FFprobe не найден внутри программы.")
            target = build_index(
                ffprobe,
                self.video,
                target_segments=600,
                min_clip=MIN_REEL_SECONDS,
                max_clip=MAX_REEL_SECONDS,
            )
            self.completed.emit(str(target))
        except Exception:
            self.failed.emit(traceback.format_exc())


class RenderWorker(QThread):
    progressed = Signal(int, str)
    logged = Signal(str)
    completed = Signal(list)
    failed = Signal(str)

    def __init__(self, source: Path, brainrot: Path, output: Path, options: RenderOptions):
        super().__init__()
        self.source = source
        self.brainrot = brainrot
        self.output = output
        self.options = options

    def run(self) -> None:
        try:
            files = render_reels(
                self.source,
                self.brainrot,
                self.output,
                self.options,
                lambda value, text: self.progressed.emit(value, text),
                self.logged.emit,
            )
            self.completed.emit([str(path) for path in files])
        except Exception:
            self.failed.emit(traceback.format_exc())


class UpdateCheckWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def run(self) -> None:
        try:
            self.completed.emit(check_for_update(__version__))
        except Exception:
            self.failed.emit(traceback.format_exc())


class UpdateDownloadWorker(QThread):
    progressed = Signal(int)
    completed = Signal(str)
    failed = Signal(str)

    def __init__(self, info: UpdateInfo):
        super().__init__()
        self.info = info

    def run(self) -> None:
        try:
            path = download_update(self.info, self.progressed.emit)
            self.completed.emit(str(path))
        except Exception:
            self.failed.emit(traceback.format_exc())


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("ARARA", "ARARA Factory")
        self.render_worker: RenderWorker | None = None
        self.index_worker: IndexWorker | None = None
        self.update_check_worker: UpdateCheckWorker | None = None
        self.update_download_worker: UpdateDownloadWorker | None = None
        self.pending_update: UpdateInfo | None = None
        self.update_check_manual = False
        self.last_render_was_preview = False
        self.reel_valid = False
        self.brainrot_valid = False

        self.setWindowTitle(f"ARARA Factory {__version__}")
        self.resize(1240, 820)
        self.setMinimumSize(990, 680)

        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(12)

        header_row = QHBoxLayout()
        header = QLabel("ARARA FACTORY")
        header.setObjectName("title")
        version = QLabel(f"v{__version__} · PERSONAL")
        version.setObjectName("version")
        self.update_button = QPushButton("Проверить обновления")
        self.update_button.setObjectName("updateButton")
        self.update_button.setMaximumWidth(175)
        self.update_button.clicked.connect(self.update_button_clicked)
        header_row.addWidget(header)
        header_row.addStretch(1)
        header_row.addWidget(version, alignment=Qt.AlignmentFlag.AlignVCenter)
        header_row.addWidget(self.update_button, alignment=Qt.AlignmentFlag.AlignVCenter)
        outer.addLayout(header_row)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        outer.addWidget(self.splitter, 1)

        left_scroll = QScrollArea()
        left_scroll.setObjectName("leftScroll")
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QFrame.Shape.NoFrame)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left_scroll.setMinimumWidth(520)
        left_scroll.setMaximumWidth(650)

        left = QWidget()
        left.setObjectName("leftPanel")
        left.setMaximumWidth(620)
        main = QVBoxLayout(left)
        main.setContentsMargins(4, 2, 14, 8)
        main.setSpacing(12)
        left_scroll.setWidget(left)
        self.splitter.addWidget(left_scroll)

        preview_host = QWidget()
        preview_layout = QHBoxLayout(preview_host)
        preview_layout.setContentsMargins(18, 0, 0, 0)
        preview_layout.addStretch(1)
        self.preview_panel = PreviewPanel()
        preview_layout.addWidget(self.preview_panel, 0, Qt.AlignmentFlag.AlignCenter)
        preview_layout.addStretch(1)
        self.splitter.addWidget(preview_host)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([590, 520])

        description = QLabel(
            "Готовый ARARA Reel + длинный Car Falling → brainrot на всю нижнюю треть → итог 9–15 секунд"
        )
        description.setObjectName("subtitle")
        description.setWordWrap(True)
        main.addWidget(description)

        self.reel_card = FileDropCard(
            "1. ГОТОВЫЙ ARARA REEL",
            "Вертикальный Reel со звуком. 9–15 секунд; более длинный автоматически обрежется до 15.",
            "Выбрать Reel",
        )
        self.reel_card.choose_button.clicked.connect(self.choose_reel)
        self.reel_card.changed.connect(self.inspect_reel)
        main.addWidget(self.reel_card)

        saved_brainrot = str(self.settings.value("brainrot", ""))
        self.brainrot_card = FileDropCard(
            "2. ДЛИННЫЙ BRAINROT",
            "Часовой Car Falling подходит. Программа запомнит файл и будет брать новый фрагмент нужной длины.",
            "Выбрать brainrot",
            saved_brainrot,
        )
        self.brainrot_card.choose_button.clicked.connect(self.choose_brainrot)
        self.brainrot_card.changed.connect(self.on_brainrot_changed)
        main.addWidget(self.brainrot_card)

        library_row = QHBoxLayout()
        library_row.setSpacing(8)
        self.library_status = QLabel("Brainrot ещё не выбран")
        self.library_status.setObjectName("libraryStatus")
        self.library_status.setWordWrap(True)
        self.prepare_button = QPushButton("Подготовить участки")
        self.prepare_button.setMaximumWidth(150)
        self.prepare_button.clicked.connect(self.prepare_brainrot)
        self.reset_button = QPushButton("Сбросить историю")
        self.reset_button.setMaximumWidth(140)
        self.reset_button.clicked.connect(self.reset_brainrot)
        library_row.addWidget(self.library_status, 1)
        library_row.addWidget(self.prepare_button)
        library_row.addWidget(self.reset_button)
        main.addLayout(library_row)

        action_grid = QGridLayout()
        action_grid.setHorizontalSpacing(9)
        action_grid.setVerticalSpacing(9)
        self.preview_button = QPushButton("ТЕСТ 5 СЕКУНД")
        self.preview_button.setObjectName("preview")
        self.preview_button.setMaximumWidth(175)
        self.preview_button.clicked.connect(lambda: self.start_render(preview=True))
        self.render_button = QPushButton("СОБРАТЬ ГОТОВЫЙ REEL")
        self.render_button.setObjectName("generate")
        self.render_button.setMaximumWidth(300)
        self.render_button.clicked.connect(lambda: self.start_render(preview=False))
        action_grid.addWidget(self.preview_button, 0, 0)
        action_grid.addWidget(self.render_button, 0, 1)
        action_grid.setColumnStretch(2, 1)
        main.addLayout(action_grid)

        utility_row = QHBoxLayout()
        utility_row.setSpacing(8)
        self.settings_button = QPushButton("Настройки")
        self.settings_button.setMaximumWidth(110)
        self.settings_button.clicked.connect(self.toggle_settings)
        self.open_button = QPushButton("Готовые ролики")
        self.open_button.setMaximumWidth(130)
        self.open_button.clicked.connect(self.open_output)
        self.next_button = QPushButton("Следующий Reel")
        self.next_button.setMaximumWidth(125)
        self.next_button.clicked.connect(self.next_reel)
        utility_row.addWidget(self.settings_button)
        utility_row.addWidget(self.open_button)
        utility_row.addWidget(self.next_button)
        utility_row.addStretch(1)
        main.addLayout(utility_row)

        self.settings_panel = QFrame()
        self.settings_panel.setObjectName("settingsPanel")
        settings_layout = QFormLayout(self.settings_panel)
        settings_layout.setContentsMargins(16, 14, 16, 14)
        settings_layout.setHorizontalSpacing(12)
        settings_layout.setVerticalSpacing(9)

        default_output = Path.home() / "Videos" / "ARARA Factory" / "renders"
        self.output_picker = FolderPicker(str(self.settings.value("output", str(default_output))))

        saved_banner = str(self.settings.value('banner_path', str(DEFAULT_BANNER_PATH)))
        self.banner_enabled = QCheckBox('Добавлять анимированный баннер arara777 сверху')
        self.banner_enabled.setChecked(
            self.settings.value(
                'banner_enabled',
                Path(saved_banner).is_file(),
                type=bool,
            )
        )
        self.banner_picker = VideoFilePicker(saved_banner)

        self.banner_width = QSpinBox()
        self.banner_width.setRange(50, 100)
        self.banner_width.setValue(int(self.settings.value('banner_width_percent', 100)))
        self.banner_width.setSuffix('%')
        self.banner_width.setMaximumWidth(120)

        self.banner_top = QDoubleSpinBox()
        self.banner_top.setRange(0.0, 20.0)
        self.banner_top.setSingleStep(0.5)
        self.banner_top.setDecimals(1)
        self.banner_top.setValue(float(self.settings.value('banner_top_percent', 2.5)))
        self.banner_top.setSuffix('% высоты')
        self.banner_top.setMaximumWidth(150)

        self.zoom = QDoubleSpinBox()
        self.zoom.setRange(1.0, 1.5)
        self.zoom.setSingleStep(0.05)
        self.zoom.setDecimals(2)
        self.zoom.setSuffix("×")
        self.zoom.setValue(float(self.settings.value("zoom", 1.25)))
        self.zoom.setMaximumWidth(120)

        self.subtitle_y = QSpinBox()
        self.subtitle_y.setRange(850, 1250)
        self.subtitle_y.setValue(int(self.settings.value("subtitle_y", 1050)))
        self.subtitle_y.setMaximumWidth(120)

        self.subtitles_enabled = QCheckBox("Добавлять субтитры")
        self.subtitles_enabled.setChecked(
            self.settings.value("subtitles_enabled", True, type=bool)
        )

        self.encoder = QComboBox()
        self.encoder.addItem("Автоматически: NVIDIA → CPU", "auto")
        self.encoder.addItem("Только NVIDIA", "nvidia")
        self.encoder.addItem("Только процессор", "cpu")
        self.encoder.setCurrentIndex(
            max(0, self.encoder.findData(str(self.settings.value("encoder", "auto"))))
        )

        self.quality = QSpinBox()
        self.quality.setRange(16, 28)
        self.quality.setValue(int(self.settings.value("quality", 20)))
        self.quality.setMaximumWidth(120)

        self.auto_open = QCheckBox("Открывать папку после полной сборки")
        self.auto_open.setChecked(self.settings.value("auto_open", True, type=bool))

        settings_layout.addRow("Куда сохранять", self.output_picker)
        settings_layout.addRow('', self.banner_enabled)
        settings_layout.addRow('Файл баннера', self.banner_picker)
        settings_layout.addRow('Ширина баннера', self.banner_width)
        settings_layout.addRow('Отступ сверху', self.banner_top)
        settings_layout.addRow("Приближение машины", self.zoom)
        settings_layout.addRow("Высота субтитров", self.subtitle_y)
        settings_layout.addRow("", self.subtitles_enabled)
        settings_layout.addRow("Ускорение", self.encoder)
        settings_layout.addRow("Качество", self.quality)
        settings_layout.addRow("", self.auto_open)
        self.settings_panel.setVisible(False)
        main.addWidget(self.settings_panel)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.status = QLabel("Выбери Reel и длинный brainrot")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        main.addWidget(self.progress)
        main.addWidget(self.status)

        self.log_button = QPushButton("Показать технический журнал")
        self.log_button.setObjectName("linkButton")
        self.log_button.setMaximumWidth(210)
        self.log_button.clicked.connect(self.toggle_log)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(130)
        self.log.setVisible(False)
        main.addWidget(self.log_button, alignment=Qt.AlignmentFlag.AlignLeft)
        main.addWidget(self.log)
        main.addStretch(1)

        self.output_picker.changed.connect(self.save_preferences)
        self.banner_enabled.toggled.connect(self.banner_controls_changed)
        self.banner_picker.changed.connect(self.save_preferences)
        self.banner_width.valueChanged.connect(self.save_preferences)
        self.banner_top.valueChanged.connect(self.save_preferences)
        self.zoom.valueChanged.connect(self.zoom_changed)
        self.subtitle_y.valueChanged.connect(self.save_preferences)
        self.subtitles_enabled.toggled.connect(self.save_preferences)
        self.encoder.currentIndexChanged.connect(self.save_preferences)
        self.quality.valueChanged.connect(self.save_preferences)
        self.auto_open.toggled.connect(self.save_preferences)
        self.sync_banner_controls()

        self.inspect_brainrot()
        self.refresh_ready_state()
        self.schedule_automatic_update_check()

    def choose_reel(self) -> None:
        value = QFileDialog.getOpenFileName(
            self,
            "Выбрать готовый ARARA Reel",
            str(Path.home() / "Videos"),
            "Видео (*.mp4 *.mov *.mkv *.webm *.avi)",
        )[0]
        if value:
            self.reel_card.set_path(value)

    def choose_brainrot(self) -> None:
        start = (
            str(Path(self.brainrot_card.path).parent)
            if self.brainrot_card.path
            else str(Path.home() / "Videos")
        )
        value = QFileDialog.getOpenFileName(
            self,
            "Выбрать длинное brainrot-видео",
            start,
            "Видео (*.mp4 *.mov *.mkv *.webm *.avi)",
        )[0]
        if value:
            self.brainrot_card.set_path(value)

    def _probe(self, path: Path):
        ffprobe = _binary("ffprobe")
        if not ffprobe:
            raise RuntimeError("FFprobe не найден внутри программы.")
        return probe_media(ffprobe, path)

    def inspect_reel(self, value: str) -> None:
        path = Path(value) if value else Path("__missing__")
        self.reel_valid = False
        if not path.is_file():
            self.reel_card.set_status("Выбери готовый Reel", "neutral")
            self.preview_panel.clear()
            self.refresh_ready_state()
            return
        try:
            info = self._probe(path)
            ratio = info.width / info.height
            if abs(ratio - 9 / 16) > 0.015:
                self.reel_card.set_status(
                    f"{info.width}×{info.height} · нужен вертикальный формат 9:16", "error"
                )
            elif not info.has_audio:
                self.reel_card.set_status(
                    "В видео нет звука — нужен Reel с аудиодорожкой", "error"
                )
            elif info.duration < MIN_REEL_SECONDS - 0.05:
                self.reel_card.set_status(
                    f"{info.duration:.1f} сек · слишком коротко, минимум 9 секунд", "error"
                )
            else:
                final = output_duration(info.duration)
                if info.duration > MAX_REEL_SECONDS + 0.05:
                    self.reel_card.set_status(
                        f"{info.width}×{info.height} · {info.duration:.1f} сек → обрежется до {final:.0f} сек",
                        "warning",
                    )
                else:
                    self.reel_card.set_status(
                        f"{info.width}×{info.height} · {info.duration:.1f} сек · готов к сборке",
                        "ok",
                    )
                self.reel_valid = True
                self.preview_panel.load_file(path, autoplay=False, title="исходный Reel")
        except Exception as exc:
            self.reel_card.set_status(str(exc), "error")
        self.refresh_ready_state()

    def on_brainrot_changed(self, value: str) -> None:
        self.settings.setValue("brainrot", value)
        self.settings.sync()
        self.inspect_brainrot()

    def inspect_brainrot(self) -> None:
        path_text = self.brainrot_card.path
        path = Path(path_text) if path_text else Path("__missing__")
        self.brainrot_valid = False
        if not path.is_file():
            self.brainrot_card.set_status(
                "Выбери длинный Car Falling или другой brainrot", "neutral"
            )
            self.library_status.setText("Brainrot ещё не выбран")
            self.prepare_button.setEnabled(False)
            self.reset_button.setEnabled(False)
            self.refresh_ready_state()
            return
        try:
            info = self._probe(path)
            if info.duration < MAX_REEL_SECONDS:
                self.brainrot_card.set_status(
                    "Видео короче 15 секунд — нужен длинный brainrot", "error"
                )
            else:
                orientation = "горизонтальный" if info.width >= info.height else "вертикальный"
                self.brainrot_card.set_status(
                    f"{info.width}×{info.height} · {info.duration / 60:.1f} мин · {orientation} · zoom {self.zoom.value():.2f}×",
                    "ok",
                )
                self.brainrot_valid = True
            index_info = get_index_info(path)
            self.prepare_button.setEnabled(True)
            self.reset_button.setEnabled(bool(index_info))
            if not index_info:
                self.library_status.setText("Индекс создастся автоматически при первой сборке")
            elif not index_info.is_current:
                self.library_status.setText("Видео изменилось · индекс обновится автоматически")
            else:
                self.library_status.setText(
                    f"Осталось {index_info.remaining} из {index_info.total} свежих участков"
                )
        except Exception as exc:
            self.brainrot_card.set_status(str(exc), "error")
        self.refresh_ready_state()

    def zoom_changed(self, *args) -> None:
        self.save_preferences()
        self.inspect_brainrot()

    def banner_controls_changed(self, *args) -> None:
        self.sync_banner_controls()
        self.save_preferences()

    def sync_banner_controls(self) -> None:
        enabled = self.banner_enabled.isChecked()
        self.banner_picker.setEnabled(enabled)
        self.banner_width.setEnabled(enabled)
        self.banner_top.setEnabled(enabled)

    def save_preferences(self, *args) -> None:
        self.settings.setValue("brainrot", self.brainrot_card.path)
        self.settings.setValue("output", self.output_picker.path)
        self.settings.setValue('banner_enabled', self.banner_enabled.isChecked())
        self.settings.setValue('banner_path', self.banner_picker.path)
        self.settings.setValue('banner_width_percent', self.banner_width.value())
        self.settings.setValue('banner_top_percent', self.banner_top.value())
        self.settings.setValue("zoom", self.zoom.value())
        self.settings.setValue("subtitle_y", self.subtitle_y.value())
        self.settings.setValue("subtitles_enabled", self.subtitles_enabled.isChecked())
        self.settings.setValue("encoder", self.encoder.currentData())
        self.settings.setValue("quality", self.quality.value())
        self.settings.setValue("auto_open", self.auto_open.isChecked())
        self.settings.sync()

    def prepare_brainrot(self) -> None:
        path = Path(self.brainrot_card.path)
        if not path.is_file():
            QMessageBox.warning(self, "Нет brainrot", "Сначала выбери длинное brainrot-видео.")
            return
        self.prepare_button.setEnabled(False)
        self.library_status.setText("Подготавливаю 600 участков…")
        self.index_worker = IndexWorker(path)
        self.index_worker.completed.connect(self.index_done)
        self.index_worker.failed.connect(self.index_failed)
        self.index_worker.start()

    def index_done(self, target: str) -> None:
        self.log.append(f"Индекс: {target}")
        self.status.setText("Brainrot подготовлен")
        self.inspect_brainrot()

    def index_failed(self, error: str) -> None:
        self.log.setPlainText(error)
        self.log.setVisible(True)
        self.library_status.setText("Не удалось подготовить brainrot")
        QMessageBox.critical(self, "Ошибка индексации", error.splitlines()[-1])
        self.inspect_brainrot()

    def reset_brainrot(self) -> None:
        path = Path(self.brainrot_card.path)
        if reset_usage(path):
            self.inspect_brainrot()
            self.status.setText("Все brainrot-участки снова доступны")

    def refresh_ready_state(self) -> None:
        ready = self.reel_valid and self.brainrot_valid
        busy = bool(self.render_worker and self.render_worker.isRunning())
        self.preview_button.setEnabled(ready and not busy)
        self.render_button.setEnabled(ready and not busy)
        if ready and not busy:
            self.status.setText("Всё готово · сделай тест 5 секунд и смотри результат справа")

    def start_render(self, preview: bool) -> None:
        source = Path(self.reel_card.path)
        brainrot = Path(self.brainrot_card.path)
        output = Path(self.output_picker.path)
        if not self.reel_valid or not self.brainrot_valid:
            QMessageBox.warning(
                self, "Проверь файлы", "Нужны подходящий Reel и длинный brainrot."
            )
            return
        if not self.output_picker.path:
            QMessageBox.warning(
                self, "Нужна папка", "В настройках выбери папку для готовых роликов."
            )
            return

        self.save_preferences()
        self.last_render_was_preview = preview
        options = RenderOptions(
            variants=1,
            subtitle_y=self.subtitle_y.value(),
            font="Arial Black",
            encoder_preset="veryfast",
            crf=self.quality.value(),
            encoder_mode=str(self.encoder.currentData()),
            preview_seconds=5.0 if preview else None,
            brainrot_zoom=self.zoom.value(),
            subtitles_enabled=self.subtitles_enabled.isChecked(),
            banner_enabled=self.banner_enabled.isChecked(),
            banner_path=self.banner_picker.path,
            banner_width_percent=self.banner_width.value(),
            banner_top_percent=self.banner_top.value(),
        )

        self.preview_panel.player.pause()
        self.set_busy(True)
        self.log.clear()
        self.progress.setValue(0)
        self.status.setText("Запускаю тест…" if preview else "Собираю готовый Reel…")
        self.render_worker = RenderWorker(source, brainrot, output, options)
        self.render_worker.progressed.connect(self.on_progress)
        self.render_worker.logged.connect(self.log.append)
        self.render_worker.completed.connect(self.render_done)
        self.render_worker.failed.connect(self.render_failed)
        self.render_worker.start()

    def set_busy(self, busy: bool) -> None:
        self.preview_button.setEnabled(not busy and self.reel_valid and self.brainrot_valid)
        self.render_button.setEnabled(not busy and self.reel_valid and self.brainrot_valid)
        self.prepare_button.setEnabled(not busy and self.brainrot_valid)
        self.next_button.setEnabled(not busy)
        self.update_button.setEnabled(not busy and not self.update_download_worker)

    def on_progress(self, value: int, text: str) -> None:
        self.progress.setValue(value)
        self.status.setText(text)

    def render_done(self, files: list[str]) -> None:
        self.set_busy(False)
        self.progress.setValue(100)
        self.inspect_brainrot()
        if not files:
            self.status.setText("Сборка завершена без выходного файла")
            return

        result = Path(files[0])
        title = "тест 5 секунд" if self.last_render_was_preview else "готовый Reel"
        self.preview_panel.load_file(result, autoplay=True, title=title)
        if self.last_render_was_preview:
            self.status.setText("Тест готов · результат проигрывается справа")
        else:
            self.status.setText(f"Готово: {result.name}")
            if self.auto_open.isChecked():
                self.open_output()

    def render_failed(self, error: str) -> None:
        self.set_busy(False)
        self.status.setText("Сборка не удалась")
        self.log.setPlainText(error)
        self.log.setVisible(True)
        self.log_button.setText("Скрыть технический журнал")
        QMessageBox.critical(self, "Ошибка", error.splitlines()[-1])

    def next_reel(self) -> None:
        self.reel_card.clear_path()
        self.reel_valid = False
        self.progress.setValue(0)
        self.status.setText("Перетащи следующий Reel")
        self.preview_panel.clear("Следующий готовый Reel появится здесь")
        self.refresh_ready_state()

    def open_output(self) -> None:
        path = Path(self.output_picker.path)
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def toggle_settings(self) -> None:
        visible = not self.settings_panel.isVisible()
        self.settings_panel.setVisible(visible)
        self.settings_button.setText("Скрыть настройки" if visible else "Настройки")

    def toggle_log(self) -> None:
        visible = not self.log.isVisible()
        self.log.setVisible(visible)
        self.log_button.setText(
            "Скрыть технический журнал" if visible else "Показать технический журнал"
        )

    def schedule_automatic_update_check(self) -> None:
        last = int(self.settings.value("last_update_check", 0) or 0)
        if int(time.time()) - last >= 24 * 60 * 60:
            QTimer.singleShot(3000, lambda: self.start_update_check(manual=False))

    def update_button_clicked(self) -> None:
        if self.pending_update:
            self.offer_update(self.pending_update)
        else:
            self.start_update_check(manual=True)

    def start_update_check(self, manual: bool) -> None:
        if self.update_check_worker and self.update_check_worker.isRunning():
            return
        self.update_check_manual = manual
        self.update_button.setEnabled(False)
        self.update_button.setText("Проверяю…")
        self.update_check_worker = UpdateCheckWorker()
        self.update_check_worker.completed.connect(self.update_checked)
        self.update_check_worker.failed.connect(self.update_check_failed)
        self.update_check_worker.start()

    def update_checked(self, info: UpdateInfo | None) -> None:
        self.settings.setValue("last_update_check", int(time.time()))
        self.settings.sync()
        self.update_button.setEnabled(True)
        if info is None:
            self.pending_update = None
            self.update_button.setText("Версия актуальна")
            if self.update_check_manual:
                self.status.setText(f"Установлена последняя версия v{__version__}")
            QTimer.singleShot(3500, lambda: self.update_button.setText("Проверить обновления"))
            return

        self.pending_update = info
        self.update_button.setText(f"Обновить до v{info.version}")
        self.status.setText(f"Доступно обновление ARARA Factory v{info.version}")
        if self.update_check_manual:
            self.offer_update(info)

    def update_check_failed(self, error: str) -> None:
        self.update_button.setEnabled(True)
        self.update_button.setText("Проверить обновления")
        if self.update_check_manual:
            self.log.append(error)
            self.status.setText("Не удалось проверить обновления · проверь интернет")

    def offer_update(self, info: UpdateInfo) -> None:
        answer = QMessageBox.question(
            self,
            "Обновление ARARA Factory",
            (
                f"Доступна версия v{info.version}.\n\n"
                "Программа скачает новый установщик, закроется и обновится поверх текущей версии. "
                "Brainrot и настройки сохранятся.\n\nУстановить обновление?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.start_update_download(info)

    def start_update_download(self, info: UpdateInfo) -> None:
        if self.update_download_worker and self.update_download_worker.isRunning():
            return
        self.update_button.setEnabled(False)
        self.update_button.setText("Скачиваю 0%")
        self.status.setText(f"Скачиваю обновление v{info.version}…")
        self.update_download_worker = UpdateDownloadWorker(info)
        self.update_download_worker.progressed.connect(self.update_download_progress)
        self.update_download_worker.completed.connect(self.update_downloaded)
        self.update_download_worker.failed.connect(self.update_download_failed)
        self.update_download_worker.start()

    def update_download_progress(self, value: int) -> None:
        self.update_button.setText(f"Скачиваю {value}%")
        self.progress.setValue(value)

    def update_downloaded(self, installer_path: str) -> None:
        try:
            self.status.setText("Обновление скачано · перезапускаю программу")
            self.update_button.setText("Устанавливаю…")
            launch_installer(Path(installer_path))
            QTimer.singleShot(700, QApplication.instance().quit)
        except Exception as exc:
            self.update_download_failed(traceback.format_exc())
            QMessageBox.critical(self, "Ошибка обновления", str(exc))

    def update_download_failed(self, error: str) -> None:
        self.update_download_worker = None
        self.update_button.setEnabled(True)
        self.update_button.setText(
            f"Обновить до v{self.pending_update.version}" if self.pending_update else "Проверить обновления"
        )
        self.status.setText("Не удалось скачать обновление")
        self.log.setPlainText(error)
        self.log.setVisible(True)

    def closeEvent(self, event) -> None:  # noqa: N802
        self.save_preferences()
        self.preview_panel.player.stop()
        super().closeEvent(event)


def main() -> None:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setApplicationName("ARARA Factory")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("ARARA")
    app.setStyleSheet(
        """
QWidget { background: #0b0a0d; color: #f4ecdf; font-family: "Segoe UI"; font-size: 14px; }
QLabel#title { color: #e7ad43; font-size: 32px; font-weight: 900; letter-spacing: 2px; }
QLabel#version { color: #8f806d; font-size: 12px; padding-right: 5px; }
QLabel#subtitle { color: #aa9d8d; font-size: 14px; padding-bottom: 2px; }
QScrollArea#leftScroll, QWidget#leftPanel { background: transparent; }
QFrame#dropCard { background: #151117; border: 1px solid #5d452b; border-radius: 13px; }
QFrame#dropCard:hover { border-color: #d59a39; background: #19131b; }
QLabel#cardTitle { color: #f1c36d; font-size: 17px; font-weight: 800; }
QLabel#cardHint { color: #a79a8b; font-size: 12px; }
QLabel#fileStatus { color: #8f857a; font-size: 12px; }
QLabel#fileStatus[state="ok"] { color: #77d57a; }
QLabel#fileStatus[state="warning"] { color: #e7ad43; }
QLabel#fileStatus[state="error"] { color: #ef7777; }
QLabel#libraryStatus { color: #bcae9b; font-size: 12px; }
QLabel#status { color: #dfc89f; font-weight: 600; }
QFrame#settingsPanel { background: #121014; border: 1px solid #4c3923; border-radius: 11px; }
QFrame#previewPanel { background: #111014; border: 1px solid #4c3923; border-radius: 14px; }
QWidget#previewCanvas { background: #070609; border-radius: 10px; }
QLabel#previewTitle { color: #f1c36d; font-size: 15px; font-weight: 800; }
QLabel#previewSource, QLabel#previewTime { color: #8f806d; font-size: 11px; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTextEdit {
    background: #0d0b0e; color: #f4ecdf; border: 1px solid #4e3b27;
    border-radius: 7px; padding: 7px; selection-background-color: #8d6429;
}
QPushButton {
    background: #241b14; color: #eee3d3; border: 1px solid #6b4d29;
    border-radius: 8px; padding: 8px 12px; font-weight: 650;
}
QPushButton:hover { border-color: #e7ad43; background: #2c2117; }
QPushButton:disabled { color: #655e56; border-color: #31291f; background: #151210; }
QPushButton#chooseButton { min-width: 118px; }
QPushButton#preview { background: #252029; min-height: 30px; }
QPushButton#generate {
    background: #d99d37; color: #0b0804; border-color: #f1be63;
    font-size: 16px; font-weight: 900; min-height: 32px;
}
QPushButton#generate:hover { background: #edb34c; }
QPushButton#updateButton { background: #18151b; color: #dfc89f; padding: 7px 10px; }
QPushButton#mediaButton { padding: 6px; border-radius: 7px; }
QPushButton#linkButton { background: transparent; border: none; color: #8f806d; padding: 2px; font-size: 12px; }
QProgressBar {
    background: #0d0b0e; border: 1px solid #4e3b27; border-radius: 6px;
    text-align: center; min-height: 18px;
}
QProgressBar::chunk { background: #d99d37; border-radius: 5px; }
QSlider::groove:horizontal { height: 5px; background: #30261c; border-radius: 2px; }
QSlider::handle:horizontal { width: 13px; margin: -4px 0; background: #e7ad43; border-radius: 6px; }
QCheckBox { spacing: 7px; }
QSplitter::handle { background: #1e1813; width: 1px; }
"""
    )
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
