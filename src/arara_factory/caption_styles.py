"""Original local caption presets, shared by the editor and ASS export."""
from dataclasses import dataclass
from functools import lru_cache
import os
from pathlib import Path
from PIL import ImageFont
from .subtitles import group_words


@dataclass(frozen=True)
class CaptionStyle:
    label: str
    font: str = 'Arial Black'
    accent: str = '#b0f563'
    uppercase: bool = True
    max_words: int = 3
    max_chars: int = 24
    outline: int = 6
    shadow: int = 2
    pop: bool = False
    highlight: bool = True


CAPTION_STYLES = {
    'reels_lime': CaptionStyle('Reels · лаймовая подсветка'),
    'reels_yellow': CaptionStyle('Reels · жёлтая подсветка', accent='#ffe24a'),
    'reels_pop': CaptionStyle('Pop · крупно по одному слову', accent='#ffe24a', max_words=1, pop=True),
    'karaoke': CaptionStyle('Классика · подсветка слов', font='Arial', uppercase=False,
                           max_words=4, max_chars=32, outline=4, shadow=1),
    'plain': CaptionStyle('Классика · обычные фразы', font='Arial', uppercase=False,
                         max_words=4, max_chars=32, outline=4, shadow=1, highlight=False),
}


def caption_groups(words, style_id):
    style = CAPTION_STYLES[style_id]
    return group_words(words, max_words=style.max_words, max_chars=style.max_chars)


def caption_lines(group, style_id):
    """Indices keep word highlighting stable when a phrase wraps to two lines."""
    style = CAPTION_STYLES[style_id]
    indices = list(range(len(group.words)))
    text = ' '.join(w.text for w in group.words)
    if style.uppercase and len(text) > 18 and len(indices) > 1:
        split = min(range(1, len(indices)), key=lambda n: abs(
            len(' '.join(w.text for w in group.words[:n])) -
            len(' '.join(w.text for w in group.words[n:]))))
        return [indices[:split], indices[split:]]
    return [indices]


def display_word(text, style):
    text = ' '.join(text.split())
    return text.upper() if style.uppercase else text


def caption_size(group, style_id, requested):
    style = CAPTION_STYLES[style_id]
    font = _measure_font(style.font, requested)
    lines = [' '.join(display_word(group.words[i].text, style) for i in line)
             for line in caption_lines(group, style_id)]
    width = max((font.getlength(line) if font else len(line)*requested*1.15 for line in lines), default=1)
    # Leave extra width for outline and the pop animation.
    return min(requested, max(1, int(requested * 860 / max(1, width))))


@lru_cache(maxsize=128)
def _measure_font(family, size):
    windows_name = 'ariblk.ttf' if family == 'Arial Black' else 'arialbd.ttf'
    for filename in [str(Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / windows_name), 'DejaVuSans-Bold.ttf']:
        try:
            return ImageFont.truetype(filename, size)
        except OSError:
            pass
    return None


def active_word(group, position):
    return next((i for i, w in enumerate(group.words) if w.start <= position < w.end), -1)


def caption_intervals(group, style):
    if not style.highlight:
        return [(group.start, group.end, -1)]
    # A pause must not pretend the preceding word is still being spoken.
    boundaries = sorted({w.start for w in group.words} | {w.end for w in group.words})
    return [(a, b, active_word(group, (a+b)/2)) for a, b in zip(boundaries, boundaries[1:]) if b > a]


def ass_color(hex_color):
    r, g, b = hex_color[1:3], hex_color[3:5], hex_color[5:7]
    return f'&H00{b}{g}{r}&'
