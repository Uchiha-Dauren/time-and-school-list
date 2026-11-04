"""Behavioral checks for exact quotas, all-school coverage, and hard constraints."""
from collections import Counter
from copy import deepcopy

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.models import (
    Base, Grade, GradeSubject, Room, SchoolClass, Settings, Subject, Teacher,
    TeachingAssignment, TimetableLesson,
)
from app.services.timetable import TimetableError, generate_timetable, validate_timetable


@pytest.fixture
def db():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        settings = Settings(id=1, max_lessons_per_day=3, solver_time_limit=2,
                            use_rooms=False)
        session.add(settings)
        session.flush()
        yield session, settings
    engine.dispose()


def add(session, entity):
    """Insert a fixture record and make its generated primary key available."""
    session.add(entity)
    session.flush()
    return entity


def small_school(session, *, cap=3, quota=2, shared_teacher=True, shared_room=False):
    """One grade, two parallel classes, one subject, configurable resources."""
    grade = add(session, Grade(number=1, max_lessons_per_day=cap))
    subject = add(session, Subject(name='Mathematics', is_hard=True))
    add(session, GradeSubject(grade_id=grade.id, subject_id=subject.id,
                             lessons_per_week=quota))
    teacher = add(session, Teacher(name='Aigerim', subjects=[subject]))
    room = add(session, Room(name='Room 101'))
    classes = []
    assignments = []
    for index, letter in enumerate('AB'):
        school_class = add(session, SchoolClass(name=f'1{letter}', grade_id=grade.id))
        classes.append(school_class)
        assigned_teacher = teacher
        if index and not shared_teacher:
            assigned_teacher = add(session, Teacher(name='Dauren', subjects=[subject]))
        assigned_room = room
        if index and not shared_room:
            assigned_room = add(session, Room(name='Room 102'))
        assignments.append(add(session, TeachingAssignment(
            class_id=school_class.id, subject_id=subject.id,
            teacher_id=assigned_teacher.id, room_id=assigned_room.id,
        )))
    return grade, subject, teacher, classes, assignments


def error_text(exc):
    error = exc.value
    return (str(error) + ' ' + ' '.join(error.details)).lower()


def test_generates_every_parallel_class_for_all_eleven_grades(db):
    session, settings = db
    settings.use_rooms = True
    math = add(session, Subject(name='Mathematics', is_hard=True))
    literacy = add(session, Subject(name='Literacy', is_hard=False))
    expected = {}
    for number in range(1, 12):
        grade = add(session, Grade(number=number, max_lessons_per_day=2 if number <= 4 else 3))
        teacher = add(session, Teacher(name=f'Teacher {number}', subjects=[math, literacy]))
        room = add(session, Room(name=f'Room {number}'))
        for subject in (math, literacy):
            add(session, GradeSubject(grade_id=grade.id, subject_id=subject.id,
                                     lessons_per_week=2))
        for letter in 'AB':
            school_class = add(session, SchoolClass(name=f'{number}{letter}', grade_id=grade.id))
            for subject in (math, literacy):
                add(session, TeachingAssignment(class_id=school_class.id,
                                               subject_id=subject.id,
                                               teacher_id=teacher.id, room_id=room.id))
                expected[(school_class.id, subject.id)] = 2
    result = generate_timetable(session, settings)
    lessons = result['lessons']
    assert len(lessons) == 88
    assert len({lesson['class_id'] for lesson in lessons}) == 22
    assert Counter((lesson['class_id'], lesson['subject_id']) for lesson in lessons) == expected
    assert validate_timetable(session, lessons, settings) == []
    for resource in ('class_id', 'teacher_id', 'room_id'):
        keys = [(lesson[resource], lesson['weekday'], lesson['period']) for lesson in lessons]
        assert len(keys) == len(set(keys))
    # Generation itself does not overwrite a timetable before callers can validate it.
    assert session.scalar(select(func.count()).select_from(TimetableLesson)) == 0


