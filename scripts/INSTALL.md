# RK3588 语音机器人 安装指南

> 本文件位于 `scripts/` 目录，**所有命令默认在项目根目录** `/opt/rk3588-voice-robot` 下执行。
> 结论先行：推荐按「一键脚本 → 手工配置 → 分层验证 → 装服务」四段推进。
> 其中 `TEXT_INPUT_API_KEY` 与 `DEEPSEEK_API_KEY` 两项必须与代码实际读取的键名严格一致，漏配后系统会静默拒绝请求或用空 Key 调接口。

---

## 一、前置条件核对

| 项 | 要求 | 说明 |
|---|---|---|
| 硬件 | RK3588 开发板 | aarch64 架构 |
| 系统 | Ubuntu 20.04 (aarch64) | 其他发行版 apt 包名需调整 |
| 音频 | USB 麦克风 + 扬声器 | 麦克风会被 `config.py` 自动扫描（优先名称含 `usb` / `nationalchip`） |
| 网络 | 可访问互联网 | LLM API、VoxCPM、模型下载 |
| 网络 | 可访问 MQTT 服务器 | 默认 `192.168.200.30:1883` |

部署前请先确认板上 sudo 可用，并且时区、DNS 正常。

---

## 二、获取代码与目录约定

```bash
sudo mkdir -p /opt/rk3588-voice-robot
sudo chown -R $USER:$USER /opt/rk3588-voice-robot
# 将项目文件拷贝/解压到 /opt/rk3588-voice-robot
cd /opt/rk3588-voice-robot
```

**路径必须为 `/opt/rk3588-voice-robot`**：`voice-robot.service` 的 `WorkingDirectory`、`EnvironmentFile`、`ExecStart` 全部硬编码该路径。`main.py` 在启动时按相对路径解析 `out_dir`（`main.py:215-222`），如果 `WorkingDirectory` 设置错误，VoxCPM 的 wav 监控目录就会指向错误的位置。

---

## 三、一键部署（推荐）

```bash
bash scripts/deploy.sh              # 基础部署
bash scripts/deploy.sh --service    # 基础部署 + 安装 systemd 服务
bash scripts/deploy.sh --skip-apt   # 离线 / 依赖已装好
```

脚本按 9 步幂等执行，可反复重跑：

1. 环境预检（架构、发行版、root/sudo、`python3/pip3/wget`）
2. 定位项目根
3. 安装 `python3-venv python3-pip alsa-utils pulseaudio-utils wget ffmpeg mosquitto-clients portaudio19-dev libsndfile1 libgomp1`
4. 创建 `venv` + `pip install -r requirements.txt`（清华源失败自动回退官方源）
5. 逐项导入自检：`sherpa_onnx, sounddevice, numpy, dotenv, requests, openai`
6. 调用 `scripts/download_models.sh` 下载 4 组模型（断点续传 + 镜像回退 + 完整性校验）
7. `robot.env` 缺失时从示例生成
8. 打印硬编码地址当前值（只提醒不修改）
9. 可选安装服务，并汇总所有待处理项

---

## 四、手工部署（需精确控制每一步时）

```bash
# 1) 系统依赖
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip alsa-utils pulseaudio-utils \
  wget ffmpeg mosquitto-clients portaudio19-dev libsndfile1 libgomp1

# 2) venv
python3 -m venv venv
source venv/bin/activate
pip install -U pip
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 3) 模型
bash scripts/download_models.sh

# 4) 配置
cp robot.env.example robot.env
vim robot.env
```

`download_models.sh` 会下载并校验以下 4 组资产，缺一不可：

- `silero_vad.onnx`（VAD）
- `sherpa-onnx-streaming-paraformer-bilingual-zh-en/`（ASR，需含 `encoder.int8.onnx` / `decoder.int8.onnx` / `tokens.txt`）
- `vits-melo-tts-zh_en/`（本地 TTS）
- `sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/`（KWS，脚本会写入唤醒词 `灵境灵境`）

---

## 五、配置 `robot.env`（关键项）

当前仓库的 `robot.env` 需要按实际情况修正：

| 变量 | 当前值 | 是否必须 | 说明 |
|---|---|---|---|
| `TEXT_INPUT_API_KEY` | `123456` | **必须改** | HTTP 接口鉴权。为空时接口拒绝所有请求；示例值易被猜中 |
| `DEEPSEEK_API_KEY` | `sk-xxxxxxxxxxxxxxxx` | **必须填** | 现为占位符，未填则 `ai_chat_cli.py` 直接报错退出 |
| `ZHIPUAI_API_KEY` | 已配置 | 是（默认服务商） | **注意拼写**：代码读的是 `ZHIPUAI_API_KEY`（`ai_chat_cli.py:30`），写成 `ZHIPU_API_KEY` 会判为未配置 |
| `CHAT_TTS_MODE` | `voxcpm` | 可选 | `voxcpm` 或 `sherpa`；VoxCPM 失败自动降级 |

**默认 LLM 服务商是智谱 GLM**（`ai_chat_cli.py:55`，`DEFAULT_PROVIDER = "zhipu"`，模型 `glm-4-flash`）。`main.py` 调 `ai_chat_cli.py --stream` 时不传 `--provider`，所以**实际走智谱，不是 DeepSeek**。若要改用 DeepSeek，需显式加 `--provider deepseek`。这与你直觉可能不一致，建议先核实。

