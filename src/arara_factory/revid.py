from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

API_BASE = "https://www.revid.ai/api/public/v3"


@dataclass(slots=True)
class RevidProject:
    pid: str
    status: str | None = None
    video_url: str | None = None


def _request(path: str, api_key: str, *, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if not api_key.strip():
        raise ValueError("Revid API key is empty")
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{API_BASE}{path}",
        data=data,
        headers={"Content-Type": "application/json", "key": api_key.strip()},
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Revid API error {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Cannot reach Revid API: {exc.reason}") from exc


def create_static_background_video(
    api_key: str,
    *,
    source_url: str,
    background_prompt: str = "GTA-style falling cars stunt gameplay, vertical, no text",
    captions: bool = False,
    music: bool = False,
) -> RevidProject:
    payload: dict[str, Any] = {
        "workflow": "static-background-video",
        "source": {"url": source_url},
        "media": {
            "prompt": background_prompt,
            "type": "gameplay",
        },
        "captions": {"enabled": captions},
        "music": {"enabled": music},
        "general": {"ratio": "9:16"},
    }
    result = _request("/render", api_key, payload=payload)
    pid = result.get("pid") or result.get("projectId")
    if not pid:
        raise RuntimeError(f"Revid did not return a project id: {result}")
    return RevidProject(pid=str(pid), status=result.get("status"))


def get_status(api_key: str, pid: str) -> RevidProject:
    result = _request(f"/status?pid={pid}", api_key)
    return RevidProject(
        pid=pid,
        status=result.get("status"),
        video_url=result.get("videoUrl") or result.get("video_url") or result.get("url"),
    )


def wait_for_video(api_key: str, pid: str, *, timeout_seconds: int = 1800, poll_seconds: int = 8) -> RevidProject:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        project = get_status(api_key, pid)
        if project.video_url:
            return project
        if (project.status or "").lower() in {"failed", "error", "cancelled"}:
            raise RuntimeError(f"Revid render failed with status: {project.status}")
        time.sleep(poll_seconds)
    raise TimeoutError(f"Revid render {pid} did not finish in time")


def download_video(url: str, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "Verticliv"})
    with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            output.write(chunk)
    return destination
