"""sherpa-onnx 流式 Paraformer ASR。"""
import sherpa_onnx
import time

import logging
log = logging.getLogger("robot")

from config import ASR_ENCODER, ASR_DECODER, ASR_TOKENS, SAMPLE_RATE


class ASREngine:
    def __init__(self):
        self.recognizer = sherpa_onnx.OnlineRecognizer.from_paraformer(
            tokens=ASR_TOKENS,
            encoder=ASR_ENCODER,
            decoder=ASR_DECODER,
            num_threads=4,
            sample_rate=SAMPLE_RATE,
            feature_dim=80,
            enable_endpoint_detection=True,
            rule1_min_trailing_silence=2.4,
            rule2_min_trailing_silence=1.5,
            rule3_min_utterance_length=600,
        )

    def create_stream(self):
        return self.recognizer.create_stream()

    def decode(self, stream, samples):
        """
        整句解码。每次内部新建 stream，彻底避免上一句残留。
        stream 参数保留兼容，实际不使用。
        """
        t0 = time.time()
        new_stream = self.recognizer.create_stream()
        new_stream.accept_waveform(SAMPLE_RATE, samples)
        while self.recognizer.is_ready(new_stream):
            self.recognizer.decode_stream(new_stream)
        result = self.recognizer.get_result(new_stream)
        t1 = time.time()
        audio_len = len(samples) / 16000
        log.info("[TIMING-ASR] 解码=%.3fs, 音频=%.2fs, RTF=%.3f",
                 t1 - t0, audio_len,
                 (t1 - t0) / audio_len if audio_len > 0 else 0)

        if hasattr(result, "text"):
            return result.text
        return str(result) if result is not None else ""

    def decode_stream(self, stream, samples):
        """
        流式解码。用传入的 stream（流式模式需要持续累积）。
        """
        stream.accept_waveform(SAMPLE_RATE, samples)
        while self.recognizer.is_ready(stream):
            self.recognizer.decode_stream(stream)
        result = self.recognizer.get_result(stream)
        if hasattr(result, "text"):
            return result.text
        return str(result) if result is not None else ""

    def is_endpoint(self, stream) -> bool:
        return self.recognizer.is_endpoint(stream)

    def reset(self, stream):
        self.recognizer.reset(stream)

