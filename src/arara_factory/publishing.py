from __future__ import annotations

import json
import mimetypes
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from .secure_store import load_credentials, update_platform_credentials


class Platform(str, Enum):
    TIKTOK = "tiktok"
    INSTAGRAM = "instagram"
    YOUTUBE = "youtube"


PLATFORM_LABELS = {
    Platform.TIKTOK: "TikTok",
    Platform.INSTAGRAM: "Instagram Reels",
    Platform.YOUTUBE: "YouTube Shorts",
}


@dataclass
class DeliveryState:
    status: str = "pending"  # pending, uploading, success, failed
    remote_id: str = ""
    error: str = ""
    attempts: int = 0
    updated_at: float = 0.0


@dataclass
class PublishJob:
    id: str
    video: str
    caption: str
    due_at: float
    created_at: float
    deliveries: dict[str, DeliveryState] = field(default_factory=dict)

    @property
    def done(self) -> bool:
        return bool(self.deliveries) and all(
            delivery.status == "success" for delivery in self.deliveries.values()
        )

    @property
    def pending_platforms(self) -> list[Platform]:
        result: list[Platform] = []
        for name, delivery in self.deliveries.items():
            if delivery.status == "success":
                continue
            try:
                result.append(Platform(name))
            except ValueError:
                continue
        return result


Progress = Callable[[int, str], None]


def _root() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    target = base / "ARARA Factory"
    target.mkdir(parents=True, exist_ok=True)
    return target


def queue_path() -> Path:
    return _root() / "publishing-queue.json"


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _caption_from_template(template: str, index: int, file: Path) -> str:
    return (
        template.replace("{n}", str(index))
        .replace("{filename}", file.stem)
        .replace("{file}", file.name)
        .strip()
    )


class PublishQueue:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or queue_path()
        self.jobs: list[PublishJob] = []
        self.load()

    def load(self) -> None:
        if not self.path.is_file():
            self.jobs = []
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            jobs: list[PublishJob] = []
            for item in payload.get("jobs") or []:
                deliveries = {
                    str(name): DeliveryState(**state)
                    for name, state in (item.get("deliveries") or {}).items()
                }
                jobs.append(
                    PublishJob(
                        id=str(item["id"]),
                        video=str(item["video"]),
                        caption=str(item.get("caption") or ""),
                        due_at=float(item.get("due_at") or 0.0),
                        created_at=float(item.get("created_at") or 0.0),
                        deliveries=deliveries,
                    )
                )
            self.jobs = jobs
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            self.jobs = []

    def save(self) -> None:
        payload = {
            "version": 1,
            "jobs": [
                {
                    "id": job.id,
                    "video": job.video,
                    "caption": job.caption,
                    "due_at": job.due_at,
                    "created_at": job.created_at,
                    "deliveries": {
                        name: asdict(state) for name, state in job.deliveries.items()
                    },
                }
                for job in self.jobs
            ],
        }
        _atomic_json(self.path, payload)

    def enqueue(
        self,
        files: list[Path],
        platforms: list[Platform],
        caption_template: str,
        interval_minutes: int,
        *,
        start_at: float | None = None,
    ) -> list[PublishJob]:
        if not platforms:
            raise RuntimeError("Не выбрана ни одна платформа для публикации.")
        interval = max(15, int(interval_minutes)) * 60
        now = time.time()
        if start_at is not None:
            start = float(start_at)
        else:
            unfinished = [job.due_at for job in self.jobs if not job.done]
            start = max(now, max(unfinished) + interval) if unfinished else now

        existing = {
            (str(Path(job.video).resolve()).lower(), tuple(sorted(job.deliveries)))
            for job in self.jobs
        }
        added: list[PublishJob] = []
        platform_names = tuple(sorted(platform.value for platform in platforms))
        for index, file in enumerate(files, start=1):
            if not file.is_file():
                continue
            resolved = str(file.resolve())
            key = (resolved.lower(), platform_names)
            if key in existing:
                continue
            job = PublishJob(
                id=uuid.uuid4().hex,
                video=resolved,
                caption=_caption_from_template(caption_template, index, file),
                due_at=start + len(added) * interval,
                created_at=time.time(),
                deliveries={platform.value: DeliveryState() for platform in platforms},
            )
            self.jobs.append(job)
            added.append(job)
            existing.add(key)
        self.save()
        return added

    def next_due(self, now: float | None = None) -> PublishJob | None:
        current = float(now if now is not None else time.time())
        candidates = [job for job in self.jobs if not job.done and job.due_at <= current]
        if not candidates:
            return None
        return min(candidates, key=lambda job: (job.due_at, job.created_at))

    def next_scheduled(self) -> PublishJob | None:
        candidates = [job for job in self.jobs if not job.done]
        if not candidates:
            return None
        return min(candidates, key=lambda job: (job.due_at, job.created_at))

    def update_delivery(
        self,
        job: PublishJob,
        platform: Platform,
        *,
        status: str,
        remote_id: str = "",
        error: str = "",
    ) -> None:
        state = job.deliveries[platform.value]
        state.status = status
        state.remote_id = remote_id or state.remote_id
        state.error = error
        state.updated_at = time.time()
        if status == "uploading":
            state.attempts += 1
        self.save()

    def retry_failed_now(self) -> int:
        count = 0
        now = time.time()
        for job in self.jobs:
            if job.done:
                continue
            failed = False
            for state in job.deliveries.values():
                if state.status == "failed":
                    state.status = "pending"
                    state.error = ""
                    failed = True
            if failed:
                job.due_at = now
                count += 1
        self.save()
        return count

    def remove_completed(self) -> int:
        before = len(self.jobs)
        self.jobs = [job for job in self.jobs if not job.done]
        self.save()
        return before - len(self.jobs)

    @property
    def remaining(self) -> int:
        return sum(1 for job in self.jobs if not job.done)

    @property
    def completed(self) -> int:
        return sum(1 for job in self.jobs if job.done)


