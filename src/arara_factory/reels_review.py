"""Self-contained local contact sheet; no trackers, uploads or external assets."""
from html import escape
from pathlib import Path
from urllib.parse import quote


def write_review(path, clips, status):
    cards = []
    for clip in clips:
        output = Path(clip['output'])
        title = escape(output.stem)
        src = quote(output.name)
        transcript = ''
        if clip.get('transcript'):
            try:
                transcript = Path(clip['transcript']).read_text(encoding='utf-8')
            except OSError:
                pass
        timing = f"{clip['start']:.1f}–{clip['end']:.1f} сек"
        cards.append(f'''<article><video controls preload="none" src="{src}"></video>
<section><h2>{title}</h2><p>{escape(Path(clip['source']).name)} · {timing}</p>
<p class="reason">{escape(clip['reason'])}</p>
<details><summary>Текст субтитров</summary><p>{escape(transcript) or 'Нет субтитров'}</p></details>
<p><a href="{src}">Открыть MP4</a> · <a href="{quote(Path(clip['project']).name)}" download>Проект</a></p>
</section></article>''')
    state = {'running': 'Обработка продолжается', 'completed': 'Подборка готова',
             'completed_with_errors': 'Есть ошибки — смотри report.json',
             'cancelled': 'Обработка остановлена; готовые клипы сохранены'}.get(status, status)
    page = '''<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Verticliv · подборка</title>
<style>body{background:#111827;color:#e6edf7;font:16px/1.5 system-ui;margin:32px}
h1{color:#b0f563}h2{font-size:17px;overflow-wrap:anywhere}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:24px}
article{background:#1b283d;border-radius:14px;overflow:hidden}video{display:block;background:#080d16;width:100%;height:460px;object-fit:contain}
section{padding:18px}p{overflow-wrap:anywhere}a{color:#b0f563}.reason{color:#afbdd3}header{max-width:1000px;margin-bottom:26px}</style>
<header><h1>Verticliv · подборка</h1><p>''' + escape(state) + '''</p>
<p>Проверь начало, конец, кадрирование и субтитры перед публикацией. Отбор эвристический, не гарантия интересности.
Это локальная страница: записи никуда не отправляются. Для исправлений открой проект в Verticliv.</p></header><main>'''
    page += ''.join(cards) or '<p>Пока нет готовых роликов.</p>'
    page += '</main></html>'
    path = Path(path)
    temp = path.with_suffix('.html.tmp')
    temp.write_text(page, encoding='utf-8')
    temp.replace(path)
