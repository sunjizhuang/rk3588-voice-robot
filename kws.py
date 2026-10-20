"""sherpa-onnx KWS 唤醒词：灵境灵境。"""
import sherpa_onnx

from config import (
    KWS_ENCODER, KWS_DECODER, KWS_JOINER, KWS_TOKENS, KEYWORDS_FILE,
    SAMPLE_RATE,
)


class KWSEngine:
    def __init__(self):
        self.kws = sherpa_onnx.KeywordSpotter(
            tokens=KWS_TOKENS,
            encoder=KWS_ENCODER,
            decoder=KWS_DECODER,
            joiner=KWS_JOINER,
            num_threads=2,
            keywords_file=KEYWORDS_FILE,
            keywords_score=1.0,
            keywords_threshold=0.05,
            num_trailing_blanks=1,
            provider="cpu",
        )

    def create_stream(self):
        return self.kws.create_stream()

    def accept(self, stream, samples):
        stream.accept_waveform(SAMPLE_RATE, samples)
        while self.kws.is_ready(stream):
            self.kws.decode_stream(stream)

    def get_result(self, stream) -> str:
        return self.kws.get_result(stream)

    def reset(self, stream):
        self.kws.reset_stream(stream)

