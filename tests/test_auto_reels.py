import json
import shutil
import subprocess
from pathlib import Path

import pytest

from arara_factory import auto_reels
from arara_factory.auto_reels import AutoReelsOptions, run_auto_reels
from arara_factory.render import MediaInfo, probe_media
from arara_factory.studio_engine import Cancelled, Highlight, StudioProject
from arara_factory.transcribe import RecognizedWord


@pytest.fixture
def source(tmp_path):
    path = tmp_path / 'исходник.mov'
    path.write_bytes(b'input')
    return path


@pytest.fixture
def fake_export(monkeypatch):
    exported = []

    def export(project, target, progress, cancel):
        if cancel():
            raise Cancelled('Операция отменена')
        exported.append(project)
        target.write_bytes(b'validated video')
        if project.captions:
            target.with_suffix('.srt').write_text(' '.join(w.text for w in project.words), encoding='utf-8')
        progress(100, 'Готово')
        return str(target)

    monkeypatch.setattr(auto_reels, 'export_video', export)
    return exported


def test_batch_uses_best_highlights_and_saves_corrected_project(source, tmp_path, monkeypatch, fake_export):
    monkeypatch.setattr(auto_reels, 'source_info', lambda _: MediaInfo(1920, 1080, 120, 30, True))
    monkeypatch.setattr(auto_reels, 'find_highlights', lambda *a, **kw: [
        Highlight(0, 20, 10, .3), Highlight(30, 50, 40, .9), Highlight(70, 90, 80, .6),
    ])
    recognized = []

    def recognize(project, progress, cancel):
        recognized.append((project.start, project.end))
        return [RecognizedWord('Привет', project.start + 1, project.start + 2, .9)]

    monkeypatch.setattr(auto_reels, 'transcribe', recognize)
    progress_values = []
    result = run_auto_reels([str(source)], tmp_path / 'out', AutoReelsOptions(count=2),
                            lambda n, text: progress_values.append(n))
    assert recognized == [(30, 50), (70, 90)]
    assert len(result.outputs) == 2 and not result.failures
    assert all(p.layout == 'fit' and p.language == 'auto' for p in fake_export)
    for output in result.outputs:
        project = StudioProject.load(Path(output).with_suffix('.verticliv.json'))
        assert project.words[0].text == 'Привет'
        assert project.transcript_ranges == [[project.start, project.end]]
        assert Path(output).with_suffix('.srt').is_file()
    report = json.loads(Path(result.report_path).read_text(encoding='utf-8'))
    assert report['status'] == 'completed'
    assert report['outputs'] == result.outputs
    assert len(report['clips']) == 2
    assert progress_values == sorted(progress_values)
    assert progress_values[-1] == 100
    assert source.read_bytes() == b'input'


@pytest.mark.parametrize('audio', [False, True])
def test_fallback_is_non_overlapping_and_explicit_about_missing_speech(source, tmp_path, monkeypatch, fake_export, audio):
    monkeypatch.setattr(auto_reels, 'source_info', lambda _: MediaInfo(720, 1280, 75, 30, audio))
    monkeypatch.setattr(auto_reels, 'find_highlights', lambda *a, **kw: [])
    monkeypatch.setattr(auto_reels, 'transcribe', lambda *a: [])
    result = run_auto_reels([str(source)], tmp_path / 'out')
    assert len(result.outputs) == 2
    assert fake_export[0].end <= fake_export[1].start
    assert all(p.layout == 'fill' for p in fake_export)
    assert any('равномерные' in warning for warning in result.warnings)
    assert any('SRT будет пустым' in warning for warning in result.warnings)
    assert all(Path(output).with_suffix('.srt').read_text(encoding='utf-8') == '' for output in result.outputs)


def test_bad_source_and_one_failed_clip_do_not_discard_success(source, tmp_path, monkeypatch, fake_export):
    monkeypatch.setattr(auto_reels, 'source_info', lambda _: MediaInfo(640, 360, 90, 30, True))
    monkeypatch.setattr(auto_reels, 'find_highlights', lambda *a, **kw: [Highlight(0, 30, 15, .8), Highlight(60, 90, 75, .7)])

    def recognize(project, progress, cancel):
        if project.start == 0:
            raise RuntimeError('Не удалось загрузить модель речи')
        return [RecognizedWord('Готово', 61, 62, 1)]

    monkeypatch.setattr(auto_reels, 'transcribe', recognize)
    result = run_auto_reels([str(tmp_path / 'missing.mp4'), str(source)], tmp_path / 'out')
    assert len(result.failures) == 2
    assert 'не найдено' in result.failures[0].error
    assert 'модель речи' in result.failures[1].error
    assert len(result.outputs) == 1
    report = json.loads(Path(result.report_path).read_text(encoding='utf-8'))
    assert report['status'] == 'completed_with_errors'
    assert len(report['clips']) == 1 and report['clips'][0]['start'] == 60


