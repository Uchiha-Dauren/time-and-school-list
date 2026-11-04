"""School setup, validated timetable generation and timetable editing endpoints."""
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from app.database import get_db, get_settings, school_lock
from app.models import (BellEvent, Grade, GradeSubject, Room, SchoolClass, Settings,
                        Subject, Teacher, TeachingAssignment, TimetableLesson)
from app.schemas import (AssignmentInput, ClassInput, CurriculumInput, GenerateInput, GradeInput,
                         GradeUpdate, LessonMove, RoomInput, SettingsInput, SubjectInput, TeacherInput)
from app.seed import seed_demo
from app.services.bells import period_times, rebuild_bell_events
from app.services.timetable import generate_timetable, validate_timetable

router = APIRouter(prefix='/api', tags=['School & timetable'])
DB = Annotated[Session, Depends(get_db)]


def row_dict(row) -> dict:
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def require(session: Session, model, record_id: int):
    row = session.get(model, record_id)
    if row is None:
        raise HTTPException(404, f'{model.__name__} {record_id} does not exist.')
    return row


def invalidate(session: Session) -> None:
    """A changed curriculum must never leave an apparently valid stale timetable."""
    session.execute(delete(BellEvent))
    session.execute(delete(TimetableLesson))


def saved(session: Session, row) -> dict:
    session.commit()
    return {'message': 'Saved. Generate the timetable again after school data changes.', 'id': row.id}


def require_shift(session: Session, shift: int) -> None:
    if shift == 2 and not get_settings(session).shifts_enabled:
        raise HTTPException(422, 'Enable two shifts in Settings before assigning a class to shift 2.')


@router.get('/catalog')
def catalog(session: DB):
    result = {}
    for name, model in [('grades', Grade), ('classes', SchoolClass), ('subjects', Subject),
                        ('grade_subjects', GradeSubject), ('assignments', TeachingAssignment), ('rooms', Room)]:
        result[name] = [row_dict(row) for row in session.scalars(select(model).order_by(model.id))]
    result['teachers'] = [dict(id=row.id, name=row.name, subject_ids=[s.id for s in row.subjects])
                          for row in session.scalars(select(Teacher).order_by(Teacher.id))]
    result['settings'] = {k: v for k, v in row_dict(get_settings(session)).items() if k != 'id'}
    return result


@router.post('/subjects', status_code=201)
def add_subject(data: SubjectInput, session: DB):
    with school_lock:
        row = Subject(**data.model_dump())
        session.add(row)
        invalidate(session)
        return saved(session, row)


def teacher_subjects(session: Session, ids: list[int]) -> list[Subject]:
    return [require(session, Subject, record_id) for record_id in sorted(set(ids))]


@router.post('/teachers', status_code=201)
def add_teacher(data: TeacherInput, session: DB):
    with school_lock:
        row = Teacher(name=data.name, subjects=teacher_subjects(session, data.subject_ids))
        session.add(row)
        invalidate(session)
        return saved(session, row)


@router.put('/teachers/{teacher_id}')
def edit_teacher(teacher_id: int, data: TeacherInput, session: DB):
    with school_lock:
        row = require(session, Teacher, teacher_id)
        assigned = set(session.scalars(select(TeachingAssignment.subject_id).where(TeachingAssignment.teacher_id == teacher_id)))
        if assigned - set(data.subject_ids):
            raise HTTPException(409, 'Reassign this teacher’s classes before removing a subject they teach.')
        row.name, row.subjects = data.name, teacher_subjects(session, data.subject_ids)
        invalidate(session)
        return saved(session, row)


@router.post('/grades', status_code=201)
def add_grade(data: GradeInput, session: DB):
    with school_lock:
        require_shift(session, data.shift)
        row = Grade(number=data.number, max_lessons_per_day=data.max_lessons_per_day or (4 if data.number <= 4 else 7))
        session.add(row)
        session.flush()
        for index in range(data.parallel_count):
            session.add(SchoolClass(name=f'{data.number}{chr(65 + index)}', grade_id=row.id,
                                    language=data.language, shift=data.shift))
        invalidate(session)
        return saved(session, row)