def _overdue_runnable_jobs(
    jobs: list[PublishJob],
    *,
    now: float,
) -> list[PublishJob]:
    """Return overdue jobs that still have at least one pending delivery."""
    return sorted(
        (
            job
            for job in jobs
            if job.due_at <= now
            and any(state.status == "pending" for state in job.deliveries.values())
        ),
        key=lambda job: (job.due_at, job.created_at, job.id),
    )


def startup_autopause_count(
    queue: PublishQueue,
    *,
    saved_active: bool,
    now: float | None = None,
) -> int:
    """Return the overdue backlog size only when automatic startup is unsafe."""
    if not saved_active:
        return 0
    current = float(time.time() if now is None else now)
    count = len(_overdue_runnable_jobs(queue.jobs, now=current))
    return count if count > 1 else 0


def spread_overdue_runnable_jobs(
    queue: PublishQueue,
    *,
    interval_minutes: int,
    now: float | None = None,
) -> int:
    """Restore spacing for an overdue backlog before a manual queue start.

    Future jobs are intentionally left untouched. This may interleave an old
    backlog with an already planned future batch, but it never silently changes
    a schedule the user has not missed.
    """
    current = float(time.time() if now is None else now)
    overdue = _overdue_runnable_jobs(queue.jobs, now=current)
    if not overdue:
        return 0

    interval = max(15, int(interval_minutes)) * 60
    for index, job in enumerate(overdue):
        job.due_at = current + index * interval
    queue.save()
    return len(overdue)


def retry_one_failed_delivery(
    queue: PublishQueue,
    *,
    platform_names: set[str] | None = None,
    now: float | None = None,
) -> tuple[PublishJob, str] | None:
    """Move exactly one failed delivery back to pending and persist it."""
    current = float(time.time() if now is None else now)
    allowed = None if platform_names is None else set(platform_names)
    jobs = sorted(queue.jobs, key=lambda job: (job.due_at, job.created_at, job.id))

    for job in jobs:
        for name, state in job.deliveries.items():
            if allowed is not None and name not in allowed:
                continue
            if state.status != "failed":
                continue
            state.status = "pending"
            state.error = ""
            state.updated_at = current
            job.due_at = current
            queue.save()
            return job, name
    return None


