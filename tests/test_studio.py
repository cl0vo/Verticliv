import shutil
import subprocess
import json
import sys
import wave
from pathlib import Path
from types import SimpleNamespace
import pytest

from arara_factory.studio_engine import (
    StudioProject, Crop, Cancelled, fitted_crop, rank_highlights,
    export_video, write_captions, clip_words, run_ffmpeg,
)
from arara_factory.render import MediaInfo, probe_media
from arara_factory.transcribe import RecognizedWord


def test_crop_mapping_and_bounds():
    assert fitted_crop(Crop(),1920,1080,9/16)==(656,0,606,1080)
    with pytest.raises(ValueError):
        Crop(.9,0,.2,1).pixels(1920,1080)


def test_captions_clipped_to_trim_and_project_roundtrip(tmp_path):
    p=StudioProject(start=10,end=12,words=[RecognizedWord('Привет',9.8,10.4,.5),RecognizedWord('мир',11,13,.9)])
    words=clip_words(p)
    assert [(round(w.start,3),round(w.end,3)) for w in words]==[(0,.4),(1,2)]
    p.save(tmp_path/'project.json')
    assert StudioProject.load(tmp_path/'project.json')==p
    write_captions(p,tmp_path/'a.ass',tmp_path/'a.srt')
    text=(tmp_path/'a.srt').read_text(encoding='utf-8-sig')
    assert '00:00:00,000 --> 00:00:00,400' in text
    assert 'Привет' in text


def test_highlight_windows_are_bounded_and_non_overlapping():
    energy=[.01]*120
    energy[20],energy[21],energy[80]=.8,.7,.9
    result=rank_highlights(energy,120,30,3)
    assert result[0].peak==80
    assert all(0<=h.start<h.end<=120 for h in result)
    assert all(a.end<=b.start or b.end<=a.start for i,a in enumerate(result) for b in result[i+1:])
    assert rank_highlights([0]*60,60)==[]


