from __future__ import annotations

import json
from pathlib import Path

import pytest

from arara_factory.brainrot_index import (
    INDEX_VERSION,
    _fingerprint,
    choose_segment,
    index_path,
    mark_segment_used,
)
from arara_factory.render import (
    MediaInfo,
    RenderOptions,
    _banner_geometry,
    _banner_input_args,
    _partial_output,
    _progress_seconds,
    _validate_banner_format,
    _validate_render_output,
    _video_encoder_args,
)


def test_partial_output_keeps_mp4_extension() -> None:
    final = Path('clip_ready_v1.mp4')
    partial = _partial_output(final)
    assert partial.name == 'clip_ready_v1.part.mp4'
    assert partial.suffix == '.mp4'


def test_ffmpeg_progress_parser_supports_both_formats() -> None:
    assert _progress_seconds('out_time_ms=12500000') == 12.5
    assert _progress_seconds('out_time=00:01:02.500000') == 62.5
    assert _progress_seconds('progress=continue') is None


def test_banner_is_aspect_safe_centered_and_in_top_safe_area() -> None:
    canvas = MediaInfo(1080, 1920, 12.0, 30.0, True)
    banner = MediaInfo(1920, 1080, 12.6, 30.0, False, has_alpha=True)

    assert _banner_geometry(canvas, banner, RenderOptions()) == (0, 48, 1080, 608)


def test_nvenc_profile_is_balanced_for_the_local_gtx_970(monkeypatch) -> None:
    monkeypatch.setattr("arara_factory.render._has_nvenc", lambda _ffmpeg: True)

    args, name = _video_encoder_args("ffmpeg", RenderOptions(crf=22))

    assert name == "NVIDIA NVENC"
    assert args[args.index("-preset") + 1] == "p4"
    assert args[args.index("-multipass") + 1] == "disabled"
    assert args[args.index("-rc-lookahead") + 1] == "0"
    assert args[args.index("-cq") + 1] == "22"


def test_vp9_banner_input_loops_and_forces_alpha_capable_decoder() -> None:
    banner = MediaInfo(
        1920,
        1080,
        12.6,
        30.0,
        False,
        codec_name='vp9',
        pixel_format='yuv420p',
        has_alpha=True,
    )
    args = _banner_input_args(Path('banner.webm'), banner, 15.0)

    assert args == [
        '-stream_loop', '-1',
        '-c:v', 'libvpx-vp9',
        '-t', '15.000',
        '-i', 'banner.webm',
    ]
    assert '-an' not in args


def test_banner_quality_gate_requires_transparency() -> None:
    with pytest.raises(RuntimeError, match='alpha'):
        _validate_banner_format(MediaInfo(1920, 1080, 12.6, 30.0, False))

    _validate_banner_format(
        MediaInfo(1920, 1080, 12.6, 30.0, False, has_alpha=True)
    )


def test_brainrot_is_committed_only_after_success(tmp_path: Path) -> None:
    video = tmp_path / 'brainrot.mp4'
    video.write_bytes(b'fake-video-for-index-state')
    payload = {
        'version': INDEX_VERSION,
        'source': str(video.resolve()),
        'fingerprint': _fingerprint(video),
        'duration': 60.0,
        'min_clip': 9.0,
        'max_clip': 15.0,
        'segments': [
            {'start': 0.0, 'duration': 15.0},
            {'start': 15.0, 'duration': 15.0},
        ],
        'used': [],
    }
    index_path(video).write_text(json.dumps(payload), encoding='utf-8')

    selected = choose_segment(
        'ffprobe-not-needed',
        video,
        12.0,
        seed=7,
        mark_used=False,
    )
    before = json.loads(index_path(video).read_text(encoding='utf-8'))
    assert before['used'] == []

    mark_segment_used(video, selected.index)
    after = json.loads(index_path(video).read_text(encoding='utf-8'))
    assert after['used'] == [selected.index]


def test_render_output_quality_gate_accepts_publishable_vertical_reel() -> None:
    _validate_render_output(
        MediaInfo(
            width=1080,
            height=1920,
            duration=12.04,
            fps=30.0,
            has_audio=True,
        ),
        expected_duration=12.0,
    )


def test_render_output_quality_gate_rejects_bad_aspect_ratio() -> None:
    with pytest.raises(RuntimeError, match='9:16'):
        _validate_render_output(
            MediaInfo(1920, 1080, 12.0, 30.0, True),
            expected_duration=12.0,
        )


def test_render_output_quality_gate_rejects_missing_audio() -> None:
    with pytest.raises(RuntimeError, match='аудиодорожки'):
        _validate_render_output(
            MediaInfo(1080, 1920, 12.0, 30.0, False),
            expected_duration=12.0,
        )


@pytest.mark.parametrize(
    ('info', 'message'),
    [
        (MediaInfo(360, 640, 12.0, 30.0, True), 'низкое разрешение'),
        (MediaInfo(1080, 1920, 12.0, 12.0, True), 'частота кадров'),
        (MediaInfo(1080, 1920, 9.0, 30.0, True), 'длительность'),
    ],
)
def test_render_output_quality_gate_rejects_unsafe_result(
    info: MediaInfo,
    message: str,
) -> None:
    with pytest.raises(RuntimeError, match=message):
        _validate_render_output(info, expected_duration=12.0)