def _json_request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    form: dict[str, Any] | None = None,
    timeout: int = 90,
) -> dict[str, Any]:
    body: bytes | None = None
    request_headers = dict(headers or {})
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json; charset=UTF-8")
    elif form is not None:
        body = urllib.parse.urlencode(form).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail[-1200:]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Ошибка сети: {exc.reason}") from exc
    try:
        result = json.loads(raw.decode("utf-8")) if raw else {}
        return result if isinstance(result, dict) else {"data": result}
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("Платформа вернула некорректный ответ.") from exc


def _upload_binary(
    url: str,
    video: Path,
    headers: dict[str, str],
    progress: Progress,
    *,
    method: str = "POST",
) -> None:
    size = video.stat().st_size
    data = video.read_bytes()
    request_headers = dict(headers)
    request_headers.setdefault("Content-Length", str(size))
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    progress(25, "Отправляю видео")
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail[-1200:]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Ошибка загрузки: {exc.reason}") from exc
    progress(78, "Видео передано платформе")


def _refresh_tiktok(credentials: dict[str, Any]) -> dict[str, Any]:
    expires_at = float(credentials.get("expires_at") or 0)
    if credentials.get("access_token") and expires_at > time.time() + 120:
        return credentials
    required = ("client_key", "client_secret", "refresh_token")
    if not all(credentials.get(name) for name in required):
        return credentials
    result = _json_request(
        "https://open.tiktokapis.com/v2/oauth/token/",
        method="POST",
        form={
            "client_key": credentials["client_key"],
            "client_secret": credentials["client_secret"],
            "grant_type": "refresh_token",
            "refresh_token": credentials["refresh_token"],
        },
    )
    if result.get("error") and not result.get("access_token"):
        raise RuntimeError(
            "TikTok OAuth: "
            + str(result.get("error_description") or result.get("error"))
        )
    updated = dict(credentials)
    updated.update(
        {
            "access_token": result.get("access_token", ""),
            "refresh_token": result.get("refresh_token", credentials.get("refresh_token", "")),
            "expires_at": time.time() + int(result.get("expires_in") or 86400),
        }
    )
    update_platform_credentials(Platform.TIKTOK.value, updated)
    return updated


def _wait_tiktok_status(
    token: str,
    publish_id: str,
    progress: Progress,
    *,
    timeout: int = 240,
) -> str:
    deadline = time.time() + timeout
    headers = {"Authorization": f"Bearer {token}"}
    while time.time() < deadline:
        result = _json_request(
            "https://open.tiktokapis.com/v2/post/publish/status/fetch/",
            method="POST",
            headers=headers,
            payload={"publish_id": publish_id},
        )
        error = result.get("error") or {}
        if error.get("code") not in (None, "", "ok"):
            raise RuntimeError(f"TikTok status: {error.get('message') or error.get('code')}")
        data = result.get("data") or {}
        status = str(data.get("status") or "")
        if status == "PUBLISH_COMPLETE":
            public_ids = data.get("publicaly_available_post_id") or []
            progress(100, "TikTok · опубликовано")
            return str(public_ids[0]) if public_ids else publish_id
        if status == "FAILED":
            raise RuntimeError(
                "TikTok отклонил публикацию: "
                + str(data.get("fail_reason") or "неизвестная причина")
            )
        progress(82, f"TikTok · обработка {status or 'PROCESSING'}")
        time.sleep(3)

    progress(100, "TikTok · принято, обработка продолжается")
    return publish_id


