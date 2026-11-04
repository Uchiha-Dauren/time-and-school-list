"""Focused API checks using the shared school fixture."""
from collections import Counter
import pytest
from test_api import api, create_small_school, wav_bytes


def test_clock_settings_recalculate_bells_and_capacity_change_invalidates(api):
    client, _, _ = api
    create_small_school(client)
    before = client.get('/api/timetable').json()['lessons']
    settings = client.get('/api/catalog').json()['settings']
    settings['start_time'] = '08:00'
    assert client.put('/api/settings', json=settings).status_code == 200
    assert client.get('/api/timetable').json()['lessons'] == before
    assert client.get('/api/bells').json()['periods'][0]['start'] == '08:00'
    assert any(x['time'] == '08:00' for x in client.get('/api/bells').json()['events'])
    settings['max_lessons_per_day'] = 9
    assert client.put('/api/settings', json=settings).status_code == 200
    assert client.get('/api/timetable').json()['lessons'] == []
    assert client.get('/api/bells').json()['events'] == []


@pytest.mark.parametrize(('path', 'payload'), [
    ('/api/grades', {'number': 12, 'parallel_count': 2}),
    ('/api/grades', {'number': 1, 'parallel_count': 27}),
    ('/api/subjects', {'name': '  '}),
    ('/api/settings', {'timezone': 'Invalid/Timezone'}),
    ('/api/settings', {'start_time': '23:00'}),
    ('/api/settings', {'playback_duration': 2, 'fade_out_seconds': 3}),
])
def test_invalid_inputs_have_validation_errors(api, path, payload):
    client, _, _ = api
    response = client.put(path, json=payload) if path == '/api/settings' else client.post(path, json=payload)
    assert response.status_code == 422


def test_shift_settings_reject_overlaps_and_midnight(api):
    client, _, _ = api
    before = client.get('/api/catalog').json()['settings']
    for changes in [dict(shifts_enabled=True),
                    dict(shifts_enabled=True, max_lessons_per_day=6, second_shift_start_time='20:00'),
                    dict(long_break_after=0), dict(second_shift_start_time='9:00')]:
        response = client.put('/api/settings', json=before | changes)
        assert response.status_code == 422, response.text
    assert client.get('/api/catalog').json()['settings'] == before
    assert client.put('/api/settings', json=before | dict(shifts_enabled=True, max_lessons_per_day=6)).status_code == 200


def test_generate_seed_rejects_invalid_input(api):
    client, _, _ = api
    for seed in [-1, 2**31, 'text', True, '42']:
        assert client.post('/api/timetable/generate', json={'seed': seed}).status_code == 422
