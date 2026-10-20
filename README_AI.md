# RK3588 智能语音对话终端（LLM + VoxCPM2 TTS）

在 Ubuntu 20.04 / RK3588 平台上，把大语言模型（智谱 GLM / DeepSeek）的文字对话，
与 VoxCPM2 的实时语音合成能力整合到同一个命令行程序里：
**文字照常流式打印，语音在后台同步合成并播放，互不阻塞。**

VoxCPM2 服务端（`app.py`）现支持 **四种语音生成方式**，本客户端已全部适配：

| 模式 | 说明 | 是否需要上传音频 | 是否需要转写文本 | 是否需要 voice_id |
|------|------|:---:|:---:|:---:|
| 🎨 Voice Design | 从零创造新声音 | 否 | 否 | 否 |
| 🎛️ Controllable Cloning | 保留音色 + 控制情感/语速 | 是 | 否 | 否 |
| 🎙️ Ultimate Cloning | 高保真还原全部声音细节 | 是 | 是 | 否 |
| 🗂️ **Voice Library** | **使用服务端预注册音色 ID** | **否** | **否** | **是** |

---

## 目录

1. [项目简介](#1-项目简介)
2. [核心特性](#2-核心特性)
3. [目录结构](#3-目录结构)
4. [系统架构](#4-系统架构)
5. [安装与依赖](#5-安装与依赖)
6. [TTS 服务端准备](#6-tts-服务端准备)
7. [快速开始](#7-快速开始)
8. [配置文件详解](#8-配置文件详解)
9. [命令行参数详解](#9-命令行参数详解)
10. [REPL 交互模式](#10-repl-交互模式)
11. [切分规则与低延迟原理](#11-切分规则与低延迟原理)
12. [运行时行为说明](#12-运行时行为说明)
13. [常见问题 FAQ](#13-常见问题-faq)
14. [故障排查](#14-故障排查)
15. [扩展与二次开发](#15-扩展与二次开发)
16. [附录：环境变量与默认值](#16-附录环境变量与默认值)

---

## 1. 项目简介

本项目包含三个文件：

| 文件 | 说明 |
|---|---|
| `ai_voxcpm_cli.py` | VoxCPM2 命令行客户端，并作为**可导入模块**对外提供动态 TTS 组件 |
| `ai_chat_cli.py` | 大模型对话主程序，集成 TTS |
| `tts_config.json` | TTS 配置文件，支持多声音 profile |

设计目标：

- **低延迟**：LLM 首个句子一出现，就立即触发首段 TTS，不必等整段回复生成完。
- **时间连贯**：段与段之间用 gapless 拼接，避免明显停顿。
- **互不阻塞**：文字打印、语音合成、语音播放各走独立线程，用户随时可以输入下一句。
- **不打断**：新一轮对话开启时，上一轮语音仍继续播完，实现"语音与新对话并行"。
- **向后兼容**：`ai_voxcpm_cli.py` 的原有 CLI 命令完全保留，不启用 TTS 时 `ai_chat_cli.py` 行为与旧版一致。
- **音色库支持**：客户端与 `app.py` 的 11 参数接口对齐，支持服务端预注册的 `voice_id`，无需每次上传参考音频。

---

## 2. 核心特性

| 特性 | 说明 |
|---|---|
| 双模型服务商 | 智谱 GLM、DeepSeek，通过 `-p` 切换 |
| 双输出模式 | 普通模式（一次性返回）、流式模式（逐 token 打印） |
| 双输出通道 | 文本 + 语音，互不阻塞 |
| **四种 TTS 模式** | design / clone / ultimate / **library**，由 `tts_config.json` 或 REPL `:mode` 切换 |
| 流式增量切分 | 边收 token 边切句：句末标点为主，逗号/分号为辅 |
| 可配置切分阈值 | `N`（逗号辅助阈值）可通过 JSON 与 CLI 双重覆盖 |
| 多声音 profile | 一份配置里定义多套音色，运行时 `--tts-profile` 切换 |
| 并发生成、顺序播放 | 后台多线程生成，播放严格按句序，避免乱序 |
| gapless 拼接 | 同一轮的连续音频段合并成一个 wav，单实例播放，消除间隙 |
| 全局固定 seed | 同一进程内所有轮次共用 seed，音色一致 |
| 后端自动回退 | 优先 `gradio_client`，不可用时自动回退 HTTP + SSE |
| **友好错误提示** | `voice_id` 拼错时返回中文提示 + 排查命令 |
| **客户端裁决** | `reference_wav` 与 `voice_id` 同时存在时按服务器规则裁决 + 警告 |
| 完整打断策略 | 语音不打断新对话，两路并行 |

---

## 3. 目录结构

```
your_project/
├── ai_voxcpm_cli.py        # VoxCPM 客户端 + 可复用动态 TTS 组件
├── ai_chat_cli.py          # LLM 对话主程序（集成 TTS）
├── tts_config.json         # TTS 配置（多 profile）
├── README_AI.md            # 本文档
├── voices/                 # 可选：参考音频目录（clone / ultimate 模式用）
│   ├── robot.wav
│   └── narrator.wav
└── outputs/                # 运行时自动创建，存放合成的 wav 文件
    └── .gapless_tmp/       # gapless 临时拼接目录（播完自动清理）
```

服务端（`app.py` 所在的机器）额外有：

```
server/
├── app.py                  # Gradio 服务端（11 参数接口，支持 voice_id）
├── voices/                 # 音色库目录（与客户端无关，服务器本地）
│   ├── manifest.json
│   ├── female.safetensors
│   └── eric.safetensors
└── src/voxcpm/
    └── voice_library.py    # 音色库实现
```

---

## 4. 系统架构

### 4.1 线程模型

```
┌─────────────────────────────────────────────────────────────┐
│ 主线程（main）                                              │
│ 读用户输入 → 构造 messages → 调 LLM（流式/普通）             │
│ 流式模式下每个 token: print + splitter.feed() → session.push()
└─────────────────────────────────────────────────────────────┘
         │                                    │
         │ push(seg)                          │ finish()
         ▼                                    ▼
┌───────────────────────┐       ┌───────────────────────────────┐
│ _executor（多线程）    │       │ _collect_executor（单线程）    │
│ 并发生成每段音频       │───▶   │ 按 push 顺序 result futures    │
│ 调 VoxCPM server       │       │ 按序放入 _play_q               │
└───────────────────────┘       └───────────────────────────────┘
                                             │
                                             ▼
                                ┌──────────────────────────┐
                                │ _play_thread（单线程）     │
                                │ 从 _play_q 取音频路径      │
                                │ gapless 时批量拼接播放     │
                                └──────────────────────────┘
```

关键点：

- **生成并行**：多段音频的 TTS 请求同时发给 server。
- **收集串行**：用一个单线程 executor 按 push 顺序 `future.result()`，保证进入播放队列的顺序就是句序。
- **播放串行**：全局只有一个播放线程，音频天然按序播放。
- **主线程零阻塞**：读 token → 打印 → 切分 → push，动作极短，所以能立刻响应下一次 `input()`。

### 4.2 组件职责

| 组件 | 位置 | 职责 |
|---|---|---|
| `IncrementalSplitter` | `ai_voxcpm_cli.py` | 面向 LLM 流式输出的增量切分器 |
| `TTSSession` | `ai_voxcpm_cli.py` | 一轮对话的会话，管理该轮所有段的 future |
| `TTSRuntime` | `ai_voxcpm_cli.py` | 全局运行时：生成池、收集池、播放线程、全局 seed |
| `GenRequest` / `GenResult` | `ai_voxcpm_cli.py` | 请求/结果数据结构，支持 `voice_id` 字段与客户端裁决 |
| `GradioClientBackend` | `ai_voxcpm_cli.py` | 通过 `gradio_client` 调用 server |
| `HttpSseBackend` | `ai_voxcpm_cli.py` | 通过原生 HTTP + SSE 调用 server |
| `StreamingPipeline` | `ai_voxcpm_cli.py` | 命令行一次性长文本模式下的流式流水线 |
| `_friendly_error` | `ai_voxcpm_cli.py` | 把服务器错误包装成中文提示 |
| `chat_once` | `ai_chat_cli.py` | LLM 调用 + 切分 + 送入 TTS |
| `setup_tts` | `ai_chat_cli.py` | 加载 JSON 配置，构造 `TTSRuntime` |

### 4.3 请求参数对齐

客户端与服务器 `app.py` 的 `_generate` 函数严格对齐 **11 个参数**：

| # | 参数 | 说明 |
|---|------|------|
| 1 | `text` | 目标文本（含 control 前缀） |
| 2 | `control_instruction` | 风格控制（ultimate 模式会清空） |
| 3 | `reference_wav` | 参考音频 FileData |
| 4 | **`voice_id`** | **音色库 ID（第四态）** |
| 5 | `use_prompt_text` | 是否开启极致克隆 |
| 6 | `prompt_text` | 参考音频转写 |
| 7 | `cfg_value` | CFG 引导强度 |
| 8 | `do_normalize` | 文本规范化 |
| 9 | `denoise` | 参考音频降噪 |
| 10 | `dit_steps` | LocDiT 步数 |
| 11 | `seed` | 随机种子 |

服务器裁决规则：**`if voice_id and not reference_wav: 音色库分支，否则走原有三种模式`**。
即 `reference_wav` 优先级高于 `voice_id`。

---

## 5. 安装与依赖

### 5.1 系统要求

- Ubuntu 20.04 / 22.04（已在 RK3588 上验证）
- Python ≥ 3.8
- 网络可访问 LLM 服务商 API
- 网络可访问 VoxCPM Gradio server

### 5.2 Python 依赖

```bash
pip install openai
# 推荐（不是必须）：装了就用 gradio_client 后端，更稳
pip install gradio_client
```

说明：`gradio_client` 缺失时，`ai_voxcpm_cli.py` 会自动回退到 `HttpSseBackend`（纯 urllib + json），无需额外安装。

### 5.3 音频播放器

程序会自动探测以下播放器，找到第一个就用：

- `ffplay`（推荐，ffmpeg 包自带）
- `afplay`（macOS）
- `aplay`（ALSA，Ubuntu 常见）
- `paplay`（PulseAudio）

```bash
# 推荐安装 ffmpeg（含 ffplay + ffmpeg concat）
sudo apt install ffmpeg
```

`ffmpeg` 还用于 gapless 拼接（`_concat_wavs_ffmpeg`）；没有 ffmpeg 时会回退到纯 Python wave 拼接（要求所有段参数一致）。

### 5.4 设置 API Key

```bash
# 智谱
export ZHIPUAI_API_KEY="your-zhipu-key"

# DeepSeek
export DEEPSEEK_API_KEY="your-deepseek-key"
```

可写入 `~/.bashrc` 或 `~/.zshrc` 持久化。

---

## 6. TTS 服务端准备

### 6.1 启动 Gradio server

`ai_chat_cli.py` 本身不启动 TTS 服务，只作为客户端。需要先在能访问的机器上启动 VoxCPM 的 Gradio server（本项目自带的 `app.py`）：

```bash
# 在 TTS 服务端机器上
python3 app.py --host 0.0.0.0 --port 8808 --voices-dir ./voices
# 默认监听 http://0.0.0.0:8808，接口名 /generate
```

### 6.2 注册音色库（仅 Voice Library 模式需要）

服务端使用 `voxcpm voice` 子命令管理音色：

```bash
# 注册
voxcpm voice register --id female --audio /path/to/female_ref.wav --display-name "Female"
voxcpm voice register --id eric   --audio /path/to/eric_ref.wav   --display-name "Eric"

# 查看
voxcpm voice list
voxcpm voice info --id female

# 删除
voxcpm voice remove --id female
```

也可以打开服务端 Gradio UI（`http://<server>:8808/`），在「🗂️ 音色库管理」面板里上传音频、点「✅ 注册音色」。UI 注册后**立即可用**（热加载），无需重启服务。

### 6.3 验证连通性

```bash
curl http://192.168.199.7:8808/
```

在 `tts_config.json` 里把 `server` 改为实际地址。

---

## 7. 快速开始

### 7.1 最小可用示例

```bash
# 1. 准备配置
cp tts_config.json ./tts_config.json
# 编辑 server 字段指向你的 TTS 服务端

# 2. 设置 LLM API Key
export ZHIPUAI_API_KEY="..."

# 3. 交互模式 + 流式输出 + TTS（默认走 female 音色库）
python3 ai_chat_cli.py --stream
```

在提示符 `你:` 后面输入问题，回车后即可看到文字逐字打印，同时听到语音。

### 7.2 四种模式快速体验

```bash
# Voice Library（音色库）
python3 ai_chat_cli.py --tts-profile female_lib --stream "你好"
python3 ai_chat_cli.py --tts-profile eric_lib   --stream "你好"

# Voice Design（声音设计）
python3 ai_chat_cli.py --tts-profile default --stream "你好"

# Controllable Cloning（可控克隆）
python3 ai_chat_cli.py --tts-profile robot --stream "你好"

# Ultimate Cloning（极致克隆）
python3 ai_chat_cli.py --tts-profile narrator --stream "你好"
```

### 7.3 一次性提问

```bash
python3 ai_chat_cli.py -p deepseek --stream "用三句话介绍杭州"
```

### 7.4 无 TTS，纯文字

```bash
python3 ai_chat_cli.py --no-tts --stream
# 或临时把 tts_config.json 的 enabled 改成 false
```

### 7.5 只用 VoxCPM 客户端

`ai_voxcpm_cli.py` 的 CLI 同样完整可用：

```bash
# 音色库模式（一次性）
python3 ai_voxcpm_cli.py --voice-id female --text "Hello world"

# 带风格控制
python3 ai_voxcpm_cli.py --voice-id female --control "轻快活泼" --text "今天天气不错。"

# 长文本 + gapless
python3 ai_voxcpm_cli.py --voice-id female --text "第一句。第二句。第三句。" --gapless

# 无参数 → 进入交互 REPL
python3 ai_voxcpm_cli.py
```

---

## 8. 配置文件详解

配置文件默认路径 `./tts_config.json`，可用 `--tts-config <path>` 指定。

### 8.1 完整示例

```json
{
  "enabled": true,
  "server": "http://192.168.199.7:8808",
  "api_name": "/generate",
  "backend": "auto",
  "out_dir": "./outputs",
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
      "transcript": "这是一段用于极致克隆的参考音频，内容请替换为你实际的 transcript。",
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

### 8.2 顶层字段

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `enabled` | bool | true | 配置文件存在时是否自动启用 TTS；`--tts` / `--no-tts` 可强制覆盖 |
| `server` | str | `http://127.0.0.1:8808` | Gradio server 地址 |
| `api_name` | str | `/generate` | 接口名，须与服务端 `app.py` 一致 |
| `backend` | str | auto | auto / gradio / http |
| `out_dir` | str | `./outputs` | 生成音频的保存目录 |
| `auto_play` | bool | true | 是否自动播放；`--tts-no-play` 可关 |
| `gapless` | bool | true | 是否启用段间拼接 |
| `stream_workers` | int | 4 | 并发生成线程数 |
| `seed` | int / null | null | null 表示随机；非 null 则整进程固定音色 |
| `split` | object | 见下 | 切分参数 |
| `active_profile` | str | default | 默认启用的音色 profile |
| `profiles` | object | — | 多套音色，至少一个 |

### 8.3 `split` 字段

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `comma_threshold` | int | 20 | N 值：缓冲区累积超过 N 字符后，遇到 `，,、；;` 也切一刀 |
| `min_chars` | int | 4 | 最小段长，短于此长度的相邻段会被合并 |

优先级：内置默认 < `tts_config.json` < `--tts-comma-threshold`

### 8.4 `profiles.<name>` 字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `mode` | str | `design` / `clone` / `ultimate` / **`library`** |
| `control_instruction` | str | Voice Design / Controllable Cloning / **Voice Library** 的风格描述 |
| `reference_wav` | str / null | 参考音频路径；`clone` 与 `ultimate` 必填 |
| `transcript` | str | `ultimate` 模式的参考音频逐字稿，必填 |
| **`voice_id`** | str | **`library` 模式下必填；服务端已注册的音色 ID** |
| `cfg_value` | float | CFG 引导强度，默认 2.0 |
| `dit_steps` | int | LocDiT 采样步数，默认 10（越小越快） |
| `do_normalize` | bool | 文本规范化 |
| `denoise` | bool | 参考音频降噪 |

### 8.5 四种 mode 对照表

| mode | 需要参考音频 | 需要 transcript | 需要 voice_id | 使用字段 |
|------|:---:|:---:|:---:|------|
| `design` | 否 | 否 | 否 | `control_instruction` |
| `clone` | 是 | 否 | 否 | `reference_wav` + 可选 `control_instruction` |
| `ultimate` | 是 | 是 | 否 | `reference_wav` + `transcript` |
| `library` | 否 | 否 | **是** | `voice_id` + 可选 `control_instruction` |

### 8.6 `reference_wav` 与 `voice_id` 同时存在的处理

如果某个 profile 手滑同时写了 `reference_wav` 和 `voice_id`：

- **服务器规则**：`reference_wav` 优先，`voice_id` 被忽略。
- **客户端行为**：提交前打印一行警告，明确告诉你哪个 `voice_id` 被忽略了：

  ```
  ⚠️  检测到 reference_wav 与 voice_id 同时存在，按服务器规则 reference_wav 优先，voice_id='female' 将被忽略。
  ```

  然后按 clone 路径生成。

---

## 9. 命令行参数详解

### 9.1 LLM 相关

| 参数 | 默认 | 说明 |
|------|------|------|
| `text`（位置参数） | — | 直传提问文本；不传则进入交互模式 |
| `-p, --provider` | `zhipu` | `zhipu` / `deepseek` |
| `--stream` | off | 启用流式输出 |
| `-m, --model` | 服务商默认 | 覆盖模型编码 |
| `--api-key` | 环境变量 | 覆盖 API Key |
| `--system` | 见附录 | 系统提示词 |
| `--temperature` | 服务商默认 | 温度 |
| `--max-tokens` | 服务商默认 | 单次回复最大 token 数 |
| `--top-p` | 服务商默认 | top_p 采样 |
| `--frequency-penalty` | 服务商默认 | 频率惩罚 |
| `--presence-penalty` | 服务商默认 | 存在惩罚 |

### 9.2 TTS 相关

| 参数 | 默认 | 说明 |
|------|------|------|
| `--tts` | — | 强制启用 TTS（即使配置不存在/enabled=false） |
| `--no-tts` | — | 强制禁用 TTS（与 `--tts` 互斥） |
| `--tts-config` | `./tts_config.json` | 配置文件路径 |
| `--tts-profile` | 配置中 `active_profile` | 选择 profile |
| `--tts-comma-threshold` | 配置中 `split.comma_threshold` | 覆盖 N |
| `--tts-no-play` | off | 只生成音频，不自动播放 |

### 9.3 `ai_voxcpm_cli.py` 独立 CLI 参数

**基础：**

| 参数 | 默认 | 说明 |
|------|------|------|
| `--server` | `http://192.168.199.7:8808` | server 地址（可用环境变量 `VOXCPM_SERVER`） |
| `--port` | None | 覆盖 server 端口 |
| `--api-name` | `/generate` | Gradio API 名称 |
| `--backend` | auto | auto / gradio / http |

**内容：**

| 参数 | 默认 | 说明 |
|------|------|------|
| `--text` | 内置默认文本 | 目标文本 |
| `--control` | `""` | Control Instruction |
| `--ref` | None | 参考音频路径 |
| **`--voice-id`** | `""` | **服务端音色库 ID；与 `--ref` 同时给时 `--ref` 优先** |
| `--transcript` | `""` | 参考音频 transcript（极致克隆用） |
| `--ultimate` | off | 开启极致克隆模式 |

**生成参数：**

| 参数 | 默认 | 说明 |
|------|------|------|
| `--cfg` | 2.0 | CFG 引导强度 |
| `--steps` | 10 | LocDiT 步数 |
| `--normalize` | off | 文本规范化 |
| `--denoise` | off | 参考音频降噪 |
| `--seed` | None | 固定 seed |
| `--random-seed` | off | 每次请求前随机 seed |
| `--out-dir` | `outputs` | 音频输出目录 |
| `--no-play` | off | 不自动播放 |

**流式相关：**

| 参数 | 默认 | 说明 |
|------|------|------|
| `--no-stream` | off | 禁用流式拆句 |
| `--max-chars` | 40 | 每段最大字符数 |
| `--stream-workers` | 4 | 并发生成线程数 |
| `--gapless` | off | 段间拼接 |

### 9.4 示例组合

```bash
# 音色库 + 风格控制 + 固定 seed
python3 ai_voxcpm_cli.py --voice-id female --control "轻快" --text "你好" --seed 42

# 音色库 + 长文本流式 + gapless
python3 ai_voxcpm_cli.py --voice-id eric --text "第一句。第二句。第三句。" --gapless --stream-workers 4

# 音色库 ID 拼错 → 友好提示
python3 ai_voxcpm_cli.py --voice-id doesnotexist --text "测试"
# 输出：
# ❌ 服务端音色库中找不到 voice_id='doesnotexist'。
#    请检查拼写，或在服务端运行：  voxcpm voice list
#    原始错误：...

# ref + voice-id 同时给 → 警告 + 走 clone
python3 ai_voxcpm_cli.py --voice-id female --ref ./robot.wav --text "测试"
# 输出：
# ⚠️  检测到 reference_wav 与 voice_id 同时存在，按服务器规则 reference_wav 优先，voice_id='female' 将被忽略。
# （走 clone 路径生成）

# 交互 + 流式 + 默认 TTS（female 音色库）
python3 ai_chat_cli.py --stream

# 一次性提问 + DeepSeek + 指定音色
python3 ai_chat_cli.py -p deepseek --tts-profile eric_lib "讲一个冷笑话"
```

---

## 10. REPL 交互模式

不带参数运行 `python3 ai_voxcpm_cli.py` 会进入 REPL：

```
===== VoxCPM2 交互式客户端 =====
server = http://192.168.199.7:8808  api = /generate
输入 :h 查看帮助，:q 退出。

voxcpm>
```

### 10.1 可用命令

```
:h, :help            显示帮助
:mode                选择模式（design / clone / ultimate / library）
:ref <path>          设置参考音频路径（设置后会清空 voice_id；空字符串清除）
:text <string>       设置目标文本
:ctrl <string>       设置 control instruction
:trans <string>      设置参考音频 transcript（极致克隆用）
:cfg <float>         设置 CFG（1.0–3.0）
:steps <int>         设置 LocDiT 步数
:norm on|off         文本规范化
:denoise on|off      参考音频降噪
:seed <int|rand>     设置随机 seed 或固定 seed
:run                 使用当前参数生成
:show                显示当前参数
:server <url>        临时切换 server（重建后端）
:q, :quit            退出
```

### 10.2 `:mode` 菜单

```
voxcpm> :mode

请选择生成模式：
  1) Voice Design         （仅 Control Instruction）
  2) Controllable Cloning （参考音频 + 可选 Control Instruction）
  3) Ultimate Cloning     （参考音频 + 手动输入 transcript）
  4) Voice Library        （使用服务端已注册的音色 ID）
输入 1/2/3/4 [1]:
```

选 4 后依次 prompt 用户输入 `Voice ID`、`Control Instruction`、`Target Text`：

```
输入 1/2/3/4 [1]: 4
Voice ID（服务端已注册的音色 ID）: female
Control Instruction（可选）: 
Target Text: 你好，我是音色库模式。
是否调整高级参数 (y/n) [n]: 
```

### 10.3 `:show` 显示当前参数

```
voxcpm> :show
  当前参数：mode=Voice Library, cfg=2.0, steps=10, normalize=False, denoise=False, random_seed=True, ref=-, voice_id=female
  text      = '你好，我是音色库模式。'
  control   = ''
  ref       = None
  voice_id  = 'female'
  effective = 'female'
  ultimate  = False
  transcript= ''
  cfg       = 2.0
  steps     = 10
  normalize = False
  denoise   = False
  seed      = random
```

注意 `effective` 字段——它是客户端裁决后的实际值：

- 当 `ref` 非空时，`effective = ''`（服务器会走 clone 路径，`voice_id` 被忽略）。
- 当 `ref` 为空时，`effective = voice_id`。

### 10.4 模式切换时的互斥清空

| 从 → 到 | 行为 |
|---------|------|
| 任意 → `library` | 清空 `reference_wav`、`prompt_text`、`use_ultimate_cloning` |
| 任意 → `design` | 清空 `reference_wav`、`voice_id`、`prompt_text`、`use_ultimate_cloning` |
| 任意 → `clone` | 清空 `voice_id`、`prompt_text`、`use_ultimate_cloning` |
| 任意 → `ultimate` | 清空 `voice_id`、`control_instruction` |
| `:ref <path>` 设置非空 | 清空 `voice_id` |

---

## 11. 切分规则与低延迟原理

### 11.1 增量切分规则（流式模式）

`IncrementalSplitter` 每收到一个 LLM delta，追加到内部缓冲区，然后按下面顺序尝试切分：

1. **句末标点或换行** → 立即切  
   字符集：`。！？.!?` 和 `\n`

2. **缓冲区长度 > N 且当前字符是逗号/分号** → 切  
   字符集：`，,、；;`  
   这里的 `N` 就是 `split.comma_threshold`。

3. **`flush()` 时（本轮回复结束）**  
   把剩余缓冲文本交给 `split_text_for_streaming` 兜底切分（含硬切长句）。

### 11.2 三种策略对比

| 策略 | 首段延迟 | 段内自然度 |
|------|---------|-----------|
| 仅句末切 | 高（等一个整句） | 最好 |
| 固定字数硬切 | 最低 | 最差，可能断在词中间 |
| 句末 + 逗号辅助（本项目） | 低 | 良好 |

**首段**：一般在遇到第一个 `，`（且长度已 > N）或第一个 `。` 时就会立即生成并播放，明显快于等整段。

**后续段**：因为生成是并行的，后面几段可能在首段还没播完时就生成好了；gapless 会把这些"已就绪的连续段"合并，用一个播放器实例一次播完，消除停顿。

### 11.3 N 的调参建议

| 场景 | 建议 N |
|------|--------|
| 想更快出声、能接受偶尔从句断裂 | 8 ~ 12 |
| 均衡（默认） | 20 |
| 更自然、能等一会儿 | 30 ~ 40 |

可用命令行临时比较：

```bash
python3 ai_chat_cli.py --tts-comma-threshold 8  --stream "同一句话"
python3 ai_chat_cli.py --tts-comma-threshold 30 --stream "同一句话"
```

---

## 12. 运行时行为说明

### 12.1 文字与语音的关系

**并行、互不阻塞。**

- 文字：在主流程中 `print(delta, end="", flush=True)`。
- 语音：切分后的段落 push 到 `_executor`，生成在后台线程，播放由 `_play_thread` 负责。

因此，即使某段语音还在合成/播放，LLM 的文字输出不会停止。

### 12.2 交互模式下的打断行为

用户在新一轮语音还在播时敲下一句，程序行为是：

1. 旧语音**继续播完**（`_play_thread` 不停）。
2. 主流程立即开始新一轮 LLM 请求。
3. 新一轮的音频会被排队到旧轮之后（同一 `_play_q` FIFO）。

效果：语音与新对话并行，不牺牲任何一方的完整性。

### 12.3 多轮音色一致

- `TTSRuntime` 在进程启动时确定一个全局 `seed`。
- 所有 `TTSSession` 都使用这个 `seed`。
- 所以每一轮、每一段都是同一个"说话人"，不会忽男忽女。

如果想每次运行都固定（可复现），把 `tts_config.json` 的 `seed` 设成具体整数，例如 `12345`。

### 12.4 gapless 拼接的边界

- 只在同一 `TTSSession` 内连续就绪的段之间拼接。
- **不跨轮拼接**：新一轮开始会开启一个新的 batch，避免把不同语义的内容粘在一起。
- 拼接优先用 `ffmpeg -f concat -c copy`；失败会尝试 `-c:a pcm_s16le` 重编码；再失败则退回纯 Python wave 拼接；再失败则退回逐段播放。

### 12.5 音色库模式的特殊行为

- **`denoise` / `normalize` 在音色库模式下不生效**：因为服务器 `generate_from_voice_library` 直接调用 `_generate_with_prompt_cache`，绕过 `core.VoxCPM._generate`。如需要降噪，应在服务端注册音色前用降噪后的音频。
- **`control_instruction` 仍可用**：音色库模式支持 `(control)text` 前缀注入，与其他模式一致。
- **`voice_id` 一旦设错**：会在生成时返回，客户端会展示友好提示（见 9.4 示例）。

### 12.6 退出时的行为

交互模式下输入 `exit` / `quit` / `:q` 结束会话。

`main` 的 `finally` 会调用 `runtime.shutdown(wait=True)`：

1. 等待收集池、生成池里的任务全部完成；
2. 给播放线程发 `SENTINEL` 并 join；
3. 清理 `.gapless_tmp` 目录。

因此在退出前，会看到 `🔊 等待语音播放完成…`。

---

## 13. 常见问题 FAQ

**Q1. 只想用文字对话，不想装 TTS 相关依赖怎么办？**

直接加 `--no-tts`，或删除 `./tts_config.json`。此时 `ai_chat_cli.py` 不会导入 `ai_voxcpm_cli`，行为与旧版 `chat_cli.py` 完全一致。

**Q2. 配置文件存在，但我不想启用 TTS。**

- 临时：`--no-tts`
- 持久：把 `enabled` 改成 `false`

**Q3. `--tts` 和 `--no-tts` 同时给会怎样？**

参数解析用 `argparse.add_mutually_exclusive_group`，同时给会直接报错退出。

**Q4. 首段语音为什么还是慢？**

可能原因：

- LLM 服务商首 token 延迟高（换 `--provider` 或换模型试）。
- N 太大，第一个逗号出现得晚 → `--tts-comma-threshold 8`。
- TTS server 本身单次推理慢 → 降低 `dit_steps`（如 8）。
- `gradio_client` 未装，走了 HTTP+SSE，握手略慢 → `pip install gradio_client`。

**Q5. 语音听起来忽快忽慢/音色不一致？**

- 确认 `seed` 没有每次随机：设一个固定整数。
- 确认 profile 没有混用：`--tts-profile` 只指定一个。
- `control_instruction` 不要一行一个风格地反复变。

**Q6. 报 `Reference audio not found`？**

`clone` / `ultimate` 模式下 `reference_wav` 路径相对于进程工作目录解析。建议用绝对路径，或确认启动目录。

**Q7. 拼接失败，退回逐段播放。**

说明相邻段的 wav 参数（采样率/位深/声道）不一致。可装 `ffmpeg` 让拼接更鲁棒：

```bash
sudo apt install ffmpeg
```

**Q8. 输出目录乱了，怎么清理？**

```bash
rm -rf outputs/
```

程序下次启动会自动重建。`ai_voxcpm_cli.py` 的一次性命令模式启动时也会清理旧的 `voxcpm_*.wav`（`_cleanup_old_outputs`），但只针对该前缀，不动其他文件。

**Q9. 如何完全静默（只落盘、不播）？**

`--tts-no-play`，或在配置里设 `"auto_play": false`。

**Q10. 想接别的 LLM 服务商。**

在 `ai_chat_cli.py` 顶部的 `PROVIDERS` 字典里加一项：

```python
"myservice": {
    "name": "我的服务",
    "base_url": "https://api.example.com/v1",
    "default_model": "model-x",
    "api_key_env": "MY_API_KEY",
    "params": { ... },
}
```

然后 `-p myservice` 即可。

**Q11. 怎么查看服务端有哪些可用音色？**

在服务端运行：

```bash
voxcpm voice list
```

或者打开服务端的 Gradio UI 查看"🗂️ 音色库"下拉框。

客户端本身不维护音色列表；若填错 `voice_id`，生成时会看到友好提示并附带原始错误。

**Q12. `voice_id` 和 `reference_wav` 同时配置了会怎样？**

按服务器规则 **`reference_wav` 优先**，`voice_id` 被忽略。

客户端会在提交前打印一行警告，说明哪个 `voice_id` 被忽略了。REPL 模式下 `:ref <path>` 会自动清空 `voice_id`，避免这种歧义。

**Q13. Voice Library 模式下 `denoise` 为什么不生效？**

因为音色库是 A 模式（reference-only），服务器直接调用 `_generate_with_prompt_cache`，不经过 `core.VoxCPM._generate` 的降噪链路。

如需降噪，请在**注册音色前**用降噪后的音频：

```bash
# 服务端可以用 ffmpeg 或其他工具先降噪
voxcpm voice register --id female --audio /path/to/female_ref_denoised.wav
```

**Q14. 服务端重建了音色库，客户端需要重启吗？**

不需要。服务端 `VoiceLibrary` 通过 manifest mtime 检测变化，客户端每次提交都会走最新配置。

**Q15. 音色库里的音色和客户端 `voices/` 目录是什么关系？**

**完全无关**。客户端 `voices/` 目录是本地参考音频（clone / ultimate 用），服务端 `voices/` 是音色库（library 用），两者独立、可能在不同机器上。

---

## 14. 故障排查

### 14.1 查看后端实际用的是哪个

启动日志里会打印：

```
🔊 TTS 已启用：server=... profile=... N=... gapless=... play=... seed=...
```

如果看到 `⚠️ gradio_client 不可用（...），回退到 HTTP/SSE。`，说明走了回退路径。

### 14.2 强制指定后端

```bash
# 只用 gradio_client，初始化失败直接报错退出
python3 ai_chat_cli.py --tts-profile female_lib --stream "..."
# 在 tts_config.json 里设置 backend
{
  "backend": "gradio"
}
```

可选值：`auto` / `gradio` / `http`。

### 14.3 打开 debug 输出

`ai_voxcpm_cli.py` 的 `_parse_audio_info` 里有一行：

```python
print(f"[debug] audio_info = {audio_info!r}")
```

会打印每次 server 返回的音频结构。排查路径/URL 问题时很有用。正式环境可注释掉。

### 14.4 定位"卡在等待语音播放完成"

大概率是某一段生成失败但没抛错（比如 server 返回了空路径）。看到类似日志：

```
⚠️  TTS 生成失败 [0001-003]：...
```

定位到具体段落后，检查：

- server 是否仍在运行
- `dit_steps` 是否过大
- 该段文本是否过长（可临时把 N 调小、`--no-stream` 切回整段试）
- **若用了 `voice_id`，确认服务端 `voxcpm voice list` 里确实存在该 ID**

### 14.5 音色库报错排查

**现象**：客户端打印 `❌ 服务端音色库中找不到 voice_id='xxx'`。

**排查**：

1. 服务端运行 `voxcpm voice list`，确认 ID 拼写。
2. 若服务端音色库为空，先注册：`voxcpm voice register --id xxx --audio ...`。
3. 若服务端刚注册完，客户端再试一次——热加载是即时的，无需重启。
4. 检查 `manifest.json` 与 `.safetensors` 是否成对存在：

```bash
ls server/voices/
cat server/voices/manifest.json | python3 -m json.tool
```

**现象**：客户端打印 `❌ 服务端音色库未就绪`。

**排查**：服务端模型尚未加载（首次注册/生成会自动加载），或 `voices/` 目录不存在。查看服务端日志确认。

### 14.6 播放器问题

音频文件生成了但听不到声音：

```bash
# 手动播一下生成的 wav
ffplay outputs/voxcpm_0001_001_*.wav
```

若能播 → 说明是播放器选择问题，装 `ffmpeg` 最省事。

```bash
sudo apt install ffmpeg
```

### 14.7 网络与超时

`HttpSseBackend` 默认超时 600s。如果 server 侧单次推理超过这个值（极罕见），会被中断。可在构造 backend 时调大 `timeout` 参数（需要改代码）。

---

## 15. 扩展与二次开发

### 15.1 作为模块复用

在你自己脚本里：

```python
from ai_voxcpm_cli import (
    GenRequest, TTSRuntime, IncrementalSplitter,
    _build_backend,
)

backend = _build_backend("http://127.0.0.1:8808", "/generate", "auto")
runtime = TTSRuntime(backend, out_dir="./outputs",
                     auto_play=True, gapless=True, seed=42)
runtime.start()

# 音色库模式
base_req = GenRequest(
    voice_id="female",
    control_instruction="沉稳男声",
    cfg_value=2.0, dit_steps=10,
)

session = runtime.new_session(base_req)
splitter = IncrementalSplitter(comma_threshold=20, min_chars=4)

# 模拟流式输入
for token in ["你好，", "欢迎使用", "本系统。"]:
    for seg in splitter.feed(token):
        session.push(seg)
for seg in splitter.flush():
    session.push(seg)
session.finish()

runtime.shutdown(wait=True)
```

### 15.2 换播放器

`ai_voxcpm_cli.py` 顶部：

```python
PLAYERS = ["ffplay", "afplay", "aplay", "paplay"]
```

把想优先用的排到前面即可。

### 15.3 换切分字符集

```python
_SENT_END_CHARS = set("。！？.!?")
_COMMA_CHARS    = set("，,、；;")
_NEWLINE_CHARS  = set("\n")
```

比如想让 `：` 也触发逗号级切分，就加到 `_COMMA_CHARS`。

### 15.4 让"新对话打断旧语音"

当前策略是"不打断"。若想改成打断：

1. 在 `TTSRuntime._play_worker` 里加一个 `threading.Event` 作为停止信号；
2. `new_session` 时 `set()` 该事件；
3. 播放器子进程用 `Popen` 起，事件触发时 `terminate()`。

（注意：`subprocess.run` 会阻塞直到播完，不适合做打断，要改成 `Popen` 并轮询。）

### 15.5 多语言切分

英文场景下 `。！？` 基本用不到，实际以 `. ! ?` 为主，本项目已包含。日文/韩文的句末标点（`。！？` 通用）也在集合内。其他语言如阿拉伯语加 `؟` 到 `_SENT_END_CHARS` 即可。

### 15.6 新增音色 profile

在 `tts_config.json` 里追加一个 profile：

```json
"my_voice": {
  "mode": "library",
  "voice_id": "my_voice_id",
  "control_instruction": "",
  "reference_wav": null,
  "transcript": "",
  "cfg_value": 2.0,
  "dit_steps": 10,
  "do_normalize": false,
  "denoise": false
}
```

然后 `--tts-profile my_voice` 使用。

---

## 16. 附录：环境变量与默认值

### 16.1 环境变量

| 变量 | 用于 | 说明 |
|------|------|------|
| `ZHIPUAI_API_KEY` | 智谱 | `-p zhipu` 时读取 |
| `DEEPSEEK_API_KEY` | DeepSeek | `-p deepseek` 时读取 |
| `VOXCPM_SERVER` | `ai_voxcpm_cli.py` | 覆盖默认 server 地址 |
| `VOXCPM_VOICES_DIR` | 服务端 `app.py` | 服务端音色库目录，默认 `./voices` |

### 16.2 内置默认值（`ai_voxcpm_cli.py`）

| 常量 | 值 |
|------|------|
| `DEFAULT_SERVER` | `http://192.168.199.7:8808` |
| `DEFAULT_API_NAME` | `/generate` |
| `DEFAULT_CFG` | 2.0 |
| `DEFAULT_DIT_STEPS` | 10 |
| `DEFAULT_OUT_DIR` | `outputs` |
| `DEFAULT_MAX_CHARS` | 40 |
| `DEFAULT_MIN_CHARS` | 4 |
| `DEFAULT_STREAM_WORKERS` | 4 |
| `GENERATE_INPUT_ORDER` | 11 项（含 `voice_id`） |

### 16.3 内置默认值（`ai_chat_cli.py`）

| 常量 | 值 |
|------|------|
| `DEFAULT_PROVIDER` | `zhipu` |
| `DEFAULT_SYSTEM` | `我是灵境机器人智能助手，请简洁、准确地回答问题。` |
| `DEFAULT_TTS_CONFIG` | `./tts_config.json` |

### 16.4 四种运行模式速查

| mode | LLM 侧 | 切分发生点 | TTS 侧 | 服务端走哪条分支 |
|------|--------|-----------|--------|------------------|
| `design` | 一次性返回 | 返回后 `split_text_for_streaming` | 并发生成、顺序播放 | `_generate` 零样本 |
| `clone` | 逐 token | 每 token `splitter.feed` | 同上 | `_generate` reference |
| `ultimate` | 逐 token | 同上 | 同上 | `_generate` continuation |
| `library` | 逐 token | 同上 | 同上 | `_generate_with_prompt_cache` |

---

## 许可与致谢

- LLM 服务：[智谱 GLM](https://open.bigmodel.cn/) / [DeepSeek](https://www.deepseek.com/)
- TTS 模型：[VoxCPM2](https://github.com/OpenBMB/VoxCPM)（ModelBest）
- 通信：`gradio_client` 或原生 HTTP + SSE
