"""REST integration tests using an isolated SQLite database and a silent player."""
from collections import Counter
from io import BytesIO
import wave

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app import main
from app.database import get_db
from app.models import Base, Settings, Subject
from app.routers import music, school


class IdleScheduler:
    """Prevent wall-clock jobs from interfering with deterministic API tests."""
    def __init__(self, _player):
        self.running = False

    def start(self):
        self.running = True

    def shutdown(self):
        self.running = False

    def status(self):
        return {'running': self.running, 'last_result': {'status': 'disabled'}}


@pytest.fixture
def api(tmp_path, monkeypatch):
    engine = create_engine(f'sqlite:///{tmp_path / "test.db"}', connect_args={'check_same_thread': False})

    @event.listens_for(engine, 'connect')
    def pragmas(connection, _record):
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA journal_mode=WAL')

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add(Settings(id=1, solver_time_limit=3))

    def isolated_db():
        with factory() as session:
            yield session

    monkeypatch.setattr(main, 'init_db', lambda: None)
    monkeypatch.setattr(main, 'BellScheduler', IdleScheduler)
    uploads = tmp_path / 'music'
    uploads.mkdir()
    monkeypatch.setattr(music, 'UPLOAD_DIR', uploads)
    main.app.dependency_overrides[get_db] = isolated_db
    try:
        with TestClient(main.app) as client:
            yield client, factory, uploads
    finally:
        main.app.dependency_overrides.clear()
        engine.dispose()


def create_small_school(client):
    subject = client.post('/api/subjects', json={'name': 'Math', 'is_hard': True}).json()['id']
    teacher = client.post('/api/teachers', json={'name': 'Teacher', 'subject_ids': [subject]}).json()['id']
    grade = client.post('/api/grades', json={'number': 1, 'parallel_count': 2}).json()['id']
    assert client.post('/api/curriculum', json={'grade_id': grade, 'subject_id': subject, 'lessons_per_week': 2}).status_code == 200
    catalog = client.get('/api/catalog').json()
    for school_class in catalog['classes']:
        assert client.post('/api/assignments', json={'class_id': school_class['id'], 'subject_id': subject, 'teacher_id': teacher}).status_code == 200
    assert client.post('/api/timetable/generate').status_code == 200
    return subject, teacher, grade


def test_demo_generates_exact_whole_school_week_and_bells(api):
    client, _, _ = api
    assert client.get('/health').json()['status'] == 'ok'
    assert client.post('/api/seed').status_code == 200
    assert client.post('/api/seed').status_code == 409
    generated = client.post('/api/timetable/generate')
    assert generated.status_code == 200, generated.text
    assert generated.json()['lesson_count'] == 96
    catalog = client.get('/api/catalog').json()
    timetable = client.get('/api/timetable').json()
    lessons = timetable['lessons']
    assert len(catalog['teachers']) == 6
    assert {g['number'] for g in catalog['grades']} == {1, 6, 11}
    expected = {(c['id'], gs['subject_id']): gs['lessons_per_week']
                for c in catalog['classes'] for gs in catalog['grade_subjects'] if c['grade_id'] == gs['grade_id']}
    assert Counter((x['class_id'], x['subject_id']) for x in lessons) == expected
    for resource in ('class_id', 'teacher_id', 'room_id'):
        assert len({(x[resource], x['weekday'], x['period']) for x in lessons}) == len(lessons)
    teacher_id = catalog['teachers'][0]['id']
    assert all(x['teacher_id'] == teacher_id for x in client.get(f'/api/timetable?teacher_id={teacher_id}').json()['lessons'])
    class_id = catalog['classes'][0]['id']
    assert len(client.get(f'/api/timetable?class_id={class_id}').json()['lessons']) == 16
    bells = client.get('/api/bells').json()
    assert bells['events']
    assert {x['weekday'] for x in bells['events']} == set(range(5))
    assert sum(x['event_type'] == 'end_of_day' for x in bells['events']) == 5
    assert timetable['periods'][:4] == [
        {'period': 1, 'start': '08:30', 'end': '09:15'},
        {'period': 2, 'start': '09:20', 'end': '10:05'},
        {'period': 3, 'start': '10:20', 'end': '11:05'},
        {'period': 4, 'start': '11:10', 'end': '11:55'},
    ]