@pytest.mark.parametrize('use_rooms', [False, True])
def test_optional_rooms_and_exact_counts(db, use_rooms):
    session, settings = db
    settings.use_rooms = use_rooms
    _, subject, _, classes, assignments = small_school(session)
    lessons = generate_timetable(session, settings)['lessons']
    assert validate_timetable(session, lessons, settings) == []
    assert Counter(lesson['class_id'] for lesson in lessons) == {c.id: 2 for c in classes}
    room_by_class = {a.class_id: a.room_id for a in assignments}
    assert all(lesson['subject_id'] == subject.id for lesson in lessons)
    assert all(lesson['room_id'] == (room_by_class[lesson['class_id']] if use_rooms else None)
               for lesson in lessons)


def test_rejects_class_weekly_capacity_overflow(db):
    session, settings = db
    small_school(session, cap=1, quota=6, shared_teacher=False)
    with pytest.raises(TimetableError) as exc:
        generate_timetable(session, settings)
    assert '1a' in error_text(exc)
    assert exc.value.status in {'invalid', 'infeasible'}


def test_rejects_missing_teacher_assignment(db):
    session, settings = db
    _, _, _, _, assignments = small_school(session)
    session.delete(assignments[0])
    session.flush()
    with pytest.raises(TimetableError) as exc:
        generate_timetable(session, settings)
    text = error_text(exc)
    assert '1a' in text
    assert 'assign' in text


def test_rejects_teacher_without_subject_qualification(db):
    session, settings = db
    _, _, teacher, _, _ = small_school(session)
    teacher.subjects.clear()
    session.flush()
    with pytest.raises(TimetableError) as exc:
        generate_timetable(session, settings)
    assert 'aigerim' in error_text(exc)


def test_teacher_capacity_accounts_for_restricted_grade_slots(db):
    session, settings = db
    settings.max_lessons_per_day = 8
    small_school(session, cap=1, quota=3, shared_teacher=True)
    with pytest.raises(TimetableError) as exc:
        generate_timetable(session, settings)
    text = error_text(exc)
    assert 'teacher' in text and 'aigerim' in text
    assert '5' in text


def test_room_capacity_accounts_for_restricted_grade_slots(db):
    session, settings = db
    settings.max_lessons_per_day = 8
    settings.use_rooms = True
    small_school(session, cap=1, quota=3, shared_teacher=False, shared_room=True)
    with pytest.raises(TimetableError) as exc:
        generate_timetable(session, settings)
    text = error_text(exc)
    assert 'room' in text and '101' in text
    assert '5' in text


def test_class_without_curriculum_cannot_silently_disappear(db):
    session, settings = db
    grade = add(session, Grade(number=11, max_lessons_per_day=3))
    add(session, SchoolClass(name='11A', grade_id=grade.id))
    with pytest.raises(TimetableError) as exc:
        generate_timetable(session, settings)
    text = error_text(exc)
    assert '11' in text


def test_validator_rejects_teacher_collision(db):
    session, settings = db
    small_school(session)
    lessons = generate_timetable(session, settings)['lessons']
    changed = deepcopy(lessons)
    first = changed[0]
    second = next(lesson for lesson in changed if lesson['class_id'] != first['class_id'])
    second.update(weekday=first['weekday'], period=first['period'])
    errors = validate_timetable(session, changed, settings)
    assert errors
    assert 'teacher' in ' '.join(errors).lower()


def test_validator_rejects_class_collision(db):
    session, settings = db
    small_school(session)
    lessons = generate_timetable(session, settings)['lessons']
    changed = deepcopy(lessons)
    first = changed[0]
    second = next(lesson for lesson in changed[1:] if lesson['class_id'] == first['class_id'])
    second.update(weekday=first['weekday'], period=first['period'])
    errors = validate_timetable(session, changed, settings)
    assert errors
    assert 'class' in ' '.join(errors).lower()