@router.put('/grades/{grade_id}')
def edit_grade(grade_id: int, data: GradeUpdate, session: DB):
    with school_lock:
        row = require(session, Grade, grade_id)
        row.max_lessons_per_day = data.max_lessons_per_day
        invalidate(session)
        return saved(session, row)


@router.post('/classes', status_code=201)
def add_class(data: ClassInput, session: DB):
    with school_lock:
        require(session, Grade, data.grade_id)
        require_shift(session, data.shift)
        row = SchoolClass(**data.model_dump())
        session.add(row)
        invalidate(session)
        return saved(session, row)


@router.put('/classes/{class_id}')
def edit_class(class_id: int, data: ClassInput, session: DB):
    with school_lock:
        row = require(session, SchoolClass, class_id)
        require(session, Grade, data.grade_id)
        require_shift(session, data.shift)
        if row.grade_id != data.grade_id and session.scalar(select(TeachingAssignment.id).where(
                TeachingAssignment.class_id == class_id).limit(1)):
            raise HTTPException(409, 'A class with teacher assignments cannot change grade.')
        for key, value in data.model_dump().items():
            setattr(row, key, value)
        invalidate(session)
        return saved(session, row)


@router.post('/rooms', status_code=201)
def add_room(data: RoomInput, session: DB):
    with school_lock:
        row = Room(**data.model_dump())
        session.add(row)
        invalidate(session)
        return saved(session, row)


@router.post('/curriculum')
def set_curriculum(data: CurriculumInput, session: DB):
    with school_lock:
        require(session, Grade, data.grade_id)
        require(session, Subject, data.subject_id)
        row = session.scalar(select(GradeSubject).where(GradeSubject.grade_id == data.grade_id,
                                                       GradeSubject.subject_id == data.subject_id))
        if row:
            row.lessons_per_week = data.lessons_per_week
        else:
            row = GradeSubject(**data.model_dump())
            session.add(row)
        invalidate(session)
        return saved(session, row)


@router.post('/assignments')
def set_assignment(data: AssignmentInput, session: DB):
    with school_lock:
        school_class = require(session, SchoolClass, data.class_id)
        teacher = require(session, Teacher, data.teacher_id)
        require(session, Subject, data.subject_id)
        if data.room_id is not None:
            require(session, Room, data.room_id)
        if data.subject_id not in {s.id for s in teacher.subjects}:
            raise HTTPException(422, 'This teacher is not qualified for the selected subject.')
        if data.lessons_per_week is None and not session.scalar(select(GradeSubject).where(
                GradeSubject.grade_id == school_class.grade_id, GradeSubject.subject_id == data.subject_id)):
            raise HTTPException(422, 'Enter lessons per week for this class, or add the subject to the grade curriculum.')
        row = session.scalar(select(TeachingAssignment).where(TeachingAssignment.class_id == data.class_id,
                                                               TeachingAssignment.subject_id == data.subject_id))
        if row:
            row.teacher_id, row.room_id = data.teacher_id, data.room_id
            row.lessons_per_week = data.lessons_per_week
        else:
            row = TeachingAssignment(**data.model_dump())
            session.add(row)
        invalidate(session)
        return saved(session, row)


@router.post('/seed')
def seed(session: DB):
    with school_lock:
        try:
            result = seed_demo(session)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        session.commit()
        return result


@router.put('/settings')
def update_settings(data: SettingsInput, session: DB):
    with school_lock:
        row = get_settings(session)
        if not data.shifts_enabled and session.scalar(select(SchoolClass.id).where(SchoolClass.shift == 2).limit(1)):
            raise HTTPException(409, 'Move all second-shift classes to shift 1 before disabling two shifts.')
        structure_changed = row.max_lessons_per_day != data.max_lessons_per_day or row.use_rooms != data.use_rooms
        clock_changed = any(getattr(row, key) != getattr(data, key) for key in
                            ('start_time', 'second_shift_start_time', 'long_break_after', 'shifts_enabled'))
        for key, value in data.model_dump().items():
            setattr(row, key, value)
        if clock_changed and not structure_changed:
            proposed = [{k: v for k, v in row_dict(lesson).items() if k != 'id'}
                        for lesson in session.scalars(select(TimetableLesson))]
            if proposed:
                problems = validate_timetable(session, proposed, row)
                if problems:
                    raise HTTPException(409, {'message': 'The new times would conflict with saved lessons.', 'details': problems})
        if structure_changed:
            invalidate(session)
        else:
            rebuild_bell_events(session, row)
        session.commit()
        return {'message': 'Settings saved.' + (' Regenerate the timetable after capacity/room changes.' if structure_changed else '')}


