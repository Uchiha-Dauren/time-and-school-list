"""Pure bell calculations shared by the UI, API, and scheduler.

Times use the school's local timezone, which the scheduler applies at runtime.
Coincident semantic events are retained; the player resolves them to one track.
"""
from datetime import datetime, timedelta
from typing import Any, Iterable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import BellEvent, SchoolClass, Settings, TimetableLesson

EVENT_TYPES = (
    'lesson_start', 'lesson_end', 'break_start', 'long_break_start', 'end_of_day',
)
EVENT_PRIORITY = {
    'end_of_day': 5,
    'long_break_start': 4,
    'break_start': 3,
    'lesson_end': 2,
    'lesson_start': 1,
}
LESSON_MINUTES = 45
NORMAL_BREAK_MINUTES = 5
LONG_BREAK_MINUTES = 15


def period_times(settings: Settings, shift: int = 1) -> list[dict[str, Any]]:
    """Build one shift's grid with one configurable 15-minute long break.

    A class keeps its own one-based period numbers in either shift. Actual
    start/end times must therefore be used for teacher and classroom conflicts.
    """
    if shift not in (1, 2):
        raise ValueError('Shift must be 1 (morning) or 2 (afternoon).')
    if shift == 2 and not getattr(settings, 'shifts_enabled', False):
        raise ValueError('Enable two shifts before scheduling an afternoon class.')
    clock = settings.start_time if shift == 1 else (getattr(settings, 'second_shift_start_time', None) or '14:00')
    try:
        start = datetime.strptime(clock, '%H:%M')
        if start.strftime('%H:%M') != clock:
            raise ValueError('Use exactly HH:MM.')
    except (ValueError, TypeError) as exc:
        raise ValueError('First lesson start time must be HH:MM, for example 08:30.') from exc
    count = settings.max_lessons_per_day
    if not isinstance(count, int) or not 1 <= count <= 12:
        raise ValueError('Maximum lessons per day must be between 1 and 12.')
    long_break_after = getattr(settings, 'long_break_after', None)
    if long_break_after is None:
        long_break_after = 2
    if not isinstance(long_break_after, int) or not 1 <= long_break_after <= 12:
        raise ValueError('The long break must follow a lesson numbered 1 through 12.')
    day = start.date()
    result: list[dict[str, Any]] = []
    for period in range(1, count + 1):
        end = start + timedelta(minutes=LESSON_MINUTES)
        if end.date() != day:
            raise ValueError('The lesson grid must finish before midnight; use an earlier start time or fewer lessons.')
        result.append({'period': period, 'start': start.strftime('%H:%M'), 'end': end.strftime('%H:%M')})
        break_minutes = LONG_BREAK_MINUTES if period == long_break_after else NORMAL_BREAK_MINUTES
        start = end + timedelta(minutes=break_minutes)
    return result


def calculate_bell_events(
    lessons: Iterable[dict[str, Any]], settings: Settings,
) -> list[dict[str, Any]]:
    """Calculate school-wide events for occupied weekly slots in both shifts.

    No events are created for empty periods, empty days, or weekends. A lesson
    end and its break/end-of-day share a timestamp intentionally. The last
    occupied lesson of the whole school determines end_of_day, even if a
    younger class or the morning shift finishes earlier. Coincident events of
    the same type are deduplicated across classes and shifts.
    """
    grids = {1: {item['period']: item for item in period_times(settings)}}
    if getattr(settings, 'shifts_enabled', False):
        grids[2] = {item['period']: item for item in period_times(settings, shift=2)}
    occupied: dict[int, set[tuple[int, int]]] = {}
    for lesson in lessons:
        weekday, period = lesson['weekday'], lesson['period']
        shift = lesson.get('shift', 1)
        if weekday not in range(5):
            raise ValueError('Timetable weekdays must be 0 (Monday) through 4 (Friday).')
        if shift not in grids:
            raise ValueError('Lesson shift is outside the enabled school shifts.')
        if period not in grids[shift]:
            raise ValueError(f'Lesson period {period} is outside the configured lesson grid.')
        occupied.setdefault(weekday, set()).add((shift, period))
    events: dict[tuple[int, str, str], dict[str, Any]] = {}
    long_break_after = getattr(settings, 'long_break_after', None) or 2
    for weekday, slots in sorted(occupied.items()):
        last_end = max(grids[shift][period]['end'] for shift, period in slots)
        for shift, period in sorted(slots):
            times = grids[shift][period]
            kind = 'end_of_day' if times['end'] == last_end else ('long_break_start' if period == long_break_after else 'break_start')
            for clock, event_type in ((times['start'], 'lesson_start'), (times['end'], 'lesson_end'), (times['end'], kind)):
                events.setdefault((weekday, clock, event_type), {
                    'weekday': weekday, 'time': clock, 'event_type': event_type, 'period': period,
                })
    return sorted(events.values(), key=lambda event: (event['weekday'], event['time'], -EVENT_PRIORITY[event['event_type']]))


def rebuild_bell_events(session: Session, settings: Settings) -> list[dict[str, Any]]:
    """Replace derived weekly events in the caller's transaction; never commit here."""
    lessons = session.execute(select(TimetableLesson, SchoolClass.shift).join(
        SchoolClass, SchoolClass.id == TimetableLesson.class_id,
    )).all()
    events = calculate_bell_events(
        ({'weekday': lesson.weekday, 'period': lesson.period, 'shift': shift} for lesson, shift in lessons), settings,
    )
    session.execute(delete(BellEvent))
    session.add_all(BellEvent(**event) for event in events)
    session.flush()
    return events
