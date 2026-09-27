from __future__ import annotations

import urllib.error

import pytest

from arara_factory.updater import (
    check_for_update,
    is_newer_version,
    release_from_payload,
    version_tuple,
)


def test_version_comparison() -> None:
    assert version_tuple("v0.10.0") == (0, 10, 0)
    assert is_newer_version("0.10.0", "0.9.0")
    assert is_newer_version("v1.0.0", "0.10.9")
    assert not is_newer_version("0.10.0", "0.10.0")
    assert not is_newer_version("0.9.9", "0.10.0")


def test_release_asset_is_selected() -> None:
    payload = {
        "tag_name": "v0.11.0",
        "assets": [
            {
                "name": "ARARA-Factory-Setup.exe",
                "browser_download_url": "https://github.com/cl0vo/vertical/releases/download/v0.11.0/ARARA-Factory-Setup.exe",
                "size": 123456,
                "digest": "sha256:" + "0" * 64,
            }
        ],
    }
    info = release_from_payload(payload, "0.10.0")
    assert info is not None
    assert info.version == "0.11.0"
    assert info.size == 123456


def test_current_or_missing_release_is_ignored() -> None:
    assert release_from_payload({"tag_name": "v0.10.0", "assets": []}, "0.10.0") is None
    assert release_from_payload({"tag_name": "v0.11.0", "assets": []}, "0.10.0") is None


def test_verticliv_installer_is_preferred_over_legacy_alias() -> None:
    assets = [
        {"name": name, "browser_download_url": f"https://github.com/cl0vo/Verticliv/releases/download/v0.19.0/{name}"}
        for name in ("ARARA-Factory-Setup.exe", "Verticliv-Setup.exe")
    ]
    info = release_from_payload({"tag_name": "v0.19.0", "assets": assets}, "0.18.0")
    assert info is not None
    assert info.download_url.endswith("/Verticliv-Setup.exe")


def test_updater_uses_renamed_repository() -> None:
    from arara_factory.updater import LATEST_RELEASE_API
    assert LATEST_RELEASE_API == "https://api.github.com/repos/cl0vo/Verticliv/releases/latest"


def test_http_404_reports_that_release_is_not_published(monkeypatch) -> None:
    def missing_release(*args, **kwargs):
        raise urllib.error.HTTPError(
            url="https://api.github.com/repos/cl0vo/vertical/releases/latest",
            code=404,
            msg="Not Found",
            hdrs=None,
            fp=None,
        )

    monkeypatch.setattr("arara_factory.updater.urllib.request.urlopen", missing_release)
    with pytest.raises(RuntimeError, match="Release ещё не опубликован"):
        check_for_update("0.10.1")
