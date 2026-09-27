"""Local recording inbox: stable files, durable deduplication and explicit retry."""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path


VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.mov', '.avi', '.webm', '.m4v', '.ts',
                    '.mts', '.m2ts', '.flv', '.wmv', '.mpg', '.mpeg', '.ogv'}


def default_journal_path():
    base = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData' / 'Local')))
    return base / 'ARARA Factory' / 'recording-inbox.json'


@dataclass(frozen=True)
class Recording:
    path: str
    size: int
    mtime_ns: int

    @property
    def key(self):
        value = f'{os.path.normcase(self.path)}\0{self.size}\0{self.mtime_ns}'
        return hashlib.sha256(value.encode('utf-8')).hexdigest()

    @classmethod
    def inspect(cls, path):
        path = Path(path).resolve()
        stat = path.stat()
        return cls(str(path), stat.st_size, stat.st_mtime_ns)


def file_is_closed(path):
    """On Windows, refuse a file still open by OBS or another writer/reader.

    This check and the quiet period are complementary, not a locking promise
    for the entire render. The final fingerprint is checked again on completion.
    """
    if os.name != 'nt':
        return True
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    handle = create(str(path), 0x80000000, 0, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        return False
    close(handle)
    return True


class FactoryJournal:
    def __init__(self, path):
        self.path = Path(path)
        self.jobs = {}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding='utf-8'))
                if data.get('version') != 1 or not isinstance(data['jobs'], dict):
                    raise ValueError('format')
                for key, job in data['jobs'].items():
                    if (not isinstance(job, dict) or not isinstance(job.get('source'), dict)
                            or job.get('status') not in {'running', 'completed', 'failed', 'cancelled', 'interrupted'}):
                        raise ValueError('job')
                    recording = Recording(**job['source'])
                    if key != recording.key:
                        raise ValueError('identity')
                self.jobs = data['jobs']
            except (ValueError, KeyError, TypeError, OSError) as exc:
                raise ValueError('Журнал автопапки повреждён или недоступен. Он сохранён без изменений; '
                                 'автоматическая обработка не запущена.') from exc
        interrupted = False
        for job in self.jobs.values():
            if job['status'] == 'running':
                job['status'] = 'interrupted'
                job['error'] = 'Программа закрылась во время обработки. Проверь готовые клипы перед повтором.'
                interrupted = True
        if interrupted:
            self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.json.tmp')
        temp.write_text(json.dumps({'version': 1, 'jobs': self.jobs}, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(self.path)

    def claim(self, recording):
        if recording.key in self.jobs:
            return False
        self.jobs[recording.key] = {'source': asdict(recording), 'status': 'running',
                                   'started_at': time.time(), 'outputs': [], 'report_path': '', 'error': ''}
        self.save()
        return True

    def finish(self, recording, result=None, error=''):
        job = self.jobs[recording.key]
        job['status'] = 'failed'
        if result is not None:
            job['outputs'] = list(result.outputs)
            job['report_path'] = result.report_path
            job['status'] = ('cancelled' if result.cancelled else
                             'failed' if result.failures or not result.outputs else 'completed')
            error = '\n'.join(f.error for f in result.failures)
            if not result.outputs and not error:
                error = 'Готовых клипов нет. Проверь отчёт.'
        try:
            unchanged = Recording.inspect(recording.path) == recording
        except OSError:
            unchanged = False
        if not unchanged:
            job['status'] = 'failed'
            error = 'Исходник изменился или исчез во время обработки. Проверь результат.'
        job['error'] = error
        job['finished_at'] = time.time()
        self.save()

    def retry_failed(self, folder):
        """Explicit user request only. Existing output files are never deleted."""
        folder = Path(folder).resolve()
        keys = [key for key, job in self.jobs.items()
                if job['status'] in {'failed', 'cancelled', 'interrupted'}
                and Path(job['source']['path']).parent == folder]
        # Preserve the previous attempt's report/output provenance before retry.
        if keys:
            archive = self.path.with_name(f'{self.path.stem}-before-retry-{time.time_ns()}.json')
            archive.write_text(json.dumps({'version': 1, 'jobs': {k: self.jobs[k] for k in keys}},
                                          ensure_ascii=False, indent=2), encoding='utf-8')
            for key in keys:
                del self.jobs[key]
            self.save()
        return len(keys)


class FolderInbox:
    def __init__(self, folder, output, journal, stable_seconds=60):
        self.folder, self.output = Path(folder).resolve(), Path(output).resolve()
        if not self.folder.is_dir():
            raise ValueError('Выбери существующую папку записей.')
        if self.folder == self.output or self.folder in self.output.parents:
            raise ValueError('Папка результатов должна находиться вне папки записей.')
        if stable_seconds < 1:
            raise ValueError('Период ожидания должен быть положительным.')
        self.journal, self.stable_seconds = journal, stable_seconds
        self.observed = {}

    def ready(self, now=None, closed=file_is_closed):
        """Top-level files only; at least two unchanged observations 60s apart."""
        now = time.monotonic() if now is None else now
        found, ready = set(), []
        for path in sorted(self.folder.iterdir()):
            if path.is_symlink() or path.suffix.lower() not in VIDEO_EXTENSIONS or not path.is_file():
                continue
            try:
                item = Recording.inspect(path)
            except OSError:
                continue
            found.add(item.path)
            if item.size == 0:
                self.observed.pop(item.path, None)
                continue
            previous = self.observed.get(item.path)
            if previous is None or previous[0] != item:
                self.observed[item.path] = (item, now)
                continue
            if now - previous[1] >= self.stable_seconds and item.key not in self.journal.jobs and closed(item.path):
                ready.append(item)
        self.observed = {p: v for p, v in self.observed.items() if p in found}
        return ready
