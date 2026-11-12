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
9. [VoxCPM 输出监控与播放](#9-voxcpm-输出监控与播放主进程内线程)
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
| 📂 VoxCPM 播放 | 主进程内监控线程 + AudioPlayer | 监控 outputs/ 目录，线程内直接播放 wav |

---

## 2. 核心特性

- **本地全链路**：唤醒、VAD、ASR 都在板端运行，不依赖外网。
- **双 TTS 引擎**：固定话术走本地 sherpa，复杂回答走云端 VoxCPM；VoxCPM 失败时自动降级到 sherpa。
- **动作优先分流**：ASR 文本先做最长包含匹配，命中动作词直接下发 MQTT，未命中才走 LLM。
- **播报期间暂停拾音**：`tts_speaking` 标志置位时，主循环跳过 VAD/KWS，避免"自问自答"。
- **VoxCPM 播放线程化**：主进程内监控 + 播放两个守护线程，随主程序启动、随 `stop_event` 退出；多段输出可用 ffmpeg 合并后无缝播放。
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
│       │                 ┌───────┐   ┌──────────────┐         │
│       │                 │ 扬声器 │   │ outputs/     │         │
│       │                 └───────┘   │  *.wav       │         │
│       │                             └─────┬────────┘         │
│       │                                   │ 监控线程发现新 wav │
│       │                                   ▼                  │
│       │                     ┌──────────────────────┐         │
│       │                     │ 播放线程（主进程内）    │         │
│       │                     │ AudioPlayer 批量播放   │         │
│       │                     └──────────┬───────────┘         │
│       │                                ▼                     │
│       │                            ┌───────┐                 │
│       │                            │ 扬声器 │                 │
│       │                            └───────┘                 │
└──────────────────────────────────────────────────────────────┘
```

**关键点：**

- **两个播放源**：本地 sherpa（`speak_async`）和 VoxCPM 监控线程（`_voxcpm_player_loop`），
  都在主进程内，共用 `AudioPlayer`。
- **`tts_speaking` 由 sherpa 分支维护**；VoxCPM 分支只做短占位（0.3s），后续靠 VAD 门限过滤回灌。
- **VoxCPM 播放是主进程内的守护线程**：`_watch_voxcpm_outputs` 每 0.1s 扫描 outputs/，
  新 wav 投入 `voxcpm_play_queue`；播放线程批量取出，多段时用 ffmpeg concat 合并后一次播放。
- 退出方式：主流程 `finally: stop_event.set()` 结束两个守护线程，无子进程需要回收。

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
├── http_server.py             # HTTP 服务（Web 对话 UI + SSE + 文本指令）
│
├── models/                    # 模型文件
│   ├── silero_vad.onnx
│   ├── sherpa-onnx-streaming-paraformer-bilingual-zh-en/
│   ├── vits-melo-tts-zh_en/
│   └── sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/
│
├── voices/                    # 客户端参考音频（clone / ultimate 用）
├── outputs/                   # VoxCPM 输出目录（每次启动清空）
├── voice-robot.service        # systemd 服务单元（开机自启）
│
├── scripts/
│   ├── deploy.sh              # 一键部署（幂等，支持 --service / --skip-apt）
│   ├── download_models.sh     # 模型下载（断点续传 + 镜像回退）
│   └── install.sh             # venv + pip 简版安装
│
└── README.md                  # 本文档
```

> 历史文件 `wav_player_orig.py` 是早期「子进程 + 常驻 aplay」播放方案的存档，
> 当前主流程已不使用，仅在需要单独回放 wav 时可参考。

---

## 5. 安装与依赖

### 5.1 系统要求

- RK3588 开发板
- Ubuntu 20.04（aarch64）
- USB 麦克风 + 扬声器
- 可访问互联网（LLM API、VoxCPM server）
- 可访问 MQTT 服务器

### 5.2 一键部署（推荐）

把 5.4–5.6 的系统依赖、venv、pip、模型下载、`robot.env` 生成和依赖自检串成一条命令，**幂等**，可重复执行：

```bash
bash scripts/deploy.sh              # 基础部署：apt + venv + pip + 模型 + env 文件 + 自检
bash scripts/deploy.sh --service    # 基础部署后，再安装/更新 systemd 服务并启动
bash scripts/deploy.sh --skip-apt   # 跳过 apt（离线或系统依赖已装好时）
bash scripts/deploy.sh --help       # 查看用法
```

脚本行为说明：

- 已完成的步骤自动跳过（如 `venv/` 存在则复用，`robot.env` 存在则不覆盖）
- 先试清华源装依赖，失败自动回退官方源重试一次
- 逐项打印 `send_mqtt.sh` 的 `MQTT_HOST`、`tts_config.json` 的 `server`、`config.py` 的 `TRANSLATE_URL`，**只提醒不修改**
- 结尾汇总待处理事项（未填的 API Key、缺失的命令、需人工确认的地址等）
- 不做的事：不修改你已有的配置值，不改 `main.py` / `config.py` / `tts_config.json`

> 手动分步安装仍然有效，见 5.4–5.6。想精确控制每一步时按下面流程操作。

### 5.3 创建虚拟环境

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

### 5.4 下载模型

```bash
bash scripts/download_models.sh
```

或将模型手动放置到 `models/` 对应目录。

### 5.5 测试麦克风与扬声器

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

### 5.6 环境变量

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

### 7.4 只落盘、不自动播放 VoxCPM 输出

VoxCPM 输出由主进程内的监控线程发现并自动播放。若只想落盘、用另一台设备拉取
音频（或调试时不想被扬声器打断），把 `tts_config.json` 的 `server` 指向一个
不存在的地址即可让生成降级/失败，或直接在板端不接扬声器；监控线程播放失败只
会记日志，不影响主流程。

> 历史版本支持 `tts_config.json` 里 `"wav_player_enabled": false` 一键关闭子进程
> 播放器；当前线程化实现中该字段**已不再读取**，保留在旧配置文件里不会有副作用。

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
  ├─ _setup_voxcpm_watcher()   ★ 启动 VoxCPM 输出监控 + 播放线程
  │    ├─ 读 tts_config.json 的 out_dir
  │    ├─ 清空 out_dir（保留目录本身）
  │    └─ 启动两个守护线程：
  │         _watch_voxcpm_outputs  每 0.1s 扫描新 wav → 投入队列
  │         _voxcpm_player_loop    从队列取 wav，批量合并播放
  │
  ├─ 初始化 VAD
  ├─ 初始化 ASR
  ├─ 初始化本地 TTS
  ├─ 初始化 ActionRouter
  ├─ 初始化 KWS（可选）
  ├─ 启动 HTTP 服务器（可选）
  │
  ├─ [纯文本模式] 执行单次查询 → finally: stop_event.set()
  │
  └─ [语音模式] 进入主循环
       │
       ├─ 读麦克风 100ms 数据
       ├─ if tts_speaking: 跳过 VAD/KWS，重置状态
       ├─ if 未唤醒: KWS 检测 → 唤醒后说"我在"
       ├─ else: VAD 检测语音段 → ASR 识别 → 分流
       │
       └─ Ctrl+C → finally: stop_event.set()
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
| VoxCPM 监控线程播放 wav | 短占位 0.3s | 同上（线程发布 `tts_done`，主循环靠 VAD 门限过滤回灌） |

**说明：** VoxCPM 监控/播放线程虽然在主进程内，但与 `tts_speaking` 的维护是解耦的：
线程只发布 `status_speak` / `tts_done` 事件，播放期间依旧靠 VAD 阈值过滤喇叭回灌，
与 sherpa 分支的强屏蔽（`tts_speaking` 置位期间跳过 VAD/KWS）是两种强度。

---

## 9. VoxCPM 输出监控与播放（主进程内线程）

### 9.1 定位

VoxCPM（Gradio server）生成的 wav 落在 `outputs/` 目录，主进程通过**两个守护线程**
完成发现与播放，不再使用独立的子进程播放器：

- `_watch_voxcpm_outputs(out_dir, stop_event)`：目录监控线程
- `_voxcpm_player_loop(player, stop_event)`：播放线程

### 9.2 工作流程

**监控线程**（`main.py: _watch_voxcpm_outputs`）：

| 步骤 | 动作 |
|------|------|
| 1 | 启动时做基线扫描：目录里已有的 wav 全部标记为「已播放」，避免重播旧文件 |
| 2 | 每 0.1s 扫描一次目录，找出新增的 `.wav` |
| 3 | 校验文件有效（大小 > 0 且 `wave.open` 可读，即写完的合法 wav） |
| 4 | 按文件名排序后投入 `voxcpm_play_queue` |

**播放线程**（`main.py: _voxcpm_player_loop`）：

| 步骤 | 动作 |
|------|------|
| 1 | 从队列取一个路径，并把队列中已就绪的路径**批量取出** |
| 2 | 单段：直接 `player.play_wav_file(path, wait=True)` |
| 3 | 多段：先尝试 `ffmpeg concat` 合并成一个 wav 播放（`-c copy`，无重编码） |
| 4 | ffmpeg 不可用或合并失败：逐段顺序播放 |
| 5 | 发布 `status_speak` / `tts_done` 事件，供 Web UI 显示状态 |

### 9.3 启动与退出

`_setup_voxcpm_watcher(player)`（`main.py:212`）：

| 步骤 | 动作 |
|------|------|
| 1 | 读 `tts_config.json` 的 `out_dir`（缺省 `outputs`） |
| 2 | 清空 `out_dir` 里的旧 wav（保留目录本身） |
| 3 | 启动上述两个守护线程 |
| 4 | 返回 `(out_dir_abs, stop_event)` |

**退出时**：主流程 `finally: stop_event.set()`，两个线程在 0.5s 内自然退出
（线程均为 daemon，进程退出时也会被强制回收）。

### 9.4 为什么先清空后启动

监控线程启动时会做基线扫描：把目录里已存在的 wav 全部标记为「已播放」，
避免重播旧文件。先清空后启动，基线扫描无事可做，语义最清晰。

### 9.5 为什么用线程而不是子进程（相对早期方案的变更）

| 维度 | 线程（当前方案） | 子进程（早期 wav_player 方案） |
|------|-----------------|------------------------------|
| 播放共享 | 直接复用主进程 `AudioPlayer`，与 sherpa TTS 用同一套输出设备管理 | 需独立管理 aplay 设备，易与主进程抢占 |
| 段间衔接 | 可在播放前用 ffmpeg 合并，实现近似无缝拼接 | 只能顺序播放，段间有 aplay 启停间隙 |
| 状态同步 | 同进程，可读 `tts_speaking`、发布 event_bus 事件 | 需文件/信号等跨进程机制 |
| 生命周期 | daemon 线程，`stop_event.set()` 即停 | 需 SIGTERM/SIGKILL 托管与超时兜底 |

早期「子进程 + 常驻 aplay」的实现保留在 `wav_player_orig.py`，仅作存档，
需要单独回放某个目录的 wav 时可以参考或手动运行。

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

大概率是 `AUDIO_OUTPUT_DEVICE` 不对。参考 [5.5 节](#55-测试麦克风与扬声器) 打印设备列表，找到能出声的索引填回 `config.py`。

**Q2. 唤醒词「灵境灵境」识别不到？**

- 检查 `models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/` 下模型文件是否齐全
- 检查 `keywords.txt` 内容是否为 `灵境灵境` 相关拼音
- 调整 `kws.py` 里的 `keywords_threshold`（默认 0.05，越低越敏感）

**Q3. VoxCPM 语音没播出来？**

- 检查 `tts_config.json` 里 `server` 是否可达：`curl http://<server>:8808/`
- 检查 `out_dir` 是否与实际写入目录一致
- 查看启动日志里 `[VoxCPM-Watch] 监控目录：...` 是否出现（监控线程已启动）
- 查看播放日志 `[VoxCPM] 播放：...` 是否出现（文件被发现且进入播放）
- 若配置了 `voice_id` 但服务端未注册，主进程日志会显示错误

**Q4. VoxCPM 输出线程没工作？**

- 看日志 `[VoxCPM-Watch] 已清空旧 wav：...` → 监控线程已启动
- 看日志 `[VoxCPM] 播放失败：...` → 检查音频设备是否被占用（见 Q1 / 14.1）
- 批量播放依赖 `ffmpeg` 合并：未安装时会自动退化为逐段播放（`which ffmpeg` 检查）

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

仓库根目录已提供 `voice-robot.service`（`User=root`、带 `--http --port 8765`、`SyslogIdentifier=voice-robot`），直接安装即可：

```bash
sudo cp voice-robot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now voice-robot
sudo journalctl -u voice-robot -f
```

或交给一键脚本：`bash scripts/deploy.sh --service`。

改用普通用户运行（非 root）时，按 `voice-robot.service` 内注释四步调整：

1. 改文件里的 `User=` / `Group=`
2. `sudo usermod -aG audio,video <用户名>`
3. `sudo chown -R <用户名>:<用户名> /opt/rk3588-voice-robot`
4. 用该用户重建 `venv`（否则 `ExecStart` 找不到解释器）

> 若板上曾部署过旧版 `robot.service`（`/opt/robot`，路径有误），先清理：
> `sudo systemctl disable --now robot.service && sudo rm -f /etc/systemd/system/robot.service`

**Q12. 播放报设备错误 / 无声？**

当前播放统一走主进程 `AudioPlayer`（sounddevice 输出）。若报 `PortAudioError`
或设备占用：

- 音频设备是否被其他进程占用（`fuser -v /dev/snd/*`）
- `aplay -l` 是否能列出设备
- 参考 5.5 节确认输出设备索引（当前 `AudioPlayer` 使用系统默认输出）

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
| `[VoxCPM-Watch] 监控目录：...` | 监控线程已启动 | 无需处理 |
| `[VoxCPM-Watch] 已清空旧 wav：...` | 启动时清理完成 | 无需处理 |
| `[VoxCPM] 播放：...` / `批量播放 N 段` | 输出 wav 正在播放 | 无需处理 |
| `[VoxCPM] 播放失败：...` | 播放出错 | 检查音频设备占用（见 14.1） |
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

### 14.4 VoxCPM 输出播放侧排查

**手动测试监控与播放：**

另开终端放一个写完的 wav 到输出目录：

```bash
cp /path/to/some.wav /opt/rk3588-voice-robot/outputs/test.wav
```

观察主进程日志是否出现 `[VoxCPM] 播放：test.wav`。若没反应：

- 检查文件名是否以 `.wav` 结尾
- 检查 wav 是否写完且合法（监控线程会 `wave.open` 校验；写一半的文件会被忽略）
- 检查是否是启动前已存在的旧文件（基线扫描会标记为已播放，不会重播）

**跳过监控线程直接测试音频输出：**

```bash
aplay /opt/rk3588-voice-robot/outputs/test.wav
```

`aplay` 有声但主进程无声 → 问题在 `AudioPlayer`/设备选择（见 14.1 与 Q1）；
`aplay` 也没声 → 系统音频问题（见 14.1）。

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

