import numpy as np

from reachy_openai_realtime.audio import (
    audio_level_dbfs,
    float32_to_pcm16,
    pcm16_to_float32,
    resample_linear,
    select_mono_float32,
    to_mono_float32,
)


def test_stereo_to_mono() -> None:
    stereo = np.array([[1.0, -1.0], [0.5, 0.5]], dtype=np.float32)
    np.testing.assert_allclose(to_mono_float32(stereo), [1.0, 0.5])


def test_stereo_phase_differences_do_not_cancel_voice() -> None:
    channel = np.array([0.1, -0.2, 0.3], dtype=np.float32)
    anti_phase = np.column_stack((channel, -channel))
    np.testing.assert_allclose(to_mono_float32(anti_phase), channel)


def test_louder_second_channel_is_selected() -> None:
    quiet = np.zeros(32, dtype=np.float32)
    voice = np.full(32, 0.1, dtype=np.float32)

    mono, selected, levels = select_mono_float32(np.column_stack((quiet, voice)))

    np.testing.assert_allclose(mono, voice)
    assert selected == 1
    np.testing.assert_allclose(levels, [-80.0, -20.0], atol=1e-5)


def test_audio_level_dbfs() -> None:
    assert audio_level_dbfs(np.zeros(32, dtype=np.float32)) == -80.0
    assert audio_level_dbfs(np.ones(32, dtype=np.float32)) == 0.0


def test_resample_16k_to_24k() -> None:
    source = np.zeros(320, dtype=np.float32)
    assert resample_linear(source, 16_000, 24_000).shape == (480,)


def test_pcm_round_trip() -> None:
    source = np.array([-1.0, -0.25, 0.0, 0.25, 1.0], dtype=np.float32)
    restored = pcm16_to_float32(float32_to_pcm16(source))
    np.testing.assert_allclose(restored, source, atol=4e-5)
