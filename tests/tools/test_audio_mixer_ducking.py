"""Regression tests for audio_mixer full_mix ducking filtergraph.

The ducking branch built an `acopy[speech_dup]` filter whose output pad was
never consumed, leaving the FFmpeg filtergraph with a dangling output. FFmpeg
rejects that, so `full_mix` with the most common shape — a single narration
track plus one music bed, with ducking enabled (the default) — always failed.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools.audio.audio_mixer import AudioMixer  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg required for full_mix"
)


def _sine(path: Path, freq: int, dur: int) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={dur}", str(path)],
        capture_output=True,
        check=True,
        timeout=30,
    )


def _has_audio(path: Path) -> bool:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    return "audio" in out.stdout


def test_full_mix_single_narration_plus_music_with_ducking(tmp_path):
    speech = tmp_path / "speech.wav"
    music = tmp_path / "music.wav"
    _sine(speech, 440, 2)
    _sine(music, 220, 3)
    out = tmp_path / "mixed.wav"

    result = AudioMixer().execute(
        {
            "operation": "full_mix",
            "tracks": [
                {"path": str(speech), "role": "speech"},
                {"path": str(music), "role": "music"},
            ],
            "ducking": {"enabled": True},
            "output_path": str(out),
        }
    )

    assert result.success is True, result.error
    assert out.exists() and _has_audio(out)


def test_full_mix_multi_narration_plus_music_with_ducking(tmp_path):
    s1, s2 = tmp_path / "s1.wav", tmp_path / "s2.wav"
    music = tmp_path / "music.wav"
    _sine(s1, 440, 2)
    _sine(s2, 330, 2)
    _sine(music, 220, 3)
    out = tmp_path / "mixed_multi.wav"

    result = AudioMixer().execute(
        {
            "operation": "full_mix",
            "tracks": [
                {"path": str(s1), "role": "speech"},
                {"path": str(s2), "role": "speech"},
                {"path": str(music), "role": "music"},
            ],
            "ducking": {"enabled": True},
            "output_path": str(out),
        }
    )

    assert result.success is True, result.error
    assert out.exists() and _has_audio(out)


def test_full_mix_disables_amix_normalization_for_scheduled_dialogue(tmp_path, monkeypatch):
    """Non-overlapping dialogue tracks must not be divided by the track count."""
    speech_paths = [tmp_path / f"speech_{index}.wav" for index in range(3)]
    music = tmp_path / "music.wav"
    for path in [*speech_paths, music]:
        path.write_bytes(b"stub")

    captured = []

    def fake_run(self, cmd, **kwargs):
        captured.append(list(cmd))

        class _R:
            stdout = "10.0\n"
            stderr = ""

        return _R()

    monkeypatch.setattr(AudioMixer, "run_command", fake_run)

    tracks = [
        {"path": str(path), "role": "speech", "start_seconds": index * 2}
        for index, path in enumerate(speech_paths)
    ]
    tracks.append({"path": str(music), "role": "music", "volume": 0.1})

    result = AudioMixer().execute(
        {
            "operation": "full_mix",
            "tracks": tracks,
            "ducking": {"enabled": True},
            "normalize": False,
            "output_path": str(tmp_path / "out.wav"),
        }
    )

    assert result.success is True, result.error
    ffmpeg_cmd = next(command for command in captured if command[0] == "ffmpeg")
    filtergraph = ffmpeg_cmd[ffmpeg_cmd.index("-filter_complex") + 1]
    assert "amix=inputs=3:duration=longest:normalize=0[speech_all]" in filtergraph
    assert "amix=inputs=2:duration=longest:normalize=0[premix]" in filtergraph
