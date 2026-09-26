from __future__ import annotations

import json
import math
import os
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode, urlparse

from .publishing import Platform, publish_instagram, publish_tiktok
from .publishing_journal import append_publish_log
from .secure_store import load_credentials, update_platform_credentials

Progress = Callable[[int, str], None]

YOUTUBE_UPLOAD_ENDPOINT = "https://www.googleapis.com/upload/youtube/v3/videos"
YOUTUBE_CHUNK_SIZE = 1024 * 1024  # Must remain a multiple of 256 KiB.
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 30
MAX_TRANSIENT_RETRIES = 5
DIRECT_FALLBACK_AFTER = 2


def _response_error(response: Any) -> str:
    status = int(getattr(response, "status_code", 0) or 0)
    text = str(getattr(response, "text", "") or "")
    reason = ""
    try:
        payload = response.json() if hasattr(response, "json") else json.loads(text)
        errors = ((payload.get("error") or {}).get("errors") or [])
        if errors:
            item = errors[0]
            reason = str(item.get("reason") or item.get("message") or "")
        if not reason:
            reason = str((payload.get("error") or {}).get("message") or "")
    except Exception:
        reason = text[-800:]
    suffix = f": {reason}" if reason else ""
    return f"YouTube HTTP {status or '?'}{suffix}"


def _http_error_message(exc: Exception) -> str:
    """Compatibility helper for tests and old googleapiclient HttpError objects."""
    status = int(getattr(getattr(exc, "resp", None), "status", 0) or 0)
    raw = getattr(exc, "content", b"") or b""
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = str(raw)

    class LegacyResponse:
        status_code = status

        def json(self):
            return json.loads(text) if text else {}

        @property
        def text(self):
            return text

    return _response_error(LegacyResponse())


def _is_google_api_url(url: str) -> bool:
    parsed = urlparse(str(url))
    hostname = (parsed.hostname or "").lower()
    return parsed.scheme.lower() == "https" and (
        hostname == "googleapis.com" or hostname.endswith(".googleapis.com")
    )


def _direct_http_session():
    import requests

    session = requests.Session()
    session.trust_env = False
    ca_bundle = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("CURL_CA_BUNDLE")
    if ca_bundle and Path(ca_bundle).exists():
        session.verify = ca_bundle
    return session


class _BoundedAuthRequest:
    def __init__(self, session=None, *, direct_only: bool = False) -> None:
        from google.auth.transport.requests import Request

        self._request = Request(session=session)
        self._direct_only = direct_only

    def __call__(
        self,
        url,
        method="GET",
        body=None,
        headers=None,
        timeout=None,
        **kwargs,
    ):
        if self._direct_only and not _is_google_api_url(str(url)):
            raise RuntimeError("YouTube OAuth: direct fallback отклонил неожиданный адрес.")
        bounded_timeout = timeout if timeout is not None else (CONNECT_TIMEOUT, READ_TIMEOUT)
        return self._request(
            url=url,
            method=method,
            body=body,
            headers=headers,
            timeout=bounded_timeout,
            **kwargs,
        )


def _make_oauth_request(*, direct: bool = False):
    session = _direct_http_session() if direct else None
    return _BoundedAuthRequest(session, direct_only=direct)