def test_validator_rejects_room_collision(db):
    session, settings = db
    settings.use_rooms = True
    small_school(session, shared_teacher=False, shared_room=True)
    lessons = generate_timetable(session, settings)['lessons']
    changed = deepcopy(lessons)
    first = changed[0]
    second = next(lesson for lesson in changed if lesson['class_id'] != first['class_id'])
    second.update(weekday=first['weekday'], period=first['period'])
    errors = validate_timetable(session, changed, settings)
    assert errors
    assert 'room' in ' '.join(errors).lower()


def test_validator_rejects_missing_required_lesson(db):
    session, settings = db
    small_school(session)
    lessons = generate_timetable(session, settings)['lessons']
    assert validate_timetable(session, lessons[:-1], settings)


@pytest.mark.parametrize(('field', 'value'), [('weekday', 5), ('period', 0), ('period', 4)])
def test_validator_rejects_out_of_range_times(db, field, value):
    session, settings = db
    small_school(session, cap=3)
    lessons = generate_timetable(session, settings)['lessons']
    changed = deepcopy(lessons)
    changed[0][field] = value
    assert validate_timetable(session, changed, settings)


def test_validator_rejects_teacher_assignment_change(db):
    session, settings = db
    _, subject, _, _, _ = small_school(session)
    other = add(session, Teacher(name='Substitute', subjects=[subject]))
    lessons = generate_timetable(session, settings)['lessons']
    changed = deepcopy(lessons)
    changed[0]['teacher_id'] = other.id
    assert validate_timetable(session, changed, settings)


def test_validator_enforces_grade_cap_below_global_cap(db):
    session, settings = db
    settings.max_lessons_per_day = 3
    small_school(session, cap=2)
    lessons = generate_timetable(session, settings)['lessons']
    changed = deepcopy(lessons)
    changed[0]['period'] = 3
    assert validate_timetable(session, changed, settings)


def test_disabled_rooms_do_not_impose_room_capacity(db):
    session, settings = db
    settings.use_rooms = False
    small_school(session, cap=1, quota=3, shared_teacher=False, shared_room=True)
    lessons = generate_timetable(session, settings)['lessons']
    assert len(lessons) == 6
    assert all(lesson['room_id'] is None for lesson in lessons)
    assert validate_timetable(session, lessons, settings) == []


def test_subject_daily_limit_remains_soft_when_quota_requires_three(db):
    session, settings = db
    small_school(session, cap=3, quota=11, shared_teacher=False)
    lessons = generate_timetable(session, settings)['lessons']
    assert len(lessons) == 22
    assert validate_timetable(session, lessons, settings) == []
    daily = Counter((lesson['class_id'], lesson['weekday']) for lesson in lessons)
    assert max(daily.values()) >= 3


def test_enabled_rooms_require_room_assignment(db):
    session, settings = db
    settings.use_rooms = True
    _, _, _, _, assignments = small_school(session)
    assignments[0].room_id = None
    session.flush()
    with pytest.raises(TimetableError) as exc:
        generate_timetable(session, settings)
    assert 'room' in error_text(exc)


