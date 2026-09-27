import os
import shutil
import subprocess

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from arara_factory.caption_styles import (
    CAPTION_STYLES, caption_groups, caption_intervals, caption_lines, caption_size, ass_color,
)
from arara_factory.studio_engine import StudioProject, write_captions, export_video
from arara_factory.transcribe import RecognizedWord as Word


@pytest.mark.parametrize('style_id', CAPTION_STYLES)
def test_presets_export_word_timing_and_unstyled_srt(tmp_path, style_id):
    words = [Word('Привет', 10, 10.2, .9), Word('мир', 10.4, 10.7, .9)]
    project = StudioProject(start=10, end=11, caption_style=style_id, words=words)
    ass, srt = tmp_path/'test.ass', tmp_path/'test.srt'
    write_captions(project, ass, srt)
    text = ass.read_text(encoding='utf-8-sig')
    assert CAPTION_STYLES[style_id].font in text
    assert 'Привет' in srt.read_text(encoding='utf-8-sig')
    assert '\\pos(540,1500)' in text
    if CAPTION_STYLES[style_id].highlight:
        assert ass_color(CAPTION_STYLES[style_id].accent) in text
    if style_id == 'reels_pop':
        assert '\\t(0,90,' in text
    elif style_id.startswith('reels_'):
        assert 'ПРИВЕТ' in text


def test_highlight_does_not_extend_across_pause():
    words = [Word('раз', 0, .2, 1), Word('два', .4, .6, 1)]
    group = caption_groups(words, 'reels_lime')[0]
    assert caption_intervals(group, CAPTION_STYLES['reels_lime']) == [(0, .2, 0), (.2, .4, -1), (.4, .6, 1)]


def test_long_caption_wrap_and_width():
    words = [Word('Сверхдлинное', 0, 1, 1), Word('предложение', 1, 2, 1)]
    group = caption_groups(words, 'reels_lime')[0]
    assert caption_lines(group, 'reels_lime') == [[0], [1]]
    wide = caption_groups([Word('W'*18, 0, 1, 1)], 'reels_lime')[0]
    assert caption_size(wide, 'reels_lime', 100) < 100


def test_invalid_preset_rejected_and_old_presets_preserved():
    with pytest.raises(ValueError, match='шаблон'):
        StudioProject(caption_style='missing').validate(60)
    from arara_factory.auto_reels import AutoReelsOptions
    with pytest.raises(ValueError, match='шаблон'):
        AutoReelsOptions(caption_style='missing').validate()
    for style in ('karaoke', 'plain'):
        StudioProject(caption_style=style).validate(60)


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='FFmpeg required')
@pytest.mark.parametrize('style_id', ['reels_lime', 'reels_yellow', 'reels_pop'])
def test_real_preset_render_has_colored_glyphs(tmp_path, style_id):
    from PIL import Image
    source, output, frame = tmp_path/'source.mp4', tmp_path/'result.mp4', tmp_path/'frame.png'
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=black:s=640x360:r=30',
                    '-t', '1', '-c:v', 'libx264', str(source)], check=True)
    project = StudioProject(source=str(source), end=.8, caption_style=style_id,
                            words=[Word('Привет', 0, .7, 1)], encoder_mode='cpu')
    export_video(project, output)
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', '0.3', '-i', str(output), '-frames:v', '1', str(frame)], check=True)
    with Image.open(frame) as rendered:
        colorful = [(x, y) for y in range(1250, 1550) for x in range(60, 1020)
                    if (lambda c: c[0]>90 and c[1]>130 and c[2]<110)(rendered.getpixel((x, y)))]
    assert len(colorful) > 500
    assert min(x for x, _ in colorful) >= 70 and max(x for x, _ in colorful) <= 1010
