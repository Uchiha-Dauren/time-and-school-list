"""Build and independently validate a whole-school weekly CP-SAT timetable.

A grade's daily limit also defines its last allowed period within the class's
shift. Assignments fix the teacher and optional room, and may override a grade's
weekly quota. No database writes occur here: callers validate and save a complete
result in one transaction.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import random
import secrets
from typing import Any

from ortools.sat.python import cp_model
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Grade, GradeSubject, Room, SchoolClass, Settings, Subject,
    Teacher, TeachingAssignment,
)
from app.services.bells import period_times

DAYS = range(5)
Lesson = dict[str, Any]


class TimetableError(ValueError):
    """A helpful configuration, infeasibility, or search-timeout diagnostic."""

    def __init__(self, message: str, details: list[str] | None = None, status: str = 'invalid') -> None:
        super().__init__(message)
        self.message = message
        self.details = details or []
        self.status = status


@dataclass(frozen=True)
class Requirement:
    class_id: int
    subject_id: int
    teacher_id: int
    room_id: int | None
    count: int
    cap: int
    label: str
    is_hard: bool
    shift: int


@dataclass
class Problem:
    classes: dict[int, SchoolClass]
    teachers: dict[int, Teacher]
    rooms: dict[int, Room]
    requirements: list[Requirement]
    caps: dict[int, int]
    times: dict[tuple[int, int], tuple[int, int]]


def _minutes(value: str) -> int:
    hour, minute = map(int, value.split(':'))
    return hour * 60 + minute


def _load_problem(session: Session, settings: Settings) -> Problem:
    """Reject incomplete data and prove elementary resource overloads early."""
    classes = {x.id: x for x in session.scalars(select(SchoolClass))}
    grades = {x.id: x for x in session.scalars(select(Grade))}
    subjects = {x.id: x for x in session.scalars(select(Subject))}
    teachers = {x.id: x for x in session.scalars(select(Teacher))}
    rooms = {x.id: x for x in session.scalars(select(Room))}
    assignments = {(x.class_id, x.subject_id): x for x in session.scalars(select(TeachingAssignment))}
    curriculum: dict[int, list[GradeSubject]] = defaultdict(list)
    for item in session.scalars(select(GradeSubject)):
        curriculum[item.grade_id].append(item)
    errors: list[str] = []
    if not classes:
        errors.append('Add at least one grade with a parallel class before generating.')
    if not isinstance(settings.max_lessons_per_day, int) or not 1 <= settings.max_lessons_per_day <= 12:
        errors.append('School maximum lessons per day must be between 1 and 12.')
    if errors:
        raise TimetableError('School configuration is incomplete.', errors)
    requirements: list[Requirement] = []
    caps: dict[int, int] = {}
    times: dict[tuple[int, int], tuple[int, int]] = {}
    for shift in {getattr(c, 'shift', 1) for c in classes.values()}:
        if shift not in (1, 2) or (shift == 2 and not getattr(settings, 'shifts_enabled', False)):
            errors.append('Second-shift classes require two shifts to be enabled in school settings.')
            continue
        try:
            for slot in period_times(settings, shift=shift):
                times[shift, slot['period']] = (_minutes(slot['start']), _minutes(slot['end']))
        except ValueError as exc:
            errors.append(str(exc))
    for grade in grades.values():
        if not any(c.grade_id == grade.id for c in classes.values()):
            errors.append(f'Grade {grade.number} has no parallel classes.')
    for school_class in classes.values():
        grade = grades.get(school_class.grade_id)
        if grade is None:
            errors.append(f'{school_class.name}: grade does not exist.')
            continue
        cap = min(settings.max_lessons_per_day, grade.max_lessons_per_day)
        caps[school_class.id] = cap
        if not 1 <= cap <= 12:
            errors.append(f'{school_class.name}: daily limit must be between 1 and 12.')
        quotas = {item.subject_id: item.lessons_per_week for item in curriculum[grade.id]}
        for (assigned_class, subject_id), assignment in assignments.items():
            if assigned_class == school_class.id and getattr(assignment, 'lessons_per_week', None) is not None:
                quotas[subject_id] = assignment.lessons_per_week
        if not quotas:
            errors.append(f'{school_class.name}: add grade {grade.number} curriculum subjects or class-specific weekly quotas.')
        for subject_id, lessons_per_week in quotas.items():
            subject = subjects.get(subject_id)
            if subject is None:
                errors.append(f'{school_class.name}: curriculum subject {subject_id} does not exist.')
                continue
            label = f'{school_class.name} / {subject.name} ({lessons_per_week} lessons/week)'
            if lessons_per_week < 1:
                errors.append(f'{label}: weekly quota must be positive.')
                continue
            assignment = assignments.get((school_class.id, subject_id))
            if assignment is None:
                errors.append(f'{label}: missing teacher assignment.')
                continue
            teacher = teachers.get(assignment.teacher_id)
            if teacher is None:
                errors.append(f'{label}: assigned teacher does not exist.')
                continue
            if subject_id not in {x.id for x in teacher.subjects}:
                errors.append(f'{label}: teacher {teacher.name} is not qualified for this subject.')
                continue
            room_id = assignment.room_id if settings.use_rooms else None
            if settings.use_rooms and room_id not in rooms:
                errors.append(f'{label}: assign an existing room, or turn room constraints off.')
                continue
            requirements.append(Requirement(
                school_class.id, subject.id, teacher.id, room_id,
                lessons_per_week, cap, label, subject.is_hard, getattr(school_class, 'shift', 1),
            ))
    if errors:
        raise TimetableError('School configuration is incomplete or inconsistent.', errors)
    overloads: list[str] = []
    for class_id, school_class in classes.items():
        required = sum(r.count for r in requirements if r.class_id == class_id)
        capacity = 5 * caps[class_id]
        if required > capacity:
            overloads.append(
                f'{school_class.name} needs {required} weekly lessons but has only {capacity} slots '
                f'(5 days × {caps[class_id]} periods). Reduce its curriculum or raise its daily limit.'
            )
    # Period sets are nested within each shift, not across the whole school.
    # A teacher can teach first period in both shifts when the times do not overlap.
    for kind, resources, field in [('Teacher', teachers, 'teacher_id'), ('Room', rooms, 'room_id')]:
        if kind == 'Room' and not settings.use_rooms:
            continue
        for resource_id, resource in resources.items():
            jobs = [r for r in requirements if getattr(r, field) == resource_id]
            for shift in {r.shift for r in jobs}:
                shift_jobs = [r for r in jobs if r.shift == shift]
                for cap in sorted({r.cap for r in shift_jobs}):
                    needed = sum(r.count for r in shift_jobs if r.cap <= cap)
                    if needed > 5 * cap:
                        overloads.append(
                            f'{kind} {resource.name} needs {needed} lessons restricted to shift {shift}, periods 1–{cap}, '
                            f'but only {5 * cap} weekly slots are available. '
                            f'Reassign some classes or increase the affected grades\' daily limits.'
                        )
    if overloads:
        raise TimetableError('The requested timetable is infeasible.', overloads, status='infeasible')
    return Problem(classes, teachers, rooms, requirements, caps, times)


def _gap_costs(model: cp_model.CpModel, occupied: list[Any], name: str) -> list[Any]:
    """Count empty periods strictly between the first and last lesson of a day."""
    penalties = []
    for p in range(1, len(occupied) - 1):
        before = model.new_bool_var(f'{name}_before_{p}')
        after = model.new_bool_var(f'{name}_after_{p}')
        gap = model.new_bool_var(f'{name}_gap_{p}')
        model.add_max_equality(before, occupied[:p])
        model.add_max_equality(after, occupied[p + 1:])
        # Exact equivalence: gap = before AND after AND NOT occupied[p].
        model.add(gap <= before)
        model.add(gap <= after)
        model.add(gap + occupied[p] <= 1)
        model.add(gap >= before + after - occupied[p] - 1)
        penalties.append(gap)
    return penalties


def generate_timetable(session: Session, settings: Settings, *, seed: int | None = None) -> dict[str, Any]:
    """Find a feasible timetable, improve soft costs, and validate before return.

    FEASIBLE means the time limit stopped optimization after a valid solution was
    found. UNKNOWN is a timeout, never evidence that the school is infeasible.
    Named quota assumptions produce an explanation if CP-SAT proves infeasibility.
    Omitting seed gives a new random tie-breaker; a fixed seed supports repeatable
    demonstrations. Time-limited results can still vary with machine speed.
    """
    if seed is None:
        seed = secrets.randbelow(2**31)
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**31:
        raise TimetableError('The random seed must be an integer between 0 and 2147483647.')
    rng = random.Random(seed)
    problem = _load_problem(session, settings)
    model = cp_model.CpModel()
    variables: dict[tuple[int, int, int], Any] = {}
    by_class: dict[tuple[int, int, int], list[Any]] = defaultdict(list)
    by_teacher: dict[tuple[int, int, int, int], list[Any]] = defaultdict(list)
    teacher_intervals: dict[tuple[int, int], list[Any]] = defaultdict(list)
    room_intervals: dict[tuple[int, int], list[Any]] = defaultdict(list)
    assumption_labels: dict[int, str] = {}
    objective: list[Any] = []
    for index, requirement in enumerate(problem.requirements):
        week = []
        for day in DAYS:
            daily = []
            for period in range(1, requirement.cap + 1):
                variable = model.new_bool_var(f'r{index}_d{day}_p{period}')
                variables[index, day, period] = variable
                by_class[requirement.class_id, day, period].append(variable)
                by_teacher[requirement.teacher_id, day, requirement.shift, period].append(variable)
                start, end = problem.times[requirement.shift, period]
                interval = model.new_optional_fixed_size_interval_var(
                    start, end - start, variable, f'r{index}_d{day}_p{period}_time',
                )
                teacher_intervals[requirement.teacher_id, day].append(interval)
                if requirement.room_id is not None:
                    room_intervals[requirement.room_id, day].append(interval)
                daily.append(variable)
                # Mild pressure to finish earlier, stronger for hard subjects.
                objective.append((period - 1) * (4 if requirement.is_hard else 1) * variable)
            used_day = model.new_bool_var(f'r{index}_d{day}_used')
            model.add(sum(daily) >= used_day)
            model.add(sum(daily) <= requirement.cap * used_day)
            objective.append(-8 * used_day)
            excess = model.new_int_var(0, requirement.cap, f'r{index}_d{day}_over_two')
            model.add(excess >= sum(daily) - 2)
            objective.append(30 * excess)
            week.extend(daily)
        quota = model.new_bool_var(f'required_{index}')
        model.add(sum(week) == requirement.count).only_enforce_if(quota)
        model.add_assumption(quota)
        assumption_labels[quota.index] = requirement.label
    for groups in (by_class, by_teacher):
        for group in groups.values():
            model.add(sum(group) <= 1)
    # Compare real clock intervals: period 2 in one shift may overlap period 1
    # in another, while the same period number at different times is safe.
    for groups in (teacher_intervals, room_intervals):
        for intervals in groups.values():
            model.add_no_overlap(intervals)
    for class_id in problem.classes:
        total = sum(r.count for r in problem.requirements if r.class_id == class_id)
        cap = problem.caps[class_id]
        for day in DAYS:
            occupied = []
            for period in range(1, cap + 1):
                busy = model.new_bool_var(f'class{class_id}_d{day}_p{period}')
                model.add(busy == sum(by_class[class_id, day, period]))
                occupied.append(busy)
            objective.extend(30 * gap for gap in _gap_costs(model, occupied, f'class{class_id}_d{day}'))
            deviation = model.new_int_var(0, 5 * cap, f'class{class_id}_d{day}_deviation')
            model.add_abs_equality(deviation, 5 * sum(occupied) - total)
            objective.append(2 * deviation)
    for teacher_id in {r.teacher_id for r in problem.requirements}:
        for shift in {r.shift for r in problem.requirements if r.teacher_id == teacher_id}:
            for day in DAYS:
                occupied = []
                for period in range(1, settings.max_lessons_per_day + 1):
                    busy = model.new_bool_var(f'teacher{teacher_id}_s{shift}_d{day}_p{period}')
                    model.add(busy == sum(by_teacher[teacher_id, day, shift, period]))
                    occupied.append(busy)
                objective.extend(8 * gap for gap in _gap_costs(model, occupied, f'teacher{teacher_id}_s{shift}_d{day}'))
    # Random weights break ties only: their entire possible range is smaller
    # than one point of the original soft-preference score.
    tie_break = sum(rng.randrange(10) * variable for variable in variables.values())
    model.minimize((9 * len(variables) + 1) * sum(objective) + tie_break)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = settings.solver_time_limit
    # One worker preserves assumption-core support and reproducible seeded runs.
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = seed
    status = solver.solve(model)
    if status == cp_model.INFEASIBLE:
        core = solver.sufficient_assumptions_for_infeasibility()
        labels = [assumption_labels[i] for i in core if i in assumption_labels]
        details = [
            'These required class/subject quotas cannot all fit while preserving class, '
            'teacher, room, and grade-period constraints. The set is sufficient, not necessarily minimal.'
        ] + labels
        if not labels:
            details.append('Review daily grade limits and shared teacher/room assignments.')
        raise TimetableError('No conflict-free timetable exists for the current configuration.', details, 'infeasible')
    if status == cp_model.UNKNOWN:
        raise TimetableError(
            'The solver reached its time limit before finding a timetable; infeasibility was not proven.',
            ['Increase solver_time_limit and retry. The previous timetable is unchanged.'], 'timeout',
        )
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise TimetableError('The solver model could not be processed.', [solver.status_name(status)])
    lessons: list[Lesson] = []
    for (index, day, period), variable in variables.items():
        if solver.boolean_value(variable):
            requirement = problem.requirements[index]
            lessons.append({
                'class_id': requirement.class_id,
                'subject_id': requirement.subject_id,
                'teacher_id': requirement.teacher_id,
                'room_id': requirement.room_id,
                'weekday': day,
                'period': period,
            })
    lessons.sort(key=lambda lesson: (lesson['class_id'], lesson['weekday'], lesson['period']))
    errors = validate_timetable(session, lessons, settings)
    if errors:
        raise TimetableError('Independent result validation failed; no timetable was saved.', errors)
    optimal = status == cp_model.OPTIMAL
    return {
        'status': 'optimal' if optimal else 'feasible',
        'lessons': lessons,
        'objective': solver.value(sum(objective)),
        'seed': seed,
        'message': ('A valid timetable with optimal soft-constraint score was found.' if optimal else
                    'A valid timetable was found. Soft preferences were improved within the time limit; optimality is not proven.'),
    }


def validate_timetable(session: Session, lessons: list[Lesson], settings: Settings) -> list[str]:
    """Check every hard constraint without relying on the solver implementation."""
    try:
        problem = _load_problem(session, settings)
    except TimetableError as exc:
        return [exc.message, *exc.details]
    required = {(r.class_id, r.subject_id): r for r in problem.requirements}
    counts: Counter[tuple[int, int]] = Counter()
    seen_class: set[tuple[int, int, int]] = set()
    seen_teacher: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
    seen_room: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
    errors: list[str] = []
    fields = ('class_id', 'subject_id', 'teacher_id', 'weekday', 'period')
    for number, lesson in enumerate(lessons, 1):
        if not isinstance(lesson, dict) or any(not isinstance(lesson.get(field), int) or isinstance(lesson.get(field), bool) for field in fields):
            errors.append(f'Lesson {number}: class, subject, teacher, weekday, and period must be integer identifiers/indices.')
            continue
        class_id, subject_id = lesson['class_id'], lesson['subject_id']
        teacher_id, day, period = lesson['teacher_id'], lesson['weekday'], lesson['period']
        room_id = lesson.get('room_id')
        if room_id is not None and (not isinstance(room_id, int) or isinstance(room_id, bool)):
            errors.append(f'Lesson {number}: room must be an integer identifier or null.')
            continue
        requirement = required.get((class_id, subject_id))
        if requirement is None:
            errors.append(f'Lesson {number}: class {class_id} / subject {subject_id} is outside the configured curriculum.')
            continue
        counts[class_id, subject_id] += 1
        if day not in DAYS:
            errors.append(f'Lesson {number}: weekday must be Monday–Friday (0–4).')
        if not 1 <= period <= requirement.cap:
            errors.append(f'Lesson {number}: {problem.classes[class_id].name} allows periods 1–{requirement.cap}.')
        if teacher_id != requirement.teacher_id:
            errors.append(f'Lesson {number}: teacher does not match the class/subject assignment.')
        if room_id != requirement.room_id:
            errors.append(f'Lesson {number}: room does not match the assignment (rooms disabled requires null).')
        class_key = (class_id, day, period)
        if class_key in seen_class:
            errors.append(f'Class {class_id} conflict on weekday {day}, period {period}.')
        seen_class.add(class_key)
        if day not in DAYS or not 1 <= period <= requirement.cap:
            continue
        start, end = problem.times[requirement.shift, period]
        for label, resource_id, seen in (('Teacher', teacher_id, seen_teacher), ('Room', room_id, seen_room)):
            if resource_id is None:
                continue
            key = (resource_id, day)
            if any(start < previous_end and previous_start < end for previous_start, previous_end in seen[key]):
                errors.append(f'{label} {resource_id} time conflict on weekday {day}, shift {requirement.shift}, period {period}.')
            seen[key].append((start, end))
    for key, requirement in required.items():
        if counts[key] != requirement.count:
            errors.append(f'{requirement.label}: expected exactly {requirement.count}, found {counts[key]}.')
    return errors