def test_joint_constraints_produce_infeasibility_core(db):
    """A class/teacher/room conflict triangle needs six slots, but has five.

    Every class, teacher, and room individually has at most four lessons, so a
    simple capacity check cannot diagnose this jointly impossible assignment.
    """
    session, settings = db
    settings.max_lessons_per_day = 1
    settings.use_rooms = True
    grade = add(session, Grade(number=1, max_lessons_per_day=1))
    alpha = add(session, Subject(name='Alpha', is_hard=False))
    beta = add(session, Subject(name='Beta', is_hard=False))
    for subject in (alpha, beta):
        add(session, GradeSubject(grade_id=grade.id, subject_id=subject.id,
                                 lessons_per_week=2))
    first = add(session, SchoolClass(name='1A', grade_id=grade.id))
    second = add(session, SchoolClass(name='1B', grade_id=grade.id))
    teachers = [
        add(session, Teacher(name='Teacher 1', subjects=[alpha])),
        add(session, Teacher(name='Teacher 2', subjects=[alpha, beta])),
        add(session, Teacher(name='Teacher 3', subjects=[beta])),
    ]
    rooms = [add(session, Room(name=f'Room {index}')) for index in range(1, 4)]
    for school_class, subject, teacher, room in (
        (first, alpha, teachers[0], rooms[0]),
        (first, beta, teachers[1], rooms[1]),
        (second, alpha, teachers[1], rooms[0]),
        (second, beta, teachers[2], rooms[2]),
    ):
        add(session, TeachingAssignment(class_id=school_class.id,
                                       subject_id=subject.id, teacher_id=teacher.id,
                                       room_id=room.id))
    with pytest.raises(TimetableError) as exc:
        generate_timetable(session, settings)
    assert exc.value.status == 'infeasible'
    text = error_text(exc)
    assert '1a' in text and 'alpha' in text and 'beta' in text
    assert exc.value.details


def test_separate_shifts_reuse_teacher_and_room_at_same_period(db):
    """The same teacher and room can serve two full shifts without false clashes."""
    session, settings = db
    settings.shifts_enabled = True
    settings.second_shift_start_time = '14:00'
    settings.use_rooms = True
    _, _, _, classes, _ = small_school(session, cap=1, quota=5, shared_room=True)
    classes[1].shift = 2
    session.flush()
    lessons = generate_timetable(session, settings, seed=123)['lessons']
    assert len(lessons) == 10
    assert all(lesson['period'] == 1 for lesson in lessons)
    assert validate_timetable(session, lessons, settings) == []


def test_two_shifts_must_be_enabled_for_second_shift_class(db):
    session, settings = db
    _, _, _, classes, _ = small_school(session)
    classes[1].shift = 2
    session.flush()
    with pytest.raises(TimetableError) as exc:
        generate_timetable(session, settings)
    assert 'shift' in error_text(exc)


def test_teacher_collision_uses_clock_times_across_different_periods(db):
    session, settings = db
    settings.shifts_enabled = True
    # Deliberately use a direct-DB configuration that bypasses API validation.
    settings.second_shift_start_time = '09:30'
    _, subject, teacher, classes, _ = small_school(session, quota=1)
    classes[1].shift = 2
    session.flush()
    lessons = [
        dict(class_id=classes[0].id, subject_id=subject.id, teacher_id=teacher.id,
             room_id=None, weekday=0, period=2),  # 09:20–10:05
        dict(class_id=classes[1].id, subject_id=subject.id, teacher_id=teacher.id,
             room_id=None, weekday=0, period=1),  # 09:30–10:15
    ]
    errors = validate_timetable(session, lessons, settings)
    assert any('teacher' in error.lower() and 'conflict' in error.lower() for error in errors)
    generated = generate_timetable(session, settings, seed=123)['lessons']
    assert validate_timetable(session, generated, settings) == []


def test_room_collision_uses_clock_times_across_different_periods(db):
    session, settings = db
    settings.shifts_enabled = True
    settings.second_shift_start_time = '09:30'
    settings.use_rooms = True
    _, subject, _, classes, assignments = small_school(
        session, quota=1, shared_teacher=False, shared_room=True,
    )
    classes[1].shift = 2
    session.flush()
    lessons = [
        dict(class_id=school_class.id, subject_id=subject.id,
             teacher_id=assignment.teacher_id, room_id=assignment.room_id,
             weekday=0, period=2 if index == 0 else 1)
        for index, (school_class, assignment) in enumerate(zip(classes, assignments))
    ]
    errors = validate_timetable(session, lessons, settings)
    assert any('room' in error.lower() and 'conflict' in error.lower() for error in errors)


