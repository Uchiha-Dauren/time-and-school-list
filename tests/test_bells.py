"""Bell calculations do not require an audio device or a running scheduler."""
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.models import Base, BellEvent, Grade, SchoolClass, Settings, Subject, Teacher, TimetableLesson
from app.services.bells import calculate_bell_events, period_times, rebuild_bell_events


def settings(**values):
    return Settings(start_time='08:30', max_lessons_per_day=values.pop('max_lessons_per_day', 6), **values)


def test_exact_45_minute_lessons_and_one_long_break():
    assert period_times(settings())[:4] == [
        {'period': 1, 'start': '08:30', 'end': '09:15'},
        {'period': 2, 'start': '09:20', 'end': '10:05'},
        {'period': 3, 'start': '10:20', 'end': '11:05'},
        {'period': 4, 'start': '11:10', 'end': '11:55'},
    ]
    # A second 15-minute break used to be inserted after lesson four.
    assert period_times(settings())[4] == {'period': 5, 'start': '12:00', 'end': '12:45'}


def test_long_break_can_follow_another_lesson():
    config = settings(long_break_after=3)
    assert period_times(config)[2] == {'period': 3, 'start': '10:10', 'end': '10:55'}
    assert period_times(config)[3]['start'] == '11:10'
    events = calculate_bell_events([{'weekday': 0, 'period': p} for p in range(1, 5)], config)
    assert [(e['period'], e['time']) for e in events if e['event_type'] == 'long_break_start'] == [(3, '10:55')]


def test_two_shift_bells_use_real_times_and_only_one_school_day_end():
    config = settings(shifts_enabled=True, second_shift_start_time='14:00')
    assert period_times(config, shift=2)[0] == {'period': 1, 'start': '14:00', 'end': '14:45'}
    events = calculate_bell_events([
        {'weekday': 0, 'period': 1, 'shift': 1},
        {'weekday': 0, 'period': 1, 'shift': 2},
        {'weekday': 0, 'period': 1, 'shift': 2},
        {'weekday': 0, 'period': 2, 'shift': 2},
    ], config)
    assert len(events) == 9
    assert [e['time'] for e in events if e['event_type'] == 'lesson_start'] == ['08:30', '14:00', '14:50']
    assert [e['time'] for e in events if e['event_type'] == 'end_of_day'] == ['15:35']


def test_disabled_second_shift_is_rejected():
    with pytest.raises(ValueError, match='two shifts'):
        period_times(settings(), shift=2)
    with pytest.raises(ValueError, match='enabled school shifts'):
        calculate_bell_events([{'weekday': 0, 'period': 1, 'shift': 2}], settings())


def test_rebuild_reads_the_class_shift_and_replaces_old_events():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            session.add_all([Grade(id=1, number=1), Subject(id=1, name='Music'), Teacher(id=1, name='Teacher')])
            session.flush()
            session.add(SchoolClass(id=1, grade_id=1, name='1A', shift=2))
            session.flush()
            session.add_all([
                TimetableLesson(class_id=1, subject_id=1, teacher_id=1, weekday=0, period=1),
                BellEvent(weekday=0, time='08:30', event_type='lesson_start', period=1),
            ])
            session.flush()
            events = rebuild_bell_events(session, settings(shifts_enabled=True, second_shift_start_time='14:00'))
            assert {event['time'] for event in events} == {'14:00', '14:45'}
            assert {row.time for row in session.scalars(select(BellEvent))} == {'14:00', '14:45'}
    finally:
        engine.dispose()


def test_multiple_classes_share_one_bell_and_day_end_is_school_wide():
    lessons = [
        {'class_id': 1, 'weekday': 0, 'period': 1},
        {'class_id': 2, 'weekday': 0, 'period': 1},
        {'class_id': 2, 'weekday': 0, 'period': 2},
        {'class_id': 2, 'weekday': 0, 'period': 3},
    ]
    events = calculate_bell_events(lessons, settings())
    assert len(events) == 9
    assert len([event for event in events if event['time'] == '08:30']) == 1
    assert {'weekday': 0, 'time': '10:05', 'event_type': 'long_break_start', 'period': 2} in events
    assert [event['time'] for event in events if event['event_type'] == 'end_of_day'] == ['11:05']
    assert not any(event['weekday'] != 0 for event in events)


def test_empty_timetable_has_no_bells():
    assert calculate_bell_events([], settings()) == []


def test_sparse_grid_never_rings_for_empty_periods():
    events = calculate_bell_events([{'weekday': 4, 'period': 3}], settings())
    assert {event['time'] for event in events} == {'10:20', '11:05'}
    assert {event['event_type'] for event in events} == {'lesson_start', 'lesson_end', 'end_of_day'}


@pytest.mark.parametrize('lesson', [{'weekday': 5, 'period': 1}, {'weekday': 0, 'period': 9}])
def test_invalid_lesson_is_rejected(lesson):
    with pytest.raises(ValueError):
        calculate_bell_events([lesson], settings())


def test_custom_start_and_midnight_guard():
    assert period_times(Settings(start_time='07:00', max_lessons_per_day=2))[1]['end'] == '08:35'
    with pytest.raises(ValueError, match='midnight'):
        period_times(Settings(start_time='23:30', max_lessons_per_day=2))
