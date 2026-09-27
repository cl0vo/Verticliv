from __future__ import annotations

import hashlib
import mimetypes
import urllib.parse
import urllib.request
from pathlib import Path


class DownloadError(RuntimeError):
    pass


def _safe_name(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    name = Path(parsed.path).name
    suffix = Path(name).suffix.lower()
    if suffix not in {'.mp4', '.mov', '.mkv', '.webm'}:
        suffix = '.mp4'
    digest = hashlib.sha256(url.encode('utf-8')).hexdigest()[:16]
    return f'brainrot_{digest}{suffix}'


def download_video(url: str, cache_dir: Path, progress=lambda done, total: None) -> Path:
    """Download a directly permitted video URL into the local cache.

    The caller is responsible for using only URLs whose owner permits download and reuse.
    Existing files are reused, making the next render instant.
    """
    url = url.strip()
    if not url.startswith(('https://', 'http://')):
        raise DownloadError('Нужна прямая ссылка http/https на разрешённое видео.')

    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / _safe_name(url)
    if target.exists() and target.stat().st_size > 1024 * 100:
        return target

    request = urllib.request.Request(
        url,
        headers={
            'User-Agent': 'Verticliv (+local video compositor)',
            'Accept': 'video/*,application/octet-stream;q=0.9,*/*;q=0.5',
        },
    )
    partial = target.with_suffix(target.suffix + '.part')
    try:
        with urllib.request.urlopen(request, timeout=45) as response, partial.open('wb') as out:
            content_type = response.headers.get_content_type()
            if content_type.startswith('text/') or content_type in {'application/json', 'text/html'}:
                raise DownloadError('Ссылка ведёт не на видеофайл. Нужна прямая MP4/WebM-ссылка.')
            total = int(response.headers.get('Content-Length') or 0)
            done = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                progress(done, total)
        if partial.stat().st_size < 1024 * 100:
            raise DownloadError('Скачанный файл слишком маленький и не похож на видео.')
        partial.replace(target)
        return target
    except Exception as exc:
        partial.unlink(missing_ok=True)
        if isinstance(exc, DownloadError):
            raise
        raise DownloadError(f'Не удалось скачать brainrot: {exc}') from exc
