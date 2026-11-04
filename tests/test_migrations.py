"""Upgrade an actual old SQLite schema while retaining saved school records."""
from sqlalchemy import create_engine, inspect
from app.migrations import upgrade_database
from app.models import Base


def test_original_database_upgrade_is_idempotent_and_keeps_lessons(tmp_path):
    engine = create_engine(f'sqlite:///{tmp_path / "old.db"}')
    with engine.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE classes (id INTEGER PRIMARY KEY, grade_id INTEGER NOT NULL REFERENCES grades(id), name VARCHAR(40) NOT NULL UNIQUE)')
        connection.exec_driver_sql('CREATE TABLE teaching_assignments (id INTEGER PRIMARY KEY, class_id INTEGER NOT NULL, subject_id INTEGER NOT NULL, teacher_id INTEGER NOT NULL, room_id INTEGER, UNIQUE(class_id, subject_id))')
        connection.exec_driver_sql('CREATE TABLE settings (id INTEGER PRIMARY KEY, start_time VARCHAR(5))')
        connection.exec_driver_sql('''CREATE TABLE timetable_lessons (
            id INTEGER PRIMARY KEY, class_id INTEGER NOT NULL REFERENCES classes(id),
            subject_id INTEGER NOT NULL REFERENCES subjects(id), teacher_id INTEGER NOT NULL REFERENCES teachers(id),
            room_id INTEGER, weekday INTEGER NOT NULL, period INTEGER NOT NULL,
            UNIQUE(class_id, weekday, period), UNIQUE(teacher_id, weekday, period), UNIQUE(room_id, weekday, period))''')
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql('PRAGMA foreign_keys=ON')
        connection.exec_driver_sql('INSERT INTO grades VALUES (1, 1, 4)')
        connection.exec_driver_sql("INSERT INTO classes VALUES (1, 1, '1A')")
        connection.exec_driver_sql("INSERT INTO teachers VALUES (1, 'Teacher')")
        connection.exec_driver_sql("INSERT INTO subjects VALUES (1, 'Math', 1)")
        connection.exec_driver_sql('INSERT INTO timetable_lessons VALUES (7, 1, 1, 1, NULL, 0, 1)')
        connection.exec_driver_sql("INSERT INTO settings VALUES (1, '08:30')")
    assert upgrade_database(engine) is True
    assert upgrade_database(engine) is False
    with engine.connect() as connection:
        assert connection.exec_driver_sql('SELECT id, class_id FROM timetable_lessons').all() == [(7, 1)]
        assert connection.exec_driver_sql('SELECT language, shift FROM classes').one() == ('kk', 1)
        assert connection.exec_driver_sql('SELECT shifts_enabled, second_shift_start_time, long_break_after FROM settings').one() == (0, '14:00', 2)
        constraints = [set(row['column_names']) for row in inspect(connection).get_unique_constraints('timetable_lessons')]
        assert constraints == [{'class_id', 'weekday', 'period'}]
        assert connection.exec_driver_sql('PRAGMA foreign_key_check').all() == []
    engine.dispose()


def test_v110_upgrade_removes_pupil_constraints_and_keeps_references(tmp_path):
    from sqlalchemy import event
    from sqlalchemy.orm import Session
    from app.models import SchoolClass

    engine = create_engine(f'sqlite:///{tmp_path / "v110.db"}')

    @event.listens_for(engine, 'connect')
    def enforce_foreign_keys(connection, _):
        connection.execute('PRAGMA foreign_keys=ON')

    with engine.begin() as connection:
        connection.exec_driver_sql('''CREATE TABLE classes (
            id INTEGER PRIMARY KEY,
            grade_id INTEGER NOT NULL REFERENCES grades(id),
            name VARCHAR(40) NOT NULL UNIQUE,
            language VARCHAR(2) NOT NULL CHECK (language IN ('kk', 'ru')),
            student_count INTEGER NOT NULL,
            shift INTEGER NOT NULL CHECK (shift IN (1, 2)),
            CHECK (student_count BETWEEN 1 AND 15))''')
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql('INSERT INTO grades VALUES (1, 5, 6)')
        connection.exec_driver_sql("INSERT INTO classes VALUES (1, 1, '5A', 'kk', 15, 1)")
        connection.exec_driver_sql("INSERT INTO teachers VALUES (1, 'Teacher')")
        connection.exec_driver_sql("INSERT INTO subjects VALUES (1, 'Math', 1)")
        connection.exec_driver_sql('INSERT INTO teaching_assignments VALUES (1, 1, 1, 1, NULL, 5)')
        connection.exec_driver_sql('INSERT INTO timetable_lessons VALUES (7, 1, 1, 1, NULL, 0, 1)')
    assert upgrade_database(engine) is True
    assert upgrade_database(engine) is False
    with Session(engine) as session:
        session.add(SchoolClass(grade_id=1, name='5B', language='ru', shift=1))
        session.commit()
    with engine.connect() as connection:
        assert 'student_count' not in {column['name'] for column in inspect(connection).get_columns('classes')}
        assert connection.exec_driver_sql('SELECT name FROM classes ORDER BY id').all() == [('5A',), ('5B',)]
        assert connection.exec_driver_sql('SELECT id, class_id FROM timetable_lessons').all() == [(7, 1)]
        assert connection.exec_driver_sql('SELECT class_id FROM teaching_assignments').scalar() == 1
        assert connection.exec_driver_sql('PRAGMA foreign_keys').scalar() == 1
        assert connection.exec_driver_sql('PRAGMA foreign_key_check').all() == []
    engine.dispose()
