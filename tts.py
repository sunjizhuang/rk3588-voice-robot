"""sherpa-onnx 本地 TTS，中英双语（vits-melo-tts-zh_en）。
只负责合成，播放交给主进程的 AudioPlayer。
"""
import re
import threading
from pathlib import Path

import sherpa_onnx

from config import TTS_DEFAULT_SID, TTS_DEFAULT_SPEED


def _normalize_text(text: str) -> str:
    """把 TTS 不支持的数字转成中文。"""
    if not text:
        return text
    digit_map = str.maketrans("0123456789", "零一二三四五六七八九")
    return re.sub(r'\d+', lambda m: m.group(0).translate(digit_map), text)


class TTSEngine:
    def __init__(self, model_dir: str, default_sid: int = TTS_DEFAULT_SID,
                 default_speed: float = TTS_DEFAULT_SPEED):
        model_dir = Path(model_dir)
        config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                    model=str(model_dir / "model.onnx"),
                    lexicon=str(model_dir / "lexicon.txt")
                        if (model_dir / "lexicon.txt").exists() else "",
                    tokens=str(model_dir / "tokens.txt"),
                    data_dir=str(model_dir / "espeak-ng-data")
                        if (model_dir / "espeak-ng-data").exists() else "",
                ),
                num_threads=2,
                provider="cpu",
            ),
            max_num_sentences=1,
        )
        if not config.validate():
            raise ValueError("TTS 配置验证失败")
        self._tts = sherpa_onnx.OfflineTts(config)
        self._lock = threading.Lock()
        self._default_sid = default_sid
        self._default_speed = default_speed

    @property
    def num_speakers(self) -> int:
        return self._tts.num_speakers

    def synthesize(self, text: str, sid: int = None, speed: float = None):
        """生成语音，返回 (samples, sample_rate)。"""
        text = _normalize_text(text)
        sid = self._default_sid if sid is None else sid
        speed = self._default_speed if speed is None else speed
        if self.num_speakers > 0:
            sid = max(0, min(sid, self.num_speakers - 1))
        with self._lock:
            audio = self._tts.generate(text, sid=sid, speed=speed)
        return audio.samples, audio.sample_rate

    def _split_sentences(self, text: str):
        """按中文标点切句，供 speak_streaming_async 用。"""
        text = _normalize_text(text)
        sentences = re.split(r'([。！？；\n])', text)
        merged = []
        for i in range(0, len(sentences) - 1, 2):
            merged.append(sentences[i] + sentences[i + 1])
        if len(sentences) % 2 == 1:
            merged.append(sentences[-1])
        return [s.strip() for s in merged if s.strip()]

    def save_wav(self, text: str, path: str, sid: int = None, speed: float = None):
        """生成并保存为 wav 文件（调试用）。"""
        samples, sr = self.synthesize(text, sid=sid, speed=speed)
        sherpa_onnx.write_wave(path, samples, sr)