def test_highlight_detection_forwards_requested_count(tmp_path, monkeypatch):
    import arara_factory.studio_engine as engine

    monkeypatch.setattr(engine, 'source_info', lambda _: MediaInfo(640, 360, 360, 30, True))

    def audio(args, *positional, **kwargs):
        with wave.open(args[-1], 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(8000)
            stream.writeframes(b'\xff\x10' * 8000)

    monkeypatch.setattr(engine, 'run_ffmpeg', audio)
    requested = []
    monkeypatch.setattr(engine, 'rank_highlights', lambda energy, duration, length, count: requested.append(count) or [])
    engine.find_highlights('source.mp4', count=12)
    engine.find_highlights('source.mp4')
    assert requested == [12, 8]


def test_cancellation_preserves_output(tmp_path):
    with pytest.raises(Cancelled):
        run_ffmpeg([],1,cancel=lambda:True)


def test_existing_projects_default_to_auto_encoder(tmp_path):
    path = tmp_path / 'legacy-project.json'
    StudioProject().save(path)
    payload = json.loads(path.read_text(encoding='utf-8'))
    payload.pop('encoder_mode')
    path.write_text(json.dumps(payload), encoding='utf-8')
    assert StudioProject.load(path).encoder_mode == 'auto'
    with pytest.raises(ValueError, match='видеокодер'):
        StudioProject(encoder_mode='unsupported').validate(60)


def test_whisper_reuses_only_last_model_and_uses_four_cpu_threads(monkeypatch):
    import arara_factory.studio_engine as engine

    loaded = []
    def create_model(name, **options):
        loaded.append((name, options))
        return object()
    monkeypatch.setitem(sys.modules, 'faster_whisper', SimpleNamespace(WhisperModel=create_model))
    engine._whisper_model.cache_clear()
    try:
        first = engine._whisper_model('small', 'cpu')
        assert engine._whisper_model('small', 'cpu') is first
        engine._whisper_model('tiny', 'cpu')
        assert engine._whisper_model('small', 'cpu') is not first
        assert len(loaded) == 3
        assert all(options == {'device': 'cpu', 'compute_type': 'int8', 'cpu_threads': 4}
                   for _, options in loaded)
        assert engine._whisper_model.cache_info().currsize == 1
    finally:
        engine._whisper_model.cache_clear()


def _mock_export(monkeypatch, *, source_audio=True, output_audio=True, output_fps=30):
    import arara_factory.studio_engine as engine

    monkeypatch.setattr(engine, 'source_info', lambda source: MediaInfo(1920, 1080, 2, 30, source_audio))
    monkeypatch.setattr(engine, 'binary', lambda name: name)
    monkeypatch.setattr(engine, 'probe_media', lambda *args: MediaInfo(1080, 1920, 1, output_fps, output_audio))
    return engine


def test_nvenc_failure_retries_cpu_before_atomic_publish(tmp_path, monkeypatch):
    import arara_factory.render as render

    engine = _mock_export(monkeypatch)
    monkeypatch.setattr(render, '_has_nvenc', lambda ffmpeg: True)
    attempts = []
    def encode(args, *positional, **kwargs):
        codec = args[args.index('-c:v') + 1]
        attempts.append(codec)
        Path(args[-1]).write_bytes(b'new-video')
        if codec == 'h264_nvenc':
            raise RuntimeError('NVENC runtime unavailable')
    monkeypatch.setattr(engine, 'run_ffmpeg', encode)
    target = tmp_path / 'result.mp4'
    target.write_bytes(b'previous-video')
    project = StudioProject(source=str(tmp_path / 'source.mp4'), end=1, captions=False)
    export_video(project, target)
    assert attempts == ['h264_nvenc', 'libx264']
    assert target.read_bytes() == b'new-video'
    assert not list(tmp_path.glob('vertical-export-*'))


def test_nvenc_cancellation_does_not_retry_or_replace_output(tmp_path, monkeypatch):
    import arara_factory.render as render

    engine = _mock_export(monkeypatch)
    monkeypatch.setattr(render, '_has_nvenc', lambda ffmpeg: True)
    attempts = []
    def encode(*args, **kwargs):
        attempts.append(args)
        raise Cancelled('cancelled')
    monkeypatch.setattr(engine, 'run_ffmpeg', encode)
    target = tmp_path / 'result.mp4'
    target.write_bytes(b'previous-video')
    with pytest.raises(Cancelled):
        export_video(StudioProject(source='source.mp4', end=1, captions=False), target)
    assert len(attempts) == 1
    assert target.read_bytes() == b'previous-video'


@pytest.mark.parametrize('audio,fps', [(False, 30), (True, 24), (True, float('nan'))])
def test_export_quality_gate_preserves_existing_output(tmp_path, monkeypatch, audio, fps):
    engine = _mock_export(monkeypatch, output_audio=audio, output_fps=fps)
    monkeypatch.setattr(engine, 'run_ffmpeg', lambda args, *a, **kw: Path(args[-1]).write_bytes(b'invalid-video'))
    target = tmp_path / 'result.mp4'
    target.write_bytes(b'previous-video')
    with pytest.raises(RuntimeError, match='Проверка'):
        export_video(StudioProject(source='source.mp4', end=1, captions=False, encoder_mode='cpu'), target)
    assert target.read_bytes() == b'previous-video'


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'),reason='FFmpeg required')
@pytest.mark.parametrize('layout,zoom,audio',[('fit',False,False),('fill',True,True),('gaming',True,True)])
def test_real_export_layout_audio_captions_and_duration(tmp_path,layout,zoom,audio):
    source=tmp_path/'source.mp4'
    cmd=['ffmpeg','-v','error','-y','-f','lavfi','-i','testsrc2=size=640x360:rate=30']
    if audio:
        cmd+=['-f','lavfi','-i','sine=frequency=440:sample_rate=48000']
    cmd+=['-t','2','-c:v','libx264','-pix_fmt','yuv420p',str(source)]
    subprocess.run(cmd,check=True)
    p=StudioProject(source=str(source),start=.5,end=1.5,layout=layout,zoom=zoom,zoom_at=.7,
                    words=[RecognizedWord('Тест',.6,1.2,1)])
    output=tmp_path/'result.mp4'
    export_video(p,output)
    info=probe_media(shutil.which('ffprobe'),output)
    assert (info.width,info.height)==(1080,1920)
    assert abs(info.duration-1)<.15
    assert info.has_audio==audio
    assert output.with_suffix('.srt').is_file()
    assert source.is_file()
    assert not list(tmp_path.glob('vertical-export-*'))


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'),reason='FFmpeg required')
def test_phone_rotation_is_applied_before_crop(tmp_path):
    from arara_factory.studio_engine import source_info
    raw,rotated=tmp_path/'raw.mp4',tmp_path/'phone.mp4'
    subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','testsrc2=size=640x360:rate=30','-t','1','-c:v','libx264',str(raw)],check=True)
    subprocess.run(['ffmpeg','-v','error','-y','-display_rotation','90','-i',str(raw),'-c','copy',str(rotated)],check=True)
    info=source_info(str(rotated))
    assert (info.width,info.height)==(360,640)
    p=StudioProject(source=str(rotated),end=.5,layout='fill',captions=False)
    export_video(p,tmp_path/'vertical.mp4')
