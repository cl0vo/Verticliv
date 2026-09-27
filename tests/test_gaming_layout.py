import os
import shutil
import subprocess

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication
from arara_factory.auto_reels import AutoReelsOptions
from arara_factory.auto_reels_ui import AutoReelsPanel
from arara_factory.studio_engine import StudioProject, Crop, gaming_panels, layout_graph, export_video


def test_batch_requires_explicit_areas():
    with pytest.raises(ValueError, match='две области'):
        AutoReelsOptions(layout='gaming_fit').validate()
    options = AutoReelsOptions(layout='gaming_fit', game_crop=(.2, 0, .8, 1),
                              webcam_crop=(0, .3, .2, .3), source_aspect=16/9)
    options.validate()
    assert gaming_panels(StudioProject(webcam_fraction=.32)) == (614, 180, 1126)


def test_gaming_graph_crops_camera_and_game_independently():
    project = StudioProject(layout='gaming_fit', main=Crop(.5, 0, .5, 1), webcam=Crop(0, 0, .5, 1))
    graph = layout_graph(project, 640, 360)
    assert '[cam]crop=320:360:0:0' in graph
    assert '[game]crop=320:360:320:0' in graph
    assert graph.count('force_original_aspect_ratio=decrease') == 2
    assert 'vstack=inputs=2' in graph


def test_template_persists_without_source_file_or_account(tmp_path):
    app = QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / 'ui.ini'), QSettings.Format.IniFormat)
    panel = AutoReelsPanel(settings=settings)
    project = StudioProject(layout='gaming_fit', main=Crop(.2, 0, .8, 1), webcam=Crop(0, .3, .2, .3), webcam_fraction=.32)
    panel.set_gaming_template(project, 1600, 900)
    restored = AutoReelsPanel(settings=settings)
    assert restored.gaming_template['game_crop'] == [.2, 0, .8, 1]
    assert restored.gaming_template['webcam_crop'] == [0, .3, .2, .3]
    assert panel.layout_mode.currentData() == 'gaming_fit'
    AutoReelsOptions(layout='gaming_fit', **restored.gaming_template).validate()
    panel.close()
    restored.close()


def test_region_switch_does_not_revert_contained_layout():
    from arara_factory.studio_ui import StudioWindow
    app = QApplication.instance() or QApplication([])
    window = StudioWindow()
    window.layout_mode.setCurrentIndex(window.layout_mode.findData('gaming_fit'))
    window.region.setCurrentIndex(window.region.findData('webcam'))
    assert window.layout_mode.currentData() == 'gaming_fit'
    assert window.project.caption_y == gaming_panels(window.project)[0] + 130
    window.close()


def test_batch_uses_saved_regions_and_rejects_other_aspect(tmp_path, monkeypatch):
    from arara_factory import auto_reels
    from arara_factory.render import MediaInfo
    source = tmp_path/'recording.mp4'
    source.write_bytes(b'source')
    made = []
    monkeypatch.setattr(auto_reels, 'source_info', lambda _: MediaInfo(1600, 900, 12, 30, False))
    def export(project, path, *args):
        made.append(project)
        path.write_bytes(b'video')
    monkeypatch.setattr(auto_reels, 'export_video', export)
    options = AutoReelsOptions(layout='gaming_fit', captions=False, game_crop=(.2, 0, .8, 1),
                              webcam_crop=(0, .3, .2, .3), source_aspect=16/9, webcam_fraction=.38)
    result = auto_reels.run_auto_reels([str(source)], tmp_path/'out', options)
    assert result.outputs and not result.failures
    assert made[0].main == Crop(.2, 0, .8, 1)
    assert made[0].webcam == Crop(0, .3, .2, .3)
    assert made[0].caption_y == gaming_panels(made[0])[0] + 130
    monkeypatch.setattr(auto_reels, 'source_info', lambda _: MediaInfo(900, 1600, 12, 30, False))
    failed = auto_reels.run_auto_reels([str(source)], tmp_path/'out', options)
    assert not failed.outputs
    assert 'Пропорции' in failed.failures[0].error


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='FFmpeg required')
def test_real_camera_and_game_occupy_separate_panels(tmp_path):
    from PIL import Image
    source, output, frame = tmp_path/'source.mp4', tmp_path/'split.mp4', tmp_path/'frame.png'
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                    'color=red:s=320x360:r=30', '-f', 'lavfi', '-i', 'color=blue:s=320x360:r=30',
                    '-filter_complex', '[0:v][1:v]hstack', '-t', '1', '-c:v', 'libx264', str(source)], check=True)
    project = StudioProject(source=str(source), end=.5, layout='gaming_fit', captions=False,
                            main=Crop(.5, 0, .5, 1), webcam=Crop(0, 0, .5, 1), webcam_fraction=.32)
    export_video(project, output)
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(output), '-frames:v', '1', str(frame)], check=True)
    with Image.open(frame) as image:
        red = image.getpixel((540, 300))
        blue = image.getpixel((540, 1400))
        band = image.getpixel((540, 700))
    assert red[0] > 180 and red[2] < 50
    assert blue[2] > 180 and blue[0] < 50
    assert max(band) < 60
