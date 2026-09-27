"""Validated Twitch references for manually imported recordings.

This module does not connect an account, download media, or check whether a VOD
is published. A dashboard reference may point to an unpublished recording.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Literal
from urllib.parse import parse_qs, urlsplit


_VOD_ID = r"[1-9][0-9]{0,19}"
_CHANNEL = r"[a-zA-Z0-9_]{1,25}"
_TIMESTAMP = re.compile(r"(?:[0-9]{1,7}|(?=[0-9])(?:[0-9]{1,4}h)?(?:[0-9]{1,3}m)?(?:[0-9]{1,3}s)?)\Z")
_ERROR = (
    "Укажи HTTPS-ссылку на запись Twitch (/videos/123), "
    "её страницу в видеостудии или библиотеку записей канала."
)


@dataclass(frozen=True)
class TwitchSource:
    """Source identity, not proof of ownership, publication, or account access."""

    kind: Literal["vod", "library"]
    canonical_url: str
    vod_id: str | None = None
    channel: str | None = None

    def to_dict(self) -> dict[str, str]:
        """JSON-ready provenance, with no raw query strings or credentials."""
        return {key: value for key, value in asdict(self).items() if value is not None}


def _check_query(query: str, *, public_vod: bool = False, public_library: bool = False) -> None:
    if not query:
        return
    try:
        values = parse_qs(query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise ValueError(_ERROR) from exc
    if any(len(items) != 1 for items in values.values()):
        raise ValueError(_ERROR)
    if public_vod and set(values) == {"t"} and _TIMESTAMP.fullmatch(values["t"][0]):
        return
    if public_library and set(values) <= {"filter", "sort"}:
        if values.get("filter", ["all"])[0] not in {"all", "archives", "highlights", "uploads"}:
            raise ValueError(_ERROR)
        if values.get("sort", ["time"])[0] not in {"time", "views"}:
            raise ValueError(_ERROR)
        return
    raise ValueError(_ERROR)


def parse_twitch_source(value: str) -> TwitchSource:
    """Parse only known Twitch VOD/library routes; return a canonical reference.

    A public VOD's optional playback timestamp and a public library's filtering
    options do not identify a different source and are intentionally discarded.
    Unknown parameters, fragments, credentials, ports, and non-Twitch hosts are
    rejected. This performs no network requests or filesystem operations.
    """
    if not isinstance(value, str):
        raise ValueError(_ERROR)
    value = value.strip()
    if not value or len(value) > 2048 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(_ERROR)
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ValueError(_ERROR) from exc
    if parsed.scheme.lower() != "https" or parsed.fragment or "#" in value or "\\" in value:
        raise ValueError(_ERROR)
    host = parsed.netloc.lower()
    if host not in {"twitch.tv", "www.twitch.tv", "dashboard.twitch.tv"}:
        raise ValueError(_ERROR)

    if host in {"twitch.tv", "www.twitch.tv"}:
        match = re.fullmatch(rf"/videos/({_VOD_ID})/?", parsed.path)
        if match:
            _check_query(parsed.query, public_vod=True)
            vod_id = match[1]
            return TwitchSource("vod", f"https://www.twitch.tv/videos/{vod_id}", vod_id=vod_id)
        match = re.fullmatch(rf"/({_CHANNEL})/videos/?", parsed.path)
        if match:
            _check_query(parsed.query, public_library=True)
            channel = match[1].lower()
            return TwitchSource("library", f"https://www.twitch.tv/{channel}/videos", channel=channel)
    else:
        match = re.fullmatch(rf"/u/({_CHANNEL})/content/video-producer(?:/edit/({_VOD_ID}))?/?", parsed.path)
        if match:
            _check_query(parsed.query)
            channel, vod_id = match[1].lower(), match[2]
            canonical = f"https://dashboard.twitch.tv/u/{channel}/content/video-producer"
            if vod_id:
                return TwitchSource("vod", canonical + f"/edit/{vod_id}", vod_id=vod_id, channel=channel)
            return TwitchSource("library", canonical, channel=channel)
    raise ValueError(_ERROR)
