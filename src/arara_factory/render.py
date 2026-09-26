from __future__ import annotations

import json
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .audio import detect_pulses, extract_wav
from .brainrot_index import choose_segment, mark_segment_used
from .geometry import (
    REFERENCE_HEIGHT,
    REFERENCE_WIDTH,
    NormalizedRect,
    normalized_to_rect,
)
from .process_utils import popen_hidden, run_hidden
from .scene_state import load_brainrot_transform
from .subtitles import arara_words_from_pulses, write_word_ass

VIDEO_EXTS = {'.mp4', '.mov', '.mkv', '.webm', '.avi'}
DEFAULT_BANNER_PATH = Path(r'D:\arara obs\arara777-obs-banner-alpha.webm')
MIN_REEL_SECONDS = 9.0
MAX_REEL_SECONDS = 15.0
MIN_PUBLISH_WIDTH = 540
MIN_PUBLISH_HEIGHT = 960
MIN_PUBLISH_FPS = 24.0
MAX_PUBLISH_FPS = 60.0
OUTPUT_DURATION_TOLERANCE = 0.75


@dataclass(frozen=True)
class MediaInfo:
    width: int
    height: int
    duration: float
    fps: float
    has_audio: bool
    codec_name: str = ''
    pixel_format: str = ''
    has_alpha: bool = False


@dataclass
class RenderOptions:
    variants: int = 1
    subtitle_y: int = 1050
    font: str = 'Arial Black'
    seed: int = 777
    encoder_preset: str = 'veryfast'
    crf: int = 20
    encoder_mode: str = 'auto'
    preview_seconds: float | None = None
    brainrot_zoom: float = 1.25
    subtitles_enabled: bool = True
    subtitle_mode: str = 'arara'
    source_start: float = 0.0
    clip_duration: float | None = None
    output_stem: str | None = None
    brainrot_x: float | None = None
    brainrot_y: float | None = None
    brainrot_width: float | None = None
    brainrot_height: float | None = None
    banner_enabled: bool = False
    banner_path: str | None = None
    banner_width_percent: int = 100
    banner_top_percent: float = 2.5


def _progress_seconds(line: str) -> float | None:
    if line.startswith('out_time_ms='):
        try:
            return max(0.0, int(line.split('=', 1)[1]) / 1_000_000.0)
        except ValueError:
            return None
    if line.startswith('out_time='):
        try:
            hours, minutes, seconds = line.split('=', 1)[1].split(':', 2)
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        except ValueError:
            return None
    return None


def _run(
    cmd: list[str],
    log,
    *,
    duration: float | None = None,
    progress=lambda value: None,
) -> subprocess.CompletedProcess[str]:
    log(' '.join(cmd))
    if duration is None or duration <= 0:
        process = run_hidden(cmd, text=True, capture_output=True)
        if process.returncode:
            raise RuntimeError((process.stderr or process.stdout)[-5000:])
        return process

    progress_cmd = [*cmd[:-1], '-progress', 'pipe:1', '-nostats', cmd[-1]]
    handle, error_name = tempfile.mkstemp(prefix='arara-render-', suffix='.log')
    os.close(handle)
    error_path = Path(error_name)
    stdout_text: list[str] = []

    try:
        with error_path.open('w', encoding='utf-8', errors='replace') as error_log:
            process = popen_hidden(
                progress_cmd,
                text=True,
                stdout=subprocess.PIPE,
                stderr=error_log,
                encoding='utf-8',
                errors='replace',
                bufsize=1,
            )
            assert process.stdout is not None
            last_percent = -1
            for raw_line in process.stdout:
                line = raw_line.strip()
                stdout_text.append(line)
                seconds = _progress_seconds(line)
                if seconds is None:
                    continue
                percent = min(99, max(0, int(seconds * 100 / duration)))
                if percent != last_percent:
                    progress(percent)
                    last_percent = percent
            return_code = process.wait()

        stderr_text = error_path.read_text(encoding='utf-8', errors='replace')
        result = subprocess.CompletedProcess(
            progress_cmd,
            return_code,
            '\n'.join(stdout_text),
            stderr_text,
        )
        if return_code:
            raise RuntimeError((stderr_text or result.stdout)[-5000:])
        progress(100)
        return result
    finally:
        error_path.unlink(missing_ok=True)


