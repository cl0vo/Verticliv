import os
import shutil
import subprocess

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication
from arara_factory.auto_reels import AutoReelsOptions
from arara_factory.auto_reels_ui import AutoReelsPanel
from arara_factory.studio_engine import (
    StudioProject, Crop, gaming_panels, layout_graph, export_video,
    natural_webcam_fraction, gaming_caption_y, fitted_crop,
)


def test_batch_requires_explicit_areas():
    with pytest.raises(ValueError, match='две области'):
        AutoReelsOptions(layout='gaming_fit').validate()
    options = AutoReelsOptions(layout='gaming_fit', game_crop=(.2, 0, .8, 1),
                              webcam_crop=(0, .3, .2, .3), source_aspect=16/9)
    options.validate()
    assert gaming_panels(StudioProject(layout='gaming_fit', webcam_fraction=.32)) == (614, 180, 1126)


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


@pytest.mark.parametrize('layout', ['gaming', 'gaming_fit'])
def test_batch_uses_saved_regions_and_rejects_other_aspect(tmp_path, monkeypatch, layout):
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
    options = AutoReelsOptions(layout=layout, captions=False, game_crop=(.2, 0, .8, 1),
                              webcam_crop=(0, .3, .2, .3), source_aspect=16/9, webcam_fraction=.38)
    result = auto_reels.run_auto_reels([str(source)], tmp_path/'out', options)
    assert result.outputs and not result.failures
    assert made[0].main == Crop(.2, 0, .8, 1)
    assert made[0].webcam == Crop(0, .3, .2, .3)
    assert made[0].caption_y == gaming_caption_y(made[0])
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


@pytest.mark.parametrize('crop,expected', [
    (Crop(0, 0, 1, 1), .32),
    (Crop(0, .315, .195, .27), .44),
    (Crop(0, 0, .1, 1), .5),
    (Crop(0, 0, 1, .1), .15),
])
def test_camera_height_respects_native_aspect_and_limits(crop, expected):
    assert natural_webcam_fraction(crop, 1600, 900) == expected


def test_selecting_webcam_sizes_edge_to_edge_layout():
    from types import SimpleNamespace
    from arara_factory.studio_ui import StudioWindow
    app = QApplication.instance() or QApplication([])
    window = StudioWindow()
    window.info = SimpleNamespace(width=1600, height=900)
    window.layout_mode.setCurrentIndex(window.layout_mode.findData('gaming'))
    window.set_crop('webcam', Crop(0, .315, .195, .27))
    assert window.cam_size.value() == 44
    assert window.project.caption_y == gaming_panels(window.project)[0] - 28
    window.cam_size.setValue(35)
    assert window.project.webcam_fraction == .35
    window.fit_webcam_height()
    assert window.cam_size.value() == 44
    window.close()


@pytest.mark.parametrize('fraction', [.15, .32, .44, .5])
def test_cover_geometry_preserves_aspect_with_pixel_rounding(fraction):
    project = StudioProject(layout='gaming', webcam_fraction=fraction)
    cam_h, gap, game_h = gaming_panels(project)
    assert gap == 0 and cam_h + game_h == 1920
    for crop, panel_h in [(Crop(0, .315, .195, .27), cam_h), (Crop(.195, 0, .805, 1), game_h)]:
        x, y, w, h = fitted_crop(crop, 1600, 900, 1080/panel_h)
        assert abs(w - h * 1080/panel_h) < 6
        sx, sy, sw, sh = crop.pixels(1600, 900)
        assert sx <= x and sy <= y and x+w <= sx+sw and y+h <= sy+sh


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='FFmpeg required')
def test_real_edge_to_edge_panels_match_preview_without_gaps(tmp_path):
    from PIL import Image
    from PySide6.QtGui import QImage
    from arara_factory.studio_ui import OutputCanvas
    source, output, frame = tmp_path/'source.mp4', tmp_path/'cover.mp4', tmp_path/'frame.png'
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                    'color=red:s=320x360:r=30', '-f', 'lavfi', '-i', 'color=blue:s=320x360:r=30',
                    '-filter_complex', '[0:v][1:v]hstack', '-t', '1', '-c:v', 'libx264', str(source)], check=True)
    project = StudioProject(source=str(source), end=.5, layout='gaming', captions=False,
                            main=Crop(.5, 0, .5, 1), webcam=Crop(0, 0, .5, 1), webcam_fraction=.44)
    export_video(project, output)
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(output), '-frames:v', '1', str(frame)], check=True)
    app = QApplication.instance() or QApplication([])
    preview = OutputCanvas()
    preview.resize(1080, 1920)
    preview.project = project
    picture = QImage(640, 360, QImage.Format.Format_RGB32)
    picture.fill(Qt.GlobalColor.blue)
    from PySide6.QtGui import QPainter
    painter = QPainter(picture)
    painter.fillRect(0, 0, 320, 360, Qt.GlobalColor.red)
    painter.end()
    preview.frame = picture
    shown = preview.grab().toImage()
    seam = gaming_panels(project)[0]
    with Image.open(frame) as exported:
        assert exported.size == (1080, 1920)
        for x in [0, 5, 540, 1074, 1079]:
            for y in [0, 5, seam-2, seam-1, seam, seam+1, 1914, 1919]:
                expected = 0 if y < seam else 2
                actual = exported.getpixel((x, y))
                ui = shown.pixelColor(x, y).getRgb()[:3]
                assert actual[expected] > 200 and ui[expected] > 200
                assert actual[2-expected] < 50 and ui[2-expected] < 50
    preview.close()
