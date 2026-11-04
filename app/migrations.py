"""Small, idempotent SQLite upgrade for databases created by the original demo."""
from sqlalchemy import inspect
from sqlalchemy.engine import Engine


def upgrade_database(engine: Engine) -> bool:
    changed = False
    additions = {
        'classes': {
            'language': "VARCHAR(2) NOT NULL DEFAULT 'kk' CHECK (language IN ('kk', 'ru'))",
            'shift': 'INTEGER NOT NULL DEFAULT 1 CHECK (shift IN (1, 2))',
        },
        'teaching_assignments': {
            'lessons_per_week': 'INTEGER CHECK (lessons_per_week IS NULL OR lessons_per_week BETWEEN 1 AND 60)',
        },
        'settings': {
            'shifts_enabled': 'BOOLEAN NOT NULL DEFAULT 0',
            'second_shift_start_time': "VARCHAR(5) NOT NULL DEFAULT '14:00'",
            'long_break_after': 'INTEGER NOT NULL DEFAULT 2',
        },
    }
    with engine.connect() as connection:
        foreign_keys = connection.exec_driver_sql('PRAGMA foreign_keys').scalar()
        connection.exec_driver_sql('PRAGMA foreign_keys=OFF')
        connection.commit()
        try:
            with connection.begin():
                # sqlite3's legacy transaction mode does not begin on DDL by itself.
                # An explicit transaction makes the schema upgrade atomic too.
                connection.exec_driver_sql('BEGIN IMMEDIATE')
                for table, columns in additions.items():
                    present = {column['name'] for column in inspect(connection).get_columns(table)}
                    for name, definition in columns.items():
                        if name not in present:
                            connection.exec_driver_sql(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
                            changed = True
                if 'student_count' in {column['name'] for column in inspect(connection).get_columns('classes')}:
                    # Remove the obsolete pupil count and its CHECK/NOT NULL constraints.
                    # Keep class IDs, names and all dependent lessons/assignments intact.
                    connection.exec_driver_sql("""CREATE TABLE classes_v2 (
                        id INTEGER PRIMARY KEY,
                        grade_id INTEGER NOT NULL REFERENCES grades(id),
                        name VARCHAR(40) NOT NULL UNIQUE,
                        language VARCHAR(2) NOT NULL CHECK (language IN ('kk', 'ru')),
                        shift INTEGER NOT NULL CHECK (shift IN (1, 2))
                    )""")
                    connection.exec_driver_sql("""INSERT INTO classes_v2 (id, grade_id, name, language, shift)
                        SELECT id, grade_id, name, language, shift FROM classes""")
                    connection.exec_driver_sql('DROP TABLE classes')
                    connection.exec_driver_sql('ALTER TABLE classes_v2 RENAME TO classes')
                    changed = True
                unique_columns = [set(item['column_names']) for item in
                                  inspect(connection).get_unique_constraints('timetable_lessons')]
                if {'teacher_id', 'weekday', 'period'} in unique_columns or {'room_id', 'weekday', 'period'} in unique_columns:
                    # Period 1 in different shifts represents different clock times.
                    # Cross-class resource conflicts are checked by the solver and validator.
                    connection.exec_driver_sql('''CREATE TABLE timetable_lessons_v2 (
                        id INTEGER PRIMARY KEY,
                        class_id INTEGER NOT NULL REFERENCES classes(id),
                        subject_id INTEGER NOT NULL REFERENCES subjects(id),
                        teacher_id INTEGER NOT NULL REFERENCES teachers(id),
                        room_id INTEGER REFERENCES rooms(id),
                        weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 4),
                        period INTEGER NOT NULL CHECK (period BETWEEN 1 AND 12),
                        UNIQUE (class_id, weekday, period)
                    )''')
                    connection.exec_driver_sql('''INSERT INTO timetable_lessons_v2
                        SELECT id, class_id, subject_id, teacher_id, room_id, weekday, period FROM timetable_lessons''')
                    connection.exec_driver_sql('DROP TABLE timetable_lessons')
                    connection.exec_driver_sql('ALTER TABLE timetable_lessons_v2 RENAME TO timetable_lessons')
                    changed = True
                if connection.exec_driver_sql('PRAGMA foreign_key_check').first() is not None:
                    raise ValueError('Database upgrade rejected: existing foreign-key references are invalid.')
        finally:
            connection.exec_driver_sql(f'PRAGMA foreign_keys={int(foreign_keys)}')
    return changed
