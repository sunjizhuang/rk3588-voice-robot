"""音频采集与重采样。"""
import numpy as np
import sounddevice as sd

from config import (
    DEVICE_RATE, CHANNELS, SAMPLES_PER_READ, SAMPLE_RATE,
    AUDIO_INPUT_DEVICE,
)


def resample_to_16k(chunk: np.ndarray, src_rate: int = DEVICE_RATE) -> np.ndarray:
    if src_rate == SAMPLE_RATE:
        return chunk.astype(np.float32)
    n_out = int(len(chunk) * SAMPLE_RATE / src_rate)
    if n_out <= 0:
        return np.zeros(0, dtype=np.float32)
    indices = np.linspace(0, len(chunk) - 1, n_out)
    return np.interp(indices, np.arange(len(chunk)), chunk).astype(np.float32)


def open_mic():
    """打开麦克风输入流。"""
    kwargs = dict(
        channels=CHANNELS,
        dtype="float32",
        samplerate=DEVICE_RATE,
        device=AUDIO_INPUT_DEVICE,
    )
    print(f"[Audio] 打开输入：device={AUDIO_INPUT_DEVICE} "
          f"sr={DEVICE_RATE} ch={CHANNELS}")
    return sd.InputStream(**kwargs)


def read_chunk(stream) -> np.ndarray:
    chunk, _ = stream.read(SAMPLES_PER_READ)
    chunk = chunk.reshape(-1)
    return resample_to_16k(chunk)