def publish_tiktok(
    video: Path,
    caption: str,
    credentials: dict[str, Any],
    progress: Progress,
) -> str:
    credentials = _refresh_tiktok(credentials)
    token = str(credentials.get("access_token") or "")
    if not token:
        raise RuntimeError("TikTok не подключён: отсутствует access token.")
    size = video.stat().st_size
    if size > 64 * 1024 * 1024:
        raise RuntimeError(
            "TikTok: файл больше 64 МБ для однокусочной загрузки. Уменьши качество рендера."
        )

    headers = {"Authorization": f"Bearer {token}"}
    creator = _json_request(
        "https://open.tiktokapis.com/v2/post/publish/creator_info/query/",
        method="POST",
        headers=headers,
        payload={},
    )
    error = creator.get("error") or {}
    if error.get("code") not in (None, "", "ok"):
        raise RuntimeError(f"TikTok: {error.get('message') or error.get('code')}")
    creator_data = creator.get("data") or {}
    options = creator_data.get("privacy_level_options") or []
    preferred = str(credentials.get("privacy_level") or "PUBLIC_TO_EVERYONE")
    privacy = preferred if preferred in options else (
        "SELF_ONLY" if "SELF_ONLY" in options else (options[0] if options else "SELF_ONLY")
    )

    progress(5, "TikTok · создаю публикацию")
    initialized = _json_request(
        "https://open.tiktokapis.com/v2/post/publish/video/init/",
        method="POST",
        headers=headers,
        payload={
            "post_info": {
                "title": caption[:2200],
                "privacy_level": privacy,
                "disable_duet": bool(
                    credentials.get("disable_duet", creator_data.get("duet_disabled", False))
                ),
                "disable_comment": bool(
                    credentials.get("disable_comment", creator_data.get("comment_disabled", False))
                ),
                "disable_stitch": bool(
                    credentials.get("disable_stitch", creator_data.get("stitch_disabled", False))
                ),
                "video_cover_timestamp_ms": 0,
                "brand_content_toggle": bool(credentials.get("brand_content", False)),
                "brand_organic_toggle": bool(credentials.get("brand_organic", False)),
                "is_aigc": bool(credentials.get("is_aigc", False)),
            },
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": size,
                "chunk_size": size,
                "total_chunk_count": 1,
            },
        },
    )
    error = initialized.get("error") or {}
    if error.get("code") != "ok":
        raise RuntimeError(
            f"TikTok: {error.get('message') or error.get('code') or 'ошибка инициализации'}"
        )
    data = initialized.get("data") or {}
    upload_url = str(data.get("upload_url") or "")
    publish_id = str(data.get("publish_id") or "")
    if not upload_url or not publish_id:
        raise RuntimeError("TikTok не вернул адрес загрузки.")

    mime = mimetypes.guess_type(video.name)[0] or "video/mp4"
    _upload_binary(
        upload_url,
        video,
        {
            "Content-Type": mime,
            "Content-Range": f"bytes 0-{size - 1}/{size}",
        },
        progress,
        method="PUT",
    )
    return _wait_tiktok_status(token, publish_id, progress)


def publish_instagram(
    video: Path,
    caption: str,
    credentials: dict[str, Any],
    progress: Progress,
) -> str:
    token = str(credentials.get("access_token") or "")
    user_id = str(credentials.get("ig_user_id") or "")
    if not token or not user_id:
        raise RuntimeError("Instagram не подключён: нужны access token и IG User ID.")
    version = str(credentials.get("api_version") or "v25.0")
    graph_host = str(credentials.get("graph_host") or "graph.instagram.com")
    base = f"https://{graph_host}/{version}"

    progress(5, "Instagram · создаю Reel-контейнер")
    container = _json_request(
        f"{base}/{urllib.parse.quote(user_id)}/media",
        method="POST",
        headers={"Authorization": f"Bearer {token}"},
        form={
            "media_type": "REELS",
            "upload_type": "resumable",
            "caption": caption[:2200],
            "share_to_feed": "true",
        },
    )
    container_id = str(container.get("id") or "")
    upload_url = str(container.get("uri") or "")
    if not container_id:
        raise RuntimeError(f"Instagram не создал контейнер: {container}")
    if not upload_url:
        upload_url = f"https://rupload.facebook.com/ig-api-upload/{version}/{container_id}"

    size = video.stat().st_size
    _upload_binary(
        upload_url,
        video,
        {
            "Authorization": f"OAuth {token}",
            "offset": "0",
            "file_size": str(size),
            "Content-Type": "application/octet-stream",
        },
        progress,
    )

    progress(82, "Instagram · обрабатываю видео")
    deadline = time.time() + 240
    while time.time() < deadline:
        status = _json_request(
            f"{base}/{container_id}?"
            + urllib.parse.urlencode(
                {"fields": "status_code,status", "access_token": token}
            )
        )
        code = str(status.get("status_code") or "")
        if code == "FINISHED":
            break
        if code in {"ERROR", "EXPIRED"}:
            raise RuntimeError(f"Instagram: {status.get('status') or code}")
        time.sleep(5)
    else:
        raise RuntimeError("Instagram слишком долго обрабатывает Reel.")

    progress(94, "Instagram · публикую Reel")
    published = _json_request(
        f"{base}/{urllib.parse.quote(user_id)}/media_publish",
        method="POST",
        headers={"Authorization": f"Bearer {token}"},
        form={"creation_id": container_id},
    )
    media_id = str(published.get("id") or "")
    if not media_id:
        raise RuntimeError(f"Instagram не подтвердил публикацию: {published}")
    progress(100, "Instagram · опубликовано")
    return media_id


