from __future__ import annotations

import numpy as np
from numpy.typing import NDArray


def to_mono_float32(samples: NDArray[np.generic]) -> NDArray[np.float32]:
    data = np.asarray(samples)
    if data.ndim == 2:
        # Reachy returns channels-last, while some stream wrappers use channels-first.
        if data.shape[0] <= 2 and data.shape[1] > data.shape[0]:
            data = data.T
        data = data.mean(axis=1)
    data = data.reshape(-1)
    if np.issubdtype(data.dtype, np.integer):
        scale = float(np.iinfo(data.dtype).max)
        data = data.astype(np.float32) / scale
    return np.clip(data.astype(np.float32), -1.0, 1.0)


def resample_linear(
    samples: NDArray[np.float32], source_rate: int, target_rate: int
) -> NDArray[np.float32]:
    if source_rate <= 0 or target_rate <= 0:
        raise ValueError("sample rates must be positive")
    if samples.size == 0 or source_rate == target_rate:
        return samples.astype(np.float32, copy=False)
    output_size = max(1, round(samples.size * target_rate / source_rate))
    source_positions = np.linspace(0.0, 1.0, samples.size, endpoint=False)
    target_positions = np.linspace(0.0, 1.0, output_size, endpoint=False)
    return np.interp(target_positions, source_positions, samples).astype(np.float32)


def float32_to_pcm16(samples: NDArray[np.float32]) -> NDArray[np.int16]:
    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * 32767.0).astype(np.int16)


def pcm16_to_float32(samples: NDArray[np.int16]) -> NDArray[np.float32]:
    return samples.astype(np.float32) / 32768.0

