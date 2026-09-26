from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from arara_factory.publishing import Platform, PublishQueue
from arara_factory.publishing_reliable_ui import (
    ReliablePublishingWindow,
    ReliablePublishWorker,
)
from arara_factory.publishing_targets_ui import TargetAwareSmartWindow


def _video(tmp_path: Path, name: str = "reel.mp4") -> Path:
    path = tmp_path / name
    path.write_bytes(b"video")
    return path


class _Box:
    def __init__(self, checked: bool) -> None:
        self._checked = checked

    def isChecked(self) -> bool:  # noqa: N802 - Qt-compatible test double
        return self._checked

    def setChecked(self, checked: bool) -> None:  # noqa: N802
        self._checked = bool(checked)


def test_deselecting_platform_in_ui_preserves_old_deliveries_and_jobs(
    tmp_path: Path,
) -> None:
    path = tmp_path / "queue.json"
    queue = PublishQueue(path)
    job = queue.enqueue(
        [_video(tmp_path)],
        [Platform.TIKTOK, Platform.YOUTUBE],
        "ARARA",
        15,
        start_at=1.0,
    )[0]
    before_ids = [item.id for item in queue.jobs]
    before_deliveries = set(job.deliveries)

    window = SimpleNamespace(
        platform_boxes={Platform.YOUTUBE: _Box(True)},
        schedule_platform_boxes={Platform.YOUTUBE: _Box(True)},
        _sync_publish_workflow=lambda: None,
        publish_queue=queue,
    )
    TargetAwareSmartWindow._schedule_target_toggled(
        window,
        Platform.YOUTUBE,
        False,
    )

    assert window.platform_boxes[Platform.YOUTUBE].isChecked() is False
    assert [item.id for item in queue.jobs] == before_ids
    assert set(job.deliveries) == before_deliveries
    restored = PublishQueue(path)
    assert [item.id for item in restored.jobs] == before_ids
    assert set(restored.jobs[0].deliveries) == before_deliveries


def test_reliable_worker_uploads_only_allowed_platforms(
    tmp_path: Path,
    monkeypatch,
) -> None:
    queue = PublishQueue(tmp_path / "queue.json")
    job = queue.enqueue(
        [_video(tmp_path)],
        [Platform.TIKTOK, Platform.YOUTUBE],
        "ARARA",
        15,
        start_at=1.0,
    )[0]
    uploaded: list[Platform] = []

    def publish(platform, video, caption, progress):
        uploaded.append(platform)
        progress(100, "done")
        return f"remote-{platform.value}"

    monkeypatch.setattr(
        "arara_factory.publishing_reliable_ui.publish_platform_reliable",
        publish,
    )
    monkeypatch.setattr(
        "arara_factory.publishing_reliable_ui.append_publish_log",
        lambda message: message,
    )

    worker = ReliablePublishWorker(
        queue,
        job,
        {Platform.YOUTUBE},
    )
    worker.run()

    assert uploaded == [Platform.YOUTUBE]
    restored = PublishQueue(tmp_path / "queue.json").jobs[0]
    assert restored.deliveries[Platform.YOUTUBE.value].status == "success"
    assert restored.deliveries[Platform.TIKTOK.value].status == "pending"
    assert restored.deliveries[Platform.TIKTOK.value].attempts == 0


def test_deselected_pending_delivery_is_not_runnable_until_reselected(
    tmp_path: Path,
) -> None:
    queue = PublishQueue(tmp_path / "queue.json")
    job = queue.enqueue(
        [_video(tmp_path)],
        [Platform.TIKTOK],
        "ARARA",
        15,
        start_at=1.0,
    )[0]
    selected = [Platform.YOUTUBE]
    window = SimpleNamespace(
        publish_queue=queue,
        selected_platforms=lambda: list(selected),
    )

    assert ReliablePublishingWindow._next_runnable_job(window) is None
    assert job.deliveries[Platform.TIKTOK.value].status == "pending"

    selected[:] = [Platform.TIKTOK]
    assert ReliablePublishingWindow._next_runnable_job(window) is job


def test_process_queue_passes_selected_platform_snapshot_to_worker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    queue = PublishQueue(tmp_path / "queue.json")
    job = queue.enqueue(
        [_video(tmp_path)],
        [Platform.TIKTOK, Platform.YOUTUBE],
        "ARARA",
        15,
        start_at=1.0,
    )[0]
    created: list[object] = []

    class SignalStub:
        def connect(self, callback) -> None:
            pass

    class WorkerStub:
        def __init__(self, received_queue, received_job, allowed_platforms) -> None:
            self.queue = received_queue
            self.job = received_job
            self.allowed_platforms = set(allowed_platforms)
            self.progressed = SignalStub()
            self.logged = SignalStub()
            self.completed = SignalStub()
            self.failed = SignalStub()
            self.started = False
            created.append(self)

        def isRunning(self) -> bool:  # noqa: N802
            return False

        def start(self) -> None:
            self.started = True

    monkeypatch.setattr(
        "arara_factory.publishing_reliable_ui.ReliablePublishWorker",
        WorkerStub,
    )
    window = SimpleNamespace(
        publish_timer=SimpleNamespace(isActive=lambda: True),
        publish_worker=None,
        batch_worker=None,
        render_worker=None,
        publish_queue=queue,
        selected_platforms=lambda: [Platform.YOUTUBE],
        _next_runnable_job=lambda: job,
        on_publish_progress=lambda *args: None,
        _append_ui_log=lambda *args: None,
        publish_done=lambda *args: None,
        publish_failed=lambda *args: None,
        refresh_publish_status=lambda: None,
    )

    ReliablePublishingWindow.process_publish_queue(window)

    assert len(created) == 1
    assert created[0].allowed_platforms == {Platform.YOUTUBE}
    assert created[0].started is True

