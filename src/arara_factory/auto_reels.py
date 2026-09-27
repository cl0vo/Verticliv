"""Sequential, recoverable automatic vertical exports for a list of local videos."""
from __future__ import annotations

import json
import math
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .studio_engine import (
    Cancelled, Highlight, StudioProject, check_cancel, export_video,
    find_highlights, source_info, transcribe,
)
from .speech_selection import cached_transcript, speech_highlights, words_in_window
from .reels_review import write_review


@dataclass(frozen=True)
class AutoReelsOptions:
    clip_length: int = 30
    count: int = 3
    captions: bool = True
    model: str = 'small'
    language: str = 'auto'
    layout: str = 'auto'
    zoom: bool = False
    device: str = 'cpu'
    selection: str = 'reactions'
    vocabulary: str = ''
    skip_start: int = 0
    skip_end: int = 0

    def validate(self) -> None:
        if self.selection not in ('reactions', 'speech'):
            raise ValueError('Выбери отбор по речи или звуковым реакциям.')
        if not isinstance(self.vocabulary, str) or len(self.vocabulary) > 2000:
            raise ValueError('Словарь должен содержать не более 2000 символов.')
        if any(not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 86400
               for v in (self.skip_start, self.skip_end)):
            raise ValueError('Пропуск начала и конца должен быть от 0 до 86400 секунд.')
        if not isinstance(self.clip_length, (int, float)) or not math.isfinite(self.clip_length) or not 10 <= self.clip_length <= 180:
            raise ValueError('Длина клипа должна быть от 10 до 180 секунд.')
        if not isinstance(self.count, int) or isinstance(self.count, bool) or not 1 <= self.count <= 12:
            raise ValueError('Количество клипов должно быть от 1 до 12 на исходник.')
        if self.layout not in ('auto', 'fit', 'fill'):
            raise ValueError('Выбери компоновку: авто, весь кадр или заполнить 9:16.')
        if self.device not in ('cpu', 'cuda'):
            raise ValueError('Устройство распознавания должно быть CPU или NVIDIA CUDA.')
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError('Выбери модель распознавания речи.')
        if not isinstance(self.language, str) or not self.language.strip():
            raise ValueError('Выбери язык распознавания речи или авто.')


@dataclass(frozen=True)
class BatchFailure:
    source: str
    error: str


@dataclass
class BatchResult:
    outputs: list[str] = field(default_factory=list)
    failures: list[BatchFailure] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    report_path: str = ''
    cancelled: bool = False