def planning_snapshot(session: Session) -> dict:
    """Detect setup/lesson changes while the solver is working."""
    return catalog(session) | {'lessons': [row_dict(row) for row in session.scalars(select(TimetableLesson).order_by(TimetableLesson.id))]}


@router.post('/timetable/generate')
def generate(session: DB, data: GenerateInput | None = None):
    with school_lock:
        original = planning_snapshot(session)
    # Solving must not block scheduled bells. A final snapshot comparison and
    # independent validation prevent a concurrent edit from being overwritten.
    result = (generate_timetable(session, get_settings(session), seed=data.seed)
              if data and data.seed is not None else generate_timetable(session, get_settings(session)))
    session.rollback()
    session.expire_all()
    with school_lock:
        if planning_snapshot(session) != original:
            raise HTTPException(409, 'School data, settings, or lessons changed while solving. Please generate again.')
        settings = get_settings(session)
        problems = validate_timetable(session, result['lessons'], settings)
        if problems:
            raise HTTPException(500, {'message': 'Independent validation rejected the solver output.', 'details': problems})
        invalidate(session)
        session.add_all(TimetableLesson(**row) for row in result['lessons'])
        session.flush()
        rebuild_bell_events(session, settings)
        session.commit()
        return {key: value for key, value in result.items() if key != 'lessons'} | {'lesson_count': len(result['lessons'])}


@router.get('/timetable')
def timetable(session: DB, class_id: int | None = None, teacher_id: int | None = None):
    query = select(TimetableLesson).order_by(TimetableLesson.weekday, TimetableLesson.period, TimetableLesson.class_id)
    if class_id is not None:
        query = query.where(TimetableLesson.class_id == class_id)
    if teacher_id is not None:
        query = query.where(TimetableLesson.teacher_id == teacher_id)
    classes = {row.id: row for row in session.scalars(select(SchoolClass))}
    teachers = {row.id: row.name for row in session.scalars(select(Teacher))}
    subjects = {row.id: row.name for row in session.scalars(select(Subject))}
    rooms = {row.id: row.name for row in session.scalars(select(Room))}
    lessons = [row_dict(row) | dict(class_name=classes[row.class_id].name, shift=classes[row.class_id].shift,
                                    language=classes[row.class_id].language,
                                    teacher_name=teachers[row.teacher_id],
                                    subject_name=subjects[row.subject_id], room_name=rooms.get(row.room_id))
               for row in session.scalars(query)]
    settings = get_settings(session)
    return {'lessons': lessons, 'periods': period_times(settings),
            'shift_periods': {str(shift): period_times(settings, shift=shift)
                              for shift in range(1, 3 if settings.shifts_enabled else 2)}}


@router.patch('/timetable/{lesson_id}')
def move_lesson(lesson_id: int, data: LessonMove, session: DB):
    """Validate the full proposed week before touching the persisted lesson."""
    with school_lock:
        lesson = require(session, TimetableLesson, lesson_id)
        settings = get_settings(session)
        proposed = []
        for row in session.scalars(select(TimetableLesson)):
            values = row_dict(row)
            values.pop('id')
            if row.id == lesson_id:
                values.update(data.model_dump())
            proposed.append(values)
        problems = validate_timetable(session, proposed, settings)
        if problems:
            raise HTTPException(409, {'message': 'Move rejected; the existing timetable is unchanged.', 'details': problems})
        lesson.weekday, lesson.period = data.weekday, data.period
        session.flush()
        rebuild_bell_events(session, settings)
        session.commit()
        return {'message': 'Lesson moved and bell events recalculated.'}
