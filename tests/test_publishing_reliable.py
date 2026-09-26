from __future__ import annotations

import json
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

import pytest

from arara_factory.publishing import Platform, PublishQueue
from arara_factory.publishing_journal import append_publish_log, tail_publish_log
from arara_factory.publishing_reliable import (
    CONNECT_TIMEOUT,
    READ_TIMEOUT,
    MAX_TRANSIENT_RETRIES,
    _AuthorizedSessionWithDirectFallback,
    _BoundedAuthRequest,
    _create_upload_session,
    _http_error_message,
    _is_google_api_url,
    _query_upload_status,
    _refresh_youtube_oauth,
    _retry_after_seconds,
    _upload_file_resumable,
    publish_youtube_reliable,
)
from arara_factory.publishing_reliable_ui import ReliablePublishWorker


def _video(tmp_path: Path, name: str = "reel.mp4", data: bytes = b"video") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


class FakeResponse:
    def __init__(self, status_code: int, *, headers=None, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


def test_persistent_publish_journal(tmp_path: Path, monkeypatch) -> None:
    log_path = tmp_path / "publishing.log"
    monkeypatch.setattr("arara_factory.publishing_journal.journal_path", lambda: log_path)
    line = append_publish_log("YouTube test diagnostic")
    assert "YouTube test diagnostic" in line
    assert "YouTube test diagnostic" in tail_publish_log()


def test_worker_persists_platform_error_in_queue_and_journal(tmp_path: Path, monkeypatch) -> None:
    log_path = tmp_path / "publishing.log"
    monkeypatch.setattr("arara_factory.publishing_journal.journal_path", lambda: log_path)
    monkeypatch.setattr(
        "arara_factory.publishing_reliable_ui.append_publish_log",
        append_publish_log,
    )

    queue = PublishQueue(tmp_path / "queue.json")
    video = _video(tmp_path)
    job = queue.enqueue(
        [video],
        [Platform.YOUTUBE],
        "ARARA",
        15,
        start_at=1.0,
    )[0]

    def fail_publish(platform, video, caption, progress):
        progress(10, "YouTube · подключаю API")
        raise RuntimeError("YouTube HTTP 403: quotaExceeded")

    monkeypatch.setattr(
        "arara_factory.publishing_reliable_ui.publish_platform_reliable",
        fail_publish,
    )

    worker = ReliablePublishWorker(queue, job)
    worker.run()

    restored = PublishQueue(tmp_path / "queue.json").jobs[0]
    state = restored.deliveries[Platform.YOUTUBE.value]
    assert state.status == "failed"
    assert "quotaExceeded" in state.error
    journal = tail_publish_log()
    assert "ОШИБКА" in journal
    assert "quotaExceeded" in journal


def test_failed_job_does_not_turn_back_to_pending_automatically(tmp_path: Path) -> None:
    queue = PublishQueue(tmp_path / "queue.json")
    first = _video(tmp_path, "first.mp4")
    second = _video(tmp_path, "second.mp4")
    jobs = queue.enqueue(
        [first, second],
        [Platform.YOUTUBE],
        "ARARA",
        15,
        start_at=1.0,
    )
    queue.update_delivery(
        jobs[0],
        Platform.YOUTUBE,
        status="failed",
        error="test",
    )
    assert jobs[0].deliveries["youtube"].status == "failed"
    assert jobs[1].deliveries["youtube"].status == "pending"


def test_http_error_message_extracts_youtube_reason() -> None:
    class Response:
        status = 403

    class FakeError(Exception):
        resp = Response()
        content = b'{"error":{"message":"Quota exceeded","errors":[{"reason":"quotaExceeded"}]}}'

    assert _http_error_message(FakeError()) == "YouTube HTTP 403: quotaExceeded"


def test_create_youtube_resumable_session_returns_location() -> None:
    class Session:
        def post(self, url, **kwargs):
            assert "uploadType=resumable" in url
            assert kwargs["headers"]["X-Upload-Content-Length"] == "8"
            return FakeResponse(
                200,
                headers={"Location": "https://www.googleapis.com/upload/session"},
            )

    values = []
    location = _create_upload_session(
        Session(),
        {"snippet": {"title": "ARARA"}, "status": {"privacyStatus": "private"}},
        8,
        lambda value, text: values.append((value, text)),
    )
    assert location == "https://www.googleapis.com/upload/session"
    assert values


def test_create_youtube_session_rejects_untrusted_location() -> None:
    class Session:
        def post(self, url, **kwargs):
            return FakeResponse(200, headers={"Location": "https://evil.example/upload"})

    with pytest.raises(RuntimeError, match="неожиданный upload URL"):
        _create_upload_session(
            Session(),
            {"snippet": {"title": "ARARA"}, "status": {"privacyStatus": "private"}},
            8,
            lambda value, text: None,
        )


def test_create_youtube_session_does_not_retry_permanent_auth_error(monkeypatch) -> None:
    from google.auth.exceptions import RefreshError

    sleeps: list[int] = []
    monkeypatch.setattr("arara_factory.publishing_reliable.time.sleep", sleeps.append)

    class Session:
        post_calls = 0

        def post(self, url, **kwargs):
            self.post_calls += 1
            raise RefreshError("invalid_grant", retryable=False)

    session = Session()
    with pytest.raises(RuntimeError, match="invalid_grant"):
        _create_upload_session(
            session,
            {"snippet": {"title": "ARARA"}, "status": {"privacyStatus": "private"}},
            8,
            lambda value, text: None,
        )

    assert session.post_calls == 1
    assert sleeps == []


def test_create_youtube_session_counts_only_consecutive_transport_failures(
    monkeypatch,
) -> None:
    sleeps: list[int] = []
    monkeypatch.setattr("arara_factory.publishing_reliable.time.sleep", sleeps.append)

    class Session:
        post_calls = 0
        switch_calls = 0
        using_direct = False

        def switch_to_direct(self) -> bool:
            self.switch_calls += 1
            self.using_direct = True
            return True

        def post(self, url, **kwargs):
            self.post_calls += 1
            if self.post_calls == 1:
                return FakeResponse(503)
            if self.post_calls == 2:
                raise ConnectionError("first transport failure")
            return FakeResponse(
                200,
                headers={"Location": "https://www.googleapis.com/upload/session"},
            )

    session = Session()
    location = _create_upload_session(
        session,
        {"snippet": {"title": "ARARA"}, "status": {"privacyStatus": "private"}},
        8,
        lambda value, text: None,
    )

    assert location == "https://www.googleapis.com/upload/session"
    assert session.post_calls == 3
    assert session.switch_calls == 0
    assert sleeps == [2, 4]


def test_youtube_resumable_upload_advances_by_confirmed_ranges(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("arara_factory.publishing_reliable.YOUTUBE_CHUNK_SIZE", 4)
    video = _video(tmp_path, data=b"abcdefgh")
    ranges = []

    class Session:
        def put(self, url, *, data, headers, timeout):
            ranges.append(headers["Content-Range"])
            if len(ranges) == 1:
                return FakeResponse(308, headers={"Range": "bytes=0-3"})
            return FakeResponse(200, payload={"id": "yt-success"})

    result = _upload_file_resumable(
        Session(),
        "https://upload.example/session",
        video,
        lambda value, text: None,
    )
    assert result == "yt-success"
    assert ranges == ["bytes 0-3/8", "bytes 4-7/8"]


def test_youtube_timeout_queries_server_and_resumes_from_confirmed_byte(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("arara_factory.publishing_reliable.YOUTUBE_CHUNK_SIZE", 4)
    monkeypatch.setattr("arara_factory.publishing_reliable.time.sleep", lambda seconds: None)
    video = _video(tmp_path, data=b"abcdefgh")
    calls = []

    class Session:
        def put(self, url, *, data, headers, timeout):
            calls.append(headers["Content-Range"])
            if len(calls) == 1:
                # Simulate: Google received the first block, but our client never got the response.
                raise TimeoutError("read timed out")
            if len(calls) == 2:
                assert data == b""
                return FakeResponse(308, headers={"Range": "bytes=0-3"})
            assert data == b"efgh"
            return FakeResponse(200, payload={"id": "yt-after-timeout"})

    result = _upload_file_resumable(
        Session(),
        "https://upload.example/session",
        video,
        lambda value, text: None,
    )
    assert result == "yt-after-timeout"
    assert calls == ["bytes 0-3/8", "bytes */8", "bytes 4-7/8"]


def test_youtube_308_without_range_retries_same_chunk(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("arara_factory.publishing_reliable.YOUTUBE_CHUNK_SIZE", 4)
    monkeypatch.setattr("arara_factory.publishing_reliable.time.sleep", lambda seconds: None)
    video = _video(tmp_path, data=b"abcdefgh")
    calls: list[tuple[str, bytes]] = []
    upload_attempt = 0

    class Session:
        def put(self, url, *, data, headers, timeout):
            nonlocal upload_attempt
            calls.append((headers["Content-Range"], data))
            upload_attempt += 1
            if upload_attempt == 1:
                # Per YouTube's protocol, no Range means that no bytes were confirmed.
                return FakeResponse(308)
            if upload_attempt == 2:
                return FakeResponse(308, headers={"Range": "bytes=0-3"})
            return FakeResponse(200, payload={"id": "yt-after-empty-range"})

    result = _upload_file_resumable(
        Session(),
        "https://upload.example/session",
        video,
        lambda value, text: None,
    )

    assert result == "yt-after-empty-range"
    assert calls == [
        ("bytes 0-3/8", b"abcd"),
        ("bytes 0-3/8", b"abcd"),
        ("bytes 4-7/8", b"efgh"),
    ]


def test_youtube_later_308_without_range_does_not_rewind_upload(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("arara_factory.publishing_reliable.YOUTUBE_CHUNK_SIZE", 4)
    monkeypatch.setattr("arara_factory.publishing_reliable.time.sleep", lambda seconds: None)
    video = _video(tmp_path, data=b"abcdefgh")
    ranges: list[str] = []

    class Session:
        def put(self, url, *, data, headers, timeout):
            ranges.append(headers["Content-Range"])
            if len(ranges) == 1:
                return FakeResponse(308, headers={"Range": "bytes=0-3"})
            if len(ranges) == 2:
                return FakeResponse(308)
            return FakeResponse(200, payload={"id": "yt-no-rewind"})

    result = _upload_file_resumable(
        Session(),
        "https://upload.example/session",
        video,
        lambda value, text: None,
    )

    assert result == "yt-no-rewind"
    assert ranges == ["bytes 0-3/8", "bytes 4-7/8", "bytes 4-7/8"]


def test_youtube_308_without_progress_stops_after_retry_limit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("arara_factory.publishing_reliable.YOUTUBE_CHUNK_SIZE", 4)
    monkeypatch.setattr("arara_factory.publishing_reliable.time.sleep", lambda seconds: None)
    video = _video(tmp_path, data=b"abcdefgh")
    ranges: list[str] = []

    class Session:
        def put(self, url, *, data, headers, timeout):
            ranges.append(headers["Content-Range"])
            if len(ranges) == 1:
                return FakeResponse(308, headers={"Range": "bytes=0-3"})
            return FakeResponse(308, headers={"Range": "bytes=0-3"})

    with pytest.raises(RuntimeError, match="не подтверждён"):
        _upload_file_resumable(
            Session(),
            "https://upload.example/session",
            video,
            lambda value, text: None,
        )

    assert ranges == ["bytes 0-3/8"] + ["bytes 4-7/8"] * MAX_TRANSIENT_RETRIES


def test_youtube_status_query_retries_before_resending_unknown_chunk(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("arara_factory.publishing_reliable.YOUTUBE_CHUNK_SIZE", 4)
    monkeypatch.setattr("arara_factory.publishing_reliable.time.sleep", lambda seconds: None)
    video = _video(tmp_path, data=b"abcdefgh")
    uploaded_ranges: list[str] = []
    status_attempts = 0

    class Session:
        def put(self, url, *, data, headers, timeout):
            nonlocal status_attempts
            if data == b"":
                status_attempts += 1
                if status_attempts < 3:
                    raise TimeoutError("status response lost")
                return FakeResponse(308, headers={"Range": "bytes=0-3"})

            uploaded_ranges.append(headers["Content-Range"])
            if len(uploaded_ranges) == 1:
                raise TimeoutError("upload response lost")
            return FakeResponse(200, payload={"id": "yt-after-status-retries"})

    result = _upload_file_resumable(
        Session(),
        "https://upload.example/session",
        video,
        lambda value, text: None,
    )

    assert result == "yt-after-status-retries"
    assert uploaded_ranges == ["bytes 0-3/8", "bytes 4-7/8"]
    assert status_attempts == 3


def test_youtube_status_without_range_never_rewinds_confirmed_chunks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("arara_factory.publishing_reliable.YOUTUBE_CHUNK_SIZE", 4)
    monkeypatch.setattr("arara_factory.publishing_reliable.time.sleep", lambda seconds: None)
    video = _video(tmp_path, data=b"abcdefgh")
    uploaded_ranges: list[str] = []
    status_ranges: list[str] = []

    class Session:
        def put(self, url, *, data, headers, timeout):
            content_range = headers["Content-Range"]
            if data == b"":
                status_ranges.append(content_range)
                return FakeResponse(308)
            uploaded_ranges.append(content_range)
            if content_range == "bytes 0-3/8":
                return FakeResponse(308, headers={"Range": "bytes=0-3"})
            raise TimeoutError("second chunk response lost")

    with pytest.raises(RuntimeError, match="не подтверждён"):
        _upload_file_resumable(
            Session(),
            "https://upload.example/session",
            video,
            lambda value, text: None,
        )

    assert uploaded_ranges == ["bytes 0-3/8"] + [
        "bytes 4-7/8"
    ] * MAX_TRANSIENT_RETRIES
    assert status_ranges == ["bytes */8"] * MAX_TRANSIENT_RETRIES


def test_youtube_status_cannot_skip_bytes_that_were_never_sent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("arara_factory.publishing_reliable.YOUTUBE_CHUNK_SIZE", 4)
    monkeypatch.setattr("arara_factory.publishing_reliable.time.sleep", lambda seconds: None)
    video = _video(tmp_path, data=b"abcdefghijkl")
    uploaded_ranges: list[str] = []
    first_attempts = 0

    class Session:
        def put(self, url, *, data, headers, timeout):
            nonlocal first_attempts
            content_range = headers["Content-Range"]
            if data == b"":
                # Only bytes 0-3 were sent. A corrupted/stale status response must
                # not let the client jump over bytes 4-7.
                return FakeResponse(308, headers={"Range": "bytes=0-7"})

            uploaded_ranges.append(content_range)
            if content_range == "bytes 0-3/12":
                first_attempts += 1
                if first_attempts == 1:
                    raise TimeoutError("first response lost")
                return FakeResponse(308, headers={"Range": "bytes=0-3"})
            if content_range == "bytes 4-7/12":
                return FakeResponse(308, headers={"Range": "bytes=0-7"})
            return FakeResponse(200, payload={"id": "yt-no-forward-skip"})

    result = _upload_file_resumable(
        Session(),
        "https://upload.example/session",
        video,
        lambda value, text: None,
    )

    assert result == "yt-no-forward-skip"
    assert uploaded_ranges == [
        "bytes 0-3/12",
        "bytes 0-3/12",
        "bytes 4-7/12",
        "bytes 8-11/12",
    ]


def test_youtube_data_failures_switch_status_and_upload_to_direct_transport(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("arara_factory.publishing_reliable.YOUTUBE_CHUNK_SIZE", 4)
    monkeypatch.setattr("arara_factory.publishing_reliable.time.sleep", lambda seconds: None)
    video = _video(tmp_path, data=b"abcdefgh")

    class Session:
        using_direct = False

        def __init__(self) -> None:
            self.switch_calls = 0
            self.upload_calls: list[tuple[bool, str]] = []
            self.status_calls: list[bool] = []
            self.first_chunk_attempts = 0

        def switch_to_direct(self) -> bool:
            self.switch_calls += 1
            self.using_direct = True
            return True

        def put(self, url, *, data, headers, timeout):
            content_range = headers["Content-Range"]
            if data == b"":
                self.status_calls.append(self.using_direct)
                return FakeResponse(308)

            self.upload_calls.append((self.using_direct, content_range))
            if content_range == "bytes 0-3/8":
                self.first_chunk_attempts += 1
                if self.first_chunk_attempts <= 2:
                    raise ConnectionError("proxy reset")
                return FakeResponse(308, headers={"Range": "bytes=0-3"})
            return FakeResponse(200, payload={"id": "yt-direct-fallback"})

    session = Session()
    result = _upload_file_resumable(
        session,
        "https://www.googleapis.com/upload/session",
        video,
        lambda value, text: None,
    )

    assert result == "yt-direct-fallback"
    assert session.switch_calls == 1
    assert session.status_calls == [False, True]
    assert session.upload_calls == [
        (False, "bytes 0-3/8"),
        (False, "bytes 0-3/8"),
        (True, "bytes 0-3/8"),
        (True, "bytes 4-7/8"),
    ]


def test_youtube_single_direct_reset_does_not_return_to_broken_proxy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("arara_factory.publishing_reliable.YOUTUBE_CHUNK_SIZE", 4)
    monkeypatch.setattr("arara_factory.publishing_reliable.time.sleep", lambda seconds: None)
    video = _video(tmp_path, data=b"abcdefgh")

    class Session:
        using_direct = True

        def __init__(self) -> None:
            self.to_proxy_calls = 0
            self.upload_calls = 0
            self.status_calls = 0

        def switch_to_system_proxy(self) -> bool:
            self.to_proxy_calls += 1
            self.using_direct = False
            return True

        def put(self, url, *, data, headers, timeout):
            assert self.using_direct is True
            if data == b"":
                self.status_calls += 1
                return FakeResponse(308)
            self.upload_calls += 1
            if self.upload_calls == 1:
                raise ConnectionError("single direct reset")
            if self.upload_calls == 2:
                return FakeResponse(308, headers={"Range": "bytes=0-3"})
            return FakeResponse(200, payload={"id": "yt-direct-recovers"})

    session = Session()
    result = _upload_file_resumable(
        session,
        "https://www.googleapis.com/upload/session",
        video,
        lambda value, text: None,
    )

    assert result == "yt-direct-recovers"
    assert session.to_proxy_calls == 0
    assert session.upload_calls == 3
    assert session.status_calls == 1
    assert session.using_direct is True


def test_youtube_status_route_switch_resets_outer_data_failure_streak(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("arara_factory.publishing_reliable.YOUTUBE_CHUNK_SIZE", 4)
    monkeypatch.setattr("arara_factory.publishing_reliable.time.sleep", lambda seconds: None)
    video = _video(tmp_path, data=b"abcdefgh")

    class Session:
        using_direct = False

        def __init__(self) -> None:
            self.to_direct_calls = 0
            self.to_proxy_calls = 0
            self.data_calls = 0
            self.status_calls = 0

        def switch_to_direct(self) -> bool:
            self.to_direct_calls += 1
            self.using_direct = True
            return True

        def switch_to_system_proxy(self) -> bool:
            self.to_proxy_calls += 1
            self.using_direct = False
            return True

        def put(self, url, *, data, headers, timeout):
            if data == b"":
                self.status_calls += 1
                if self.status_calls <= 2:
                    assert self.using_direct is False
                    raise ConnectionError("proxy status reset")
                assert self.using_direct is True
                return FakeResponse(308)

            self.data_calls += 1
            if self.data_calls == 1:
                assert self.using_direct is False
                raise ConnectionError("proxy data reset")
            assert self.using_direct is True
            if self.data_calls == 2:
                raise ConnectionError("single direct data reset")
            if self.data_calls == 3:
                return FakeResponse(308, headers={"Range": "bytes=0-3"})
            return FakeResponse(200, payload={"id": "yt-status-switched-route"})

    session = Session()
    result = _upload_file_resumable(
        session,
        "https://www.googleapis.com/upload/session",
        video,
        lambda value, text: None,
    )

    assert result == "yt-status-switched-route"
    assert session.to_direct_calls == 1
    assert session.to_proxy_calls == 0
    assert session.using_direct is True


def test_youtube_transport_streak_resets_on_retryable_auth_response(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from google.auth.exceptions import RefreshError

    monkeypatch.setattr("arara_factory.publishing_reliable.YOUTUBE_CHUNK_SIZE", 4)
    monkeypatch.setattr("arara_factory.publishing_reliable.time.sleep", lambda seconds: None)
    video = _video(tmp_path, data=b"abcdefgh")

    class Session:
        using_direct = False

        def __init__(self) -> None:
            self.switch_calls = 0
            self.upload_calls = 0

        def switch_to_direct(self) -> bool:
            self.switch_calls += 1
            self.using_direct = True
            return True

        def put(self, url, *, data, headers, timeout):
            if data == b"":
                return FakeResponse(308)
            self.upload_calls += 1
            if self.upload_calls in {1, 3}:
                raise ConnectionError("isolated transport reset")
            if self.upload_calls == 2:
                raise RefreshError("temporarily_unavailable", retryable=True)
            if self.upload_calls == 4:
                return FakeResponse(308, headers={"Range": "bytes=0-3"})
            return FakeResponse(200, payload={"id": "yt-reset-streak"})

    session = Session()
    result = _upload_file_resumable(
        session,
        "https://www.googleapis.com/upload/session",
        video,
        lambda value, text: None,
    )

    assert result == "yt-reset-streak"
    assert session.switch_calls == 0


def test_youtube_5xx_backoff_is_kept_after_status_confirms_progress(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("arara_factory.publishing_reliable.YOUTUBE_CHUNK_SIZE", 4)
    sleeps: list[int] = []
    monkeypatch.setattr("arara_factory.publishing_reliable.time.sleep", sleeps.append)
    video = _video(tmp_path, data=b"abcdefgh")

    class Session:
        upload_calls = 0

        def put(self, url, *, data, headers, timeout):
            if data == b"":
                return FakeResponse(308, headers={"Range": "bytes=0-3"})
            self.upload_calls += 1
            if self.upload_calls == 1:
                return FakeResponse(503, headers={"Retry-After": "11"})
            return FakeResponse(200, payload={"id": "yt-after-503-progress"})

    result = _upload_file_resumable(
        Session(),
        "https://www.googleapis.com/upload/session",
        video,
        lambda value, text: None,
    )

    assert result == "yt-after-503-progress"
    assert sleeps == [11]


def test_youtube_status_query_retries_transient_http_response(monkeypatch) -> None:
    sleeps: list[int] = []
    monkeypatch.setattr("arara_factory.publishing_reliable.time.sleep", sleeps.append)
    responses = [
        FakeResponse(503, headers={"Retry-After": "3"}),
        FakeResponse(308, headers={"Range": "bytes=0-3"}),
    ]

    class Session:
        def put(self, url, *, data, headers, timeout):
            return responses.pop(0)

    offset, payload = _query_upload_status(
        Session(),
        "https://upload.example/session",
        8,
        lambda value, text: None,
    )

    assert offset == 4
    assert payload is None
    assert sleeps == [3]
    assert not responses


def test_youtube_status_query_does_not_retry_permanent_auth_error(monkeypatch) -> None:
    from google.auth.exceptions import RefreshError

    sleeps: list[int] = []
    monkeypatch.setattr("arara_factory.publishing_reliable.time.sleep", sleeps.append)

    class Session:
        put_calls = 0

        def put(self, url, *, data, headers, timeout):
            self.put_calls += 1
            raise RefreshError("invalid_grant", retryable=False)

    session = Session()
    with pytest.raises(RuntimeError, match="invalid_grant"):
        _query_upload_status(
            session,
            "https://www.googleapis.com/upload/session",
            8,
            lambda value, text: None,
        )

    assert session.put_calls == 1
    assert sleeps == []


def test_youtube_status_query_honors_retry_after_on_308(monkeypatch) -> None:
    sleeps: list[int] = []
    monkeypatch.setattr("arara_factory.publishing_reliable.time.sleep", sleeps.append)

    class Session:
        def put(self, url, *, data, headers, timeout):
            return FakeResponse(
                308,
                headers={"Range": "bytes=0-3", "Retry-After": "7"},
            )

    offset, payload = _query_upload_status(
        Session(),
        "https://www.googleapis.com/upload/session",
        8,
        lambda value, text: None,
    )

    assert offset == 4
    assert payload is None
    assert sleeps == [7]


def test_youtube_retry_budget_resets_after_confirmed_progress(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("arara_factory.publishing_reliable.YOUTUBE_CHUNK_SIZE", 4)
    monkeypatch.setattr("arara_factory.publishing_reliable.time.sleep", lambda seconds: None)
    video = _video(tmp_path, data=b"abcdefghijklmnopqrstuvwx")
    uploaded_ranges: list[str] = []
    attempts_by_start: dict[int, int] = {}
    confirmed_end = -1

    class Session:
        def put(self, url, *, data, headers, timeout):
            nonlocal confirmed_end
            if data == b"":
                response_headers = (
                    {"Range": f"bytes=0-{confirmed_end}"} if confirmed_end >= 0 else {}
                )
                return FakeResponse(308, headers=response_headers)

            content_range = headers["Content-Range"]
            uploaded_ranges.append(content_range)
            range_part = content_range.split(" ", 1)[1].split("/", 1)[0]
            start, end = map(int, range_part.split("-", 1))
            attempts_by_start[start] = attempts_by_start.get(start, 0) + 1

            if start in {0, 4, 8, 12}:
                confirmed_end = end
                raise TimeoutError("response lost after accepted block")
            if start == 16 and attempts_by_start[start] == 1:
                raise TimeoutError("block was not accepted")
            if end == 23:
                return FakeResponse(200, payload={"id": "yt-independent-timeouts"})

            confirmed_end = end
            return FakeResponse(308, headers={"Range": f"bytes=0-{confirmed_end}"})

    result = _upload_file_resumable(
        Session(),
        "https://upload.example/session",
        video,
        lambda value, text: None,
    )

    assert result == "yt-independent-timeouts"
    assert uploaded_ranges == [
        "bytes 0-3/24",
        "bytes 4-7/24",
        "bytes 8-11/24",
        "bytes 12-15/24",
        "bytes 16-19/24",
        "bytes 16-19/24",
        "bytes 20-23/24",
    ]
    assert attempts_by_start[16] == 2


def test_youtube_oauth_refresh_retries_transient_network_errors(
    tmp_path: Path,
    monkeypatch,
) -> None:
    video = _video(tmp_path, data=b"video")
    sleeps: list[int] = []
    saved: list[tuple[str, dict]] = []
    request_modes: list[bool] = []

    class FakeCredentials:
        expired = True
        refresh_token = "refresh-token"

        def __init__(self) -> None:
            self.refresh_calls = 0

        def refresh(self, request) -> None:
            self.refresh_calls += 1
            request_modes.append(bool(getattr(request, "_direct_only", False)))
            if self.refresh_calls < 3:
                raise TimeoutError("temporary OAuth TLS failure")

        def to_json(self) -> str:
            return json.dumps(
                {"token": "new-access-token", "refresh_token": "refresh-token"}
            )

    fake_creds = FakeCredentials()
    monkeypatch.setattr(
        "google.oauth2.credentials.Credentials.from_authorized_user_info",
        lambda token_info, scopes: fake_creds,
    )
    monkeypatch.setattr(
        "google.auth.transport.requests.Request",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr("arara_factory.publishing_reliable.time.sleep", sleeps.append)
    monkeypatch.setattr(
        "arara_factory.publishing_reliable.update_platform_credentials",
        lambda platform, value: saved.append((platform, value)),
    )

    class FakeSession:
        using_direct = False
        switch_calls = 0

        def switch_to_direct(self) -> bool:
            self.switch_calls += 1
            self.using_direct = True
            return True

    fake_session = FakeSession()
    monkeypatch.setattr(
        "arara_factory.publishing_reliable._make_authorized_session",
        lambda creds: fake_session,
    )
    monkeypatch.setattr(
        "arara_factory.publishing_reliable._create_upload_session",
        lambda session, body, total, progress: "https://upload.example/session",
    )
    monkeypatch.setattr(
        "arara_factory.publishing_reliable._upload_file_resumable",
        lambda session, url, path, progress: "yt-after-refresh",
    )

    result = publish_youtube_reliable(
        video,
        "ARARA",
        {
            "token": {"token": "expired", "refresh_token": "refresh-token"},
            "privacy_status": "private",
        },
        lambda value, text: None,
    )

    assert result == "yt-after-refresh"
    assert fake_creds.refresh_calls == 3
    assert request_modes == [False, False, True]
    assert sleeps == [2, 4]
    assert fake_session.using_direct is True
    assert fake_session.switch_calls == 1
    assert len(saved) == 1
    assert saved[0][0] == Platform.YOUTUBE.value
    assert saved[0][1]["token"]["token"] == "new-access-token"


def test_youtube_oauth_refresh_does_not_retry_permanent_error(monkeypatch) -> None:
    from google.auth.exceptions import RefreshError

    sleeps: list[int] = []
    monkeypatch.setattr("arara_factory.publishing_reliable.time.sleep", sleeps.append)

    class FakeCredentials:
        refresh_calls = 0

        def refresh(self, request) -> None:
            self.refresh_calls += 1
            raise RefreshError("invalid_grant")

    credentials = FakeCredentials()
    with pytest.raises(RuntimeError, match="invalid_grant"):
        _refresh_youtube_oauth(credentials, lambda: object(), lambda value, text: None)

    assert credentials.refresh_calls == 1
    assert sleeps == []


def test_youtube_oauth_refresh_retries_retryable_google_error(monkeypatch) -> None:
    from google.auth.exceptions import RefreshError

    sleeps: list[int] = []
    monkeypatch.setattr("arara_factory.publishing_reliable.time.sleep", sleeps.append)

    class FakeCredentials:
        refresh_calls = 0
        modes: list[str] = []

        def refresh(self, request) -> None:
            self.refresh_calls += 1
            self.modes.append(request)
            if self.refresh_calls < 3:
                raise RefreshError("temporarily_unavailable", retryable=True)

    credentials = FakeCredentials()
    direct = _refresh_youtube_oauth(
        credentials,
        lambda: "proxy",
        lambda value, text: None,
        direct_request_factory=lambda: "direct",
    )

    assert direct is False
    assert credentials.refresh_calls == 3
    assert credentials.modes == ["proxy", "proxy", "proxy"]
    assert sleeps == [2, 4]


def test_youtube_oauth_direct_probe_can_return_to_recovered_proxy(monkeypatch) -> None:
    sleeps: list[int] = []
    monkeypatch.setattr("arara_factory.publishing_reliable.time.sleep", sleeps.append)

    class FakeCredentials:
        modes: list[str] = []

        def refresh(self, request) -> None:
            self.modes.append(request)
            if request == "direct" or len(self.modes) <= 2:
                raise TimeoutError("route unavailable")

    credentials = FakeCredentials()
    direct = _refresh_youtube_oauth(
        credentials,
        lambda: "proxy",
        lambda value, text: None,
        direct_request_factory=lambda: "direct",
    )

    assert direct is False
    assert credentials.modes == ["proxy", "proxy", "direct", "direct", "proxy"]
    assert sleeps == [2, 4, 8, 16]


def test_youtube_oauth_request_uses_bounded_timeout() -> None:
    captured: dict = {}

    class InnerRequest:
        def __call__(self, **kwargs):
            captured.update(kwargs)
            return "ok"

    request = object.__new__(_BoundedAuthRequest)
    request._request = InnerRequest()
    request._direct_only = False

    assert request("https://oauth2.googleapis.com/token", method="POST") == "ok"
    assert captured["timeout"] == (CONNECT_TIMEOUT, READ_TIMEOUT)


def test_youtube_retry_after_supports_long_delay_and_http_date(monkeypatch) -> None:
    monkeypatch.setattr("arara_factory.publishing_reliable.time.time", lambda: 1_700_000_000.0)
    retry_at = datetime.fromtimestamp(1_700_000_120, tz=timezone.utc)

    assert _retry_after_seconds(FakeResponse(308, headers={"Retry-After": "120"})) == 120
    assert _retry_after_seconds(
        FakeResponse(
            308,
            headers={"Retry-After": format_datetime(retry_at, usegmt=True)},
        )
    ) == 120


def test_youtube_authorized_session_preserves_proxy_for_failed_direct_probe(
    monkeypatch,
) -> None:
    sessions: list[object] = []

    class FakeAuthorizedSession:
        def __init__(self, creds, **kwargs) -> None:
            self.closed = False
            self.trust_env = True
            self.verify = True
            sessions.append(self)

        def close(self) -> None:
            self.closed = True

    class FakeDirectTransport:
        verify = "custom-ca.pem"
        closed = False

        def close(self) -> None:
            self.closed = True

    direct_transport = FakeDirectTransport()
    monkeypatch.setattr(
        "google.auth.transport.requests.AuthorizedSession",
        FakeAuthorizedSession,
    )
    monkeypatch.setattr(
        "google.auth.transport.requests.Request",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        "arara_factory.publishing_reliable._direct_http_session",
        lambda: direct_transport,
    )

    session = _AuthorizedSessionWithDirectFallback(object())
    primary = session._session
    assert session.switch_to_direct() is True
    direct = session._session
    assert primary.closed is False
    assert direct is not primary
    assert session.switch_to_system_proxy() is True
    assert session._session is primary
    assert direct.closed is True
    assert direct_transport.closed is True
    assert session.switch_to_direct() is False


def test_youtube_upload_does_not_query_status_after_permanent_auth_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from google.auth.exceptions import RefreshError

    monkeypatch.setattr("arara_factory.publishing_reliable.YOUTUBE_CHUNK_SIZE", 4)
    sleeps: list[int] = []
    monkeypatch.setattr("arara_factory.publishing_reliable.time.sleep", sleeps.append)
    video = _video(tmp_path, data=b"abcdefgh")

    class Session:
        upload_calls = 0
        status_calls = 0

        def put(self, url, *, data, headers, timeout):
            if data == b"":
                self.status_calls += 1
                return FakeResponse(308)
            self.upload_calls += 1
            raise RefreshError("invalid_grant", retryable=False)

    session = Session()
    with pytest.raises(RuntimeError, match="invalid_grant"):
        _upload_file_resumable(
            session,
            "https://www.googleapis.com/upload/session",
            video,
            lambda value, text: None,
        )

    assert session.upload_calls == 1
    assert session.status_calls == 0
    assert sleeps == []


def test_youtube_upload_honors_retry_after_on_progressing_308(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("arara_factory.publishing_reliable.YOUTUBE_CHUNK_SIZE", 4)
    sleeps: list[int] = []
    monkeypatch.setattr("arara_factory.publishing_reliable.time.sleep", sleeps.append)
    video = _video(tmp_path, data=b"abcdefgh")

    class Session:
        upload_calls = 0

        def put(self, url, *, data, headers, timeout):
            self.upload_calls += 1
            if self.upload_calls == 1:
                return FakeResponse(
                    308,
                    headers={"Range": "bytes=0-3", "Retry-After": "9"},
                )
            return FakeResponse(200, payload={"id": "yt-after-retry-after"})

    result = _upload_file_resumable(
        Session(),
        "https://www.googleapis.com/upload/session",
        video,
        lambda value, text: None,
    )

    assert result == "yt-after-retry-after"
    assert sleeps == [9]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.googleapis.com/upload/session", True),
        ("https://oauth2.googleapis.com/token", True),
        ("http://www.googleapis.com/upload/session", False),
        ("https://googleapis.com.evil.example/upload", False),
        ("https://evil.example/upload", False),
    ],
)
def test_youtube_direct_transport_google_url_allowlist(url: str, expected: bool) -> None:
    assert _is_google_api_url(url) is expected
