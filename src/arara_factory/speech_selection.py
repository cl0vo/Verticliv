"""Transcript-boundary selection, deliberately not advertised as semantic AI."""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict
from pathlib import Path

from .studio_engine import Highlight, check_cancel
from .transcribe import RecognizedWord


def transcript_identity(project):
    source = Path(project.source).resolve()
    stat = source.stat()
    return {'source': str(source), 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns,
            'model': project.model, 'language': project.language, 'device': project.device,
            'vocabulary': project.vocabulary, 'start': project.start, 'end': project.end,
            'recognizer_version': 1}


def cached_transcript(project, cache_dir, recognize, progress, cancel):
    """Cache only a completed, validated transcript. Never cache a cancellation."""
    identity = transcript_identity(project)
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    cache_dir = Path(cache_dir)
    target = cache_dir / (key + '.json')
    check_cancel(cancel)
    if target.is_file() and target.stat().st_size < 100_000_000:
        try:
            data = json.loads(target.read_text(encoding='utf-8'))
            if data['identity'] == identity and data['version'] == 1:
                words = [RecognizedWord(**item) for item in data['words']]
                if valid_words(words, project.start, project.end):
                    progress(100, 'Готовая расшифровка из кэша')
                    return words
        except (OSError, ValueError, TypeError, KeyError):
            pass
    words = recognize(project, progress, cancel)
    check_cancel(cancel)
    if transcript_identity(project) != identity:
        raise ValueError('Исходник изменился во время распознавания. Дождись завершения записи.')
    if not valid_words(words, project.start, project.end):
        raise ValueError('Распознавание вернуло некорректные тайминги слов.')
    cache_dir.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix('.json.tmp')
    temp.write_text(json.dumps({'version': 1, 'identity': identity,
                               'words': [asdict(w) for w in words]}, ensure_ascii=False), encoding='utf-8')
    temp.replace(target)
    return words


def valid_words(words, start, end):
    previous = start
    for word in words:
        if (not isinstance(word.text, str) or not word.text.strip()
                or not all(isinstance(x, (float, int)) and math.isfinite(x)
                           for x in (word.start, word.end, word.confidence))
                or word.start < start or word.start < previous or word.end <= word.start
                or word.end > end + .1 or not 0 <= word.confidence <= 1):
            return False
        previous = word.start
    return True


def speech_highlights(words, duration, clip_length=30, count=3, *, start=0):
    """Rank complete speech windows by clarity/density and suppress repetitions.

    No claim about story, humour or gameplay understanding. Starts follow pauses
    or sentence punctuation; endings prefer the closest complete phrase. A
    continuous long sentence may be split between words to bound clip length.
    """
    if not words:
        return []
    words = sorted(words, key=lambda w: w.start)
    boundaries = [0]
    last_boundary = 0
    for i in range(1, len(words)):
        if (words[i].start - words[i-1].end >= .8
                or re.search(r'[.!?…][»"\)]*$', words[i-1].text)
                or words[i].start - words[last_boundary].start >= clip_length):
            boundaries.append(i)
            last_boundary = i
    boundaries.append(len(words))
    candidates = []
    minimum = min(10., duration - start)
    maximum = min(180., clip_length * 1.35)
    for b, first in enumerate(boundaries[:-1]):
        possible = []
        for boundary_index in range(b + 1, len(boundaries)):
            stop = boundaries[boundary_index]
            length = words[stop-1].end - words[first].start
            if length > maximum:
                break
            if length >= minimum:
                possible.append(stop)
        if not possible:
            continue
        stop = min(possible, key=lambda j: abs(words[j-1].end - words[first].start - clip_length))
        selected = words[first:stop]
        begin = max(start, selected[0].start - .15)
        finish = min(duration, selected[-1].end + .25)
        # Do not accidentally include half a neighbouring word via padding.
        if first and words[first-1].end <= selected[0].start:
            begin = max(begin, words[first-1].end)
        if stop < len(words) and words[stop].start >= selected[-1].end:
            finish = min(finish, words[stop].start)
        if finish - begin > 180:
            begin, finish = selected[0].start, min(duration, selected[-1].end)
        tokens = {t.lower() for w in selected for t in re.findall(r'\w+', w.text) if len(t) > 2}
        spoken = sum(w.end - w.start for w in selected)
        confidence = sum(w.confidence for w in selected) / len(selected)
        density = min(1., spoken / max(.1, finish - begin))
        # Too few words or an almost silent/musical region is not a speech reel.
        if len(selected) < 5 or density < .12 or confidence < .3:
            continue
        score = .45 * confidence + .35 * density + .2 * min(1., len(tokens) / 25)
        candidates.append((score, begin, finish, tokens))
    chosen = []
    for score, begin, finish, tokens in sorted(candidates, key=lambda c: (-c[0], c[1])):
        if any(begin < h.end and finish > h.start for h, _ in chosen):
            continue
        if any(len(tokens & old) / max(1, len(tokens | old)) > .8 for _, old in chosen):
            continue
        chosen.append((Highlight(begin, finish, (begin + finish)/2, score,
                                 'Границы реплик и паузы · эвристика по речи, не смысловой AI-анализ'), tokens))
        if len(chosen) >= count:
            break
    return sorted((h for h, _ in chosen), key=lambda h: h.start)


def words_in_window(words, start, end):
    # Timestamps stay in original-source coordinates for editable projects.
    return [w for w in words if w.start >= start and w.end <= end + .001]
