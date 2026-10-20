"""Silero VAD 封装，匹配当前 sherpa-onnx API。"""
import numpy as np
import sherpa_onnx
import time
import logging
log = logging.getLogger("robot")

from config import SAMPLE_RATE, VAD_WINDOW_SIZE


class SileroVAD:
    def __init__(self, model_path: str):
        self._model_path = model_path
        self._build()
        self._build()
        # 时间戳记录
        self._speech_start_time = None      # 首次检测到语音累积
        self._speech_end_time = None        # 段结束
        self._last_seg_len = 0        

    def _build(self):
        config = sherpa_onnx.VadModelConfig()
        config.silero_vad.model = self._model_path
        config.silero_vad.threshold = 0.3               ## 0.2 → 0.5，防底噪误触发
        config.silero_vad.min_silence_duration = 0.7  ## 0.2 → 0.5，防底噪误触发
        config.silero_vad.min_speech_duration = 0.15    #防噪声误触发：
        config.silero_vad.max_speech_duration = 30.0
        config.sample_rate = SAMPLE_RATE
        config.num_threads = 1
        self._vad = sherpa_onnx.VoiceActivityDetector(
            config, buffer_size_in_seconds=120.0)
        self._buffer = np.zeros(0, dtype=np.float32)
        # ... 原有逻辑 ...
        self._speech_start_time = None
        self._speech_end_time = None
        self._last_seg_len = 0        

    def reset(self):
        self._build()

    def feed(self, pcm16k: np.ndarray):
        x = np.asarray(pcm16k, dtype=np.float32).reshape(-1)
        if self._buffer.shape[0] > 0:
            x = np.concatenate([self._buffer, x])
        n_frames = len(x) // VAD_WINDOW_SIZE
        for i in range(n_frames):
            frame = x[i * VAD_WINDOW_SIZE:(i + 1) * VAD_WINDOW_SIZE]
            self._vad.accept_waveform(frame)
        remaining = len(x) % VAD_WINDOW_SIZE
        self._buffer = x[-remaining:] if remaining > 0 else np.zeros(0, dtype=np.float32)
        # 记录首次语音累积
        try:
            seg = self._vad.current_segment
            seg_len = len(seg.samples) if seg else 0
        except Exception:
            seg_len = 0

        if seg_len > 0 and self._speech_start_time is None:
            self._speech_start_time = time.time()
            log.info("[VAD-TIMING] 语音开始累积 t=%.3f, seg_len=%d", self._speech_start_time, seg_len)

        self._last_seg_len = seg_len

    def flush(self):
        """
        把 buffer 里不足一帧的剩余数据也喂进去，并 flush VAD。
        在 has_speech() 为 True 后调用，确保尾音不丢。
        """
        if self._buffer.shape[0] > 0:
            self._vad.accept_waveform(self._buffer)
            self._buffer = np.zeros(0, dtype=np.float32)
        self._vad.flush()

    def has_speech(self) -> bool:
        """是否已有完成的语音段。empty 是方法，必须调用。"""
        return not self._vad.empty()

    def pop_speech(self):
        """
        取最早的语音段。
        返回 (samples, start_sec, duration_sec)，无则 None。
        SpeechSegment 只有 samples 和 start，duration 用 samples 长度算。
        """
        if self._vad.empty():
            return None
        seg = self._vad.front
        samples = np.asarray(seg.samples, dtype=np.float32)
        start_sec = float(seg.start)
        duration_sec = len(samples) / SAMPLE_RATE
        self._speech_end_time = time.time()
        return samples, start_sec, duration_sec

    def pop(self):
        self._vad.pop()

    @property
    def speech_start_time(self):
        return self._speech_start_time

    @property
    def speech_end_time(self):
        return self._speech_end_time