def _fallback_clips(duration: float, length: float, count: int) -> list[Highlight]:
    """Evenly spaced, non-overlapping excerpts; these are not detected highlights."""
    length = min(length, duration)
    count = min(count, max(1, int(duration // length)))
    starts = [(duration - length) / 2] if count == 1 else [
        i * (duration - length) / (count - 1) for i in range(count)
    ]
    return [Highlight(start, start + length, start + length / 2, 0,
                      'Равномерный фрагмент: яркие звуковые реакции не определены')
            for start in starts]


def _safe_stem(source: Path) -> str:
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', source.stem).strip(' .')[:64].rstrip(' .')
    return stem or 'video'


def run_auto_reels(
    sources: list[str],
    output_dir: Path,
    options: AutoReelsOptions | None = None,
    progress: Callable[[int, str], None] = lambda n, text: None,
    cancel: Callable[[], bool] = lambda: False,
) -> BatchResult:
    """Export up to ``count`` clips per source, retaining successes on errors/cancel.

    Each run gets its own directory. The JSON report is replaced atomically after
    every clip and source failure, so previously completed videos remain usable.
    A failed recognition is reported as an error, never passed off as subtitles.
    """
    options = options or AutoReelsOptions()
    options.validate()
    if not sources:
        raise ValueError('Добавь хотя бы одно исходное видео.')
    paths = [Path(source).expanduser().resolve() for source in sources]
    output_dir = Path(output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix=datetime.now().strftime('Verticliv-%Y%m%d-%H%M%S-'), dir=output_dir))
    result = BatchResult(report_path=str(run_dir / 'report.json'))
    started_at = datetime.now(timezone.utc).isoformat()
    clips: list[dict] = []
    last_progress = 0

    def notify(value: float, text: str) -> None:
        nonlocal last_progress
        last_progress = max(last_progress, min(100, int(value)))
        progress(last_progress, text)

    def warn(message: str) -> None:
        if message not in result.warnings:
            result.warnings.append(message)

    def save_report(status: str = 'running') -> None:
        path = Path(result.report_path)
        temp = path.with_suffix('.json.tmp')
        payload = {
            'version': 1, 'status': status, 'started_at': started_at,
            'updated_at': datetime.now(timezone.utc).isoformat(),
            'sources': [str(path) for path in paths], 'options': asdict(options),
            **asdict(result), 'clips': clips,
        }
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(path)
        try:
            write_review(run_dir / 'review.html', clips, status)
        except OSError:
            warn('Не удалось сохранить страницу просмотра подборки; MP4 и отчёт доступны отдельно.')

    save_report()
    try:
        for source_index, source in enumerate(paths):
            check_cancel(cancel)
            base = source_index * 100 / len(paths)
            share = 100 / len(paths)
            prefix = f'{source_index + 1}/{len(paths)} · {source.name}'
            notify(base, f'{prefix} · проверка видео')
            try:
                if not source.is_file():
                    raise ValueError('Исходное видео не найдено или указана папка.')
                info = source_info(str(source))
                if not math.isfinite(info.duration) or info.duration <= 0 or min(info.width, info.height) < 2:
                    raise ValueError('Не удалось определить длительность или размер видео.')
                check_cancel(cancel)
                selected = []
                transcript = None
                start, end = options.skip_start, info.duration - options.skip_end
                if start >= end:
                    raise ValueError('После пропуска начала и конца не осталось видео.')
                if options.selection == 'speech':
                    if not info.has_audio:
                        raise ValueError('Для отбора по речи нужна аудиодорожка. Выбери режим звуковых реакций для видео без речи.')
                    analysis = StudioProject(source=str(source), start=start, end=end,
                                             model=options.model, language=options.language,
                                             device=options.device, vocabulary=options.vocabulary)
                    transcript = cached_transcript(
                        analysis, output_dir / '.verticliv-cache', transcribe,
                        lambda n, text: notify(base + share * .2 * n / 100, f'{prefix} · {text}'), cancel)
                    selected = speech_highlights(transcript, end, options.clip_length, options.count, start=start)
                    if not selected:
                        raise ValueError('Не найдено достаточно связной речи для клипа. Попробуй режим звуковых реакций или ручной редактор.')
                    warn(f'{source.name}: отбор по репликам и паузам — эвристика, а не оценка смысла или игрового события.')
                elif info.has_audio:
                    bounds = {'start': start, 'end': end} if options.skip_start or options.skip_end else {}
                    selected = find_highlights(
                        str(source), options.clip_length,
                        lambda n, text: notify(base + share * .2 * n / 100, f'{prefix} · поиск звуковых реакций'),
                        cancel, count=options.count, **bounds,
                    )
                    selected = sorted(selected, key=lambda item: item.score, reverse=True)[:options.count]
                    selected.sort(key=lambda item: item.start)
                if not selected:
                    selected = [Highlight(h.start + start, h.end + start, h.peak + start, h.score, h.reason)
                                for h in _fallback_clips(end - start, options.clip_length, options.count)]
                    why = 'нет аудиодорожки' if not info.has_audio else 'не найдены яркие звуковые реакции'
                    warn(f'{source.name}: {why}; выбраны равномерные фрагменты, их стоит просмотреть.')
                if options.captions and not info.has_audio:
                    warn(f'{source.name}: субтитры недоступны без аудиодорожки; SRT будет пустым.')
                if options.layout == 'auto':
                    layout = 'fill' if info.height > info.width else 'fit'
                else:
                    layout = options.layout
            except Cancelled:
                raise
            except Exception as exc:
                result.failures.append(BatchFailure(str(source), str(exc)))
                save_report()
                notify(base + share, f'{prefix} · ошибка, продолжаю со следующим видео')
                continue

            for clip_index, highlight in enumerate(selected):
                check_cancel(cancel)
                clip_base = base + share * (.2 + .8 * clip_index / len(selected))
                clip_share = share * .8 / len(selected)
                label = f'{prefix} · клип {clip_index + 1}/{len(selected)}'
                stem = f'{source_index + 1:03d}_{_safe_stem(source)}_{clip_index + 1:02d}'
                target = run_dir / f'{stem}.mp4'
                project_path = run_dir / f'{stem}.verticliv.json'
                project = StudioProject(
                    source=str(source), start=highlight.start, end=highlight.end,
                    layout=layout, captions=options.captions, model=options.model,
                    language=options.language, device=options.device,
                    zoom=options.zoom, zoom_at=highlight.peak,
                    vocabulary=options.vocabulary,
                )
                if not info.has_audio:
                    # No recognition is possible anywhere in this source. Keep
                    # an editable empty caption track without retrying Whisper
                    # when the saved clip is opened in the manual editor.
                    project.transcript_ranges = [[0, info.duration]]
                try:
                    project.validate(info.duration)
                    if transcript is not None:
                        project.words = words_in_window(transcript, project.start, project.end)
                        project.transcript_ranges = [[project.start, project.end]]
                    if options.captions and info.has_audio:
                        project.words = words_in_window(transcript, project.start, project.end) if transcript is not None else transcribe(
                            project,
                            lambda n, text: notify(clip_base + clip_share * .45 * n / 100, f'{label} · {text}'),
                            cancel,
                        )
                        project.transcript_ranges = [[project.start, project.end]]
                        if not project.words:
                            warn(f'{source.name}, {project.start:.1f}–{project.end:.1f} сек: речь не распознана, SRT будет пустым.')
                    check_cancel(cancel)
                    temp_project = project_path.with_suffix('.json.tmp')
                    project.save(temp_project)
                    temp_project.replace(project_path)
                    export_video(
                        project, target,
                        lambda n, text: notify(clip_base + clip_share * (.45 + .55 * n / 100), f'{label} · экспорт 9:16'),
                        cancel,
                    )
                    if not target.is_file() or target.stat().st_size == 0:
                        raise RuntimeError('Экспорт не создал готовый видеофайл.')
                    result.outputs.append(str(target))
                    # Reusable text is drawn only from recognized source words.
                    text_path = target.with_suffix('.txt')
                    clip_text = ' '.join(w.text for w in project.words)
                    try:
                        text_path.write_text(clip_text, encoding='utf-8')
                    except OSError:
                        warn(f'{target.name}: не удалось сохранить текстовый файл субтитров.')
                    clips.append({
                        'source': str(source), 'start': project.start, 'end': project.end,
                        'output': str(target), 'project': str(project_path),
                        'srt': str(target.with_suffix('.srt')) if project.captions else None,
                        'words': len(project.words), 'reason': highlight.reason,
                        'transcript': str(text_path) if text_path.is_file() else None,
                    })
                except Cancelled:
                    raise
                except Exception as exc:
                    result.failures.append(BatchFailure(
                        str(source), f'{highlight.start:.1f}–{highlight.end:.1f} сек: {exc}',
                    ))
                save_report()
                notify(clip_base + clip_share, f'{label} · обработка завершена')
    except Cancelled:
        result.cancelled = True
        save_report('cancelled')
        notify(last_progress, f'Остановлено. Сохранено клипов: {len(result.outputs)}')
        return result

    save_report('completed_with_errors' if result.failures else 'completed')
    notify(100, f'Готово: {len(result.outputs)} клипов; ошибок: {len(result.failures)}')
    return result
