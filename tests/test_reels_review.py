from arara_factory.reels_review import write_review


def test_review_escapes_transcript_and_keeps_local_urls(tmp_path):
    transcript = tmp_path / 'clip.txt'
    transcript.write_text('<script>alert(1)</script>', encoding='utf-8')
    clip = {'output': str(tmp_path / 'clip with space.mp4'), 'source': 'source <name>.mp4',
            'start': 1, 'end': 30, 'reason': 'speech <reason>',
            'project': str(tmp_path / 'clip.verticliv.json'), 'transcript': str(transcript)}
    page = tmp_path / 'review.html'
    write_review(page, [clip], 'completed')
    html = page.read_text(encoding='utf-8')
    assert '<script>' not in html
    assert '&lt;script&gt;' in html
    assert 'src="clip%20with%20space.mp4"' in html
    assert 'https://' not in html
    assert 'Подборка готова' in html
    assert not page.with_suffix('.html.tmp').exists()


def test_empty_cancelled_review_is_explicit(tmp_path):
    page = tmp_path / 'review.html'
    write_review(page, [], 'cancelled')
    assert 'Пока нет готовых роликов' in page.read_text(encoding='utf-8')
    assert 'остановлена' in page.read_text(encoding='utf-8')
