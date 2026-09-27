"""Explicit, local-only import of a recording downloaded by its Twitch owner."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout,
)

from .twitch_source import parse_twitch_source


TWITCH_URL_SETTING = 'auto_reels/twitch_source_url'


class TwitchImportDialog(QDialog):
    """Do not imply an OAuth connection or access to private recordings."""

    def __init__(self, settings, video_extensions, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.video_extensions = frozenset(video_extensions)
        self.selected_path = ''
        self.selected_source = None
        self.setWindowTitle('Twitch · ручной импорт записи')
        self.setMinimumWidth(540)
        layout = QVBoxLayout(self)
        intro = QLabel(
            'Скачай свою запись через видеостудию Twitch, затем добавь файл сюда. '
            'Verticliv создаст локальные клипы и ничего не опубликует.'
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        notice = QLabel(
            'Это ручной импорт, а не подключение аккаунта Twitch. '
            'Пароли и cookies не запрашиваются. Неопубликованную запись можно '
            'скачать из своей видеостудии — публиковать её для этого не нужно.'
        )
        notice.setWordWrap(True)
        layout.addWidget(notice)
        layout.addWidget(QLabel('Ссылка на запись или свою видеостудию Twitch'))
        self.url = QLineEdit()
        self.url.setPlaceholderText('https://dashboard.twitch.tv/u/канал/content/video-producer')
        # Only restore a URL if it passes the same strict check as new input.
        saved = str(settings.value(TWITCH_URL_SETTING, ''))
        if saved:
            try:
                self.url.setText(parse_twitch_source(saved).canonical_url)
            except ValueError:
                pass
        layout.addWidget(self.url)
        self.open_button = QPushButton('1 · Открыть Twitch в браузере')
        self.open_button.clicked.connect(self.open_twitch)
        layout.addWidget(self.open_button)
        hint = QLabel(
            'В браузере войди в Twitch самостоятельно. В меню нужной записи '
            'выбери «Скачать» и дождись окончания загрузки.'
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.choose_button = QPushButton('2 · Выбрать скачанное видео')
        self.choose_button.clicked.connect(self.choose_download)
        layout.addWidget(self.choose_button)
        self.status = QLabel('Используй только свои записи или материал, на который у тебя есть разрешение.')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton('Отмена')
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)

    def validated_source(self):
        try:
            return parse_twitch_source(self.url.text().strip())
        except ValueError:
            self.status.setText('Нужна корректная ссылка Twitch на запись или видеостудию канала.')
            return None

    def remember(self, source):
        self.settings.setValue(TWITCH_URL_SETTING, source.canonical_url)
        self.url.setText(source.canonical_url)

    def open_twitch(self):
        source = self.validated_source()
        if source is None:
            return
        try:
            opened = QDesktopServices.openUrl(QUrl(source.canonical_url))
        except Exception:
            opened = False
        if not opened:
            self.status.setText('Не удалось открыть браузер. Открой проверенную ссылку вручную и скачай запись.')
            return
        self.remember(source)
        self.status.setText('После окончания загрузки выбери скачанный файл. Вход в Twitch остаётся только в браузере.')

    def choose_download(self):
        source = self.validated_source()
        if source is None:
            return
        video_filter = 'Видео (' + ' '.join('*' + s for s in sorted(self.video_extensions)) + ')'
        name, _ = QFileDialog.getOpenFileName(self, 'Скачанная запись Twitch', '', video_filter)
        if not name:
            return
        path = Path(name).resolve()
        if not path.is_file() or path.suffix.lower() not in self.video_extensions:
            self.status.setText('Выбери полностью скачанный видеофайл: MP4, MKV, MOV, WebM или другой поддерживаемый формат.')
            return
        self.selected_path = str(path)
        self.selected_source = source
        self.remember(source)
        self.accept()
