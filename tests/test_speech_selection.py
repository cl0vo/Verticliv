import json
from pathlib import Path

import pytest
from arara_factory.speech_selection import cached_transcript, speech_highlights, valid_words
from arara_factory.studio_engine import Cancelled, StudioProject
from arara_factory.transcribe import RecognizedWord as Word
from arara_factory import auto_reels
from arara_factory.auto_reels import AutoReelsOptions
from arara_factory.render import MediaInfo


def words(offset=0, text='word'):
    return [Word(f'{text}{i}' + ('.' if i % 10 == 9 else ''), offset+i, offset+i+.7, .95)
            for i in range(60)]


def test_selection_keeps_word_boundaries_and_no_overlap():
    transcript = words()
    selected = speech_highlights(transcript, 60, 20, 3)
    assert selected
    for h in selected:
        assert h.end - h.start <= 27.5
        assert not any(w.start < h.start < w.end or w.start < h.end < w.end for w in transcript)
        assert 'эвристика' in h.reason
    assert all(a.end <= b.start for a, b in zip(selected, selected[1:]))


def test_speech_selection_does_not_manufacture_silent_clips():
    assert speech_highlights([], 120) == []
    sparse = [Word(str(i), i*10, i*10+.1, .95) for i in range(10)]
    assert speech_highlights(sparse, 120) == []
    low_confidence = [Word(w.text, w.start, w.end, .1) for w in words()]
    assert speech_highlights(low_confidence, 60) == []


def test_duplicate_phrases_not_exported_twice():
    phrase = words()[:20]
    duplicate = [Word(w.text, w.start+40, w.end+40, w.confidence) for w in phrase]
    selected = speech_highlights(phrase + duplicate, 65, 20, 4)
    assert len(selected) == 1


def test_cache_reused_and_invalidated_by_settings_and_source(tmp_path):
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'source')
    project = StudioProject(source=str(source), end=60)
    calls = []
    def recognize(*args):
        calls.append(True)
        return words()
    cache = tmp_path / 'cache'
    invoke = lambda: cached_transcript(project, cache, recognize, lambda *a: None, lambda: False)
    assert invoke() == invoke()
    assert len(calls) == 1
    project.vocabulary = 'Hearthstone'
    invoke()
    assert len(calls) == 2
    source.write_bytes(b'changed source')
    invoke()
    assert len(calls) == 3


def test_cancellation_and_changed_source_do_not_poison_cache(tmp_path):
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'source')
    project = StudioProject(source=str(source), end=60)
    cache = tmp_path / 'cache'
    with pytest.raises(Cancelled):
        cached_transcript(project, cache, lambda *a: words(), lambda *a: None, lambda: True)
    assert not cache.exists()
    def changed(*args):
        source.write_bytes(b'changed')
        return words()
    with pytest.raises(ValueError, match='изменился'):
        cached_transcript(project, cache, changed, lambda *a: None, lambda: False)
    assert not cache.exists()


def test_corrupt_cache_is_rebuilt(tmp_path):
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'source')
    project = StudioProject(source=str(source), end=60)
    cache = tmp_path / 'cache'
    calls = []
    def recognize(*args):
        calls.append(True)
        return words()
    invoke = lambda: cached_transcript(project, cache, recognize, lambda *a: None, lambda: False)
    invoke()
    next(cache.glob('*.json')).write_text('{broken')
    assert invoke() == words()
    assert len(calls) == 2


@pytest.mark.parametrize('word', [Word('x', 0, float('nan'), 1), Word('x', 0, 2, 2), Word('', 0, 2, 1)])
def test_invalid_words_rejected(word):
    assert not valid_words([word], 0, 60)


def test_batch_recognizes_once_reuses_cache_and_keeps_skip_bounds(tmp_path, monkeypatch):
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'input')
    monkeypatch.setattr(auto_reels, 'source_info', lambda _: MediaInfo(1920, 1080, 80, 30, True))
    calls, projects = [], []
    def recognize(project, *args):
        calls.append((project.start, project.end))
        return words(10)
    def export(project, target, *args):
        projects.append(project)
        target.write_bytes(b'export')
    monkeypatch.setattr(auto_reels, 'transcribe', recognize)
    monkeypatch.setattr(auto_reels, 'export_video', export)
    options = AutoReelsOptions(selection='speech', clip_length=20, skip_start=10, skip_end=10)
    for _ in range(2):
        result = auto_reels.run_auto_reels([str(source)], tmp_path / 'out', options)
        assert result.outputs and not result.failures
        assert all(Path(p).with_suffix('.txt').is_file() for p in result.outputs)
    assert calls == [(10, 70)]
    assert all(p.start >= 10 and p.end <= 70 for p in projects)
    assert all(p.words and p.words[0].start >= p.start and p.words[-1].end <= p.end for p in projects)


@pytest.mark.parametrize('options', [AutoReelsOptions(selection='bad'), AutoReelsOptions(skip_start=-1),
                                   AutoReelsOptions(skip_end=float('inf')), AutoReelsOptions(vocabulary='x'*2001)])
def test_new_options_validate(options):
    with pytest.raises(ValueError):
        options.validate()
