"""Manual Twitch import never connects an account or downloads via credentials."""
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication
from arara_factory import twitch_import_ui as ui
from arara_factory import auto_reels_ui as batch_ui
from arara_factory.twitch_source import parse_twitch_source

URL = 'https://dashboard.twitch.tv/u/levakiselev/content/video-producer/edit/2884988010'


@pytest.fixture
def dialog(tmp_path):
    app = QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / 'test.ini'), QSettings.Format.IniFormat)
    result = ui.TwitchImportDialog(settings, {'.mp4', '.mkv'})
    result.url.setText(URL)
    yield result
    result.close()


def test_open_only_validated_twitch_url(dialog, monkeypatch):
    opened = []
    monkeypatch.setattr(ui.QDesktopServices, 'openUrl', lambda u: opened.append(u.toString()) or True)
    dialog.open_twitch()
    assert opened == [URL]
    assert dialog.settings.value(ui.TWITCH_URL_SETTING) == URL
    dialog.url.setText('https://twitch.tv.evil.test/videos/123')
    dialog.open_twitch()
    assert opened == [URL]
    assert dialog.settings.value(ui.TWITCH_URL_SETTING) == URL


def test_browser_failure_not_saved(dialog, monkeypatch):
    monkeypatch.setattr(ui.QDesktopServices, 'openUrl', lambda _: False)
    dialog.open_twitch()
    assert 'Не удалось' in dialog.status.text()
    assert not dialog.settings.contains(ui.TWITCH_URL_SETTING)


def test_cancelled_download_selection_leaves_dialog_unaccepted(dialog, monkeypatch):
    monkeypatch.setattr(ui.QFileDialog, 'getOpenFileName', lambda *a: ('', ''))
    dialog.choose_download()
    assert not dialog.selected_path and dialog.selected_source is None
    assert not dialog.settings.contains(ui.TWITCH_URL_SETTING)


@pytest.mark.parametrize('name', ['unfinished.mp4.part', 'notes.txt', 'missing.mp4'])
def test_invalid_download_not_accepted(dialog, monkeypatch, tmp_path, name):
    path = tmp_path / name
    if name != 'missing.mp4':
        path.write_bytes(b'not video')
    monkeypatch.setattr(ui.QFileDialog, 'getOpenFileName', lambda *a: (str(path), ''))
    dialog.choose_download()
    assert not dialog.selected_path
    assert dialog.result() != ui.QDialog.DialogCode.Accepted


def test_selected_file_is_referenced_not_modified(dialog, monkeypatch, tmp_path):
    path = tmp_path / 'vod.MP4'
    path.write_bytes(b'local source')
    monkeypatch.setattr(ui.QFileDialog, 'getOpenFileName', lambda *a: (str(path), ''))
    dialog.choose_download()
    assert dialog.selected_path == str(path.resolve())
    assert dialog.selected_source.vod_id == '2884988010'
    assert dialog.result() == ui.QDialog.DialogCode.Accepted
    assert path.read_bytes() == b'local source'


def test_panel_deduplicates_and_blocks_import_while_busy(dialog, monkeypatch, tmp_path):
    path = tmp_path / 'vod.mp4'
    path.write_bytes(b'local source')
    opened = []

    class FakeDialog:
        DialogCode = ui.QDialog.DialogCode
        selected_path = str(path.resolve())
        selected_source = parse_twitch_source(URL)

        def __init__(self, *args):
            opened.append(True)

        def exec(self):
            return self.DialogCode.Accepted

        def deleteLater(self):
            pass

    monkeypatch.setattr(batch_ui, 'TwitchImportDialog', FakeDialog)
    panel = batch_ui.AutoReelsPanel(settings=dialog.settings)
    panel.choose_twitch()
    panel.choose_twitch()
    assert panel.sources.count() == 1
    item = panel.sources.item(0)
    assert item.data(int(Qt.ItemDataRole.UserRole) + 1)['vod_id'] == '2884988010'
    assert URL in item.toolTip()
    panel.task = object()
    panel.choose_twitch()
    assert len(opened) == 2
    panel.task = None
    panel.close()