class _AuthorizedSessionWithDirectFallback:
    def __init__(self, creds: Any) -> None:
        from google.auth.transport.requests import AuthorizedSession

        self._creds = creds
        self._authorized_session_type = AuthorizedSession
        self._primary_session = AuthorizedSession(
            creds,
            auth_request=_BoundedAuthRequest(),
            refresh_timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )
        self._session = self._primary_session
        self._direct_auth_transport = None
        self._direct_attempted = False
        self.using_direct = False

    def switch_to_direct(self) -> bool:
        if self.using_direct or self._direct_attempted:
            return False
        self._direct_attempted = True
        direct_transport = _direct_http_session()
        direct = self._authorized_session_type(
            self._creds,
            auth_request=_BoundedAuthRequest(direct_transport, direct_only=True),
            refresh_timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )
        direct.trust_env = False
        direct.verify = direct_transport.verify
        self._direct_auth_transport = direct_transport
        self._session = direct
        self.using_direct = True
        return True

    def switch_to_system_proxy(self) -> bool:
        if not self.using_direct:
            return False
        direct = self._session
        direct_auth_transport = self._direct_auth_transport
        self._session = self._primary_session
        self._direct_auth_transport = None
        self.using_direct = False
        try:
            direct.close()
        except Exception:
            pass
        try:
            if direct_auth_transport is not None:
                direct_auth_transport.close()
        except Exception:
            pass
        return True

    def _validate_direct_url(self, url: str) -> None:
        if self.using_direct and not _is_google_api_url(url):
            raise RuntimeError("YouTube: direct fallback отклонил неожиданный upload URL.")

    def post(self, url: str, **kwargs):
        self._validate_direct_url(url)
        return self._session.post(url, **kwargs)

    def put(self, url: str, **kwargs):
        self._validate_direct_url(url)
        return self._session.put(url, **kwargs)


def _make_authorized_session(creds):
    return _AuthorizedSessionWithDirectFallback(creds)


def _retry_after_seconds(response: Any) -> int | None:
    raw = str(getattr(response, "headers", {}).get("Retry-After", "") or "").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(raw)
        return max(1, math.ceil(retry_at.timestamp() - time.time()))
    except (TypeError, ValueError, OverflowError):
        return None


def _retry_delay(attempt: int, response: Any | None = None) -> int:
    if response is not None:
        retry_after = _retry_after_seconds(response)
        if retry_after is not None:
            return retry_after
    return min(30, 2 ** max(1, attempt))


