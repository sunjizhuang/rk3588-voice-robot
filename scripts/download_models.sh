#!/usr/bin/env bash
# 下载本项目实际使用的 sherpa-onnx 模型。
# - 断点续传：wget -c
# - 国内网络回退：GitHub 直连失败后自动尝试镜像加速站
set -e
MODELS_DIR="$(cd "$(dirname "$0")/.." && pwd)/models"
mkdir -p "$MODELS_DIR"
cd "$MODELS_DIR"

# GitHub 直连失败时的镜像前缀列表（按需增删，镜像可用性可能变化）
MIRRORS=(
  "https://ghfast.top/https://github.com"
  "https://gh-proxy.com/https://github.com"
)

command -v wget >/dev/null 2>&1 || { echo "错误：未找到 wget，请先安装（sudo apt install wget）"; exit 1; }

# fetch <github-release-url> <输出文件名>
# 依次尝试：直连 -> 镜像1 -> 镜像2；全部失败给出排查提示
fetch() {
  local url="$1" out="$2"
  local attempts=("$url")
  for m in "${MIRRORS[@]}"; do
    attempts+=("${m/https:\/\/github.com/$url}")
  done
  local i
  for i in "${!attempts[@]}"; do
    local try_url="${attempts[$i]}"
    if [ "$i" -gt 0 ]; then
      echo "    直连失败，尝试镜像：$try_url"
    fi
    if wget -c --timeout=30 --tries=2 -O "$out" "$try_url"; then
      [ -s "$out" ] && return 0
    fi
    echo "    下载失败：$try_url"
    rm -f "$out"
  done
  cat <<'HINT'

============================================================
下载失败排查提示（国内网络常见问题）：
  1. github.com 直连超时 -> 上面的镜像前缀可能已失效，
     更换 MIRRORS 里的加速站后重跑本脚本（wget -c 支持续传）
  2. 或手动下载后放到 models/ 下：
     - *.tar.bz2 解压：tar xvf xxx.tar.bz2
     - 单文件（如 silero_vad.onnx）直接放入 models/
  3. 需要的文件清单见本脚本各 "==> " 小节
============================================================
HINT
  exit 1
}

# fetch_and_extract <github-release-url> <目录名>
# 目录已存在则跳过（断点续传/重跑安全）
fetch_and_extract() {
  local url="$1" dir="$2" tarball="${2}.tar.bz2"
  if [ -d "$dir" ]; then
    echo "    已存在，跳过：$dir"
    return 0
  fi
  fetch "$url" "$tarball"
  tar xvf "$tarball"
  rm -f "$tarball"
}

echo "==> Silero VAD"
[ -s silero_vad.onnx ] || fetch \
  https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx \
  silero_vad.onnx

echo "==> Paraformer ASR（中英双语流式）"
fetch_and_extract \
  https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-streaming-paraformer-bilingual-zh-en.tar.bz2 \
  sherpa-onnx-streaming-paraformer-bilingual-zh-en

echo "==> VITS TTS（vits-melo-tts-zh_en，config.py 实际使用）"
fetch_and_extract \
  https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/vits-melo-tts-zh_en.tar.bz2 \
  vits-melo-tts-zh_en

echo "==> KWS 唤醒词模型"
fetch_and_extract \
  https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01.tar.bz2 \
  sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01

echo "==> 写入唤醒词（覆盖，重跑安全）"
cat > sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/keywords.txt <<'KWS'
l íng j ìng l íng j ìng @灵境灵境
KWS
echo "    注意：唤醒词内容在此维护；触发灵敏度（keywords_threshold 等）在 kws.py 中调整。"

echo "==> 完成校验"
MISSING=""
for f in \
  silero_vad.onnx \
  sherpa-onnx-streaming-paraformer-bilingual-zh-en/encoder.int8.onnx \
  sherpa-onnx-streaming-paraformer-bilingual-zh-en/decoder.int8.onnx \
  sherpa-onnx-streaming-paraformer-bilingual-zh-en/tokens.txt \
  vits-melo-tts-zh_en/model.onnx \
  vits-melo-tts-zh_en/tokens.txt \
  sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/encoder-epoch-12-avg-2-chunk-16-left-64.onnx \
  sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/decoder-epoch-12-avg-2-chunk-16-left-64.onnx \
  sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/joiner-epoch-12-avg-2-chunk-16-left-64.onnx \
  sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/tokens.txt \
  sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/keywords.txt; do
  [ -f "$f" ] || MISSING="$MISSING $f"
done
if [ -n "$MISSING" ]; then
  echo "警告：以下文件缺失：$MISSING"
  exit 1
fi

echo "==> 完成。模型位于 $MODELS_DIR"
