"""Audio behavior is verified with a fake mixer; no speakers are required."""
from pathlib import Path
from threading import Event
from time import monotonic, sleep

import pytest

from app.services.audio import AudioPlayer


class FakeMusic:
    def __init__(self) -> None:
        self.busy = False
        self.loaded: list[str] = []
        self.volumes: list[float] = []
        self.stops = 0
        self.started = Event()

    def load(self, path: str) -> None:
        self.loaded.append(path)

    def play(self) -> None:
        self.busy = True
        self.started.set()

    def stop(self) -> None:
        self.busy = False
        self.stops += 1

    def get_busy(self) -> bool:
        return self.busy

    def set_volume(self, volume: float) -> None:
        self.volumes.append(volume)


class FakeMixer:
    def __init__(self) -> None:
        self.music = FakeMusic()
        self.ready = False

    def init(self) -> None:
        self.ready = True

    def get_init(self) -> bool:
        return self.ready

    def quit(self) -> None:
        self.ready = False


def wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = monotonic() + timeout
    while not predicate():
        if monotonic() >= deadline:
            pytest.fail('Audio worker did not reach the expected state.')
        sleep(0.005)


@pytest.fixture
def track(tmp_path: Path) -> Path:
    path = tmp_path / 'test.wav'
    path.write_bytes(b'fake decoder input')
    return path


def test_dry_run_never_loads_mixer() -> None:
    def forbidden():
        pytest.fail('Dry runs must not import or initialize pygame.')
    player = AudioPlayer(mixer_factory=forbidden)
    try:
        result = player.play('demo.wav', 0.05, 0.01, enabled=False)
        assert result['accepted'] and result['mode'] == 'dry-run'
        wait_until(lambda: not player.status()['busy'])
        assert player.status()['error'] is None
    finally:
        player.close()


def test_total_duration_includes_fade(track: Path) -> None:
    mixer = FakeMixer()
    player = AudioPlayer(mixer_factory=lambda: mixer)
    try:
        player.play(track, 0.15, 0.10, enabled=True)
        assert mixer.music.started.wait(1.0)
        wait_until(lambda: not player.status()['busy'])
        assert not mixer.music.busy
        assert mixer.music.volumes[0] == 1.0
        assert any(0 < value < 1 for value in mixer.music.volumes)
        assert mixer.music.volumes == sorted(mixer.music.volumes, reverse=True)
    finally:
        player.close()


def test_manual_stop_is_immediate_and_old_worker_cannot_stop_new_track(track: Path) -> None:
    mixer = FakeMixer()
    player = AudioPlayer(mixer_factory=lambda: mixer)
    try:
        player.play(track, 0.10, 0.04, enabled=True)
        assert mixer.music.started.wait(1.0)
        result = player.stop()
        assert result['stopped'] and not result['busy']
        assert not mixer.music.busy
        mixer.music.started.clear()
        player.play(track, 0.40, 0.05, enabled=True)
        assert mixer.music.started.wait(1.0)
        sleep(0.15)  # The previous playback's entire lifetime has elapsed.
        assert player.status()['busy'] and mixer.music.busy
    finally:
        player.close()
    assert not mixer.ready


def test_cancel_during_slow_mixer_load_does_not_start_stale_audio(track: Path) -> None:
    mixer = FakeMixer()
    loading, release = Event(), Event()
    def factory():
        loading.set()
        release.wait(1.0)
        return mixer
    player = AudioPlayer(mixer_factory=factory)
    try:
        player.play(track, 1, 0, enabled=True)
        assert loading.wait(1.0)
        player.stop()
        release.set()
        sleep(0.05)
        assert not mixer.music.loaded
        assert not player.status()['busy']
    finally:
        release.set()
        player.close()


def test_unavailable_audio_device_is_reported_without_crashing(track: Path) -> None:
    class BrokenMixer(FakeMixer):
        def init(self):
            raise RuntimeError('No output device')
    player = AudioPlayer(mixer_factory=BrokenMixer)
    try:
        assert player.play(track, 1, 0, enabled=True)['accepted']
        wait_until(lambda: not player.status()['busy'])
        assert 'No output device' in player.status()['error']
        assert 'dry-run' in player.status()['error']
    finally:
        player.close()


@pytest.mark.parametrize('duration,fade', [(0, 0), (-1, 0), (float('nan'), 0), (10, -1), (10, 11)])
def test_invalid_duration_is_rejected(duration: float, fade: float) -> None:
    player = AudioPlayer()
    with pytest.raises(ValueError):
        player.play('anything.wav', duration, fade)
    player.close()


def test_missing_file_and_closed_player_are_rejected(tmp_path: Path) -> None:
    player = AudioPlayer()
    with pytest.raises(FileNotFoundError):
        player.play(tmp_path / 'missing.mp3', 10, 2, enabled=True)
    player.close()
    player.close()
    with pytest.raises(RuntimeError):
        player.play('demo.wav', 10, 2)