def test_manual_move_preserves_counts_and_rejects_conflicts_atomically(api):
    client, _, _ = api
    create_small_school(client)
    before = client.get('/api/timetable').json()['lessons']
    lesson = before[0]
    clash = next(x for x in before if x['class_id'] == lesson['class_id'] and x['id'] != lesson['id'])
    response = client.patch(f'/api/timetable/{lesson["id"]}', json={'weekday': clash['weekday'], 'period': clash['period']})
    assert response.status_code == 409
    assert client.get('/api/timetable').json()['lessons'] == before
    occupied = {(x['weekday'], x['period']) for x in before}
    day, period = next((d, p) for d in range(5) for p in range(1, 5) if (d, p) not in occupied)
    assert client.patch(f'/api/timetable/{lesson["id"]}', json={'weekday': day, 'period': period}).status_code == 200
    after = client.get('/api/timetable').json()['lessons']
    moved = next(x for x in after if x['id'] == lesson['id'])
    assert (moved['weekday'], moved['period']) == (day, period)
    assert Counter((x['class_id'], x['subject_id']) for x in after) == Counter((x['class_id'], x['subject_id']) for x in before)
    assert any(x['weekday'] == day and x['period'] == period and x['event_type'] == 'lesson_start'
               for x in client.get('/api/bells').json()['events'])


def test_curriculum_changes_invalidate_timetable_and_bells(api):
    client, _, _ = api
    subject, _, grade = create_small_school(client)
    assert client.get('/api/bells').json()['events']
    assert client.post('/api/curriculum', json={'grade_id': grade, 'subject_id': subject, 'lessons_per_week': 3}).status_code == 200
    assert client.get('/api/timetable').json()['lessons'] == []
    assert client.get('/api/bells').json()['events'] == []




def test_unqualified_assignment_and_skill_removal_are_rejected(api):
    client, _, _ = api
    subject, teacher, _ = create_small_school(client)
    other = client.post('/api/teachers', json={'name': 'Unqualified', 'subject_ids': []}).json()['id']
    school_class = client.get('/api/catalog').json()['classes'][0]
    response = client.post('/api/assignments', json={'class_id': school_class['id'], 'subject_id': subject, 'teacher_id': other})
    assert response.status_code == 422
    assert 'qualified' in response.json()['detail']
    assert client.put(f'/api/teachers/{teacher}', json={'name': 'Teacher', 'subject_ids': []}).status_code == 409




def test_duplicate_data_rolls_back_and_following_request_succeeds(api):
    client, _, _ = api
    assert client.post('/api/subjects', json={'name': 'Math'}).status_code == 201
    assert client.post('/api/subjects', json={'name': 'Math'}).status_code == 409
    assert client.post('/api/subjects', json={'name': 'Physics'}).status_code == 201
    assert [x['name'] for x in client.get('/api/catalog').json()['subjects']] == ['Math', 'Physics']


def wav_bytes():
    output = BytesIO()
    with wave.open(output, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b'\x00\x00' * 800)
    return output.getvalue()






def test_solver_result_does_not_overwrite_concurrent_changes(api, monkeypatch):
    client, factory, _ = api
    create_small_school(client)
    before = client.get('/api/timetable').json()['lessons']

    def conflicting_generation(_session, _settings):
        with factory.begin() as other:
            other.add(Subject(name='Concurrent edit'))
        return {'status': 'FEASIBLE', 'lessons': [], 'objective': 0, 'message': 'stub'}

    monkeypatch.setattr(school, 'generate_timetable', conflicting_generation)
    response = client.post('/api/timetable/generate')
    assert response.status_code == 409
    assert 'changed while solving' in response.json()['detail']
    assert client.get('/api/timetable').json()['lessons'] == before
    assert 'Concurrent edit' in {x['name'] for x in client.get('/api/catalog').json()['subjects']}


def test_cross_origin_mutations_are_blocked(api):
    client, _, _ = api
    response = client.post('/api/subjects', json={'name': 'Math'}, headers={'Origin': 'https://unrelated.example'})
    assert response.status_code == 403
    assert client.get('/api/catalog').json()['subjects'] == []