def connect_youtube(client_secret_path: Path) -> dict[str, Any]:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise RuntimeError("В сборке отсутствует модуль Google OAuth.") from exc
    scopes = ["https://www.googleapis.com/auth/youtube.upload"]
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), scopes)
    credentials = flow.run_local_server(port=0, open_browser=True, prompt="consent")
    result = {
        "client_secret_path": str(client_secret_path.resolve()),
        "token": json.loads(credentials.to_json()),
    }
    update_platform_credentials(Platform.YOUTUBE.value, result)
    return result


def publish_youtube(
    video: Path,
    caption: str,
    credentials: dict[str, Any],
    progress: Progress,
) -> str:
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload
    except ImportError as exc:
        raise RuntimeError("В сборке отсутствуют модули YouTube API.") from exc

    token_info = credentials.get("token") or {}
    if not token_info:
        raise RuntimeError("YouTube не подключён. Нажми «Подключения».")
    scopes = ["https://www.googleapis.com/auth/youtube.upload"]
    creds = Credentials.from_authorized_user_info(token_info, scopes=scopes)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        updated = dict(credentials)
        updated["token"] = json.loads(creds.to_json())
        update_platform_credentials(Platform.YOUTUBE.value, updated)

    title_line = next(
        (line.strip() for line in caption.splitlines() if line.strip()),
        "ARARA",
    )
    title = title_line[:90]
    if "#shorts" not in title.lower():
        title = (title + " #shorts")[:100]
    body = {
        "snippet": {
            "title": title,
            "description": caption[:5000],
            "categoryId": "20",
        },
        "status": {
            "privacyStatus": str(credentials.get("privacy_status") or "public"),
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": bool(
                credentials.get("contains_synthetic_media", False)
            ),
        },
    }
    progress(5, "YouTube · начинаю загрузку")
    service = build("youtube", "v3", credentials=creds, cache_discovery=False)
    media = MediaFileUpload(
        str(video),
        mimetype="video/mp4",
        resumable=True,
        chunksize=2 * 1024 * 1024,
    )
    request = service.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
        notifySubscribers=False,
    )
    response = None
    retry = 0
    while response is None:
        try:
            upload_status, response = request.next_chunk()
            if upload_status is not None:
                progress(
                    min(95, max(5, int(upload_status.progress() * 95))),
                    "YouTube · загружаю видео",
                )
        except HttpError as exc:
            status_code = int(getattr(exc.resp, "status", 0) or 0)
            if status_code not in {500, 502, 503, 504} or retry >= 5:
                raise RuntimeError(f"YouTube API: {exc}") from exc
            retry += 1
            time.sleep(min(30, 2**retry))

    video_id = str((response or {}).get("id") or "")
    if not video_id:
        raise RuntimeError(f"YouTube не подтвердил загрузку: {response}")
    progress(100, "YouTube · загружено")
    return video_id


def publish_platform(
    platform: Platform,
    video: Path,
    caption: str,
    progress: Progress,
) -> str:
    credentials = load_credentials().get(platform.value) or {}
    if platform == Platform.TIKTOK:
        return publish_tiktok(video, caption, credentials, progress)
    if platform == Platform.INSTAGRAM:
        return publish_instagram(video, caption, credentials, progress)
    if platform == Platform.YOUTUBE:
        return publish_youtube(video, caption, credentials, progress)
    raise RuntimeError(f"Неизвестная платформа: {platform}")


def platform_connected(platform: Platform) -> bool:
    credentials = load_credentials().get(platform.value) or {}
    if platform == Platform.TIKTOK:
        return bool(credentials.get("access_token"))
    if platform == Platform.INSTAGRAM:
        return bool(credentials.get("access_token") and credentials.get("ig_user_id"))
    if platform == Platform.YOUTUBE:
        return bool(credentials.get("token"))
    return False