def test_class_specific_quota_overrides_grade_quota(db):
    session, settings = db
    _, subject, _, classes, assignments = small_school(session, quota=2)
    assignments[1].lessons_per_week = 4
    session.flush()
    lessons = generate_timetable(session, settings, seed=7)['lessons']
    assert Counter((lesson['class_id'], lesson['subject_id']) for lesson in lessons) == {
        (classes[0].id, subject.id): 2,
        (classes[1].id, subject.id): 4,
    }
    assert validate_timetable(session, lessons, settings) == []


def test_class_can_have_its_own_curriculum_without_grade_defaults(db):
    session, settings = db
    grade = add(session, Grade(number=7, max_lessons_per_day=3))
    kazakh = add(session, Subject(name='Қазақ тілі'))
    russian = add(session, Subject(name='Русский язык'))
    expected = {}
    for language, subject in [('kk', kazakh), ('ru', russian)]:
        school_class = add(session, SchoolClass(name=f'7A {language}', grade_id=grade.id,
                                                language=language))
        teacher = add(session, Teacher(name=f'Teacher {language}', subjects=[subject]))
        add(session, TeachingAssignment(class_id=school_class.id, subject_id=subject.id,
                                        teacher_id=teacher.id, lessons_per_week=3))
        expected[school_class.id, subject.id] = 3
    lessons = generate_timetable(session, settings, seed=7)['lessons']
    assert Counter((lesson['class_id'], lesson['subject_id']) for lesson in lessons) == expected
    assert validate_timetable(session, lessons, settings) == []


def test_random_seeds_produce_valid_variants_and_allow_repeatable_demo(db):
    session, settings = db
    small_school(session, quota=2)
    first = generate_timetable(session, settings, seed=17)
    repeated = generate_timetable(session, settings, seed=17)
    assert first['seed'] == repeated['seed'] == 17
    assert first['lessons'] == repeated['lessons']
    variants = {str(first['lessons'])}
    for seed in (23, 42, 91):
        result = generate_timetable(session, settings, seed=seed)
        assert validate_timetable(session, result['lessons'], settings) == []
        variants.add(str(result['lessons']))
    assert len(variants) > 1
    unseeded = generate_timetable(session, settings)
    assert isinstance(unseeded['seed'], int)
    assert 0 <= unseeded['seed'] < 2**31


@pytest.mark.parametrize('seed', [-1, 2**31, True, '42'])
def test_invalid_random_seed_is_rejected(db, seed):
    session, settings = db
    small_school(session)
    with pytest.raises(TimetableError):
        generate_timetable(session, settings, seed=seed)


@pytest.mark.parametrize('resource', ['teacher', 'room'])
def test_solver_proves_actual_overlap_infeasible_across_shifts(db, resource):
    session, settings = db
    settings.shifts_enabled = True
    settings.second_shift_start_time = '09:00'
    settings.use_rooms = resource == 'room'
    _, _, _, classes, _ = small_school(
        session, cap=1, quota=3,
        shared_teacher=resource == 'teacher', shared_room=resource == 'room',
    )
    classes[1].shift = 2
    session.flush()
    # Both periods overlap, so the shared resource has five slots for six jobs.
    # Each individual shift has enough capacity; only the clock check can fail.
    with pytest.raises(TimetableError) as exc:
        generate_timetable(session, settings, seed=123)
    assert exc.value.status == 'infeasible'


def test_resource_can_start_next_lesson_exactly_when_previous_one_ends(db):
    session, settings = db
    settings.shifts_enabled = True
    settings.second_shift_start_time = '09:15'
    settings.use_rooms = True
    _, _, _, classes, _ = small_school(session, cap=1, quota=5, shared_room=True)
    classes[1].shift = 2
    session.flush()
    lessons = generate_timetable(session, settings, seed=123)['lessons']
    assert len(lessons) == 10
    assert validate_timetable(session, lessons, settings) == []