def test_cancel_keeps_completed_outputs_and_persisted_manifest(source, tmp_path, monkeypatch):
    monkeypatch.setattr(auto_reels, 'source_info', lambda _: MediaInfo(640, 360, 90, 30, True))
    monkeypatch.setattr(auto_reels, 'find_highlights', lambda *a, **kw: [Highlight(0, 30, 15, .8), Highlight(60, 90, 75, .7)])
    cancelled = False

    def export(project, target, progress, cancel):
        nonlocal cancelled
        target.write_bytes(b'complete')
        cancelled = True
        return str(target)

    monkeypatch.setattr(auto_reels, 'export_video', export)
    result = run_auto_reels([str(source)], tmp_path / 'out', AutoReelsOptions(captions=False), cancel=lambda: cancelled)
    assert result.cancelled and len(result.outputs) == 1
    assert Path(result.outputs[0]).read_bytes() == b'complete'
    report = json.loads(Path(result.report_path).read_text(encoding='utf-8'))
    assert report['status'] == 'cancelled'
    assert report['outputs'] == result.outputs
    assert not result.failures


def test_runs_and_duplicate_stems_do_not_overwrite(source, tmp_path, monkeypatch, fake_export):
    monkeypatch.setattr(auto_reels, 'source_info', lambda _: MediaInfo(640, 360, 12, 30, False))
    second = tmp_path / 'another' / source.name
    second.parent.mkdir()
    second.write_bytes(b'other input')
    first = run_auto_reels([str(source), str(second)], tmp_path / 'out', AutoReelsOptions(captions=False))
    repeated = run_auto_reels([str(source)], tmp_path / 'out', AutoReelsOptions(captions=False))
    assert len(set(first.outputs + repeated.outputs)) == 3
    assert Path(first.report_path).parent != Path(repeated.report_path).parent
    assert all(Path(output).is_file() for output in first.outputs + repeated.outputs)


@pytest.mark.parametrize('options', [
    AutoReelsOptions(count=0), AutoReelsOptions(count=13), AutoReelsOptions(count=1.5),
    AutoReelsOptions(clip_length=9), AutoReelsOptions(clip_length=float('nan')),
    AutoReelsOptions(layout='unknown'), AutoReelsOptions(device='unknown'),
])
def test_bad_options_fail_before_creating_output(source, tmp_path, options):
    destination = tmp_path / 'out'
    with pytest.raises(ValueError):
        run_auto_reels([str(source)], destination, options)
    assert not destination.exists()


def test_empty_batch_fails_before_creating_output(tmp_path):
    with pytest.raises(ValueError, match='исходное видео'):
        run_auto_reels([], tmp_path / 'out')
    assert not (tmp_path / 'out').exists()


def test_export_without_output_is_not_reported_as_success(source, tmp_path, monkeypatch):
    monkeypatch.setattr(auto_reels, 'source_info', lambda _: MediaInfo(640, 360, 12, 30, False))
    monkeypatch.setattr(auto_reels, 'export_video', lambda *a: None)
    result = run_auto_reels([str(source)], tmp_path / 'out', AutoReelsOptions(captions=False))
    assert result.outputs == []
    assert len(result.failures) == 1
    assert 'не создал' in result.failures[0].error


def test_batch_requests_all_twelve_highlights(source, tmp_path, monkeypatch, fake_export):
    monkeypatch.setattr(auto_reels, 'source_info', lambda _: MediaInfo(640, 360, 360, 30, True))
    requested = []

    def highlights(*args, count):
        requested.append(count)
        return [Highlight(i * 30, (i + 1) * 30, i * 30 + 15, .5) for i in range(count)]

    monkeypatch.setattr(auto_reels, 'find_highlights', highlights)
    result = run_auto_reels([str(source)], tmp_path / 'out', AutoReelsOptions(count=12, captions=False))
    assert requested == [12]
    assert len(result.outputs) == 12
    assert not result.failures


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='FFmpeg required')
def test_real_batch_accepts_mixed_formats_and_retains_successes(tmp_path):
    wide = tmp_path / 'wide.mkv'
    portrait = tmp_path / 'portrait.mov'
    for source, dimensions, audio in [(wide, '640x360', False), (portrait, '360x640', True)]:
        args = ['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', f'testsrc2=size={dimensions}:rate=24']
        if audio:
            args += ['-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000']
        subprocess.run([*args, '-t', '2', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(source)], check=True)
    result = run_auto_reels(
        [str(wide), str(tmp_path / 'missing.mp4'), str(portrait)],
        tmp_path / 'out', AutoReelsOptions(clip_length=10, captions=False),
    )
    assert len(result.outputs) == 2
    assert len(result.failures) == 1
    assert not result.cancelled
    for output, audio, layout in zip(result.outputs, [False, True], ['fit', 'fill']):
        info = probe_media(shutil.which('ffprobe'), Path(output))
        assert (info.width, info.height, info.fps, info.has_audio) == (1080, 1920, 30, audio)
        assert abs(info.duration - 2) < .15
        assert StudioProject.load(Path(output).with_suffix('.verticliv.json')).layout == layout
    report = json.loads(Path(result.report_path).read_text(encoding='utf-8'))
    assert report['status'] == 'completed_with_errors'
    assert report['outputs'] == result.outputs
    assert wide.is_file() and portrait.is_file()
