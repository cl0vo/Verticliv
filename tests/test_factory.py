import json

import pytest
from arara_factory.factory import FactoryJournal, FolderInbox, Recording, file_is_closed
from arara_factory.auto_reels import BatchResult


def setup(tmp_path):
    folder = tmp_path / 'recordings'
    folder.mkdir()
    path = folder / 'recording.mkv'
    path.write_bytes(b'video')
    journal = FactoryJournal(tmp_path / 'journal.json')
    return folder, path, journal, FolderInbox(folder, tmp_path / 'out', journal)


def test_waits_for_stable_closed_file_and_ignores_partial_files(tmp_path):
    folder, path, journal, inbox = setup(tmp_path)
    (folder / 'video.mp4.part').write_bytes(b'incomplete')
    assert not inbox.ready(0)
    assert not inbox.ready(59)
    assert not inbox.ready(60, closed=lambda _: False)
    assert inbox.ready(60, closed=lambda _: True) == [Recording.inspect(path)]
    path.write_bytes(b'still writing')
    assert not inbox.ready(61)
    assert not inbox.ready(120)
    assert inbox.ready(121, closed=lambda _: True)


def test_persistent_dedup_and_explicit_retry(tmp_path):
    folder, path, journal, inbox = setup(tmp_path)
    recording = Recording.inspect(path)
    assert journal.claim(recording)
    assert not journal.claim(recording)
    journal.finish(recording, BatchResult(outputs=['clip.mp4'], report_path='report.json'))
    restored = FactoryJournal(journal.path)
    assert restored.jobs[recording.key]['status'] == 'completed'
    assert restored.retry_failed(folder) == 0
    inbox = FolderInbox(folder, tmp_path / 'out', restored)
    inbox.ready(0)
    assert not inbox.ready(60, closed=lambda _: True)


def test_interrupted_jobs_wait_for_manual_retry_and_archive_prior_attempt(tmp_path):
    folder, path, journal, inbox = setup(tmp_path)
    recording = Recording.inspect(path)
    journal.claim(recording)
    restored = FactoryJournal(journal.path)
    assert restored.jobs[recording.key]['status'] == 'interrupted'
    assert restored.retry_failed(folder) == 1
    assert list(tmp_path.glob('journal-before-retry-*.json'))
    assert path.read_bytes() == b'video'


def test_corrupt_journal_is_preserved_and_blocks_processing(tmp_path):
    path = tmp_path / 'journal.json'
    path.write_text('{broken')
    with pytest.raises(ValueError, match='сохранён'):
        FactoryJournal(path)
    assert path.read_text() == '{broken'


def test_outputs_cannot_loop_back_into_input(tmp_path):
    folder, path, journal, inbox = setup(tmp_path)
    for output in (folder, folder / 'outputs'):
        with pytest.raises(ValueError):
            FolderInbox(folder, output, journal)


def test_changed_source_marks_failure_even_with_output(tmp_path):
    folder, path, journal, inbox = setup(tmp_path)
    recording = Recording.inspect(path)
    journal.claim(recording)
    path.write_bytes(b'changed')
    journal.finish(recording, BatchResult(outputs=['clip.mp4']))
    assert journal.jobs[recording.key]['status'] == 'failed'
    assert journal.jobs[recording.key]['outputs'] == ['clip.mp4']


def test_closed_file_probe(tmp_path):
    path = tmp_path / 'probe.mkv'
    path.write_bytes(b'test')
    assert file_is_closed(path)
