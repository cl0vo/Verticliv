"""No credentials, network access or real user preferences in GUI checks."""
import os
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication

from arara_factory.auto_reels import BatchFailure, BatchResult
from arara_factory.auto_reels_ui import AutoReelsPanel
from arara_factory.studio_ui import StudioWindow


def panel(tmp_path):
    app = QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / 'test.ini'), QSettings.Format.IniFormat)
    return app, AutoReelsPanel(settings=settings)


def test_import_multiple_formats_deduplicates_and_preserves_files(tmp_path):
    app, w = panel(tmp_path)
    paths = [tmp_path / name for name in ('phone.MOV', 'stream.mkv', 'note.txt')]
    for path in paths:
        path.write_bytes(b'test')
    w.add_paths(paths + paths)
    assert w.sources.count() == 2
    assert set(w.source_paths()) == {str(p) for p in paths[:2]}
    w.sources.item(0).setSelected(True)
    w.remove_selected()
    assert w.sources.count() == 1
    assert all(path.read_bytes() == b'test' for path in paths)
    w.close()


def test_result_warnings_and_editable_project(tmp_path):
    app, w = panel(tmp_path)
    output = tmp_path / 'clip.mp4'
    project = output.with_suffix('.verticliv.json')
    project.write_text('{}')
    result = BatchResult(outputs=[str(output)], failures=[BatchFailure('broken.mov', 'bad media')],
                         warnings=['No speech'], report_path=str(tmp_path / 'report.json'), cancelled=True)
    w.completed(result)
    assert w.results.count() == 1
    assert 'Остановлено' in w.status.text()
    assert 'No speech' in w.log.toPlainText()
    assert 'broken.mov' in w.log.toPlainText()
    requested = []
    w.edit_requested.connect(requested.append)
    w.edit_result()
    assert requested == [str(project)]
    w.close()


def test_empty_batch_cannot_start(tmp_path):
    app, w = panel(tmp_path)
    w.start()
    assert not w.busy
    assert 'Добавь' in w.status.text()
    w.close()


def test_material_profiles_and_local_speech_defaults(tmp_path):
    app, w = panel(tmp_path)
    assert w.selection.currentData() == 'speech'
    w.profile.setCurrentIndex(w.profile.findData('hearthstone'))
    assert w.selection.currentData() == 'reactions'
    assert w.layout_mode.currentData() == 'gaming'
    assert w.clip_length.value() == 45
    assert 'Hearthstone' in w.vocabulary.text()
    assert not w.zoom.isChecked()
    w.profile.setCurrentIndex(w.profile.findData('speech'))
    assert w.selection.currentData() == 'speech'
    assert w.clip_length.value() == 30
    assert w.vocabulary.text() == ''
    w.close()


def test_studio_starts_in_auto_workspace_and_locks_editor():
    app = QApplication.instance() or QApplication([])
    w = StudioWindow()
    assert w.windowTitle().startswith('Verticliv ')
    assert w.workspaces.currentIndex() == 0
    w.auto_busy_changed(True)
    assert not w.workspaces.isTabEnabled(1)
    w.auto_busy_changed(False)
    assert w.workspaces.isTabEnabled(1)
    w.close()
