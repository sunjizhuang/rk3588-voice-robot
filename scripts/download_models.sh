#!/usr/bin/env bash
set -e
MODELS_DIR="$(cd "$(dirname "$0")/.." && pwd)/models"
mkdir -p "$MODELS_DIR"
cd "$MODELS_DIR"

echo "==> Silero VAD"
[ -f silero_vad.onnx ] || wget -O silero_vad.onnx \
  https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx

echo "==> Paraformer ASR"
if [ ! -d sherpa-onnx-streaming-paraformer-bilingual-zh-en ]; then
  wget https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-streaming-paraformer-bilingual-zh-en.tar.bz2
  tar xvf sherpa-onnx-streaming-paraformer-bilingual-zh-en.tar.bz2
  rm sherpa-onnx-streaming-paraformer-bilingual-zh-en.tar.bz2
fi

echo "==> VITS TTS"
if [ ! -d sherpa-onnx-vits-zh-ll ]; then
  wget https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/sherpa-onnx-vits-zh-ll.tar.bz2
  tar xvf sherpa-onnx-vits-zh-ll.tar.bz2
  rm sherpa-onnx-vits-zh-ll.tar.bz2
fi

echo "==> KWS"
if [ ! -d sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01 ]; then
  wget https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01.tar.bz2
  tar xvf sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01.tar.bz2
  rm sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01.tar.bz2
fi

echo "==> 写入唤醒词"
cat > sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/keywords.txt <<'KWS'
l íng j ìng l íng j ìng @灵境灵境
KWS

echo "==> 完成。模型位于 $MODELS_DIR"

