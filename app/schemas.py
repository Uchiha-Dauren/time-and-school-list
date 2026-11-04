"""Validated API input models; period numbers are one-based, weekdays zero-based."""
from datetime import datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
PositiveId = Annotated[int, Field(gt=0)]

class InputModel(BaseModel):
    model_config = ConfigDict(extra='forbid')

class TeacherInput(InputModel):
    name: Name
    subject_ids: list[PositiveId] = Field(default_factory=list, max_length=100)

class SubjectInput(InputModel):
    name: Name
    is_hard: bool = False

class GradeInput(InputModel):
    number: int = Field(ge=1, le=11)
    parallel_count: int = Field(default=2, ge=0, le=26)
    max_lessons_per_day: int | None = Field(default=None, ge=1, le=12)
    language: Literal['kk', 'ru'] = 'kk'
    shift: int = Field(default=1, ge=1, le=2)

class GradeUpdate(InputModel):
    max_lessons_per_day: int = Field(ge=1, le=12)

class ClassInput(InputModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
    grade_id: PositiveId
    language: Literal['kk', 'ru'] = 'kk'
    shift: int = Field(default=1, ge=1, le=2)

class RoomInput(InputModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]

class CurriculumInput(InputModel):
    grade_id: PositiveId
    subject_id: PositiveId
    lessons_per_week: int = Field(ge=1, le=60)

class AssignmentInput(InputModel):
    class_id: PositiveId
    subject_id: PositiveId
    teacher_id: PositiveId
    room_id: PositiveId | None = None
    lessons_per_week: int | None = Field(default=None, ge=1, le=60)

class GenerateInput(InputModel):
    seed: int | None = Field(default=None, ge=0, le=2_147_483_647, strict=True)

class LessonMove(InputModel):
    weekday: int = Field(ge=0, le=4)
    period: int = Field(ge=1, le=12)

class PlaylistInput(InputModel):
    track_ids: list[PositiveId] = Field(default_factory=list, max_length=100)

class TestPlayInput(InputModel):
    track_id: PositiveId

class SettingsInput(InputModel):
    start_time: str = '08:30'
    shifts_enabled: bool = False
    second_shift_start_time: str = '14:00'
    long_break_after: int = Field(default=2, ge=1, le=11)
    max_lessons_per_day: int = Field(default=8, ge=1, le=12)
    timezone: str = 'Asia/Almaty'
    playback_duration: int = Field(default=15, ge=1, le=120)
    fade_out_seconds: int = Field(default=2, ge=0, le=30)
    use_rooms: bool = False
    scheduler_enabled: bool = False
    audio_enabled: bool = False
    solver_time_limit: int = Field(default=20, ge=1, le=300)

    @field_validator('start_time', 'second_shift_start_time')
    @classmethod
    def check_time(cls, value: str) -> str:
        try:
            parsed = datetime.strptime(value, '%H:%M')
        except ValueError as exc:
            raise ValueError('Use a 24-hour time such as 08:30.') from exc
        if parsed.strftime('%H:%M') != value:
            raise ValueError('Use exactly HH:MM.')
        return value

    @field_validator('timezone')
    @classmethod
    def check_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError('Use an IANA timezone such as Asia/Almaty.') from exc
        return value

    @model_validator(mode='after')
    def check_day(self):
        if self.fade_out_seconds > self.playback_duration:
            raise ValueError('Fade-out must not exceed total playback duration.')
        hour, minute = map(int, self.start_time.split(':'))
        n = self.max_lessons_per_day
        duration = n * 45 + sum(15 if p == self.long_break_after else 5 for p in range(1, n))
        end = hour * 60 + minute + duration
        if end >= 24 * 60:
            raise ValueError('The school day must finish before midnight.')
        if self.shifts_enabled:
            second_hour, second_minute = map(int, self.second_shift_start_time.split(':'))
            second_start = second_hour * 60 + second_minute
            if second_start < end:
                raise ValueError('The second shift must start after the first shift finishes. '
                                 'For 08:30 / 14:00, use at most 6 periods per shift.')
            if second_start + duration >= 24 * 60:
                raise ValueError('The second shift must finish before midnight.')
        return self
