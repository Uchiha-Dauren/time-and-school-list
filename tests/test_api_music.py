"""Focused API checks using the shared school fixture."""
from collections import Counter
import pytest
from test_api import api, create_small_school, wav_bytes


def test_valid_upload_playlist_dry_run_stop_and_safe_filename(api):
    client, _, uploads = api
    response = client.post('/api/music/upload', files={'file': ('../../outside.wav', wav_bytes(), 'audio/wav')})
    assert response.status_code == 201, response.text
    track = response.json()
    assert track['original_name'] == 'outside.wav'
    paths = list(uploads.iterdir())
    assert len(paths) == 1 and paths[0].suffix == '.wav'
    assert paths[0].name != 'outside.wav'
    assert not (uploads.parent / 'outside.wav').exists()
    assert client.put('/api/music/playlists/lesson_start', json={'track_ids': [track['id']]}).status_code == 200
    assert client.get('/api/music').json()['playlists']['lesson_start'] == [track['id']]
    response = client.post('/api/music/test', json={'track_id': track['id']})
    assert response.status_code == 200
    assert response.json()['mode'] == 'dry-run'
    assert client.get('/api/music/status').json()['mode'] == 'dry-run'
    assert client.post('/api/music/stop').status_code == 200
    assert client.get('/api/music/status').json()['busy'] is False
    assert client.put('/api/music/playlists/lesson_start', json={'track_ids': [999]}).status_code == 404
    assert client.get('/api/music').json()['playlists']['lesson_start'] == [track['id']]


def test_fake_audio_is_rejected_and_partial_upload_removed(api):
    client, _, uploads = api
    assert client.post('/api/music/upload', files={'file': ('fake.mp3', b'not an MP3', 'audio/mpeg')}).status_code == 422
    assert list(uploads.iterdir()) == []
    assert client.get('/api/music').json()['tracks'] == []
    assert client.post('/api/music/upload', files={'file': ('script.html', b'<html/>', 'text/html')}).status_code == 422


def test_missing_audio_file_returns_actionable_error(api):
    client, _, uploads = api
    response = client.post('/api/music/upload', files={'file': ('missing.wav', wav_bytes(), 'audio/wav')})
    track_id = response.json()['id']
    for path in uploads.iterdir():
        path.unlink()
    settings = client.get('/api/catalog').json()['settings']
    settings['audio_enabled'] = True
    assert client.put('/api/settings', json=settings).status_code == 200
    response = client.post('/api/music/test', json={'track_id': track_id})
    assert response.status_code == 404
    assert 'missing from disk' in response.json()['detail']
