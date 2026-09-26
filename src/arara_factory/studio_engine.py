"""General-purpose vertical editing pipeline. Times in projects are source seconds."""
from __future__ import annotations

import json
import math
import subprocess
import tempfile
import time
import wave
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from .process_utils import popen_hidden, run_hidden, keep_system_awake
from .render import _binary, probe_media
from .subtitles import ass_time, _ass_escape, group_words
from .transcribe import RecognizedWord


class Cancelled(RuntimeError):
    pass


def check_cancel(cancel=lambda: False):
    if cancel():
        raise Cancelled('Операция отменена')


@dataclass
class Crop:
    x: float = 0
    y: float = 0
    w: float = 1
    h: float = 1

    def validate(self):
        if not all(math.isfinite(v) for v in (self.x, self.y, self.w, self.h)):
            raise ValueError('Некорректная область кадра')
        if min(self.x, self.y) < 0 or min(self.w, self.h) <= 0 or self.x + self.w > 1.000001 or self.y + self.h > 1.000001:
            raise ValueError('Область должна находиться внутри видео')

    def pixels(self, width, height):
        self.validate()
        x, y = int(self.x * width) // 2 * 2, int(self.y * height) // 2 * 2
        w = min(width - x, max(2, int(self.w * width) // 2 * 2))
        h = min(height - y, max(2, int(self.h * height) // 2 * 2))
        return x, y, w, h


def fitted_crop(crop: Crop, width: int, height: int, aspect: float, zoom=1.0):
    """Same center-cover geometry is used by preview and FFmpeg."""
    x, y, w, h = crop.pixels(width, height)
    cw, ch = min(w, h * aspect), min(h, w / aspect)
    cw, ch = max(2, int(cw / zoom) // 2 * 2), max(2, int(ch / zoom) // 2 * 2)
    return int(x + (w - cw) / 2) // 2 * 2, int(y + (h - ch) / 2) // 2 * 2, cw, ch


@dataclass
class StudioProject:
    source: str = ''
    start: float = 0
    end: float = 60
    layout: str = 'fit'
    main: Crop = field(default_factory=Crop)
    webcam: Crop = field(default_factory=lambda: Crop(0, 0, .25, .3))
    webcam_fraction: float = .28
    captions: bool = True
    caption_style: str = 'karaoke'
    caption_y: int = 1500
    font_size: int = 64
    model: str = 'small'
    language: str = 'ru'
    device: str = 'cpu'
    vocabulary: str = ''
    zoom: bool = False
    zoom_at: float = 0
    zoom_duration: float = 1.2
    words: list[RecognizedWord] = field(default_factory=list)
    transcript_ranges: list[list[float]] = field(default_factory=list)

    def validate(self, duration):
        if self.layout not in ('fit', 'fill', 'gaming'):
            raise ValueError('Неизвестная компоновка')
        if not all(math.isfinite(x) for x in (self.start, self.end, self.webcam_fraction, self.zoom_at, self.zoom_duration)):
            raise ValueError('Некорректное время')
        if not 0 <= self.start < self.end <= duration + .05:
            raise ValueError('Проверь начало и конец фрагмента')
        if not .15 <= self.webcam_fraction <= .5:
            raise ValueError('Размер вебки должен быть от 15 до 50%')
        if not 24 <= self.font_size <= 100 or not 200 <= self.caption_y <= 1750:
            raise ValueError('Некорректный размер или положение субтитров')
        self.main.validate()
        self.webcam.validate()
        for word in self.words:
            if not all(math.isfinite(v) for v in (word.start, word.end, word.confidence)) or not 0 <= word.start < word.end:
                raise ValueError('Некорректные тайминги субтитров')

    def save(self, path: Path):
        path.write_text(json.dumps({'version': 1, **asdict(self)}, ensure_ascii=False, indent=2), encoding='utf-8')

    @classmethod
    def load(cls, path: Path):
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.pop('version', None) != 1:
            raise ValueError('Неподдерживаемая версия проекта')
        data['main'], data['webcam'] = Crop(**data['main']), Crop(**data['webcam'])
        data['words'] = [RecognizedWord(**w) for w in data.get('words', [])]
        return cls(**data)


def binary(name):
    value = _binary(name)
    if not value:
        raise RuntimeError(f'{name} не найден. Установи FFmpeg или используй Windows-сборку программы.')
    return value


def source_info(source):
    """Account for phone-video display rotation, applied automatically by FFmpeg/Qt."""
    probe = binary('ffprobe')
    info = probe_media(probe, Path(source))
    result = run_hidden([probe, '-v', 'error', '-select_streams', 'v:0',
        '-show_entries', 'stream_side_data=rotation:stream_tags=rotate', '-of', 'json', str(source)],
        capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr[-2000:])
    streams = json.loads(result.stdout).get('streams', [])
    rotation = 0
    if streams:
        stream = streams[0]
        rotation = float(stream.get('tags', {}).get('rotate', 0))
        for side in stream.get('side_data_list', []):
            if 'rotation' in side:
                rotation = float(side['rotation'])
    if round(rotation) % 180 == 90:
        info = replace(info, width=info.height, height=info.width)
    return info


def run_ffmpeg(args, duration, progress=lambda n, s: None, cancel=lambda: False, cwd=None):
    """Bounded-memory, interruptible subprocess with temporary progress files."""
    check_cancel(cancel)
    with tempfile.TemporaryDirectory(prefix='vertical-process-') as td:
        err, meter = Path(td) / 'error.log', Path(td) / 'progress.txt'
        command = [binary('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                   '-progress', str(meter), *args]
        with err.open('wb') as errors:
            process = popen_hidden(command, stdout=subprocess.DEVNULL, stderr=errors, cwd=cwd)
            try:
                while process.poll() is None:
                    check_cancel(cancel)
                    if meter.exists():
                        # Read only the tail for multi-hour exports.
                        with meter.open('rb') as stream:
                            stream.seek(max(0, meter.stat().st_size - 2048))
                            lines = stream.read().decode(errors='ignore').splitlines()
                        for line in reversed(lines):
                            if line.startswith('out_time_us='):
                                try:
                                    progress(min(99, int(float(line.split('=')[1]) / 1e6 / max(.01, duration) * 100)), 'Обработка видео')
                                except ValueError:
                                    pass
                                break
                    time.sleep(.1)
                if process.returncode:
                    raise RuntimeError(err.read_text(errors='replace')[-3000:])
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
    progress(100, 'Готово')


def transcribe(project, progress=lambda n, s: None, cancel=lambda: False):
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError('Не установлен faster-whisper. Обнови программу или выполни pip install faster-whisper.') from exc
    info = source_info(project.source)
    project.validate(info.duration)
    if not info.has_audio:
        raise ValueError('В видео нет аудиодорожки. Отключи субтитры для экспорта.')
    with tempfile.TemporaryDirectory(prefix='vertical-speech-') as td:
        audio = Path(td) / 'speech.wav'
        duration = project.end - project.start
        run_ffmpeg(['-ss', str(project.start), '-i', project.source, '-t', str(duration),
                    '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(audio)], duration,
                   lambda n, s: progress(n // 10, 'Подготовка звука'), cancel)
        progress(10, 'Загрузка модели Whisper (первый запуск требует интернет). Отмена — после загрузки модели.')
        model = WhisperModel(project.model, device=project.device,
                             compute_type='int8' if project.device == 'cpu' else 'float16',
                             cpu_threads=4)
        check_cancel(cancel)
        segments, _ = model.transcribe(str(audio), language=None if project.language == 'auto' else project.language,
            word_timestamps=True, vad_filter=True, beam_size=5,
            condition_on_previous_text=False, initial_prompt=project.vocabulary or None,
            vad_parameters={'min_silence_duration_ms': 500})
        words = []
        for segment in segments:
            check_cancel(cancel)
            for word in segment.words or []:
                if word.word.strip() and word.end > word.start:
                    words.append(RecognizedWord(word.word.strip(), word.start + project.start,
                                                word.end + project.start, word.probability))
            progress(min(99, 10 + int(segment.end / duration * 89)), 'Распознавание речи')
        return words


def clip_words(project):
    return [RecognizedWord(w.text, max(0, w.start - project.start), min(project.end, w.end) - project.start, w.confidence)
            for w in sorted(project.words, key=lambda w: w.start)
            if w.end > project.start and w.start < project.end and w.text.strip()]


def write_captions(project, ass_path, srt_path):
    groups = group_words(clip_words(project), max_words=4, max_chars=32)
    header = f'''[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes
[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Base,Arial,{project.font_size},&H00FFFFFF,&H00FFFFFF,&H00101010,&H80000000,-1,0,0,0,100,100,0,0,1,4,1,2,70,70,240,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
    events, srt = [], []
    def stamp(t):
        ms = max(0, round(t * 1000))
        h, ms = divmod(ms, 3600000)
        m, ms = divmod(ms, 60000)
        s, ms = divmod(ms, 1000)
        return f'{h:02}:{m:02}:{s:02},{ms:03}'
    for i, group in enumerate(groups):
        srt.append(f'{i+1}\n{stamp(group.start)} --> {stamp(group.end)}\n' + ' '.join(w.text for w in group.words) + '\n')
        intervals = [(group.start, group.end, -1)]
        if project.caption_style == 'karaoke':
            intervals = [(w.start, group.words[j+1].start if j+1 < len(group.words) else w.end, j) for j, w in enumerate(group.words)]
        for start, end, active in intervals:
            text = ' '.join((r'{\c&H0063F5B0&}' if j == active else r'{\c&H00FFFFFF&}') + _ass_escape(w.text.replace('\n', ' ')) for j, w in enumerate(group.words))
            tags = rf'{{\an2\pos(540,{project.caption_y})}}'
            events.append(f'Dialogue: 0,{ass_time(start)},{ass_time(end)},Base,,0,0,0,,{tags}{text}')
    ass_path.write_text(header + '\n'.join(events), encoding='utf-8-sig')
    srt_path.write_text('\n'.join(srt), encoding='utf-8-sig')


def layout_graph(project, width, height):
    cam_h = int(1920 * project.webcam_fraction) // 2 * 2
    def crop_chain(crop, out_w, out_h, zoom=1):
        x, y, w, h = fitted_crop(crop, width, height, out_w / out_h, zoom)
        return f'crop={w}:{h}:{x}:{y},scale={out_w}:{out_h},setsar=1'
    if project.layout == 'gaming':
        graph = f'[0:v]split=2[cam][game];[cam]{crop_chain(project.webcam,1080,cam_h)}[top];[game]{crop_chain(project.main,1080,1920-cam_h)}[bottom];[top][bottom]vstack=inputs=2[layout]'
    elif project.layout == 'fill':
        graph = f'[0:v]{crop_chain(project.main,1080,1920)}[layout]'
    else:
        x, y, w, h = project.main.pixels(width, height)
        graph = f'[0:v]crop={w}:{h}:{x}:{y},split=2[bg][fg];[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:2,setsar=1[blur];[fg]scale=1080:1920:force_original_aspect_ratio=decrease,setsar=1[front];[blur][front]overlay=(W-w)/2:(H-h)/2[layout]'
    if project.zoom:
        # Zoom the composition briefly at the selected highlight; timestamps are clip-relative.
        t = project.zoom_at - project.start
        graph += f";[layout]split=2[normal][effect];[effect]crop=iw/1.12:ih/1.12,scale=1080:1920[zoom];[normal][zoom]overlay=enable='between(t,{max(0,t):.3f},{max(0,t+project.zoom_duration):.3f})'[composed]"
    else:
        graph += ';[layout]null[composed]'
    return graph


def export_video(project, target: Path, progress=lambda n, s: None, cancel=lambda: False):
    target = target.resolve()
    info = source_info(project.source)
    project.validate(info.duration)
    if Path(project.source).resolve() == target:
        raise ValueError('Нельзя перезаписать исходное видео')
    target.parent.mkdir(parents=True, exist_ok=True)
    # Temporary files live beside output for atomic rename, with filter-safe fixed names.
    with keep_system_awake(), tempfile.TemporaryDirectory(prefix='vertical-export-', dir=target.parent) as td:
        tmp = Path(td)
        graph = layout_graph(project, info.width, info.height)
        if project.captions:
            write_captions(project, tmp / 'captions.ass', tmp / 'captions.srt')
            graph += ";[composed]subtitles=filename=captions.ass[vout]"
        else:
            graph += ';[composed]null[vout]'
        duration = project.end - project.start
        args = ['-ss', str(project.start), '-i', str(Path(project.source).resolve()), '-t', str(duration),
                '-filter_complex_threads', '1', '-filter_complex', graph, '-map', '[vout]', '-map', '0:a:0?',
                '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p', '-r', '30',
                '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', str(tmp / 'result.mp4')]
        run_ffmpeg(args, duration, progress, cancel, cwd=tmp)
        check_cancel(cancel)
        result = probe_media(binary('ffprobe'), tmp / 'result.mp4')
        if result.width != 1080 or result.height != 1920 or abs(result.duration-duration) > .3:
            raise RuntimeError('Проверка готового видео не пройдена')
        (tmp / 'result.mp4').replace(target)
        if project.captions:
            (tmp / 'captions.srt').replace(target.with_suffix('.srt'))
    return str(target)


@dataclass(frozen=True)
class Highlight:
    start: float
    end: float
    peak: float
    score: float
    reason: str = 'Пик звука / реакции — проверь момент'


def rank_highlights(energy, duration, clip_length=30, count=8):
    """Non-overlapping candidate windows; energy is one RMS value per second."""
    length = min(clip_length, duration)
    if length <= 0:
        return []
    candidates = []
    for peak in sorted(range(len(energy)), key=lambda i: energy[i], reverse=True):
        if energy[peak] < .005:
            break
        start = min(max(0, peak - length * .65), max(0, duration-length))
        end = start + length
        if any(start < h.end and end > h.start for h in candidates):
            continue
        candidates.append(Highlight(start, end, float(peak), float(energy[peak])))
        if len(candidates) >= count:
            break
    return candidates


def find_highlights(source, clip_length=30, progress=lambda n, s: None, cancel=lambda: False):
    import numpy as np
    info = source_info(source)
    if not info.has_audio:
        raise ValueError('Для поиска по реакциям нужна аудиодорожка')
    with tempfile.TemporaryDirectory(prefix='vertical-highlights-') as td:
        wav = Path(td) / 'audio.wav'
        run_ffmpeg(['-i', source, '-vn', '-ac', '1', '-ar', '8000', '-c:a', 'pcm_s16le', str(wav)],
                   info.duration, progress, cancel)
        energy = []
        with wave.open(str(wav), 'rb') as stream:
            while chunk := stream.readframes(8000):
                check_cancel(cancel)
                samples = np.frombuffer(chunk, dtype='<i2').astype(np.float32) / 32768
                energy.append(float(np.sqrt(np.mean(samples * samples))))
        return rank_highlights(energy, info.duration, clip_length)
