from __future__ import annotations

import json
import os
import sys
import wave
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RecognizedWord:
    text: str
    start: float
    end: float
    confidence: float


_MODEL = None
_MODEL_PATH: Path | None = None


def _candidate_model_paths() -> list[Path]:
    roots = [
        Path(getattr(sys, '_MEIPASS', Path.cwd())),
        Path(sys.executable).resolve().parent,
        Path.cwd(),
    ]
    paths: list[Path] = []
    custom = os.environ.get('ARARA_VOSK_MODEL')
    if custom:
        paths.append(Path(custom))
    for root in roots:
        paths.extend([
            root / 'assets' / 'vosk-model-small-ru-0.22',
            root / 'vosk-model-small-ru-0.22',
        ])
    return paths


def model_path() -> Path:
    for candidate in _candidate_model_paths():
        if candidate.is_dir() and (candidate / 'am').is_dir():
            return candidate
    raise RuntimeError(
        'Русская модель субтитров не найдена внутри программы. '
        'Переустанови последнюю версию Verticliv.'
    )


def _load_model():
    global _MODEL, _MODEL_PATH
    path = model_path()
    if _MODEL is not None and _MODEL_PATH == path:
        return _MODEL
    try:
        from vosk import Model, SetLogLevel
    except ImportError as exc:
        raise RuntimeError('Модуль русских субтитров не установлен.') from exc
    SetLogLevel(-1)
    _MODEL = Model(str(path))
    _MODEL_PATH = path
    return _MODEL


def _result_words(raw: str) -> list[RecognizedWord]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    words: list[RecognizedWord] = []
    for item in payload.get('result') or []:
        text = str(item.get('word') or '').strip()
        if not text:
            continue
        try:
            start = float(item['start'])
            end = float(item['end'])
            confidence = float(item.get('conf', 0.0))
        except (KeyError, TypeError, ValueError):
            continue
        words.append(RecognizedWord(text=text, start=start, end=end, confidence=confidence))
    return words


def transcribe_wav(wav_path: Path) -> list[RecognizedWord]:
    try:
        from vosk import KaldiRecognizer
    except ImportError as exc:
        raise RuntimeError('Модуль русских субтитров не установлен.') from exc

    model = _load_model()
    words: list[RecognizedWord] = []
    with wave.open(str(wav_path), 'rb') as stream:
        if stream.getnchannels() != 1 or stream.getsampwidth() != 2:
            raise RuntimeError('Внутренняя аудиодорожка для субтитров имеет неверный формат.')
        recognizer = KaldiRecognizer(model, stream.getframerate())
        recognizer.SetWords(True)
        while True:
            chunk = stream.readframes(4000)
            if not chunk:
                break
            if recognizer.AcceptWaveform(chunk):
                words.extend(_result_words(recognizer.Result()))
        words.extend(_result_words(recognizer.FinalResult()))

    return sorted(words, key=lambda word: (word.start, word.end))
