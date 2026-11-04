"""Validated audio uploads, ordered playlists, player controls and bell schedules."""
from pathlib import Path
from typing import Annotated
from uuid import uuid4
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from mutagen.mp3 import MP3
from mutagen.wave import WAVE
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from app.config import MAX_UPLOAD_BYTES, UPLOAD_DIR
from app.database import get_db, get_settings, school_lock
from app.models import BellEvent, MusicTrack, PlaylistCursor, PlaylistEntry
from app.routers.school import require, row_dict
from app.schemas import PlaylistInput, TestPlayInput
from app.services.bells import EVENT_PRIORITY, EVENT_TYPES, period_times

router = APIRouter(prefix='/api', tags=['Music & bells'])
DB = Annotated[Session, Depends(get_db)]


@router.get('/bells')
def bells(session: DB):
    settings = get_settings(session)
    shift_periods = {'1': period_times(settings)}
    if settings.shifts_enabled:
        shift_periods['2'] = period_times(settings, shift=2)
    return {'events': [row_dict(row) for row in session.scalars(select(BellEvent).order_by(BellEvent.weekday, BellEvent.time, BellEvent.id))],
            'periods': shift_periods['1'], 'shift_periods': shift_periods,
            'priority': sorted(EVENT_PRIORITY, key=lambda name: -EVENT_PRIORITY[name])}


@router.get('/music')
def music(session: DB):
    playlists = {kind: [] for kind in EVENT_TYPES}
    for entry in session.scalars(select(PlaylistEntry).order_by(PlaylistEntry.position)):
        playlists[entry.event_type].append(entry.track_id)
    return {'tracks': [{'id': row.id, 'original_name': row.original_name} for row in session.scalars(select(MusicTrack).order_by(MusicTrack.id))],
            'playlists': playlists, 'event_types': list(EVENT_TYPES)}


@router.post('/music/upload', status_code=201)
def upload_music(session: DB, file: UploadFile = File(...)):
    """Store UUID filenames, bound size, and inspect the audio container itself."""
    filename = Path((file.filename or '').replace('\\', '/')).name
    suffix = Path(filename).suffix.lower()
    if suffix not in {'.mp3', '.wav'}:
        raise HTTPException(422, 'Only MP3 and WAV audio files are accepted.')
    destination = UPLOAD_DIR / f'{uuid4().hex}{suffix}'
    try:
        count = 0
        with destination.open('xb') as output:
            while chunk := file.file.read(1024 * 1024):
                count += len(chunk)
                if count > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, 'Maximum music file size is 25 MiB.')
                output.write(chunk)
        try:
            audio = MP3(destination) if suffix == '.mp3' else WAVE(destination)
            if audio.info.length <= 0:
                raise ValueError('empty audio')
        except Exception as exc:
            raise HTTPException(422, 'The file is not a readable, non-empty MP3/WAV audio track.') from exc
        with school_lock:
            track = MusicTrack(original_name=filename[:255], stored_name=destination.name)
            session.add(track)
            session.commit()
            return {'id': track.id, 'original_name': track.original_name, 'message': 'Music uploaded.'}
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        file.file.close()


@router.put('/music/playlists/{event_type}')
def set_playlist(event_type: str, data: PlaylistInput, session: DB):
    if event_type not in EVENT_TYPES:
        raise HTTPException(422, 'Unknown bell event type.')
    with school_lock:
        for track_id in data.track_ids:
            require(session, MusicTrack, track_id)
        session.execute(delete(PlaylistEntry).where(PlaylistEntry.event_type == event_type))
        session.add_all(PlaylistEntry(event_type=event_type, track_id=track_id, position=position)
                        for position, track_id in enumerate(data.track_ids))
        cursor = session.get(PlaylistCursor, event_type)
        if cursor:
            cursor.next_index = 0
        session.commit()
    return {'message': 'Playlist saved in this order; rotation reset.'}


@router.post('/music/test')
def test_music(data: TestPlayInput, request: Request, session: DB):
    track = require(session, MusicTrack, data.track_id)
    settings = get_settings(session)
    try:
        return request.app.state.player.play(UPLOAD_DIR / track.stored_name, duration=settings.playback_duration,
                                             fade_seconds=settings.fade_out_seconds, enabled=settings.audio_enabled)
    except FileNotFoundError as exc:
        raise HTTPException(404, 'The uploaded audio file is missing from disk. Upload it again.') from exc


@router.post('/music/stop')
def stop_music(request: Request):
    request.app.state.player.stop()
    return {'message': 'Playback stopped. Future scheduled bells remain enabled.'}


@router.get('/music/status')
def player_status(request: Request, session: DB):
    state = request.app.state.player.status()
    original_name = session.scalar(select(MusicTrack.original_name).where(MusicTrack.stored_name == state.get('track')))
    return state | {'original_name': original_name, 'scheduler': request.app.state.bell_scheduler.status()}
