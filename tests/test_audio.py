import numpy as np

from reachy_japanese_realtime.audio import (
    float32_to_pcm16,
    pcm16_to_float32,
    resample_linear,
    to_mono_float32,
)


def test_stereo_to_mono() -> None:
    stereo = np.array([[1.0, -1.0], [0.5, 0.5]], dtype=np.float32)
    np.testing.assert_allclose(to_mono_float32(stereo), [0.0, 0.5])


def test_resample_16k_to_24k() -> None:
    source = np.zeros(320, dtype=np.float32)
    assert resample_linear(source, 16_000, 24_000).shape == (480,)


def test_pcm_round_trip() -> None:
    source = np.array([-1.0, -0.25, 0.0, 0.25, 1.0], dtype=np.float32)
    restored = pcm16_to_float32(float32_to_pcm16(source))
    np.testing.assert_allclose(restored, source, atol=4e-5)

