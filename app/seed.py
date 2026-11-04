"""Seed an empty school: python -m app.seed --generate."""
import argparse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.database import SessionLocal, get_settings, init_db, school_lock
from app.models import Grade, GradeSubject, Room, SchoolClass, Subject, Teacher, TeachingAssignment


def seed_demo(session: Session) -> dict:
    """Create 3 grades, 6 classes and 6 qualified teachers, without overwriting data."""
    if any(session.scalar(select(func.count()).select_from(model)) for model in (Grade, Subject, Teacher, Room, SchoolClass)):
        raise ValueError('Demo data can only be loaded into an empty school. Existing data was preserved.')
    curriculum = {
        1: [('Literacy', 5), ('Math', 4), ('Nature Studies', 2), ('Art', 2), ('PE', 2), ('Music', 1)],
        6: [('Math', 4), ('Literature', 4), ('Science', 3), ('History', 2), ('PE', 2), ('Art', 1)],
        11: [('Algebra', 4), ('Geometry', 2), ('Physics', 3), ('Literature', 3), ('History', 2), ('PE', 2)],
    }
    subjects = {}
    for name in sorted({name for rows in curriculum.values() for name, _ in rows}):
        subjects[name] = Subject(name=name, is_hard=name in {'Math', 'Algebra', 'Geometry', 'Physics'})
        session.add(subjects[name])
    teacher_specs = [
        ('Aigerim — Mathematics', ['Math', 'Algebra', 'Geometry']),
        ('Dana — Languages', ['Literacy', 'Literature']),
        ('Yersultan — Science', ['Nature Studies', 'Science', 'Physics']),
        ('Zhanar — History & Music', ['History', 'Music']),
        ('Asel — Art', ['Art']),
        ('Dauren — Physical Education', ['PE']),
    ]
    by_subject = {}
    for name, names in teacher_specs:
        teacher = Teacher(name=name, subjects=[subjects[n] for n in names])
        session.add(teacher)
        for subject_name in names:
            by_subject[subject_name] = teacher
    session.flush()
    for number, entries in curriculum.items():
        grade = Grade(number=number, max_lessons_per_day={1: 4, 6: 6, 11: 7}[number])
        session.add(grade)
        session.flush()
        for name, count in entries:
            session.add(GradeSubject(grade_id=grade.id, subject_id=subjects[name].id, lessons_per_week=count))
        for suffix in ('A', 'B'):
            room = Room(name=f'Room {number}{suffix}')
            school_class = SchoolClass(name=f'{number}{suffix}', grade_id=grade.id,
                                       language='kk' if suffix == 'A' else 'ru')
            session.add_all([room, school_class])
            session.flush()
            for name, _ in entries:
                session.add(TeachingAssignment(class_id=school_class.id, subject_id=subjects[name].id,
                                               teacher_id=by_subject[name].id, room_id=room.id))
    get_settings(session).use_rooms = True
    session.flush()
    return {'message': 'Demo school created: grades 1, 6, 11; 6 classes; 6 teachers; 96 weekly lessons.'}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--generate', action='store_true', help='Also solve and save the initial timetable.')
    args = parser.parse_args()
    init_db()
    with school_lock, SessionLocal.begin() as session:
        try:
            print(seed_demo(session)['message'])
        except ValueError as exc:
            print(exc)
        if args.generate:
            from app.models import TimetableLesson
            from app.services.bells import rebuild_bell_events
            from app.services.timetable import generate_timetable
            from sqlalchemy import delete
            settings = get_settings(session)
            result = generate_timetable(session, settings)
            session.execute(delete(TimetableLesson))
            session.add_all(TimetableLesson(**row) for row in result['lessons'])
            session.flush()
            rebuild_bell_events(session, settings)
            print(f"Saved {len(result['lessons'])} lessons ({result['status']}).")

if __name__ == '__main__':
    main()
