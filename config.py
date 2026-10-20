## config.py
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / 'robot.env')

import os
import subprocess
import re

def _find_usb_mic():
    """自动扫描 USB 麦克风，返回 sounddevice 索引或 None。"""
    import sounddevice as sd
    devices = sd.query_devices()
    # 优先选择名称含 "USB" 且输入通道 > 0 的设备
    for i, d in enumerate(devices):
        if d.get("max_input_channels", 0) > 0:
            name = d.get("name", "").lower()
            if "usb" in name or "nationalchip" in name:
                return i
    # 没有 USB 设备，回退到第一个有输入通道的设备
    for i, d in enumerate(devices):
        if d.get("max_input_channels", 0) > 0:
            return i
    return None


BASE_DIR = Path(__file__).resolve().parent
MODELS_DIR = BASE_DIR / "models"

# ---------- 音频 ----------
AUDIO_INPUT_DEVICE = _find_usb_mic()        #  USB Audio
#AUDIO_OUTPUT_DEVICE = None                  # 走 PulseAudio 默认 sink

SAMPLE_RATE = 16000     # sherpa-onnx VAD/KWS/ASR 模型要求的输入采样率
DEVICE_RATE = 16000     # 采集采样率，必须匹配设备原生
CHANNELS = 1            # 取几路 mic
SAMPLES_PER_READ = int(0.1 * DEVICE_RATE)   # 每次读 100ms
VAD_WINDOW_SIZE = 512                       # VAD 模型固定输入长度

# ---------- ASR 显示模式 ----------
# "sentence" = 整句显示（VAD 切段后一次性输出，默认）
# "streaming" = 流式显示（说话过程中逐步刷新）
ASR_DISPLAY_MODE = "sentence"

# ---------- 模型路径 ----------
VAD_MODEL = str(MODELS_DIR / "silero_vad.onnx")

ASR_MODEL_DIR = MODELS_DIR / "sherpa-onnx-streaming-paraformer-bilingual-zh-en"
ASR_ENCODER = str(ASR_MODEL_DIR / "encoder.int8.onnx")
ASR_DECODER = str(ASR_MODEL_DIR / "decoder.int8.onnx")
ASR_TOKENS = str(ASR_MODEL_DIR / "tokens.txt")

TTS_MODEL_DIR = str(MODELS_DIR / "vits-melo-tts-zh_en")
TTS_DEFAULT_SID = 0
TTS_DEFAULT_SPEED = 1.0

KWS_MODEL_DIR = MODELS_DIR / "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
KWS_ENCODER = str(KWS_MODEL_DIR / "encoder-epoch-12-avg-2-chunk-16-left-64.onnx")
KWS_DECODER = str(KWS_MODEL_DIR / "decoder-epoch-12-avg-2-chunk-16-left-64.onnx")
KWS_JOINER = str(KWS_MODEL_DIR / "joiner-epoch-12-avg-2-chunk-16-left-64.onnx")
KWS_TOKENS = str(KWS_MODEL_DIR / "tokens.txt")
KEYWORDS_FILE = str(KWS_MODEL_DIR / "keywords.txt")

# ---------- 动作 ----------
ACTION_CONFIG = str(BASE_DIR / "action_config.json")
SEND_MQTT_SCRIPT = str(BASE_DIR / "send_mqtt.sh")
MQTT_DEVICE = "lite3"

# ---------- ai_chat_cli ----------
CHAT_CLI = str(BASE_DIR / "ai_chat_cli.py")
CHAT_TIMEOUT = 120
CHAT_TTS_MODE_DEFAULT = os.environ.get("CHAT_TTS_MODE", "voxcpm")

# ---------- HTTP ----------
TEXT_INPUT_API_KEY = os.environ.get("TEXT_INPUT_API_KEY", "")
TEXT_INPUT_PORT = 8765

# ---------- 翻译服务（HY-MT2 / llama.cpp） ----------
TRANSLATE_URL = "http://192.168.199.7:8788/v1/completions"
TRANSLATE_MODEL = "hy-mt2-1.8b"
TRANSLATE_TARGET_LANG = "English"
TRANSLATE_CHUNK_SIZE = 30

