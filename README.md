# RK3588 语音实时信息查询 + 动作控制机器人

基于 RK3588 + Ubuntu 20.04，实现本地唤醒、语音识别、动作控制、LLM 对话与双 TTS 播放的完整链路。

---

## 目录

1. [项目概览](#1-项目概览)
2. [核心特性](#2-核心特性)
3. [系统架构](#3-系统架构)
4. [目录结构](#4-目录结构)
5. [安装与依赖](#5-安装与依赖)
6. [配置文件详解](#6-配置文件详解)
7. [快速开始](#7-快速开始)
8. [主循环工作流程](#8-主循环工作流程)
9. [WAV 播放器子进程](#9-wav-播放器子进程)
10. [TTS 引擎与降级策略](#10-tts-引擎与降级策略)
11. [动作分流与 MQTT](#11-动作分流与-mqtt)
12. [HTTP 文本输入接口](#12-http-文本输入接口)
13. [常见问题 FAQ](#13-常见问题-faq)
14. [故障排查](#14-故障排查)
15. [附录：环境变量与默认值](#15-附录环境变量与默认值)

---

## 1. 项目概览

本项目运行在 RK3588 开发板（Ubuntu 20.04 aarch64）上，把以下能力整合到一个进程里：

| 模块 | 引擎 | 说明 |
|------|------|------|
| 🎤 唤醒词 | sherpa-onnx KWS | 唤醒词「灵境灵境」 |
| 🔇 VAD | Silero VAD | 语音活动检测 |
| 🗣️ ASR | sherpa-onnx Paraformer | 中英双语流式识别 |
| 🧠 LLM | 智谱 GLM / DeepSeek | 通过 `ai_chat_cli.py` 调用 |
| 🔊 本地 TTS | sherpa-onnx VITS | 固定话术、降级播报 |
| 🎙️ 云端 TTS | VoxCPM2（Gradio server） | DeepSeek 结果的语音合成 |
| 🤖 动作控制 | MQTT | 通过 `send_mqtt.sh` 下发 |
| 📂 WAV 播放器 | 常驻 aplay + 目录监控 | 播放 VoxCPM 输出的 wav |

---

## 2. 核心特性

- **本地全链路**：唤醒、VAD、ASR 都在板端运行，不依赖外网。
- **双 TTS 引擎**：固定话术走本地 sherpa，复杂回答走云端 VoxCPM；VoxCPM 失败时自动降级到 sherpa。
- **动作优先分流**：ASR 文本先做最长包含匹配，命中动作词直接下发 MQTT，未命中才走 LLM。
- **播报期间暂停拾音**：`tts_speaking` 标志置位时，主循环跳过 VAD/KWS，避免"自问自答"。
- **WAV 播放器子进程托管**：随主程序启动、随主程序退出；配置禁用时可一键关闭。
- **HTTP 文本输入接口**：带 API Key 校验，支持外部系统远程播报。
- **开机自启**：通过 systemd 管理，断电恢复后自动运行。

---

## 3. 系统架构

```
┌──────────────────────────────────────────────────────────────┐
│                        main.py（主进程）                       │
│                                                              │
│  ┌─────────┐  ┌───────┐  ┌───────┐  ┌─────────────┐           │
│  │  Mic    │─▶│  VAD  │─▶│  ASR  │─▶│ActionRouter │           │
│  └─────────┘  └───────┘  └───────┘  └──────┬──────┘           │
│       ▲              ▲                     │                  │
│       │              │                     │                  │
│       │         ┌────┴────┐                │                  │
│       │         │   KWS   │                │                  │
│       │         └─────────┘                │                  │
│       │                                    │                  │
│       │          ┌───────────────────┐     │                  │
│       │          │  Action（命中动作）│◀────┤                  │
│       │          └────────┬──────────┘     │                  │
│       │                   │                │                  │
│       │                   ▼                ▼                  │
│       │            ┌──────────┐     ┌─────────────┐           │
│       │            │  MQTT    │     │ Chat（LLM）  │           │
│       │            └──────────┘     └──────┬──────┘           │
│       │                                    │                  │
│       │                     ┌──────────────┼──────────────┐   │
│       │                     ▼              ▼              ▼   │
│       │              ┌────────────┐ ┌──────────┐  ┌─────────┐ │
│       │              │ Sherpa TTS │ │ VoxCPM   │  │  HTTP   │ │
│       │              │ （本地）    │ │（Gradio）│  │ Server  │ │
│       │              └──────┬─────┘ └─────┬────┘  └─────────┘ │
│       │                     │             │                  │
│       │                     ▼             ▼                  │
│       │                 ┌───────┐   ┌──────────┐             │
│       │                 │ 扬声器 │   │ outputs/ │             │
│       │                 └───────┘   │  *.wav   │             │
│       │                             └─────┬────┘             │
│       │                                   │                  │
│  ┌────┴───────────────────────────────────┼────────────────┐ │
│  │         tts_speaking（全局 Event）      │                │ │
│  └────────────────────────────────────────┼────────────────┘ │
│                                           ▼                  │
│                              ┌────────────────────────┐      │
│                              │ wav_player.py（子进程） │      │
│                              │  监控目录 + 常驻 aplay  │      │
│                              └────────────┬───────────┘      │
│                                           │                  │
│                                           ▼                  │
│                                       ┌───────┐              │
│                                       │ 扬声器 │              │
│                                       └───────┘              │
└──────────────────────────────────────────────────────────────┘
```

**关键点：**

- **两个播放源**：本地 sherpa（`speak_async`）和 wav_player（播放 VoxCPM 输出）。
- **`tts_speaking` 由 sherpa 分支维护**；VoxCPM 分支只做短占位（0.3s），后续靠 VAD 门限过滤回灌。
- **wav_player 是子进程**，主进程不感知它的播放状态（选定策略 ④）。

---

## 4. 目录结构

```
/opt/rk3588-voice-robot/
├── main.py                    # 主入口
├── config.py                  # 全局配置（路径、设备索引）
├── robot.env                  # 环境变量（API Key 等）
├── requirements.txt           # Python 依赖
│
├── audio.py                   # 麦克风采集与重采样
├── vad.py                     # Silero VAD 封装
├── asr.py                     # sherpa-onnx Paraformer ASR
├── kws.py                     # sherpa-onnx KWS 唤醒词
├── tts.py                     # sherpa-onnx VITS 本地 TTS
│
├── action_router.py           # 动作分流 + MQTT + LLM 调用
├── action_config.json         # 动作词 → word_id 映射
├── action_mapping.py          # word_id → action_str 映射
├── send_mqtt.sh               # MQTT 下发脚本
│
├── ai_chat_cli.py             # LLM 对话主程序（集成 VoxCPM TTS）
├── ai_voxcpm_cli.py           # VoxCPM 客户端 + 动态 TTS 组件
├── tts_config.json            # TTS 配置（多 profile）
│
├── http_server.py             # HTTP 文本输入接口
├── wav_player.py              # WAV 目录监控播放器（子进程）
│
├── models/                    # 模型文件
│   ├── silero_vad.onnx
│   ├── sherpa-onnx-streaming-paraformer-bilingual-zh-en/
│   ├── sherpa-onnx-vits-zh-ll/
│   └── sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/
│
├── voices/                    # 客户端参考音频（clone / ultimate 用）
├── outputs/                   # VoxCPM 输出目录（每次启动清空）
└── README.md                  # 本文档
```

---

## 5. 安装与依赖

### 5.1 系统要求

- RK3588 开发板
- Ubuntu 20.04（aarch64）
- USB 麦克风 + 扬声器
- 可访问互联网（LLM API、VoxCPM server）
- 可访问 MQTT 服务器

### 5.2 创建虚拟环境

```bash
cd /opt/rk3588-voice-robot
python3 -m venv venv
source venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

若 `pip install sherpa-onnx` 慢，换清华源：

```bash
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

验证：

```bash
python -c "import sherpa_onnx, sounddevice, numpy; print('ok')"
```

### 5.3 下载模型

```bash
bash scripts/download_models.sh
```

或将模型手动放置到 `models/` 对应目录。

### 5.4 测试麦克风与扬声器

```bash
# 录音 3 秒
arecord -d 3 -f cd test.wav

# 播放
aplay test.wav

# 测试输出设备
python << 'EOF'
import sounddevice as sd
import numpy as np

sr = 48000
t = np.linspace(0, 1, sr, endpoint=False)
tone = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)

for dev in [0, 2, 7, 10, 11]:
    try:
        info = sd.query_devices(dev)
        if info['max_output_channels'] == 0:
            continue
        print(f"\n播放到 device {dev}: {info['name']}")
        sd.play(tone, sr, device=dev)
        sd.wait()
        print("  ✅ 完成")
    except Exception as e:
        print(f"  ❌ {e}")
EOF
```

能听到声音的索引填到 `config.py` 的 `AUDIO_OUTPUT_DEVICE`。

### 5.5 环境变量

复制 `robot.env.example` 为 `robot.env`：

```bash
cp robot.env.example robot.env
```

编辑内容：

```bash
# HTTP 文本输入接口的 API Key
TEXT_INPUT_API_KEY=change-me-to-a-strong-secret

# DeepSeek 结果的 TTS 引擎：voxcpm 或 sherpa
CHAT_TTS_MODE=voxcpm

# ai_chat_cli.py 需要的 API Key
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxx
ZHIPUAI_API_KEY=xxxxxxxxxxxxxxxx
```

---

## 6. 配置文件详解

### 6.1 `config.py` 关键字段

| 字段 | 说明 |
|------|------|
| `AUDIO_INPUT_DEVICE` | 麦克风设备索引（`sounddevice` 里的编号） |
| `AUDIO_OUTPUT_DEVICE` | 扬声器设备索引 |
| `SAMPLE_RATE` | 音频采样率，固定 16000 |
| `DEVICE_RATE` | 采集采样率，通常等于 `SAMPLE_RATE` |
| `CHANNELS` | 麦克风声道数，取 1 |
| `SAMPLES_PER_READ` | 每次读取的样本数，100ms = 1600 点 |
| `VAD_WINDOW_SIZE` | VAD 模型输入长度，512 点 |
| `VAD_MODEL` | Silero VAD 模型路径 |
| `ASR_MODEL_DIR` | Paraformer 模型目录 |
| `TTS_MODEL_DIR` | VITS 模型目录 |
| `KWS_MODEL_DIR` | KWS 模型目录 |
| `ACTION_CONFIG` | 动作词配置文件 |
| `SEND_MQTT_SCRIPT` | MQTT 下发脚本路径 |
| `CHAT_CLI` | `ai_chat_cli.py` 路径 |
| `CHAT_TTS_MODE_DEFAULT` | 默认 TTS 模式（从环境变量读） |
| `TEXT_INPUT_API_KEY` | HTTP 接口 API Key |
| `TEXT_INPUT_PORT` | HTTP 接口端口，默认 8765 |

### 6.2 `tts_config.json` 完整示例

```json
{
  "enabled": true,
  "server": "http://192.168.199.7:8808",
  "api_name": "/generate",
  "backend": "auto",
  "out_dir": "/opt/rk3588-voice-robot/outputs",
  "wav_player_enabled": true,
  "auto_play": true,
  "gapless": true,
  "stream_workers": 4,
  "seed": null,

  "split": {
    "comma_threshold": 20,
    "min_chars": 4
  },

  "active_profile": "female_lib",

  "profiles": {
    "default": {
      "mode": "design",
      "control_instruction": "温暖自然的女声，语速适中",
      "reference_wav": null,
      "transcript": "",
      "cfg_value": 2.0,
      "dit_steps": 10,
      "do_normalize": false,
      "denoise": false
    },
    "robot": {
      "mode": "clone",
      "control_instruction": "",
      "reference_wav": "./voices/robot.wav",
      "transcript": "",
      "cfg_value": 2.0,
      "dit_steps": 10,
      "do_normalize": false,
      "denoise": false
    },
    "narrator": {
      "mode": "ultimate",
      "control_instruction": "",
      "reference_wav": "./voices/narrator.wav",
      "transcript": "这是一段用于极致克隆的参考音频的逐字稿……",
      "cfg_value": 2.0,
      "dit_steps": 10,
      "do_normalize": false,
      "denoise": false
    },
    "female_lib": {
      "mode": "library",
      "voice_id": "female",
      "control_instruction": "",
      "reference_wav": null,
      "transcript": "",
      "cfg_value": 2.0,
      "dit_steps": 10,
      "do_normalize": false,
      "denoise": false
    },
    "eric_lib": {
      "mode": "library",
      "voice_id": "eric",
      "control_instruction": "",
      "reference_wav": null,
      "transcript": "",
      "cfg_value": 2.0,
      "dit_steps": 10,
      "do_normalize": false,
      "denoise": false
    }
  }
}
```

**顶层字段：**

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `enabled` | bool | true | 是否启用 TTS |
| `server` | str | `http://127.0.0.1:8808` | VoxCPM Gradio server 地址 |
| `api_name` | str | `/generate` | 接口名 |
| `backend` | str | auto | auto / gradio / http |
| `out_dir` | str | `./outputs` | VoxCPM 输出目录；建议绝对路径 |
| **`wav_player_enabled`** | bool | true | **是否随主程序启动 wav_player 子进程** |
| `auto_play` | bool | true | 是否自动播放 |
| `gapless` | bool | true | 段间拼接 |
| `stream_workers` | int | 4 | 并发生成线程数 |
| `seed` | int / null | null | null 表示随机 |
| `split` | object | — | 切分参数 |
| `active_profile` | str | default | 默认 profile |
| `profiles` | object | — | 多套音色 |

**`profiles.<name>` 字段：**

| 字段 | 说明 |
|------|------|
| `mode` | `design` / `clone` / `ultimate` / `library` |
| `control_instruction` | 风格描述 |
| `reference_wav` | 参考音频路径（clone / ultimate 用） |
| `transcript` | 逐字稿（ultimate 用） |
| `voice_id` | 服务端音色 ID（library 用） |
| `cfg_value` / `dit_steps` / `do_normalize` / `denoise` | 生成参数 |

---

## 7. 快速开始

### 7.1 纯语音模式

```bash
export DEEPSEEK_API_KEY="..."
cd /opt/rk3588-voice-robot
source venv/bin/activate
python main.py
```

流程：

1. 说「灵境灵境」唤醒 → 听到「我在」
2. 说指令（如"向前走"）→ 动作下发 MQTT + 听到「好的」
3. 说问题（如"今天天气怎么样"）→ DeepSeek 回答 + VoxCPM 语音播报

### 7.2 语音模式 + HTTP 接口

```bash
python main.py --http --port 8765
```

### 7.3 纯文本调试

```bash
python main.py --text "今天北京天气怎么样"
```

跳过麦克风和 ASR，直接走文本分流。

### 7.4 禁用 WAV 播放器

修改 `tts_config.json`：

```json
{
  "wav_player_enabled": false
}
```

或直接删除这一行——`tts_config.json` 里没有该字段时视为 `true`。

### 7.5 HTTP 调用

```bash
curl -X POST http://localhost:8765/text_input \
  -H "Content-Type: application/json" \
  -H "X-API-Key: my-secret" \
  -d '{"text": "查询茅台股票", "sid": 1, "speed": 1.0}'
```

---

## 8. 主循环工作流程

### 8.1 启动顺序

```
main()
  │
  ├─ check_models()            检查所有模型文件
  │
  ├─ _setup_wav_player()       ★ 启动 WAV 播放器子进程
  │    ├─ 读 tts_config.json
  │    ├─ 清空 out_dir（保留目录本身）
  │    └─ subprocess.Popen(wav_player.py, out_dir_abs)
  │
  ├─ 初始化 VAD
  ├─ 初始化 ASR
  ├─ 初始化本地 TTS
  ├─ 初始化 ActionRouter
  ├─ 初始化 KWS（可选）
  ├─ 启动 HTTP 服务器（可选）
  │
  ├─ [纯文本模式] 执行单次查询 → finally: stop_wav_player()
  │
  └─ [语音模式] 进入主循环
       │
       ├─ 读麦克风 100ms 数据
       ├─ if tts_speaking: 跳过 VAD/KWS，重置状态
       ├─ if 未唤醒: KWS 检测 → 唤醒后说"我在"
       ├─ else: VAD 检测语音段 → ASR 识别 → 分流
       │
       └─ Ctrl+C → finally: stop_wav_player()
```

### 8.2 分流规则

ASR 出文本后：

1. **在 `action_config.json` 中做最长包含匹配**
2. **命中且动作已支持** → 调 `send_mqtt.sh` + 本地 TTS 播「好的」
3. **命中但动作未支持** → 本地 TTS 播「这个动作暂不支持」
4. **未命中** → subprocess 调 `ai_chat_cli.py` 走 DeepSeek

### 8.3 播报期间的麦克风屏蔽

| 播报来源 | 触发方式 | 维护者 |
|----------|---------|--------|
| 本地 sherpa TTS | `speak_async()` | `tts_speaking` Event |
| VoxCPM（ai_chat_cli 内部播放） | 短占位 0.3s | 同上 |
| wav_player 播放 wav | **不维护** | 无（靠 VAD 门限过滤） |

**为什么 wav_player 不维护 `tts_speaking`：** wav_player 是子进程，与主进程不共享内存。若要感知，需引入心跳文件或信号机制——选定策略 ④ 是不感知，靠 VAD 阈值过滤回灌。

---

## 9. WAV 播放器子进程

### 9.1 定位

`wav_player.py` 是一个**独立脚本**，职责单一：

- 监控指定目录
- 检测新出现的、写完的 wav 文件
- 按文件名时间戳排序，依次播放
- 常驻一个 aplay 进程，避免反复 fork/exec

**它本身不感知主进程**，可以被单独运行：

```bash
python3 wav_player.py /opt/rk3588-voice-robot/outputs
```

### 9.2 主进程如何托管

`main.py` 里的 `_setup_wav_player()`：

| 步骤 | 动作 |
|------|------|
| 1 | 读 `tts_config.json` 的 `out_dir` 与 `wav_player_enabled` |
| 2 | 若禁用，返回 no-op stop 函数 |
| 3 | 清空 `out_dir`（保留目录本身） |
| 4 | `subprocess.Popen([python, wav_player.py, out_dir_abs])` |
| 5 | 返回幂等 stop 闭包 |

**退出时**：`stop_wav_player()` 调用 `proc.terminate()`（SIGTERM），wav_player 内部的 signal handler 走 `watcher.stop()` → 关闭 aplay → 退出。3 秒超时则 SIGKILL。

### 9.3 为什么先清空后启动

`wav_player.py` 启动时会做**基线扫描**：把目录里已存在的 wav 全部标记为「已播放」，避免重播旧文件。

如果先启动后清空：

- 旧 wav 被标记为已播 → 无副作用但语义混乱
- 清空发生在播放线程可能已取走文件之后 → 竞态

先清空后启动：启动时目录为空，基线扫描无事可做，语义最清晰。

### 9.4 为什么用子进程而不是线程

| 维度 | 子进程（选定方案） | 线程 |
|------|-------------------|------|
| wav_player 改动 | **0 行** | 需加 `stop_event` / 回调 |
| 崩溃隔离 | aplay 崩溃不影响主进程 | 会拖垮主进程 |
| 职责边界 | 完全独立 | 需处理共享状态 |
| 与主进程通信 | 通过文件（隐式） | 可直接共享 `tts_speaking` |

选定方案以「**不改变 wav_player 本身职责**」为硬约束，子进程最符合。

### 9.5 禁用方式

`tts_config.json`：

```json
{
  "wav_player_enabled": false
}
```

禁用后：

- 不清空 `out_dir`
- 不启动子进程
- VoxCPM 生成的 wav 仍会写到 `out_dir`，但不自动播放

适合以下场景：

- 只想落盘、用另一台设备拉取音频
- 调试 VoxCPM 输出，不想被扬声器打断
- 板端没有扬声器

---

## 10. TTS 引擎与降级策略

### 10.1 双引擎对比

| 场景 | 默认（`voxcpm`） | 备选（`sherpa`） |
|------|:---:|:---:|
| 动作指令「好的」 | sherpa | sherpa |
| 未支持动作提示 | sherpa | sherpa |
| DeepSeek 结果 | `ai_chat_cli.py` 内部 VoxCPM | 主程序 sherpa |
| VoxCPM 失败 | 自动降级到 sherpa | — |

### 10.2 切换方式

环境变量：

```bash
export CHAT_TTS_MODE=voxcpm   # 或 sherpa
```

命令行：

```bash
python main.py --chat-tts sherpa
```

### 10.3 VoxCPM 四种生成模式

`tts_config.json` 里通过 `mode` 字段切换：

| mode | 说明 | 需要 |
|------|------|------|
| `design` | 从零创造新声音 | `control_instruction` |
| `clone` | 保留音色 + 控制风格 | `reference_wav` |
| `ultimate` | 高保真还原 | `reference_wav` + `transcript` |
| `library` | 使用服务端预注册音色 | `voice_id` |

切换 profile：

```bash
python3 ai_chat_cli.py --tts-profile eric_lib --stream "..."
```

---

## 11. 动作分流与 MQTT

### 11.1 匹配规则

`action_router.py` 从 `action_config.json` 读取「动作词 → word_id」映射：

```json
{
  "站起来": 1,
  "向前走": 3,
  "往前走": 4,
  "向左转": 21,
  "打招呼": 31,
  "停下": 27
}
```

匹配逻辑：

- 按 key 长度降序排序
- 遍历，只要 `key in text` 就命中
- 保证最长匹配优先

### 11.2 word_id → action_str

`action_mapping.py` 里：

```python
WORD_ID_TO_ACTION_STR = {
    1:  "stand",
    3:  "forward",
    21: "turn_left_90",
    31: "greet",
    27: "stop",
    ...
}

UNSUPPORTED_WORD_IDS = {0, 13, 14, ..., 42}
```

未在 `WORD_ID_TO_ACTION_STR` 中或列在 `UNSUPPORTED_WORD_IDS` 里的，视为「未支持」。

### 11.3 MQTT 下发

`send_mqtt.sh` 用 `mosquitto_pub` 发送 JSON：

```bash
#!/bin/bash
MQTT_HOST="192.168.200.30"
MQTT_PORT="1883"
MQTT_TOPIC="device/command"

JSON_MSG='{"cmd":"action","action":"forward","device":"lite3"}'
timeout 2 mosquitto_pub -h "$MQTT_HOST" -p "$MQTT_PORT" \
    -t "$MQTT_TOPIC" -m "$JSON_MSG"
```

---

## 12. HTTP 文本输入接口

### 12.1 启动

```bash
python main.py --http --port 8765
```

### 12.2 接口

```
POST /text_input
Headers:
    Content-Type: application/json
    X-API-Key: <your-key>
Body:
    {
        "text": "今天北京天气怎么样",
        "sid": 1,
        "speed": 1.0
    }
```

### 12.3 响应

```json
{ "status": "ok", "text": "今天北京天气怎么样" }
```

### 12.4 调用示例

```bash
curl -X POST http://localhost:8765/text_input \
  -H "Content-Type: application/json" \
  -H "X-API-Key: my-secret" \
  -d '{"text": "查询茅台股票"}'
```

### 12.5 安全说明

- 未设置 `TEXT_INPUT_API_KEY` 时，接口拒绝所有请求
- 仅监听 `0.0.0.0`，生产环境建议用防火墙限制来源

---

## 13. 常见问题 FAQ

**Q1. 唤醒后没有听到「我在」？**

大概率是 `AUDIO_OUTPUT_DEVICE` 不对。参考 [5.4 节](#54-测试麦克风与扬声器) 打印设备列表，找到能出声的索引填回 `config.py`。

**Q2. 唤醒词「灵境灵境」识别不到？**

- 检查 `models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/` 下模型文件是否齐全
- 检查 `keywords.txt` 内容是否为 `灵境灵境` 相关拼音
- 调整 `kws.py` 里的 `keywords_threshold`（默认 0.05，越低越敏感）

**Q3. VoxCPM 语音没播出来？**

- 检查 `tts_config.json` 里 `server` 是否可达：`curl http://<server>:8808/`
- 检查 `out_dir` 是否与实际写入目录一致
- 检查 `wav_player_enabled` 是否为 `true`
- 查看启动日志里 `[WavPlayer] 已启动 (pid=...)` 是否出现
- 若配置了 `voice_id` 但服务端未注册，主进程日志会显示错误

**Q4. `wav_player` 没启动？**

- 看日志 `[WavPlayer] 已在 tts_config.json 中禁用` → 把 `wav_player_enabled` 改成 `true`
- 看日志 `[WavPlayer] 脚本不存在` → 确认 `wav_player.py` 与 `main.py` 同目录
- 看日志 `[WavPlayer] 启动失败` → 手动运行 `python3 wav_player.py /path/to/out_dir` 看具体错误

**Q5. 播报期间还会被唤醒吗？**

- 本地 sherpa 播报期间：主循环会跳过 KWS
- VoxCPM 播报期间：只做 0.3s 短占位，之后靠 VAD 门限过滤
- 若回灌严重导致误触发，可调整 `vad.py` 里的 `threshold`（默认 0.3，调高更严）
- 或加物理隔音（让喇叭远离麦克风）

**Q6. 动作下发成功但机器人没反应？**

- 检查 MQTT 服务器地址：`send_mqtt.sh` 里的 `MQTT_HOST`
- 手动测试：`./send_mqtt.sh '{"cmd":"action","action":"forward","device":"lite3"}'`
- 检查 MQTT 服务器上是否能看到 `device/command` 主题的消息

**Q7. HTTP 接口返回 401？**

- 检查 `robot.env` 里的 `TEXT_INPUT_API_KEY` 是否设置
- 检查请求头 `X-API-Key` 是否一致
- 空 API Key 时会拒绝所有请求

**Q8. 如何查看有哪些动作词？**

```bash
cat action_config.json | python3 -m json.tool
```

**Q9. 想加新动作词？**

1. 在 `action_config.json` 里加 `"新动作词": <word_id>`
2. 如果 `word_id` 已在 `action_mapping.py` 的 `WORD_ID_TO_ACTION_STR` 里，直接生效
3. 否则需要在 `action_mapping.py` 里补充映射 + 在 C 代码里实现对应动作

**Q10. 如何只测试文本模式？**

```bash
python main.py --text "今天北京天气怎么样"
```

**Q11. 如何开机自启？**

创建 systemd service：

```ini
# /etc/systemd/system/voice-robot.service
[Unit]
Description=RK3588 Voice Robot
After=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/rk3588-voice-robot
EnvironmentFile=/opt/rk3588-voice-robot/robot.env
ExecStart=/opt/rk3588-voice-robot/venv/bin/python /opt/rk3588-voice-robot/main.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

启用：

```bash
sudo systemctl daemon-reload
sudo systemctl enable voice-robot
sudo systemctl start voice-robot
sudo journalctl -u voice-robot -f
```

**Q12. wav_player 的 aplay 崩溃了怎么办？**

`wav_player.py` 里的 `ResidentAplay` 会在 `BrokenPipeError` 时自动重启 aplay 进程。若频繁崩溃，检查：

- 音频设备是否被其他进程占用
- `aplay -l` 是否能列出设备
- `config.py` 里是否需要指定 ALSA 设备号

---

## 14. 故障排查

### 14.1 系统性问题

**无声音 / 声音杂音：**

```bash
# 检查 ALSA 设备
aplay -l

# 检查默认设备
aplay -L | head -20

# 测试
speaker-test -t sine -c 1 -l 1
```

**麦克风无输入：**

```bash
# 检查采集设备
arecord -l

# 测试
arecord -d 3 -f cd -r 16000 -c 1 test.wav
aplay test.wav
```

### 14.2 主进程日志速查

| 日志关键字 | 含义 | 处理 |
|-----------|------|------|
| `模型文件不存在：...` | 模型缺失 | 下载对应模型 |
| `[WavPlayer] 已启动 (pid=...)` | 子进程正常 | 无需处理 |
| `[WavPlayer] 已在 tts_config.json 中禁用` | 被显式禁用 | 改 `wav_player_enabled` |
| `[WavPlayer] 已终止` | 正常退出 | 无需处理 |
| `[KWS] 唤醒：...` | 唤醒成功 | — |
| `[ASR] ...` | 识别结果 | — |
| `[Route] 动作指令：...` | 命中动作 | — |
| `[Route] 走 DeepSeek` | 走 LLM | — |
| `[ChatCLI] VoxCPM 模式成功` | VoxCPM 正常 | — |
| `[ChatCLI] VoxCPM 模式失败` | 降级到 sherpa | 检查 server |

### 14.3 VoxCPM 侧排查

**服务端未启动：**

```bash
# 在 TTS 服务器上
python app.py --host 0.0.0.0 --port 8808 --voices-dir ./voices
```

**客户端连不上：**

```bash
curl http://192.168.199.7:8808/
# 应返回 Gradio 的 HTML
```

**音色库为空：**

```bash
# 在 TTS 服务器上
voxcpm voice list
voxcpm voice register --id female --audio /path/to/ref.wav
```

**`voice_id` 拼错：**

客户端会打印友好提示：

```
❌ 服务端音色库中找不到 voice_id='xxx'。
   请检查拼写，或在服务端运行：  voxcpm voice list
```

### 14.4 wav_player 侧排查

**手动运行看细节：**

```bash
python3 wav_player.py /opt/rk3588-voice-robot/outputs
```

另开终端生成一个 wav：

```bash
cp /path/to/some.wav /opt/rk3588-voice-robot/outputs/test.wav
```

观察是否被播放。若没反应：

- 检查 wav 是否写完（`wav_player.py` 有「大小稳定 2 次」的检测）
- 检查文件名是否以 `.wav` 结尾
- 检查播放是否被去重（同文件同 mtime 只播一次）

**aplay 直接测试：**

```bash
aplay /opt/rk3588-voice-robot/outputs/test.wav
```

### 14.5 查看 systemd 日志

```bash
sudo journalctl -u voice-robot -f
sudo journalctl -u voice-robot --since "10 minutes ago"
```

---

## 15. 附录：环境变量与默认值

### 15.1 环境变量

| 变量 | 用于 | 说明 |
|------|------|------|
| `TEXT_INPUT_API_KEY` | `http_server.py` | HTTP 接口 API Key |
| `CHAT_TTS_MODE` | `main.py` | `voxcpm` / `sherpa` |
| `DEEPSEEK_API_KEY` | `ai_chat_cli.py` | DeepSeek API Key |
| `ZHIPUAI_API_KEY` | `ai_chat_cli.py` | 智谱 API Key |
| `VOXCPM_SERVER` | `ai_voxcpm_cli.py` | 覆盖默认 server 地址 |

### 15.2 `config.py` 默认值

| 常量 | 值 |
|------|------|
| `AUDIO_INPUT_DEVICE` | 3（USB Audio） |
| `AUDIO_OUTPUT_DEVICE` | 0（板载 ES8388） |
| `SAMPLE_RATE` | 16000 |
| `DEVICE_RATE` | 16000 |
| `CHANNELS` | 1 |
| `SAMPLES_PER_READ` | 1600（100ms） |
| `VAD_WINDOW_SIZE` | 512 |
| `TTS_DEFAULT_SID` | 0 |
| `TTS_DEFAULT_SPEED` | 1.0 |
| `MQTT_DEVICE` | lite3 |
| `CHAT_TIMEOUT` | 120 |
| `TEXT_INPUT_PORT` | 8765 |

### 15.3 `tts_config.json` 默认值

| 字段 | 默认值 |
|------|--------|
| `enabled` | true |
| `server` | `http://127.0.0.1:8808` |
| `api_name` | `/generate` |
| `backend` | auto |
| `out_dir` | `./outputs` |
| `wav_player_enabled` | true |
| `auto_play` | true |
| `gapless` | true |
| `stream_workers` | 4 |
| `seed` | null |
| `split.comma_threshold` | 20 |
| `split.min_chars` | 4 |
| `active_profile` | default |

### 15.4 四种 TTS 模式速查

| mode | 需要参考音频 | 需要 transcript | 需要 voice_id | 用途 |
|------|:---:|:---:|:---:|------|
| `design` | 否 | 否 | 否 | 从零创造 |
| `clone` | 是 | 否 | 否 | 音色 + 风格 |
| `ultimate` | 是 | 是 | 否 | 高保真还原 |
| `library` | 否 | 否 | 是 | 复用服务端音色 |

### 15.5 命令行参数速查

```bash
python main.py [OPTIONS]

Options:
  --text TEXT          纯文本模式，跳过麦克风
  --http               启用 HTTP 文本输入接口
  --port PORT          HTTP 端口（默认 8765）
  --no-kws             跳过唤醒词，直接进入拾音
  --chat-tts {voxcpm,sherpa}
                       DeepSeek 结果的 TTS 引擎
```

---

## 许可与致谢

- **语音模型**：[sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)（KWS / VAD / ASR / TTS）
- **TTS 模型**：[VoxCPM2](https://github.com/OpenBMB/VoxCPM)（ModelBest）
- **LLM 服务**：[智谱 GLM](https://open.bigmodel.cn/) / [DeepSeek](https://www.deepseek.com/)
- **通信**：[gradio_client](https://www.gradio.app/) 或原生 HTTP + SSE
- **MQTT**：[mosquitto](https://mosquitto.org/)

