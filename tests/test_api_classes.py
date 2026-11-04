"""Focused API checks using the shared school fixture."""
from collections import Counter
import pytest
from test_api import api, create_small_school, wav_bytes


def test_existing_classes_registered_without_pupil_counts(api):
    client, _, _ = api
    grade = client.post('/api/grades', json={'number': 5, 'parallel_count': 0}).json()['id']
    assert client.get('/api/catalog').json()['classes'] == []
    for name, language in [('5A', 'kk'), ('5B', 'ru')]:
        response = client.post('/api/classes', json={'grade_id': grade, 'name': name, 'language': language})
        assert response.status_code == 201, response.text
    classes = client.get('/api/catalog').json()['classes']
    assert [row['name'] for row in classes] == ['5A', '5B']
    assert all('student_count' not in row for row in classes)
    assert '/api/classes/split' not in client.get('/openapi.json').json()['paths']
    for invalid in [dict(name='Language', grade_id=grade, language='en'),
                    dict(name='Afternoon', grade_id=grade, shift=2)]:
        assert client.post('/api/classes', json=invalid).status_code == 422


def test_class_specific_curricula_two_shifts_and_same_period_persist(api):
    client, _, _ = api
    settings = client.get('/api/catalog').json()['settings']
    settings.update(shifts_enabled=True, max_lessons_per_day=1, second_shift_start_time='14:00')
    assert client.put('/api/settings', json=settings).status_code == 200
    grade = client.post('/api/grades', json={'number': 7, 'parallel_count': 0}).json()['id']
    subject = client.post('/api/subjects', json={'name': 'Математика'}).json()['id']
    teacher = client.post('/api/teachers', json={'name': 'Мұғалім', 'subject_ids': [subject]}).json()['id']
    for shift, language in [(1, 'kk'), (2, 'ru')]:
        school_class = client.post('/api/classes', json={'name': f'7-{language}', 'grade_id': grade,
                                                          'shift': shift, 'language': language}).json()['id']
        result = client.post('/api/assignments', json={'class_id': school_class, 'subject_id': subject,
                                                      'teacher_id': teacher, 'lessons_per_week': 5})
        assert result.status_code == 200, result.text
    generated = client.post('/api/timetable/generate', json={'seed': 123})
    assert generated.status_code == 200, generated.text
    assert generated.json()['seed'] == 123
    assert generated.json()['lesson_count'] == 10
    timetable = client.get('/api/timetable').json()
    assert set(timetable['shift_periods']) == {'1', '2'}
    assert timetable['shift_periods']['2'][0]['start'] == '14:00'
    assert Counter((row['weekday'], row['period']) for row in timetable['lessons']) == {(day, 1): 2 for day in range(5)}
    assert {row['language'] for row in timetable['lessons']} == {'kk', 'ru'}
    assert [row['time'] for row in client.get('/api/bells').json()['events'] if row['event_type'] == 'end_of_day'] == ['14:45'] * 5
    settings['shifts_enabled'] = False
    assert client.put('/api/settings', json=settings).status_code == 409
    assert len(client.get('/api/timetable').json()['lessons']) == 10


def test_class_edit_invalidates_and_changes_effective_quota(api):
    client, _, _ = api
    subject, teacher, grade = create_small_school(client)
    school_class = client.get('/api/catalog').json()['classes'][0]
    payload = {key: value for key, value in school_class.items() if key != 'id'}
    payload.update(language='ru')
    assert client.put(f'/api/classes/{school_class["id"]}', json=payload).status_code == 200
    assert client.get('/api/timetable').json()['lessons'] == []
    assert client.post('/api/assignments', json={'class_id': school_class['id'], 'subject_id': subject,
                                                'teacher_id': teacher, 'lessons_per_week': 3}).status_code == 200
    result = client.post('/api/timetable/generate')
    assert result.status_code == 200, result.text
    assert result.json()['lesson_count'] == 5
    assert len(client.get(f'/api/timetable?class_id={school_class["id"]}').json()['lessons']) == 3
