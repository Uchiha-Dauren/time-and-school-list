"""One local music channel with cancellable, duration-limited playback.

Sound comes from the computer running Python, not from the web browser. Pygame
is imported lazily so administration and dry runs work on headless computers.
"""
from __future__ import annotations

import importlib
import logging
import math
import os
from pathlib import Path
from threading import Event, RLock, Thread
from time import monotonic
from typing import Any, Callable

logger = logging.getLogger(__name__)


class AudioPlayer:
    """Serialize one pygame channel and invalidate old workers on replacement.

    ``mixer_factory`` is an optional dependency injection point for hardware-free
    tests. A playback request is asynchronous: inspect ``status()['error']`` for
    mixer initialization or decoder failures after an accepted request.
    """

    def __init__(self, mixer_factory: Callable[[], Any] | None = None) -> None:
        self._lock = RLock()
        self._mixer_factory = mixer_factory or self._import_mixer
        self._mixer: Any | None = None
        self._generation = 0
        self._cancel = Event()
        self._threads: set[Thread] = set()
        self._closed = False
        self._state: dict[str, Any] = {
            'busy': False,
            'mode': 'idle',
            'track': None,
            'error': None,
            'duration': None,
            'fade_out_seconds': None,
        }

    @staticmethod
    def _import_mixer() -> Any:
        os.environ.setdefault('PYGAME_HIDE_SUPPORT_PROMPT', '1')
        return importlib.import_module('pygame').mixer

    def status(self) -> dict[str, Any]:
        """Return a JSON-safe snapshot, including asynchronous device errors."""
        with self._lock:
            return dict(self._state)

    def _stop_mixer_locked(self) -> None:
        if self._mixer is not None:
            try:
                if self._mixer.get_init():
                    self._mixer.music.stop()
            except Exception:
                logger.exception('Could not stop the local mixer')

    def _cancel_locked(self) -> None:
        self._cancel.set()
        self._generation += 1
        self._stop_mixer_locked()
        self._state['busy'] = False

    def play(
        self,
        path: Path | str,
        duration: float,
        fade_seconds: float,
        enabled: bool = False,
    ) -> dict[str, Any]:
        """Replace current playback; the fade is included in ``duration``.

        With ``enabled=False`` a timed dry run is logged without loading pygame.
        Invalid requests raise ``ValueError`` or ``FileNotFoundError`` before
        interrupting a currently playing track.
        """
        duration = float(duration)
        fade_seconds = float(fade_seconds)
        if not math.isfinite(duration) or not 0 < duration <= 3600:
            raise ValueError('Playback duration must be greater than 0 and at most 3600 seconds.')
        if not math.isfinite(fade_seconds) or not 0 <= fade_seconds <= duration:
            raise ValueError('Fade-out must be between 0 and the playback duration.')
        path = Path(path)
        if enabled and not path.is_file():
            raise FileNotFoundError(f'Music file does not exist: {path.name}')

        with self._lock:
            if self._closed:
                raise RuntimeError('The audio player is closed.')
            self._cancel_locked()
            self._cancel = Event()
            generation, cancel = self._generation, self._cancel
            self._state.update(
                busy=True,
                mode='audio' if enabled else 'dry-run',
                track=path.name,
                error=None,
                duration=duration,
                fade_out_seconds=fade_seconds,
            )
            worker = Thread(
                target=self._run,
                args=(path, duration, fade_seconds, enabled, generation, cancel),
                name=f'school-audio-{generation}',
                daemon=True,
            )
            self._threads.add(worker)
            worker.start()
            return {'accepted': True, **self._state}

    def _is_current_locked(self, generation: int, cancel: Event) -> bool:
        return generation == self._generation and not cancel.is_set() and not self._closed

    def _run(
        self,
        path: Path,
        duration: float,
        fade_seconds: float,
        enabled: bool,
        generation: int,
        cancel: Event,
    ) -> None:
        from threading import current_thread

        try:
            if enabled:
                # Import can be slow; do it outside the channel lock. The token
                # check afterwards prevents a cancelled import from playing.
                with self._lock:
                    mixer = self._mixer
                if mixer is None:
                    mixer = self._mixer_factory()
                with self._lock:
                    if not self._is_current_locked(generation, cancel):
                        return
                    self._mixer = mixer
                    if not mixer.get_init():
                        mixer.init()
                    mixer.music.load(str(path))
                    mixer.music.set_volume(1.0)
                    mixer.music.play()
            else:
                logger.info('DRY RUN: %s for %.2fs (fade %.2fs)', path.name, duration, fade_seconds)

            started = monotonic()
            while not cancel.is_set():
                remaining = duration - (monotonic() - started)
                if remaining <= 0:
                    break
                with self._lock:
                    if not self._is_current_locked(generation, cancel):
                        return
                    if enabled:
                        if not self._mixer.music.get_busy():
                            break  # A short track finishes naturally.
                        if fade_seconds and remaining < fade_seconds:
                            self._mixer.music.set_volume(max(0.0, remaining / fade_seconds))
                # Event.wait makes stop immediate, unlike a long sleep/fadeout.
                cancel.wait(min(0.025, remaining))
        except Exception as exc:
            with self._lock:
                if self._is_current_locked(generation, cancel):
                    self._state['error'] = (
                        f'Audio playback failed: {exc}. Check the server audio device '
                        'and track format, or disable audio for dry-run mode.'
                    )
                    logger.exception('Local music playback failed')
        finally:
            with self._lock:
                if generation == self._generation:
                    if enabled:
                        self._stop_mixer_locked()
                    self._state['busy'] = False
                self._threads.discard(current_thread())

    def stop(self) -> dict[str, Any]:
        """Stop the current track immediately and cancel its fade worker."""
        with self._lock:
            self._cancel_locked()
            return {'stopped': True, **self._state}

    def close(self) -> None:
        """Cancel workers and release the local audio device; safe to repeat."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._cancel_locked()
            workers = tuple(self._threads)
            if self._mixer is not None:
                try:
                    self._mixer.quit()
                except Exception:
                    logger.exception('Could not close the local mixer')
        for worker in workers:
            worker.join(timeout=1.0)