def _parse_received_offset(
    response: Any,
    fallback: int = 0,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    raw = str(getattr(response, "headers", {}).get("Range", "") or "")
    if not raw:
        return fallback
    # YouTube returns Range: bytes=0-1048575 for resumable uploads.
    try:
        unit, values = raw.split("=", 1)
        start_text, end_text = values.split("-", 1)
        start = int(start_text)
        end = int(end_text)
        offset = end + 1
        if unit.strip().lower() != "bytes" or start != 0 or end < start:
            return fallback
        if offset < minimum:
            return fallback
        if maximum is not None and offset > maximum:
            return fallback
        return offset
    except (ValueError, IndexError):
        return fallback


def _query_upload_status(
    session,
    upload_url: str,
    total: int,
    progress: Progress,
    *,
    confirmed_floor: int = 0,
    confirmed_ceiling: int | None = None,
) -> tuple[int, dict | None]:
    """Ask YouTube which byte range was durably accepted by the upload session."""
    transport_failures = 0
    for attempt in range(1, MAX_TRANSIENT_RETRIES + 1):
        try:
            response = session.put(
                upload_url,
                data=b"",
                headers={
                    "Content-Length": "0",
                    "Content-Range": f"bytes */{total}",
                },
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )
        except Exception as exc:
            message = (
                "YouTube: не удалось проверить состояние загрузки: "
                f"{exc.__class__.__name__}: {exc}"
            )
            append_publish_log(message)
            if not _is_retryable_request_error(exc):
                raise RuntimeError(message) from exc
            if _is_transport_error(exc):
                transport_failures += 1
                was_direct = bool(getattr(session, "using_direct", False))
                is_direct = _maybe_activate_direct_transport(
                    session,
                    transport_failures,
                    exc,
                    progress,
                )
                if is_direct != was_direct:
                    transport_failures = 0
            else:
                transport_failures = 0
            if attempt >= MAX_TRANSIENT_RETRIES:
                raise RuntimeError(message) from exc
            delay = _retry_delay(attempt)
            progress(5, f"YouTube · проверка не ответила · повтор через {delay} сек")
            time.sleep(delay)
            continue

        transport_failures = 0
        if response.status_code in {200, 201}:
            try:
                payload = response.json()
            except Exception:
                payload = {}
            return total, payload if isinstance(payload, dict) else {}
        if response.status_code == 308:
            ceiling = confirmed_ceiling if confirmed_ceiling is not None else total
            offset = _parse_received_offset(
                response,
                confirmed_floor,
                minimum=confirmed_floor,
                maximum=min(total, ceiling),
            )
            percent = int(offset * 100 / total) if total else 0
            progress(min(95, max(5, percent)), f"YouTube · подтверждено {percent}%")
            retry_after = _retry_after_seconds(response)
            if retry_after is not None:
                progress(5, f"YouTube · сервер просит паузу {retry_after} сек")
                time.sleep(retry_after)
            return offset, None
        if response.status_code in {404, 410}:
            raise RuntimeError(
                "YouTube upload-session истекла. Видео не отмечено опубликованным; "
                "проверь канал перед ручным повтором."
            )
        if response.status_code in {429, 500, 502, 503, 504} and attempt < MAX_TRANSIENT_RETRIES:
            message = _response_error(response)
            append_publish_log(message)
            delay = _retry_delay(attempt, response)
            progress(5, f"YouTube · проверка HTTP {response.status_code} · повтор через {delay} сек")
            time.sleep(delay)
            continue
        raise RuntimeError(_response_error(response))

    raise RuntimeError("YouTube: не удалось проверить состояние загрузки.")


def _is_transport_error(exc: Exception) -> bool:
    try:
        from google.auth.exceptions import TransportError
    except ImportError:
        transport_errors: tuple[type[BaseException], ...] = ()
    else:
        transport_errors = (TransportError,)
    return isinstance(exc, (TimeoutError, ConnectionError, OSError, *transport_errors))


def _is_retryable_request_error(exc: Exception) -> bool:
    return _is_transport_error(exc) or bool(getattr(exc, "retryable", False))


def _activate_direct_transport(session: Any, progress: Progress) -> bool:
    switch = getattr(session, "switch_to_direct", None)
    if not callable(switch) or not switch():
        return bool(getattr(session, "using_direct", False))
    message = (
        "YouTube · системный прокси нестабилен · "
        "использую прямое соединение с Google для этого задания"
    )
    append_publish_log(message)
    progress(5, message)
    return True


def _deactivate_direct_transport(session: Any, progress: Progress) -> bool:
    switch = getattr(session, "switch_to_system_proxy", None)
    if not callable(switch) or not switch():
        return bool(getattr(session, "using_direct", False))
    message = (
        "YouTube · прямое соединение недоступно · "
        "возвращаю системный прокси для оставшихся попыток"
    )
    append_publish_log(message)
    progress(5, message)
    return False


def _maybe_activate_direct_transport(
    session: Any,
    transport_failures: int,
    exc: Exception,
    progress: Progress,
) -> bool:
    if not _is_transport_error(exc):
        return bool(getattr(session, "using_direct", False))
    if bool(getattr(session, "using_direct", False)):
        if transport_failures < DIRECT_FALLBACK_AFTER:
            return True
        return _deactivate_direct_transport(session, progress)
    if transport_failures < DIRECT_FALLBACK_AFTER:
        return False
    return _activate_direct_transport(session, progress)


def _refresh_youtube_oauth(
    creds: Any,
    request_factory: Callable[[], Any],
    progress: Progress,
    *,
    direct_request_factory: Callable[[], Any] | None = None,
) -> bool:
    using_direct = False
    direct_attempted = False
    transport_failures = 0
    for attempt in range(1, MAX_TRANSIENT_RETRIES + 1):
        progress(2, f"YouTube · обновляю OAuth · попытка {attempt}/{MAX_TRANSIENT_RETRIES}")
        try:
            factory = direct_request_factory if using_direct else request_factory
            creds.refresh(factory())
            return using_direct
        except Exception as exc:
            message = f"YouTube OAuth refresh: {exc.__class__.__name__}: {exc}"
            if not _is_retryable_request_error(exc):
                raise RuntimeError(message) from exc
            append_publish_log(message)
            if _is_transport_error(exc):
                transport_failures += 1
                if using_direct:
                    if transport_failures >= DIRECT_FALLBACK_AFTER:
                        using_direct = False
                        transport_failures = 0
                        fallback_message = (
                            "YouTube · прямое OAuth-соединение недоступно · "
                            "возвращаю системный прокси"
                        )
                        append_publish_log(fallback_message)
                        progress(2, fallback_message)
                else:
                    if (
                        direct_request_factory is not None
                        and not direct_attempted
                        and transport_failures >= DIRECT_FALLBACK_AFTER
                    ):
                        using_direct = True
                        direct_attempted = True
                        transport_failures = 0
                        fallback_message = (
                            "YouTube · системный прокси нестабилен · "
                            "проверяю прямое OAuth-соединение"
                        )
                        append_publish_log(fallback_message)
                        progress(2, fallback_message)
            else:
                transport_failures = 0
            if attempt >= MAX_TRANSIENT_RETRIES:
                raise RuntimeError(message) from exc
            delay = _retry_delay(attempt)
            progress(2, f"YouTube · OAuth-сеть не ответила · повтор через {delay} сек")
            time.sleep(delay)

    raise RuntimeError("YouTube OAuth refresh: исчерпан лимит повторов.")


def _create_upload_session(session, body: dict, total: int, progress: Progress) -> str:
    query = urlencode(
        {
            "uploadType": "resumable",
            "part": "snippet,status",
            "notifySubscribers": "false",
        }
    )
    url = f"{YOUTUBE_UPLOAD_ENDPOINT}?{query}"
    headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Length": str(total),
        "X-Upload-Content-Type": "video/mp4",
    }

    attempt = 0
    transport_failures = 0
    while True:
        attempt += 1
        progress(4, f"YouTube · создаю upload-session · попытка {attempt}")
        try:
            response = session.post(
                url,
                json=body,
                headers=headers,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )
        except Exception as exc:
            message = (
                "YouTube: не удалось создать upload-session: "
                f"{exc.__class__.__name__}: {exc}"
            )
            if not _is_retryable_request_error(exc):
                raise RuntimeError(message) from exc
            if _is_transport_error(exc):
                transport_failures += 1
                was_direct = bool(getattr(session, "using_direct", False))
                is_direct = _maybe_activate_direct_transport(
                    session,
                    transport_failures,
                    exc,
                    progress,
                )
                if is_direct != was_direct:
                    transport_failures = 0
            else:
                transport_failures = 0
            if attempt >= MAX_TRANSIENT_RETRIES:
                raise RuntimeError(message) from exc
            delay = _retry_delay(attempt)
            progress(4, f"YouTube · API не ответил · повтор через {delay} сек")
            time.sleep(delay)
            continue

        transport_failures = 0
        if response.status_code in {200, 201}:
            location = str(response.headers.get("Location", "") or "")
            if not location:
                raise RuntimeError("YouTube не вернул Location для resumable upload.")
            if not _is_google_api_url(location):
                raise RuntimeError(
                    "YouTube вернул неожиданный upload URL; отправка токена остановлена."
                )
            return location
        if response.status_code in {429, 500, 502, 503, 504} and attempt < MAX_TRANSIENT_RETRIES:
            delay = _retry_delay(attempt, response)
            progress(4, f"YouTube · HTTP {response.status_code} · повтор через {delay} сек")
            time.sleep(delay)
            continue
        raise RuntimeError(_response_error(response))


