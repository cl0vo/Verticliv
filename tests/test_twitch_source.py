import json

import pytest

from arara_factory.twitch_source import parse_twitch_source


def test_public_vod_reference_is_canonical_without_claiming_channel_access():
    source = parse_twitch_source('  https://TWITCH.TV/videos/2884988010/?t=1h2m3s  ')
    assert source.kind == 'vod'
    assert source.vod_id == '2884988010'
    assert source.channel is None
    assert source.canonical_url == 'https://www.twitch.tv/videos/2884988010'
    assert json.loads(json.dumps(source.to_dict())) == {
        'kind': 'vod', 'vod_id': '2884988010',
        'canonical_url': 'https://www.twitch.tv/videos/2884988010',
    }


def test_observed_unpublished_dashboard_vod_retains_dashboard_reference():
    url = 'https://dashboard.twitch.tv/u/levakiselev/content/video-producer/edit/2884988010'
    source = parse_twitch_source(url)
    assert source.kind == 'vod'
    assert source.channel == 'levakiselev'
    assert source.vod_id == '2884988010'
    assert source.canonical_url == url
    # Do not convert an unpublished editor URL into a claim of public access.
    assert source.to_dict()['canonical_url'].startswith('https://dashboard.twitch.tv/')


@pytest.mark.parametrize('url,canonical', [
    ('https://dashboard.twitch.tv/u/Levakiselev/content/video-producer/',
     'https://dashboard.twitch.tv/u/levakiselev/content/video-producer'),
    ('https://www.twitch.tv/Levakiselev/videos?filter=archives&sort=time',
     'https://www.twitch.tv/levakiselev/videos'),
])
def test_library_is_not_mistaken_for_a_downloadable_recording(url, canonical):
    source = parse_twitch_source(url)
    assert source.kind == 'library'
    assert source.vod_id is None
    assert source.channel == 'levakiselev'
    assert source.canonical_url == canonical
    assert 'vod_id' not in source.to_dict()


@pytest.mark.parametrize('url', [
    '', None, 2884988010,
    'http://www.twitch.tv/videos/2884988010',
    '//www.twitch.tv/videos/2884988010',
    'https://www.twitch.tv.evil.example/videos/2884988010',
    'https://evil.example/www.twitch.tv/videos/2884988010',
    'https://www.twitch.tv@evil.example/videos/2884988010',
    'https://user:secret@www.twitch.tv/videos/2884988010',
    'https://www.twitch.tv:443/videos/2884988010',
    'https://www.twitch.tv./videos/2884988010',
    'https://www.twitch.tv/videos/2884988010/extra',
    'https://www.twitch.tv/videos/0',
    'https://www.twitch.tv/videos/02884988010',
    'https://www.twitch.tv/videos/２８８４９８８０１０',
    'https://www.twitch.tv/videos/%32%38%38',
    'https://www.twitch.tv/videos/2884988010#token',
    'https://www.twitch.tv/videos/2884988010#',
    'https://www.twitch.tv/videos/2884988010?access_token=secret',
    'https://www.twitch.tv/videos/2884988010?t=1m&t=2m',
    'https://www.twitch.tv/videos/2884988010?t=',
    'https://www.twitch.tv/videos/2884988010?t=tomorrow',
    'https://www.twitch.tv/videos/2884988010?filter=archives',
    'https://www.twitch.tv/levakiselev/videos?filter=secret',
    'https://www.twitch.tv/levakiselev/videos?sort=unknown',
    'https://www.twitch.tv/levakiselev/videos?token=secret',
    'https://dashboard.twitch.tv/u/levakiselev/content/video-producer?token=secret',
    'https://dashboard.twitch.tv/u/levakiselev/content/video-producer/edit/not-a-number',
    'https://www.twitch.tv/u/levakiselev/content/video-producer/edit/2884988010',
    'https://dashboard.twitch.tv/videos/2884988010',
    'https://www.twitch.tv\\evil.example/videos/2884988010',
    'https://www.twitch.tv/vid\neos/2884988010',
    'https://www.twitch.tv/videos/2884988010\x00',
    'https://www.twitch.tv/videos/' + '1' * 2100,
])
def test_invalid_or_sensitive_urls_are_rejected_without_echoing_input(url):
    with pytest.raises(ValueError) as error:
        parse_twitch_source(url)
    assert 'secret' not in str(error.value)


@pytest.mark.parametrize('timestamp', ['30', '1h', '02m', '30s', '1h2m', '2m30s'])
def test_share_link_timestamp_is_not_kept_as_source_metadata(timestamp):
    source = parse_twitch_source('https://www.twitch.tv/videos/2884988010?t=' + timestamp)
    assert source.canonical_url.endswith('/2884988010')
    assert '?' not in json.dumps(source.to_dict())