---

## 六、修改必须核对的硬编码地址

以下三处按新网络环境填写（`deploy.sh` 只打印、不修改）：

| 文件 | 项 | 当前值 |
|---|---|---|
| `send_mqtt.sh` | `MQTT_HOST` / `MQTT_PORT` | `192.168.200.30` / `1883` |
| `tts_config.json` | `server` | `http://192.168.199.7:8808` |
| `config.py` | `TRANSLATE_URL` | `http://192.168.199.7:8788/v1/completions` |

另外 `tts_config.json` 的 `active_profile` 当前为 `female_lib`（`mode: library`，`voice_id: female`），这要求 VoxCPM 服务端已注册该音色；否则语音合成会失败。

**⚠️ 注意 `auto_play` 当前为 `false`**（`tts_config.json:7`）。此值不影响主进程播放——`main.py` 的 VoxCPM 监控线程读取的是 `out_dir`，播放由线程自己的队列驱动，不读 `auto_play`。但若你单独跑 `ai_voxcpm_cli.py` 做验证，该值会导致「生成了但没声音」。

---

## 七、分层验证（务必按序）

```bash
cd /opt/rk3588-voice-robot && source venv/bin/activate

# ① 依赖
python -c "import sherpa_onnx, sounddevice, numpy, dotenv, requests, openai; print('ok')"

# ② 音频设备
aplay -l
arecord -d 3 -f cd /tmp/t.wav && aplay /tmp/t.wav

# ③ 纯文本（只验 LLM，不涉及麦克风）
python main.py --text "今天北京天气怎么样"

# ④ 只验 ASR
python main.py --asr-only

# ⑤ 跳过唤醒，验 ASR + TTS 全链路
python main.py --no-kws

# ⑥ 完整模式
python main.py
```

`main.py` 支持的参数：`--text` / `--http` / `--port` / `--no-kws` / `--chat-tts {voxcpm,sherpa}` / `--asr-mode {sentence,streaming}` / `--asr-only`。

**音频输出机制需了解**：`audio_setup.py` 会按 `usb → es8388 → hdmi → pulse → default` 优先级自动挑选设备，并调用 `pactl` 设默认 sink、取消静音、音量设为 `80%`（可用环境变量 `PULSE_VOLUME` 覆盖）。**以 root 运行 systemd 服务时没有用户态 PulseAudio，`pactl` 调用会失败——代码有异常兜底，只打 warning，随后 ALSA 直连 `/dev/snd/*` 仍可出声。** 这是 `User=root` 可用的前提；如果改用普通用户，必须加入 `audio,video` 组。

---

## 八、安装开机自启服务

```bash
sudo cp voice-robot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now voice-robot
sudo journalctl -u voice-robot -f
```

服务单元关键内容：`User=root`、`WorkingDirectory=/opt/rk3588-voice-robot`、`ExecStart=.../venv/bin/python .../main.py --http --port 8765`、`Restart=on-failure`、`RestartSec=5`。

改用普通用户（四步，缺一不可）：

1. 改 `User=` / `Group=`
2. `sudo usermod -aG audio,video <用户名>`
3. `sudo chown -R <用户名>:<用户名> /opt/rk3588-voice-robot`
4. **用该用户重建 `venv`** —— 否则 `ExecStart` 的解释器路径不存在，服务反复重启失败

若板上残留旧服务，先清理：

```bash
sudo systemctl disable --now robot.service && sudo rm -f /etc/systemd/system/robot.service
```

---

## 九、验证清单

| 检查项 | 命令 | 期望 |
|---|---|---|
| 服务状态 | `systemctl is-active voice-robot` | `active` |
| HTTP 接口 | `curl -H "X-API-Key: <key>" -H "Content-Type: application/json" -d '{"text":"测试"}' http://127.0.0.1:8765/` | 200 |
| 未授权拒绝 | 去掉 `X-API-Key` 再请求 | 401 |
| MQTT | `./send_mqtt.sh '{"cmd":"action","action":"forward","device":"lite3"}'` | `发送成功` |
| 唤醒 | 说「灵境灵境」 | 回复「我在」 |

---

## 十、常见安装失败

| 现象 | 根因 | 处理 |
|---|---|---|
| `venv` 创建失败 | 缺 `python3-venv` | `sudo apt install python3-venv` |
| `pip install sherpa-onnx` 慢/失败 | 网络 | 用清华源；脚本已自动回退官方源 |
| 模型下载全部失败 | GitHub 直连超时、镜像失效 | 改 `download_models.sh` 的 `MIRRORS` 后重跑（`wget -c` 续传） |
| AI 无回应，日志报 Key | `DEEPSEEK_API_KEY` 为占位符 / `ZHIPUAI_API_KEY` 拼写错 | 填真实 Key，注意变量名 |
| HTTP 全部 401 | `TEXT_INPUT_API_KEY` 为空或请求头不一致 | 核对 `robot.env` 与 `X-API-Key` |
| 服务不断重启 | `venv` 路径与 `User=` 不匹配 | 用该用户重建 venv |
| 无声音 | 设备被占用 / sink 错误 | `fuser -v /dev/snd/*`、`aplay -l` |