def _upload_file_resumable(
    session,
    upload_url: str,
    video: Path,
    progress: Progress,
) -> str:
    total = video.stat().st_size
    offset = 0
    transient_attempt = 0
    data_transport_failures = 0
    retry_offset: int | None = None
    chunk_number = 0

    with video.open("rb") as handle:
        while offset < total:
            if retry_offset != offset:
                retry_offset = offset
                transient_attempt = 0
                data_transport_failures = 0
            handle.seek(offset)
            chunk = handle.read(min(YOUTUBE_CHUNK_SIZE, total - offset))
            if not chunk:
                raise RuntimeError("YouTube: файл неожиданно закончился во время загрузки.")
            start = offset
            end = start + len(chunk) - 1
            chunk_number += 1
            current_percent = int(start * 100 / total) if total else 0
            progress(
                min(94, max(5, current_percent)),
                f"YouTube · блок {chunk_number} · {current_percent}% · ожидаю ответ до {READ_TIMEOUT} сек",
            )

            try:
                response = session.put(
                    upload_url,
                    data=chunk,
                    headers={
                        "Content-Type": "video/mp4",
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {start}-{end}/{total}",
                    },
                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                )
            except Exception as exc:
                transient_attempt += 1
                error_message = (
                    f"YouTube · блок {chunk_number}: {exc.__class__.__name__}: {exc}"
                )
                append_publish_log(error_message)
                if not _is_retryable_request_error(exc):
                    raise RuntimeError(error_message) from exc
                if not _is_transport_error(exc):
                    data_transport_failures = 0
                    if transient_attempt >= MAX_TRANSIENT_RETRIES:
                        raise RuntimeError(error_message) from exc
                    delay = _retry_delay(transient_attempt)
                    progress(
                        min(94, max(5, current_percent)),
                        f"YouTube · авторизация временно недоступна · повтор через {delay} сек",
                    )
                    time.sleep(delay)
                    continue
                data_transport_failures += 1
                progress(
                    min(94, max(5, current_percent)),
                    f"YouTube · блок {chunk_number} · таймаут, проверяю принятые байты",
                )
                was_direct = bool(getattr(session, "using_direct", False))
                is_direct = _maybe_activate_direct_transport(
                    session,
                    data_transport_failures,
                    exc,
                    progress,
                )
                if is_direct != was_direct:
                    data_transport_failures = 0
                query_started_direct = bool(getattr(session, "using_direct", False))
                offset, payload = _query_upload_status(
                    session,
                    upload_url,
                    total,
                    progress,
                    confirmed_floor=start,
                    confirmed_ceiling=end + 1,
                )
                if bool(getattr(session, "using_direct", False)) != query_started_direct:
                    data_transport_failures = 0
                if payload is not None:
                    video_id = str(payload.get("id") or "")
                    if video_id:
                        return video_id
                if transient_attempt >= MAX_TRANSIENT_RETRIES and offset <= start:
                    raise RuntimeError(
                        f"YouTube: блок {chunk_number} не подтверждён после {MAX_TRANSIENT_RETRIES} попыток."
                    )
                if offset <= start:
                    delay = _retry_delay(transient_attempt)
                    progress(
                        min(94, max(5, current_percent)),
                        f"YouTube · повтор блока {chunk_number} через {delay} сек",
                    )
                    time.sleep(delay)
                continue

            data_transport_failures = 0
            if response.status_code in {200, 201}:
                try:
                    payload = response.json()
                except Exception:
                    payload = {}
                video_id = str((payload or {}).get("id") or "")
                if not video_id:
                    raise RuntimeError(f"YouTube завершил upload без video_id: {response.text[-500:]}")
                progress(100, f"YouTube · готово · ID {video_id}")
                return video_id

            if response.status_code == 308:
                offset = _parse_received_offset(
                    response,
                    start,
                    minimum=start,
                    maximum=end + 1,
                )
                if offset == start:
                    transient_attempt += 1
                    if transient_attempt >= MAX_TRANSIENT_RETRIES:
                        raise RuntimeError(
                            f"YouTube: блок {chunk_number} не подтверждён после "
                            f"{MAX_TRANSIENT_RETRIES} попыток."
                        )
                    delay = _retry_delay(transient_attempt, response)
                    progress(
                        min(94, max(5, current_percent)),
                        f"YouTube · блок {chunk_number} не подтверждён · повтор через {delay} сек",
                    )
                    time.sleep(delay)
                else:
                    retry_after = _retry_after_seconds(response)
                    if retry_after is not None:
                        progress(
                            min(94, max(5, current_percent)),
                            f"YouTube · сервер просит паузу {retry_after} сек",
                        )
                        time.sleep(retry_after)
                percent = int(offset * 100 / total) if total else 100
                progress(min(95, max(5, percent)), f"YouTube · загружено {percent}%")
                continue

            if response.status_code in {429, 500, 502, 503, 504}:
                transient_attempt += 1
                message = _response_error(response)
                append_publish_log(message)
                progress(
                    min(94, max(5, current_percent)),
                    f"YouTube · HTTP {response.status_code} · сверяю принятые байты",
                )
                offset, payload = _query_upload_status(
                    session,
                    upload_url,
                    total,
                    progress,
                    confirmed_floor=start,
                    confirmed_ceiling=end + 1,
                )
                if payload is not None:
                    video_id = str(payload.get("id") or "")
                    if video_id:
                        return video_id
                if transient_attempt >= MAX_TRANSIENT_RETRIES and offset <= start:
                    raise RuntimeError(message)
                delay = _retry_delay(transient_attempt, response)
                progress(
                    min(94, max(5, current_percent)),
                    f"YouTube · пауза перед продолжением {delay} сек",
                )
                time.sleep(delay)
                continue

            raise RuntimeError(_response_error(response))

    # A resumable session should return 200/201 on the final chunk. Query once if
    # the local byte pointer nevertheless reached EOF without that final response.
    _, payload = _query_upload_status(
        session,
        upload_url,
        total,
        progress,
        confirmed_floor=total,
    )
    video_id = str((payload or {}).get("id") or "") if payload else ""
    if not video_id:
        raise RuntimeError("YouTube принял все байты, но не вернул video_id.")
    return video_id


