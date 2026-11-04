"""Deterministic schedule dispatch tests use an injected clock and fake player."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import Base, BellEvent, BellPlayback, MusicTrack, PlaylistEntry, Settings
from app.scheduler.runner import BellScheduler


class FakePlayer:
    def __init__(self):
        self.calls = []

    def play(self, path, **kwargs):
        self.calls.append((path, kwargs))
        return {'accepted': True}


@pytest.fixture
def fixture_scheduler():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add(Settings(id=1, scheduler_enabled=True, timezone='Asia/Almaty', audio_enabled=False))
        session.add_all([
            MusicTrack(id=1, original_name='one.wav', stored_name='one.wav'),
            MusicTrack(id=2, original_name='two.wav', stored_name='two.wav'),
            MusicTrack(id=3, original_name='end.wav', stored_name='end.wav'),
        ])
        session.flush()
        session.add_all([
            BellEvent(weekday=0, time='09:15', event_type='lesson_end', period=1),
            BellEvent(weekday=0, time='09:15', event_type='break_start', period=1),
            PlaylistEntry(event_type='break_start', track_id=1, position=0),
            PlaylistEntry(event_type='break_start', track_id=2, position=1),
            PlaylistEntry(event_type='lesson_end', track_id=3, position=0),
        ])
    player = FakePlayer()
    scheduler = BellScheduler(player, factory)
    yield scheduler, player, factory
    scheduler.shutdown()
    engine.dispose()


def monday(week=0, second=0):
    return datetime(2026, 10, 19 + 7 * week, 9, 15, second, tzinfo=ZoneInfo('Asia/Almaty'))


def test_one_track_for_coincident_events_and_persistent_deduplication(fixture_scheduler):
    scheduler, player, factory = fixture_scheduler
    result = scheduler.tick(monday())
    assert result['event_type'] == 'break_start'
    assert result['track_id'] == 1
    assert result['status'] == 'dry_run'
    assert scheduler.tick(monday(second=1))['status'] == 'already_dispatched'
    restarted = BellScheduler(player, factory)
    assert restarted.tick(monday(second=2))['status'] == 'already_dispatched'
    assert len(player.calls) == 1
    with factory() as session:
        assert len(session.scalars(select(BellPlayback)).all()) == 1


def test_playlist_rotates_across_weeks(fixture_scheduler):
    scheduler, _, _ = fixture_scheduler
    assert scheduler.tick(monday())['track_id'] == 1
    assert scheduler.tick(monday(week=1))['track_id'] == 2


def test_weekend_and_late_bells_do_not_play(fixture_scheduler):
    scheduler, player, _ = fixture_scheduler
    assert scheduler.tick(datetime(2026, 10, 24, 9, 15, tzinfo=ZoneInfo('Asia/Almaty')))['status'] == 'weekend'
    assert scheduler.tick(monday(second=4))['status'] == 'outside_grace'
    assert player.calls == []


def test_timezone_converts_utc_clock(fixture_scheduler):
    scheduler, player, _ = fixture_scheduler
    utc = datetime(2026, 10, 19, 4, 15, tzinfo=ZoneInfo('UTC'))
    assert scheduler.tick(utc)['track_id'] == 1
    assert len(player.calls) == 1


def test_empty_higher_priority_playlist_falls_back(fixture_scheduler):
    scheduler, _, factory = fixture_scheduler
    with factory.begin() as session:
        session.add(BellEvent(weekday=0, time='09:15', event_type='end_of_day', period=1))
    assert scheduler.tick(monday())['event_type'] == 'break_start'


def test_disabled_scheduler_makes_no_claim(fixture_scheduler):
    scheduler, player, factory = fixture_scheduler
    with factory.begin() as session:
        session.get(Settings, 1).scheduler_enabled = False
    assert scheduler.tick(monday())['status'] == 'disabled'
    assert player.calls == []
    with factory() as session:
        assert session.scalars(select(BellPlayback)).all() == []


def test_no_playlist_is_claimed_without_playback(fixture_scheduler):
    scheduler, player, factory = fixture_scheduler
    with factory.begin() as session:
        session.add(BellEvent(weekday=0, time='08:30', event_type='lesson_start', period=1))
    now = datetime(2026, 10, 19, 8, 30, tzinfo=ZoneInfo('Asia/Almaty'))
    assert scheduler.tick(now)['status'] == 'no_playlist'
    assert scheduler.tick(now)['status'] == 'already_dispatched'
    assert player.calls == []


def test_naive_clock_rejected(fixture_scheduler):
    with pytest.raises(ValueError, match='timezone-aware'):
        fixture_scheduler[0].tick(datetime(2026, 10, 19, 9, 15))
