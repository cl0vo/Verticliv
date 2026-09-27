from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

REPOSITORY = "cl0vo/Verticliv"
LATEST_RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
INSTALLER_ASSET_NAME = "Verticliv-Setup.exe"
LEGACY_INSTALLER_ASSET_NAME = "ARARA-Factory-Setup.exe"
USER_AGENT = "Verticliv-Updater"


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    tag: str
    download_url: str
    size: int
    digest: str | None = None


def version_tuple(value: str) -> tuple[int, ...]:
    clean = value.strip().lower().lstrip("v")
    parts: list[int] = []
    for item in clean.split("."):
        digits = "".join(char for char in item if char.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts or [0])


def is_newer_version(latest: str, current: str) -> bool:
    left = list(version_tuple(latest))
    right = list(version_tuple(current))
    width = max(len(left), len(right))
    left.extend([0] * (width - len(left)))
    right.extend([0] * (width - len(right)))
    return tuple(left) > tuple(right)


def release_from_payload(payload: dict, current_version: str) -> UpdateInfo | None:
    tag = str(payload.get("tag_name") or "").strip()
    if not tag or not is_newer_version(tag, current_version):
        return None

    assets = payload.get("assets") or []
    asset = next(
        (
            item
            for name in (INSTALLER_ASSET_NAME, LEGACY_INSTALLER_ASSET_NAME)
            for item in assets
            if str(item.get("name") or "").lower() == name.lower()
        ),
        None,
    )
    if not asset:
        return None

    url = str(asset.get("browser_download_url") or "").strip()
    if not url.startswith("https://github.com/"):
        return None

    digest = str(asset.get("digest") or "").strip() or None
    return UpdateInfo(
        version=tag.lstrip("vV"),
        tag=tag,
        download_url=url,
        size=int(asset.get("size") or 0),
        digest=digest,
    )


def check_for_update(current_version: str, timeout: int = 15) -> UpdateInfo | None:
    request = urllib.request.Request(
        LATEST_RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RuntimeError(
                "GitHub Release ещё не опубликован. Дождись зелёной сборки Actions."
            ) from exc
        raise RuntimeError(f"GitHub вернул ошибку HTTP {exc.code} при проверке обновления.") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("Не удалось подключиться к GitHub для проверки обновления.") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("GitHub вернул некорректные данные обновления.") from exc
    return release_from_payload(payload, current_version)


def update_directory() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    root = Path(local) if local else Path.home() / "AppData" / "Local"
    target = root / "ARARA Factory" / "updates"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _verify_digest(path: Path, digest: str | None) -> None:
    if not digest or not digest.lower().startswith("sha256:"):
        return
    expected = digest.split(":", 1)[1].strip().lower()
    actual = hashlib.sha256(path.read_bytes()).hexdigest().lower()
    if actual != expected:
        raise RuntimeError("Контрольная сумма обновления не совпала. Файл удалён.")


def download_update(
    info: UpdateInfo,
    progress: Callable[[int], None] | None = None,
    timeout: int = 60,
) -> Path:
    target = update_directory() / f"Verticliv-Setup-{info.version}.exe"
    partial = target.with_suffix(".download")
    request = urllib.request.Request(
        info.download_url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/octet-stream"},
    )

    received = 0
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response, partial.open("wb") as output:
            total = int(response.headers.get("Content-Length") or info.size or 0)
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                received += len(chunk)
                if progress and total > 0:
                    progress(min(99, int(received * 100 / total)))
        if info.size and received != info.size:
            raise RuntimeError(
                f"Обновление скачалось не полностью: {received} из {info.size} байт."
            )
        partial.replace(target)
        _verify_digest(target, info.digest)
        if progress:
            progress(100)
        return target
    except Exception:
        partial.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        raise


def launch_installer(installer: Path) -> None:
    if sys.platform != "win32":
        raise RuntimeError("Автообновление поддерживается только на Windows.")
    if not installer.is_file():
        raise FileNotFoundError(installer)

    flags = 0
    flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
    flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(
        [
            str(installer),
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/CLOSEAPPLICATIONS",
            "/RESTARTAPPLICATIONS",
        ],
        cwd=str(installer.parent),
        creationflags=flags,
        close_fds=True,
    )