def publish_youtube_reliable(
    video: Path,
    caption: str,
    credentials: dict,
    progress: Progress,
) -> str:
    try:
        from google.oauth2.credentials import Credentials
    except ImportError as exc:
        raise RuntimeError("В сборке отсутствуют модули Google OAuth.") from exc

    token_info = credentials.get("token") or {}
    if not token_info:
        raise RuntimeError("YouTube не подключён. Открой «Подключения» и войди в аккаунт.")
    if not video.is_file():
        raise RuntimeError(f"YouTube: файл не найден: {video}")
    total = video.stat().st_size
    if total <= 0:
        raise RuntimeError("YouTube: выбран пустой видеофайл.")

    scopes = ["https://www.googleapis.com/auth/youtube.upload"]
    creds = Credentials.from_authorized_user_info(token_info, scopes=scopes)
    using_direct = False
    if creds.expired and creds.refresh_token:
        using_direct = _refresh_youtube_oauth(
            creds,
            lambda: _make_oauth_request(direct=False),
            progress,
            direct_request_factory=lambda: _make_oauth_request(direct=True),
        )
        updated = dict(credentials)
        updated["token"] = json.loads(creds.to_json())
        update_platform_credentials(Platform.YOUTUBE.value, updated)
        credentials = updated

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
            "containsSyntheticMedia": bool(credentials.get("contains_synthetic_media", False)),
        },
    }

    progress(3, "YouTube · подключаю API")
    session = _make_authorized_session(creds)
    if using_direct:
        _activate_direct_transport(session, progress)
    upload_url = _create_upload_session(session, body, total, progress)
    progress(5, "YouTube · upload-session создана")
    return _upload_file_resumable(session, upload_url, video, progress)


def publish_platform_reliable(
    platform: Platform,
    video: Path,
    caption: str,
    progress: Progress,
) -> str:
    credentials = load_credentials().get(platform.value) or {}
    if platform == Platform.YOUTUBE:
        return publish_youtube_reliable(video, caption, credentials, progress)
    if platform == Platform.TIKTOK:
        return publish_tiktok(video, caption, credentials, progress)
    if platform == Platform.INSTAGRAM:
        return publish_instagram(video, caption, credentials, progress)
    raise RuntimeError(f"Неизвестная платформа: {platform}")