def _escape_filter_path(path: Path) -> str:
    return str(path.resolve()).replace('\\', '/').replace(':', '\\:').replace("'", "\\'")


def _binary(name: str) -> str | None:
    bundled = Path(getattr(sys, '_MEIPASS', Path.cwd())) / (name + ('.exe' if sys.platform == 'win32' else ''))
    if bundled.exists():
        return str(bundled)
    return shutil.which(name)


@lru_cache(maxsize=64)
def _probe_media_cached(
    ffprobe: str,
    resolved_path: str,
    size: int,
    mtime_ns: int,
) -> MediaInfo:
    del size, mtime_ns
    cmd = [
        ffprobe,
        '-v', 'error',
        '-show_entries',
        'stream=codec_type,codec_name,pix_fmt,width,height,r_frame_rate:'
        'stream_tags=ALPHA_MODE:format=duration',
        '-of', 'json',
        resolved_path,
    ]
    process = run_hidden(cmd, text=True, capture_output=True)
    if process.returncode:
        raise RuntimeError((process.stderr or process.stdout)[-2000:])
    try:
        payload = json.loads(process.stdout)
        streams = payload.get('streams') or []
        video = next(stream for stream in streams if stream.get('codec_type') == 'video')
        rate = str(video.get('r_frame_rate') or '30/1')
        num, den = rate.split('/', 1)
        pixel_format = str(video.get('pix_fmt') or '').lower()
        alpha_formats = {'rgba', 'argb', 'bgra', 'abgr', 'ya8', 'ya16be', 'ya16le'}
        return MediaInfo(
            width=int(video['width']),
            height=int(video['height']),
            duration=float(payload['format']['duration']),
            fps=float(num) / max(float(den), 1.0),
            has_audio=any(stream.get('codec_type') == 'audio' for stream in streams),
            codec_name=str(video.get('codec_name') or ''),
            pixel_format=pixel_format,
            has_alpha=(
                str((video.get('tags') or {}).get('ALPHA_MODE') or '') == '1'
                or pixel_format.startswith(('yuva', 'gbrap'))
                or pixel_format in alpha_formats
            ),
        )
    except (KeyError, StopIteration, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError('Не удалось определить формат видео.') from exc


def probe_media(ffprobe: str, path: Path) -> MediaInfo:
    stat = path.stat()
    return _probe_media_cached(
        ffprobe,
        str(path.resolve()),
        int(stat.st_size),
        int(stat.st_mtime_ns),
    )


def output_duration(source_duration: float, preview_seconds: float | None = None) -> float:
    if source_duration < MIN_REEL_SECONDS - 0.05:
        raise RuntimeError(
            f'Reel слишком короткий: {source_duration:.1f} сек. Нужен ролик от 9 до 15 секунд.'
        )
    final_duration = min(source_duration, MAX_REEL_SECONDS)
    if preview_seconds is not None:
        return min(final_duration, max(1.0, float(preview_seconds)))
    return final_duration


def _brainrot_files(source: Path) -> list[Path]:
    if source.is_file() and source.suffix.lower() in VIDEO_EXTS:
        return [source]
    if source.is_dir():
        return sorted(path for path in source.rglob('*') if path.suffix.lower() in VIDEO_EXTS)
    return []


@lru_cache(maxsize=8)
def _has_nvenc(ffmpeg: str) -> bool:
    try:
        process = run_hidden(
            [ffmpeg, '-hide_banner', '-encoders'],
            text=True,
            capture_output=True,
            timeout=15,
        )
        return process.returncode == 0 and 'h264_nvenc' in process.stdout
    except (OSError, subprocess.SubprocessError):
        return False


def _video_encoder_args(ffmpeg: str, options: RenderOptions, force_cpu: bool = False) -> tuple[list[str], str]:
    use_nvenc = not force_cpu and options.encoder_mode in {'auto', 'nvidia'} and _has_nvenc(ffmpeg)
    if use_nvenc:
        return [
            '-c:v', 'h264_nvenc',
            # GTX 970: the legacy ``fast`` alias selects the low-quality HP
            # preset. P4 keeps the encoder comfortably below the expensive
            # P6/P7 range while producing cleaner text and game detail.
            '-preset', 'p4',
            '-tune', 'hq',
            '-multipass', 'disabled',
            '-rc', 'vbr',
            '-cq', str(max(16, min(30, options.crf))),
            '-b:v', '0',
            '-g', '60',
            '-bf', '2',
            '-rc-lookahead', '0',
            '-profile:v', 'high',
            '-pix_fmt', 'yuv420p',
        ], 'NVIDIA NVENC'
    return [
        '-c:v', 'libx264',
        '-preset', options.encoder_preset,
        '-crf', str(options.crf),
        '-pix_fmt', 'yuv420p',
        '-threads', '0',
    ], 'CPU x264'


def _safe_output(
    output_dir: Path,
    source: Path,
    variant: int,
    preview: bool,
    output_stem: str | None = None,
) -> Path:
    label = 'preview' if preview else 'ready'
    stem = (output_stem or source.stem).strip() or source.stem
    base = output_dir / f'{stem}_{label}_v{variant}.mp4'
    if not base.exists():
        return base
    stamp = time.strftime('%H%M%S') + f'-{time.time_ns() % 1_000_000:06d}'
    return output_dir / f'{stem}_{label}_v{variant}_{stamp}.mp4'


def _partial_output(final_output: Path) -> Path:
    return final_output.with_name(f'{final_output.stem}.part{final_output.suffix}')


def _validate_reel_format(info: MediaInfo) -> None:
    expected = REFERENCE_WIDTH / REFERENCE_HEIGHT
    actual = info.width / info.height
    if abs(actual - expected) > 0.015:
        raise RuntimeError(f'Reel должен быть вертикальным 9:16. Получено {info.width}×{info.height}.')
    if not info.has_audio:
        raise RuntimeError('В Reel нет аудиодорожки. Для субтитров и итогового звука нужен Reel со звуком.')


def _validate_render_output(info: MediaInfo, expected_duration: float) -> None:
    """Reject an incomplete render before it can reach the publishing queue."""
    if info.width < MIN_PUBLISH_WIDTH or info.height < MIN_PUBLISH_HEIGHT:
        raise RuntimeError(
            'Контроль качества: готовый Reel имеет слишком низкое разрешение '
            f'{info.width}×{info.height}. Нужно минимум '
            f'{MIN_PUBLISH_WIDTH}×{MIN_PUBLISH_HEIGHT}.'
        )

    _validate_reel_format(info)

    if not math.isfinite(info.fps) or not MIN_PUBLISH_FPS <= info.fps <= MAX_PUBLISH_FPS:
        raise RuntimeError(
            'Контроль качества: некорректная частота кадров готового Reel '
            f'({info.fps:.2f} FPS). Ожидается {MIN_PUBLISH_FPS:.0f}–{MAX_PUBLISH_FPS:.0f} FPS.'
        )

    if not math.isfinite(info.duration) or info.duration <= 0:
        raise RuntimeError('Контроль качества: не удалось определить длительность готового Reel.')

    tolerance = max(OUTPUT_DURATION_TOLERANCE, expected_duration * 0.05)
    if abs(info.duration - expected_duration) > tolerance:
        raise RuntimeError(
            'Контроль качества: готовый Reel имеет неожиданную длительность '
            f'{info.duration:.2f} сек вместо {expected_duration:.2f} сек '
            f'(допуск ±{tolerance:.2f} сек).'
        )


def _segment_timing(info: MediaInfo, options: RenderOptions) -> tuple[float, float]:
    start = max(0.0, float(options.source_start or 0.0))
    available = max(0.0, info.duration - start)
    if options.clip_duration is None:
        full_duration = output_duration(available)
    else:
        requested = min(MAX_REEL_SECONDS, float(options.clip_duration))
        if requested < MIN_REEL_SECONDS - 0.05:
            raise RuntimeError('Пакетный фрагмент должен быть длиной от 9 до 15 секунд.')
        full_duration = min(requested, available)
        if full_duration < MIN_REEL_SECONDS - 0.05:
            raise RuntimeError('В конце исходной записи осталось меньше 9 секунд.')
    duration = full_duration
    if options.preview_seconds is not None:
        duration = min(full_duration, max(1.0, float(options.preview_seconds)))
    return start, duration


def _brain_rect(info: MediaInfo, options: RenderOptions):
    if None in (
        options.brainrot_x,
        options.brainrot_y,
        options.brainrot_width,
        options.brainrot_height,
    ):
        normalized = load_brainrot_transform()
    else:
        normalized = NormalizedRect(
            float(options.brainrot_x),
            float(options.brainrot_y),
            float(options.brainrot_width),
            float(options.brainrot_height),
        )
    return normalized_to_rect(normalized, info.width, info.height)


def _even(value: float) -> int:
    return max(2, int(round(value / 2.0) * 2))


def _validate_banner_format(info: MediaInfo) -> None:
    if info.width <= 0 or info.height <= 0 or info.duration <= 0:
        raise RuntimeError('Не удалось определить формат анимированного баннера.')
    if not info.has_alpha:
        raise RuntimeError(
            'У баннера нет прозрачного alpha-канала. Выбери прозрачный WebM/VP9-файл.'
        )


def _banner_geometry(
    canvas: MediaInfo,
    banner: MediaInfo,
    options: RenderOptions,
) -> tuple[int, int, int, int]:
    """Fit the full animation near the top without stretching or clipping it."""
    width_percent = max(50, min(100, int(options.banner_width_percent)))
    width = _even(canvas.width * width_percent / 100)
    height = _even(width * banner.height / banner.width)

    # A replacement asset may be tall. Keep it in the upper safe area instead of
    # allowing it to cover most of a vertical Reel.
    max_height = _even(canvas.height * 0.42)
    if height > max_height:
        height = max_height
        width = _even(height * banner.width / banner.height)

    x = max(0, (canvas.width - width) // 2)
    top_percent = max(0.0, min(20.0, float(options.banner_top_percent)))
    y = max(0, int(round(canvas.height * top_percent / 100)))
    y = min(y, max(0, canvas.height - height))
    return x, y, width, height


def _banner_input_args(path: Path, info: MediaInfo, duration: float) -> list[str]:
    args = ['-stream_loop', '-1']
    # FFmpeg's native VP9 decoder ignores WebM alpha. libvpx-vp9 exposes the
    # yuva420p frames, so selecting it explicitly is required for transparency.
    if info.codec_name == 'vp9':
        args.extend(['-c:v', 'libvpx-vp9'])
    args.extend(['-t', f'{duration:.3f}', '-i', str(path)])
    return args


def _zoom_crop(info: MediaInfo, target_aspect: float, zoom: float) -> tuple[int, int, int, int]:
    zoom = max(1.0, min(1.5, float(zoom)))
    source_aspect = info.width / info.height
    if source_aspect >= target_aspect:
        base_h = info.height
        base_w = _even(base_h * target_aspect)
    else:
        base_w = info.width
        base_h = _even(base_w / target_aspect)

    crop_w = min(info.width, _even(base_w / zoom))
    crop_h = min(info.height, _even(crop_w / target_aspect))
    if crop_h > info.height:
        crop_h = min(info.height, _even(base_h / zoom))
        crop_w = min(info.width, _even(crop_h * target_aspect))

    x = max(0, (info.width - crop_w) // 2)
    y = max(0, (info.height - crop_h) // 2)
    return x, y, crop_w, crop_h


def _prepare_subtitles(
    ffmpeg: str,
    source: Path,
    work: Path,
    source_start: float,
    reel_duration: float,
    options: RenderOptions,
    progress,
    log,
) -> Path | None:
    wav = work / 'audio.wav'
    ass = work / 'captions.ass'
    progress(3, 'Извлекаю голос')
    extract_wav(ffmpeg, source, wav, duration=reel_duration, start=source_start)
    progress(6, 'Определяю тайминги ARARA по голосу')
    pulses = [pulse for pulse in detect_pulses(wav, min_silence=0.08) if pulse.start < reel_duration]
    words = arara_words_from_pulses(pulses)
    if not words:
        log('Голосовые фрагменты не найдены. Reel будет собран без субтитров, а не остановлен ошибкой.')
        return None
    log(f'ARARA Timing: найдено голосовых фрагментов — {len(words)}')
    write_word_ass(words, ass, options.font, options.subtitle_y)
    return ass


def render_reels(
    source: Path,
    brainrot_source: Path,
    output_dir: Path,
    options: RenderOptions,
    progress=lambda n, s: None,
    log=lambda s: None,
) -> list[Path]:
    ffmpeg = _binary('ffmpeg')
    ffprobe = _binary('ffprobe')
    if not ffmpeg or not ffprobe:
        raise RuntimeError('FFmpeg не найден внутри программы.')
    if not source.is_file():
        raise RuntimeError('Не выбран готовый Reel или длинная запись.')

    clips = _brainrot_files(brainrot_source)
    if not clips:
        raise RuntimeError('Не выбран длинный brainrot-файл.')

    source_info = probe_media(ffprobe, source)
    _validate_reel_format(source_info)
    source_start, reel_duration = _segment_timing(source_info, options)
    brain_rect = _brain_rect(source_info, options)
    banner_path: Path | None = None
    banner_info: MediaInfo | None = None
    banner_geometry: tuple[int, int, int, int] | None = None
    if options.banner_enabled:
        banner_path = Path(options.banner_path or DEFAULT_BANNER_PATH)
        if not banner_path.is_file():
            raise RuntimeError(
                f'Анимированный баннер не найден: {banner_path}. '
                'Выбери файл баннера в «Дополнительно» или отключи его.'
            )
        banner_info = probe_media(ffprobe, banner_path)
        _validate_banner_format(banner_info)
        banner_geometry = _banner_geometry(source_info, banner_info, options)

    if options.clip_duration is None and source_info.duration > MAX_REEL_SECONDS + 0.05 and not options.preview_seconds:
        log(f'Reel {source_info.duration:.1f} сек автоматически обрезан до 15.0 сек.')
    log(
        f'Reel {source_info.width}×{source_info.height} · исходник {source_start:.2f}–'
        f'{source_start + reel_duration:.2f} сек · итог {reel_duration:.2f} сек · '
        f'brainrot x={brain_rect.x} y={brain_rect.y} w={brain_rect.width} h={brain_rect.height}'
    )
    if banner_info is not None and banner_geometry is not None:
        banner_x, banner_y, banner_width, banner_height = banner_geometry
        log(
            f'Баннер {banner_info.width}×{banner_info.height} · alpha есть · '
            f'итог {banner_width}×{banner_height} · x={banner_x} y={banner_y} · '
            'зациклен на всю длину Reel'
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(options.seed + time.time_ns())
    made: list[Path] = []

    with tempfile.TemporaryDirectory(prefix='arara_') as tmp:
        work = Path(tmp)
        subtitle_ass: Path | None = None
        subtitles_active = False

        if options.subtitles_enabled:
            ass = _prepare_subtitles(
                ffmpeg,
                source,
                work,
                source_start,
                reel_duration,
                options,
                progress,
                log,
            )
            if ass is not None:
                subtitles_active = True
                subtitle_ass = ass

        for variant in range(1, options.variants + 1):
            progress(12 + int(82 * (variant - 1) / max(1, options.variants)), f'Подготавливаю вариант {variant}')
            brainrot = rng.choice(clips)
            brain_info = probe_media(ffprobe, brainrot)
            if brain_info.duration < reel_duration:
                raise RuntimeError(
                    f'Brainrot короче итогового Reel: {brain_info.duration:.1f} сек. Нужен файл длиннее 15 сек.'
                )
            segment = choose_segment(
                ffprobe,
                brainrot,
                reel_duration,
                seed=options.seed + variant + time.time_ns(),
                mark_used=False,
            )
            final_out = _safe_output(
                output_dir,
                source,
                variant,
                bool(options.preview_seconds),
                options.output_stem,
            )
            partial_out = _partial_output(final_out)
            partial_out.unlink(missing_ok=True)
            crop_x, crop_y, crop_w, crop_h = _zoom_crop(
                brain_info,
                brain_rect.width / brain_rect.height,
                options.brainrot_zoom,
            )
            log(
                f'Brainrot {brain_info.width}×{brain_info.height} · '
                f'{segment.start:.2f}–{segment.start + reel_duration:.2f} сек · '
                f'zoom {options.brainrot_zoom:.2f}× · crop {crop_w}×{crop_h}+{crop_x}+{crop_y}'
            )

            graph_parts = [
                '[0:v]fps=30,setsar=1,setpts=PTS-STARTPTS[base]',
                f'[1:v]crop={crop_w}:{crop_h}:{crop_x}:{crop_y},'
                f'scale={brain_rect.width}:{brain_rect.height}:flags=lanczos,'
                f'setsar=1,fps=30,setpts=PTS-STARTPTS[brain]',
                f'[base][brain]overlay=x={brain_rect.x}:y={brain_rect.y}:shortest=1[layout]',
            ]
            video_label = 'layout'
            if banner_info is not None and banner_geometry is not None:
                banner_x, banner_y, banner_width, banner_height = banner_geometry
                graph_parts.extend([
                    f'[2:v]fps=30,setpts=PTS-STARTPTS,format=yuva420p,'
                    f'scale={banner_width}:{banner_height}:flags=lanczos[banner]',
                    f'[{video_label}][banner]overlay=x={banner_x}:y={banner_y}:'
                    'shortest=1:eof_action=repeat[branded]',
                ])
                video_label = 'branded'
            if subtitles_active and subtitle_ass is not None:
                graph_parts.append(
                    f"[{video_label}]subtitles='{_escape_filter_path(subtitle_ass)}'[vout]"
                )
                video_map = '[vout]'
            else:
                video_map = f'[{video_label}]'
            graph = ';'.join(graph_parts)

            base_cmd = [ffmpeg, '-y', '-hide_banner', '-loglevel', 'error']
            if source_start > 0:
                base_cmd.extend(['-ss', f'{source_start:.3f}'])
            base_cmd.extend([
                '-t', f'{reel_duration:.3f}', '-i', str(source),
                '-ss', f'{segment.start:.3f}', '-t', f'{reel_duration:.3f}', '-i', str(brainrot),
            ])
            if banner_path is not None and banner_info is not None:
                base_cmd.extend(_banner_input_args(banner_path, banner_info, reel_duration))
            base_cmd.extend([
                '-filter_complex', graph,
                '-map', video_map, '-map', '0:a?',
            ])
            audio_args = [
                '-af', 'asetpts=PTS-STARTPTS',
                '-c:a', 'aac', '-b:a', '160k',
                '-t', f'{reel_duration:.3f}',
                '-movflags', '+faststart', '-shortest', str(partial_out),
            ]
            encoder_args, encoder_name = _video_encoder_args(ffmpeg, options)
            log(f'Кодирование: {encoder_name}')

            def encoding_progress(value: int) -> None:
                mapped = 12 + int(82 * value / 100)
                progress(min(94, mapped), f'Кодирую Reel · {value}%')

            try:
                try:
                    _run(
                        [*base_cmd, *encoder_args, *audio_args],
                        log,
                        duration=reel_duration,
                        progress=encoding_progress,
                    )
                except RuntimeError as exc:
                    if encoder_name != 'NVIDIA NVENC':
                        raise
                    log(f'NVENC недоступен, повторяю на CPU: {exc}')
                    progress(12, 'NVIDIA недоступна · повторяю на CPU')
                    partial_out.unlink(missing_ok=True)
                    cpu_args, _ = _video_encoder_args(ffmpeg, options, force_cpu=True)
                    _run(
                        [*base_cmd, *cpu_args, *audio_args],
                        log,
                        duration=reel_duration,
                        progress=encoding_progress,
                    )

                if not partial_out.is_file() or partial_out.stat().st_size <= 0:
                    raise RuntimeError('FFmpeg завершился без готового выходного файла.')
                progress(96, 'Проверяю качество готового Reel')
                output_info = probe_media(ffprobe, partial_out)
                _validate_render_output(output_info, reel_duration)
                log(
                    'Контроль качества: готово · '
                    f'{output_info.width}×{output_info.height} · '
                    f'{output_info.fps:.2f} FPS · {output_info.duration:.2f} сек · аудио есть'
                )
                partial_out.replace(final_out)
                mark_segment_used(brainrot, segment.index)
            except Exception:
                partial_out.unlink(missing_ok=True)
                raise

            progress(98, 'Сохраняю готовый файл')
            made.append(final_out)

    progress(100, 'Готово')
    return made
