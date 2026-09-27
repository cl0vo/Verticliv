"""Local batch workspace: import many sources, produce a reviewable set of reels."""
from __future__ import annotations

import os
import json
from pathlib import Path

from PySide6.QtCore import QSettings, QThread, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QProgressBar, QPushButton, QScrollArea, QSpinBox, QSplitter, QTextEdit, QVBoxLayout, QWidget,
)

from .auto_reels import AutoReelsOptions, run_auto_reels
from .twitch_import_ui import TwitchImportDialog
from .caption_styles import CAPTION_STYLES


VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.mov', '.avi', '.webm', '.m4v', '.ts',
                    '.mts', '.m2ts', '.flv', '.wmv', '.mpg', '.mpeg', '.ogv'}
VIDEO_FILTER = 'Видео (' + ' '.join('*' + s for s in sorted(VIDEO_EXTENSIONS)) + ');;Все файлы (*)'


class AutoReelsTask(QThread):
    progress = Signal(int, str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, sources, output, options, parent=None):
        super().__init__(parent)
        self.sources, self.output, self.options = sources, output, options

    def run(self):
        try:
            result = run_auto_reels(self.sources, self.output, self.options,
                                    self.progress.emit, self.isInterruptionRequested)
            self.done.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


class AutoReelsPanel(QWidget):
    busy_changed = Signal(bool)
    edit_requested = Signal(str)
    layout_requested = Signal(str)

    def __init__(self, parent=None, settings=None):
        super().__init__(parent)
        # Keep the existing settings namespace: upgrading must not reset the user.
        self.settings = settings if settings is not None else QSettings('ARARA', 'ARARA Factory')
        self.task = None
        self.report_path = ''
        try:
            self.gaming_template = json.loads(str(self.settings.value('auto_reels/gaming_template', 'null')))
            if not isinstance(self.gaming_template, dict):
                self.gaming_template = None
        except (ValueError, TypeError):
            self.gaming_template = None
        self.setAcceptDrops(True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        title = QLabel('Из исходников — в подборку Reels')
        title.setStyleSheet('font-size:26px;font-weight:800;color:#b0f563')
        outer.addWidget(title)
        intro = QLabel('1 · Добавь видео    →    2 · Выбери длину и субтитры    →    3 · Получи готовые клипы')
        intro.setWordWrap(True)
        outer.addWidget(intro)
        self.controls = QWidget()
        body = QHBoxLayout(self.controls)
        body.setContentsMargins(0, 12, 0, 12)
        sources_box = QVBoxLayout()
        row = QHBoxLayout()
        self.add_button = self.button('＋ Видео', self.choose_files, row)
        self.folder_button = self.button('＋ Папка', self.choose_folder, row)
        self.remove_button = self.button('Убрать выбранные', self.remove_selected, row)
        sources_box.addLayout(row)
        self.sources = QListWidget()
        self.sources.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.sources.setMinimumHeight(130)
        sources_box.addWidget(self.sources, 1)
        self.summary = QLabel('Перетащи сюда несколько видео. Исходники останутся без изменений.')
        self.summary.setWordWrap(True)
        sources_box.addWidget(self.summary)
        body.addLayout(sources_box, 3)
        options_widget = QWidget()
        form = QFormLayout(options_widget)
        self.profile = QComboBox()
        for label, value in [('Настроить вручную', 'custom'), ('Разговор / рассказ', 'speech'),
                             ('Игра / Hearthstone', 'hearthstone'), ('Экран / обучение', 'screen')]:
            self.profile.addItem(label, value)
        form.addRow('Тип материала', self.profile)
        self.selection = self.combo([
            ('Реплики и паузы · полная расшифровка', 'speech'),
            ('Звуковые реакции · быстрый поиск', 'reactions'),
        ], 'selection', 'speech')
        form.addRow('Как отбирать', self.selection)
        self.clip_length = QSpinBox()
        self.clip_length.setRange(10, 180)
        self.clip_length.setSuffix(' сек')
        self.clip_length.setValue(int(self.setting('clip_length', 30)))
        form.addRow('Ориентир длины', self.clip_length)
        self.count = QSpinBox()
        self.count.setRange(1, 12)
        self.count.setValue(int(self.setting('count', 3)))
        form.addRow('До клипов с исходника', self.count)
        self.layout_mode = self.combo([
            ('Вебка + игра · до краёв, без полей', 'gaming'),
            ('Вебка + игра · целиком, с полями', 'gaming_fit'),
            ('Авто · сохранить содержимое', 'auto'),
            ('Весь кадр + размытый фон', 'fit'),
            ('Заполнить 9:16 · обрезать края', 'fill'),
        ], 'layout', 'auto')
        form.addRow('Кадр 1080 × 1920', self.layout_mode)
        self.configure_layout_button = QPushButton('Настроить области игры и вебки')
        self.configure_layout_button.clicked.connect(self.configure_gaming)
        form.addRow(self.configure_layout_button)
        layout_note = QLabel('Вебка сверху, игра снизу — вплотную, без растягивания. '
                            'Для заполнения блоков края обрезаются. Выдели две области один раз '
                            'и проверь результат: шаблон подходит для записей с той же сценой.')
        layout_note.setWordWrap(True)
        form.addRow(layout_note)
        self.captions = QCheckBox('Субтитры с подсветкой слов + SRT')
        self.captions.setChecked(self.setting('captions', True, bool))
        form.addRow(self.captions)
        self.caption_style = self.combo([(v.label, k) for k, v in CAPTION_STYLES.items()],
                                       'caption_style', 'reels_lime')
        form.addRow('Шаблон субтитров', self.caption_style)
        self.model = self.combo([
            ('Small · основной', 'small'), ('Tiny · быстрый черновик', 'tiny'),
            ('Medium · медленнее, точнее', 'medium'),
        ], 'model', 'small')
        form.addRow('Распознавание на CPU', self.model)
        self.language = self.combo([('Авто', 'auto'), ('Русский', 'ru'), ('English', 'en')], 'language', 'auto')
        form.addRow('Язык', self.language)
        self.vocabulary = QLineEdit(str(self.setting('vocabulary', '')))
        self.vocabulary.setMaxLength(2000)
        self.vocabulary.setPlaceholderText('Имена, названия, игровые термины')
        form.addRow('Словарь речи', self.vocabulary)
        self.skip_start = QSpinBox()
        self.skip_end = QSpinBox()
        for control, key in ((self.skip_start, 'skip_start'), (self.skip_end, 'skip_end')):
            control.setRange(0, 86400)
            control.setSuffix(' сек')
            control.setValue(int(self.setting(key, 0)))
        form.addRow('Пропустить начало', self.skip_start)
        form.addRow('Пропустить конец', self.skip_end)
        self.zoom = QCheckBox('Лёгкий зум на пике реакции')
        self.zoom.setChecked(self.setting('zoom', False, bool))
        form.addRow(self.zoom)
        note = QLabel('Реплики: сначала расшифровка всей выбранной части — дольше, '
                      'зато границы по словам и паузам. Длина может отличаться от ориентира. '
                      'Реакции: быстрый поиск по громкости. Оба режима пока эвристические, '
                      'без понимания игрового события.\n\n'
                      'Речь обрабатывается локально. Кэш расшифровок в .verticliv-cache рядом '
                      'с подборками ускоряет повторный запуск. Видео никуда не отправляется.')
        note.setWordWrap(True)
        note.setMaximumWidth(360)
        note.setStyleSheet('color:#9daac0')
        form.addRow(note)
        options_scroll = QScrollArea()
        options_scroll.setWidgetResizable(True)
        options_scroll.setWidget(options_widget)
        options_scroll.setMinimumWidth(370)
        body.addWidget(options_scroll, 2)
        self.profile.currentIndexChanged.connect(self.apply_profile)
        outer.addWidget(self.controls, 2)
        output_row = QHBoxLayout()
        output_row.addWidget(QLabel('Сохранять в'))
        self.output = QLineEdit(str(self.setting('output', str(Path.home() / 'Videos' / 'Verticliv' / 'Reels'))))
        output_row.addWidget(self.output, 1)
        self.output_button = self.button('Выбрать папку', self.choose_output, output_row)
        outer.addLayout(output_row)
        actions = QHBoxLayout()
        self.start_button = self.button('Создать подборку', self.start, actions)
        self.start_button.setStyleSheet('background:#b0f563;color:#111827;font-weight:800;padding:12px 24px')
        self.cancel_button = self.button('Остановить после текущей операции', self.cancel, actions)
        self.cancel_button.setEnabled(False)
        self.progress = QProgressBar()
        actions.addWidget(self.progress, 1)
        outer.addLayout(actions)
        self.status = QLabel('Готов к работе. Автонарезка ничего не публикует сама.')
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        results = QSplitter(Qt.Orientation.Horizontal)
        self.results = QListWidget()
        self.results.itemDoubleClicked.connect(self.open_result)
        self.results.setToolTip('Двойной клик — посмотреть клип')
        results.addWidget(self.results)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText('Здесь будут замечания по исходникам и итоговый отчёт.')
        results.addWidget(self.log)
        outer.addWidget(results, 1)
        bottom = QHBoxLayout()
        self.button('Смотреть подборку', self.open_review, bottom)
        self.button('Открыть папку результатов', self.open_folder, bottom)
        self.button('Править выбранный клип', self.edit_result, bottom)
        self.button('Открыть отчёт', self.open_report, bottom)
        bottom.addStretch()
        outer.addLayout(bottom)

    @staticmethod
    def button(text, callback, layout):
        button = QPushButton(text)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def setting(self, key, default, value_type=None):
        if value_type is None:
            return self.settings.value('auto_reels/' + key, default)
        return self.settings.value('auto_reels/' + key, default, type=value_type)

    def combo(self, items, key, default):
        combo = QComboBox()
        for label, value in items:
            combo.addItem(label, value)
        combo.setCurrentIndex(max(0, combo.findData(self.setting(key, default))))
        return combo

    @property
    def busy(self):
        return self.task is not None

    def source_paths(self):
        return [self.sources.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.sources.count())]

    def add_paths(self, paths):
        if self.busy:
            return
        existing = {os.path.normcase(p) for p in self.source_paths()}
        for value in paths:
            path = Path(value).resolve()
            if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            key = os.path.normcase(str(path))
            if key in existing:
                continue
            item = QListWidgetItem(path.name)
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            item.setToolTip(str(path))
            self.sources.addItem(item)
            existing.add(key)
        self.summary.setText(f'Исходников: {self.sources.count()}. MP4, MOV, MKV, WebM и другие форматы. '
                             'Размеры и поворот телефона определяются автоматически.')

    def choose_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, 'Добавить исходники', '', VIDEO_FILTER)
        self.add_paths(paths)

    def apply_profile(self):
        profile = self.profile.currentData()
        if profile == 'custom':
            return
        self.selection.setCurrentIndex(self.selection.findData('reactions' if profile == 'hearthstone' else 'speech'))
        self.layout_mode.setCurrentIndex(self.layout_mode.findData('gaming' if profile == 'hearthstone' else 'fit'))
        self.clip_length.setValue(45 if profile in ('hearthstone', 'screen') else 30)
        self.captions.setChecked(True)
        self.zoom.setChecked(False)
        self.vocabulary.setText('Hearthstone, Хартстоун, таверна, Боб, поля сражений, боевой клич, предсмертный хрип'
                                if profile == 'hearthstone' else '')
        self.status.setText('Игровой профиль: настрой отдельные области игры и вебки. Реакции нужно просмотреть.'
                            if profile == 'hearthstone' else 'Профиль применён. Для границ реплик будет распознана вся выбранная часть записи.')

    def configure_gaming(self):
        if self.busy:
            return
        item = self.sources.currentItem()
        path = item.data(Qt.ItemDataRole.UserRole) if item else next(iter(self.source_paths()), None)
        if not path:
            self.status.setText('Сначала добавь запись, чтобы выделить игру и вебку на её кадре.')
            return
        self.layout_requested.emit(path)

    def set_gaming_template(self, project, width, height):
        from dataclasses import astuple
        values = {'game_crop': astuple(project.main), 'webcam_crop': astuple(project.webcam),
                  'webcam_fraction': project.webcam_fraction, 'source_aspect': width / height}
        AutoReelsOptions(layout=project.layout, **values).validate()
        self.gaming_template = values
        self.settings.setValue('auto_reels/gaming_template', json.dumps(values))
        self.settings.setValue('auto_reels/layout', project.layout)
        self.layout_mode.setCurrentIndex(self.layout_mode.findData(project.layout))
        self.status.setText('Игра и вебка настроены отдельно. Шаблон сохранён для записей с таким же расположением областей.')

    def choose_twitch(self):
        if self.busy:
            return
        dialog = TwitchImportDialog(self.settings, VIDEO_EXTENSIONS, self)
        try:
            if dialog.exec() != TwitchImportDialog.DialogCode.Accepted:
                return
            if self.busy or not dialog.selected_path or dialog.selected_source is None:
                return
            self.add_paths([dialog.selected_path])
            key = os.path.normcase(dialog.selected_path)
            for index in range(self.sources.count()):
                item = self.sources.item(index)
                path = item.data(Qt.ItemDataRole.UserRole)
                if os.path.normcase(path) == key:
                    metadata = dialog.selected_source.to_dict()
                    item.setData(int(Qt.ItemDataRole.UserRole) + 1, metadata)
                    item.setToolTip(path + '\nTwitch · источник: ' + dialog.selected_source.canonical_url)
                    self.status.setText('Запись Twitch добавлена из локального файла. Проверь длину клипов и создай подборку.')
                    break
        finally:
            dialog.deleteLater()

    def choose_folder(self):
        name = QFileDialog.getExistingDirectory(self, 'Добавить видео из папки')
        if name:
            self.add_paths(sorted(Path(name).iterdir()))

    def remove_selected(self):
        if not self.busy:
            for item in self.sources.selectedItems():
                self.sources.takeItem(self.sources.row(item))
            self.add_paths([])

    def choose_output(self):
        name = QFileDialog.getExistingDirectory(self, 'Папка для подборок', self.output.text())
        if name:
            self.output.setText(name)

    def dragEnterEvent(self, event):
        if not self.busy and event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        self.add_paths([url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()])
        event.acceptProposedAction()

    def start(self):
        if self.busy:
            return
        if not self.source_paths():
            self.status.setText('Добавь хотя бы одно видео.')
            return
        if not self.output.text().strip():
            self.status.setText('Выбери папку для готовых клипов.')
            return
        options = AutoReelsOptions(
            clip_length=self.clip_length.value(), count=self.count.value(),
            captions=self.captions.isChecked(), model=self.model.currentData(),
            caption_style=self.caption_style.currentData(),
            language=self.language.currentData(), layout=self.layout_mode.currentData(),
            zoom=self.zoom.isChecked(), device='cpu',
            selection=self.selection.currentData(), vocabulary=self.vocabulary.text().strip(),
            skip_start=self.skip_start.value(), skip_end=self.skip_end.value(),
            **{k: v for k, v in (self.gaming_template or {}).items()
               if k in {'game_crop', 'webcam_crop', 'webcam_fraction', 'source_aspect'}},
        )
        try:
            options.validate()
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        for key, value in {'clip_length': options.clip_length, 'count': options.count,
                           'captions': options.captions, 'model': options.model,
                           'caption_style': options.caption_style,
                           'language': options.language, 'layout': options.layout,
                           'zoom': options.zoom, 'output': self.output.text().strip(),
                           'selection': options.selection, 'vocabulary': options.vocabulary,
                           'skip_start': options.skip_start, 'skip_end': options.skip_end}.items():
            self.settings.setValue('auto_reels/' + key, value)
        self.results.clear()
        self.log.clear()
        self.report_path = ''
        self.progress.setValue(0)
        self.status.setText('Подготовка исходников…')
        self.task = AutoReelsTask(self.source_paths(), Path(self.output.text().strip()), options, self)
        self.task.progress.connect(self.on_progress)
        self.task.done.connect(self.completed)
        self.task.failed.connect(self.failed)
        self.task.finished.connect(self.idle)
        self.set_busy(True)
        self.task.start()

    def set_busy(self, busy):
        for control in (self.controls, self.output, self.output_button, self.start_button):
            control.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)
        self.busy_changed.emit(busy)

    def on_progress(self, value, text):
        self.progress.setValue(value)
        self.status.setText(text)

    def cancel(self):
        if self.task:
            self.task.requestInterruption()
            self.status.setText('Останавливаю обработку. Уже готовые клипы сохранятся. '
                                'Загрузка модели может завершиться не сразу.')

    def completed(self, result):
        self.report_path = result.report_path
        for path in result.outputs:
            item = QListWidgetItem(Path(path).name)
            item.setData(Qt.ItemDataRole.UserRole, path)
            item.setToolTip(path)
            self.results.addItem(item)
        if self.results.count():
            self.results.setCurrentRow(0)
        lines = list(result.warnings)
        lines.extend(f'Ошибка · {Path(f.source).name}: {f.error}' for f in result.failures)
        lines.append('Отчёт: ' + result.report_path)
        self.log.setPlainText('\n\n'.join(lines))
        prefix = 'Остановлено' if result.cancelled else 'Подборка готова'
        self.status.setText(f'{prefix}: {len(result.outputs)} клипов, ошибок: {len(result.failures)}. '
                            'Двойной клик — просмотр; «Править» — субтитры и кадрирование.')
        if not result.cancelled:
            self.progress.setValue(100)

    def failed(self, error):
        self.status.setText('Не удалось начать обработку: ' + error[:240])
        self.log.setPlainText(error)

    def idle(self):
        task, self.task = self.task, None
        self.set_busy(False)
        if task is not None:
            task.deleteLater()

    def open_result(self, item=None):
        item = item or self.results.currentItem()
        if item:
            QDesktopServices.openUrl(QUrl.fromLocalFile(item.data(Qt.ItemDataRole.UserRole)))

    def edit_result(self):
        if self.busy:
            return
        item = self.results.currentItem()
        if item:
            project = Path(item.data(Qt.ItemDataRole.UserRole)).with_suffix('.verticliv.json')
            if project.is_file():
                self.edit_requested.emit(str(project))
            else:
                self.status.setText('Файл проекта не найден. Открой клип в ручном редакторе.')

    def open_folder(self):
        directory = Path(self.report_path).parent if self.report_path else Path(self.output.text())
        if directory.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory.resolve())))

    def open_report(self):
        if self.report_path and Path(self.report_path).is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.report_path))

    def open_review(self):
        if self.report_path:
            path = Path(self.report_path).parent / 'review.html'
            if path.is_file():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
