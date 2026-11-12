#!/usr/bin/env bash
# =============================================================================
# RK3588 Voice Robot 一键部署脚本（在 RK3588 板上执行）
#
# 用法：
#   bash scripts/deploy.sh              # 基础部署（apt + venv + pip + 模型 + env 文件 + 自检）
#   bash scripts/deploy.sh --service    # 基础部署后再安装/更新 systemd 服务并启动
#   bash scripts/deploy.sh --skip-apt   # 跳过 apt 安装（离线或已装好时）
#
# 特性：幂等。已完成的步骤自动跳过，重复执行安全。
# 不做的事：不修改你已有的配置值（只打印提醒），不动 main.py / config.py / tts_config.json。
# =============================================================================
set -uo pipefail
# 注意：不使用 set -e，改为关键步骤显式检查，便于汇总所有问题而不是遇错即停。

# ---------- 参数解析 ----------
WITH_SERVICE=0
SKIP_APT=0
for arg in "$@"; do
  case "$arg" in
    --service)  WITH_SERVICE=1 ;;
    --skip-apt) SKIP_APT=1 ;;
    -h|--help)
      sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "未知参数：$arg（可用：--service / --skip-apt / --help）" >&2
      exit 1
      ;;
  esac
done

# ---------- 输出工具 ----------
if [ -t 1 ]; then
  C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YEL=$'\033[33m'; C_CYA=$'\033[36m'; C_RST=$'\033[0m'
else
  C_RED=''; C_GRN=''; C_YEL=''; C_CYA=''; C_RST=''
fi
step()  { echo; echo "${C_CYA}==> $*${C_RST}"; }
ok()    { echo "  ${C_GRN}[OK]${C_RST} $*"; }
warn()  { echo "  ${C_YEL}[警告]${C_RST} $*"; }
fail()  { echo "  ${C_RED}[失败]${C_RST} $*"; }
info()  { echo "        $*"; }

WARNINGS=()
FAILED=0

# ---------- 0. 环境预检 ----------
step "0/9 环境预检"
ARCH="$(uname -m)"
KERNEL="$(uname -s)"
if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then
  ok "架构：$ARCH"
else
  warn "当前架构是 $ARCH，不是 aarch64 —— 本项目面向 RK3588（aarch64），继续但可能无法运行"
  WARNINGS+=("架构为 $ARCH，非 aarch64")
fi
if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  info "系统：${PRETTY_NAME:-unknown}（$KERNEL）"
  if ! echo "${ID:-}" | grep -qi ubuntu; then
    warn "非 Ubuntu 系统（ID=${ID:-unknown}），apt 包名可能需要调整"
    WARNINGS+=("非 Ubuntu 系统")
  fi
else
  warn "读不到 /etc/os-release，无法识别发行版"
fi

# 是否 root
if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
  IS_ROOT=1
  info "当前用户：root"
else
  IS_ROOT=0
  if command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
  else
    SUDO=""
    warn "当前非 root 且未找到 sudo，apt/服务安装步骤将失败"
  fi
  info "当前用户：$(id -un)（apt 与服务安装将使用 sudo）"
fi

# 必要命令
MISSING_CMD=()
for c in python3 pip3 wget; do
  if command -v "$c" >/dev/null 2>&1; then
    ok "命令存在：$c"
  else
    warn "缺少命令：$c"
    MISSING_CMD+=("$c")
  fi
done
if [ "${#MISSING_CMD[@]}" -gt 0 ]; then
  WARNINGS+=("缺少命令：${MISSING_CMD[*]}")
fi
if ! command -v python3 >/dev/null 2>&1; then
  fail "缺少 python3，无法继续。请先执行：${SUDO:+sudo }apt install -y python3 python3-venv python3-pip"
  exit 1
fi

# ---------- 1. 定位项目根 ----------
step "1/9 定位项目根目录"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || { fail "无法进入 $ROOT"; exit 1; }
ok "项目根：$ROOT"
if [ ! -f "$ROOT/main.py" ]; then
  fail "$ROOT/main.py 不存在，请在项目目录内执行本脚本"
  exit 1
fi

# ---------- 2. 系统依赖 ----------
step "2/9 安装系统依赖"
APT_PKGS="python3-venv python3-pip alsa-utils pulseaudio-utils wget ffmpeg mosquitto-clients portaudio19-dev libsndfile1 libgomp1"
if [ "$SKIP_APT" -eq 1 ]; then
  info "已指定 --skip-apt，跳过（以下包请自行确认已安装）："
  info "  $APT_PKGS"
else
  if ! command -v apt-get >/dev/null 2>&1; then
    warn "没有 apt-get，跳过系统依赖安装"
    WARNINGS+=("未安装 apt 依赖（无 apt-get）")
  else
    info "需要：$APT_PKGS"
    if ${SUDO} apt-get update -y >/dev/null 2>&1; then
      ok "apt-get update"
    else
      warn "apt-get update 失败（可先检查软件源 / 网络）"
      WARNINGS+=("apt-get update 失败")
    fi
    if ${SUDO} apt-get install -y $APT_PKGS; then
      ok "系统依赖安装完成"
    else
      warn "部分系统依赖安装失败，请查看上方 apt 输出"
      WARNINGS+=("部分 apt 依赖安装失败")
    fi
  fi
fi
for c in ffmpeg aplay mosquitto_pub; do
  if command -v "$c" >/dev/null 2>&1; then
    ok "命令存在：$c"
  else
    case "$c" in
      ffmpeg) MISS_DESC="批量播放合并" ;;
      aplay) MISS_DESC="音频设备排查" ;;
      mosquitto_pub) MISS_DESC="MQTT 动作下发" ;;
      *) MISS_DESC="相关功能" ;;
    esac
    warn "未找到 $c（影响：$MISS_DESC）"
    WARNINGS+=("缺少 $c")
  fi
done

# ---------- 3. venv + pip ----------
step "3/9 创建虚拟环境并安装 Python 依赖"
VENV="$ROOT/venv"
# 兼容两种布局：POSIX（bin/python）与 Windows（Scripts/python.exe）
PY="$VENV/bin/python"
[ -x "$PY" ] || { [ -f "$VENV/Scripts/python.exe" ] && PY="$VENV/Scripts/python.exe"; }
if [ -x "$PY" ]; then
  ok "venv 已存在，复用：$VENV"
  info "版本：$("$PY" -V 2>&1)"
else
  info "创建 venv：$VENV"
  if python3 -m venv "$VENV"; then
    ok "venv 创建完成"
  else
    fail "venv 创建失败（Ubuntu 上通常是缺 python3-venv，先 apt install python3-venv）"
    exit 1
  fi
fi
if [ ! -x "$PY" ]; then
  fail "找不到解释器 $PY，venv 不完整，请删除 $VENV 后重跑"
  exit 1
fi

info "升级 pip ..."
"$PY" -m pip install -U pip >/dev/null 2>&1 && ok "pip 已升级" || warn "pip 升级失败（不影响后续，继续）"

PIP_LOG="$(mktemp)"
info "安装 requirements.txt（清华源）..."
if "$PY" -m pip install -r "$ROOT/requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple 2>&1 | tee "$PIP_LOG"; then
  ok "依赖安装完成（清华源）"
else
  warn "清华源安装失败，回退官方源重试一次..."
  if "$PY" -m pip install -r "$ROOT/requirements.txt" 2>&1 | tee "$PIP_LOG"; then
    ok "依赖安装完成（官方源）"
  else
    fail "pip 依赖安装失败，请检查网络或 requirements.txt"
    FAILED=1
  fi
fi
rm -f "$PIP_LOG"

# ---------- 4. Python 依赖自检 ----------
step "4/9 Python 依赖自检"
if "$PY" -c "import sherpa_onnx, sounddevice, numpy, dotenv, requests, openai" 2>/dev/null; then
  ok "关键依赖导入正常：sherpa_onnx, sounddevice, numpy, dotenv, requests, openai"
else
  fail "依赖导入失败，逐项诊断："
  for m in sherpa_onnx sounddevice numpy dotenv requests openai; do
    if "$PY" -c "import $m" 2>/dev/null; then
      ok "$m"
    else
      fail "$m 导入失败"
      FAILED=1
    fi
  done
fi

# ---------- 5. 模型下载 ----------
step "5/9 下载 sherpa-onnx 模型"
if [ -f "$ROOT/scripts/download_models.sh" ]; then
  if bash "$ROOT/scripts/download_models.sh"; then
    ok "模型下载/校验完成"
  else
    warn "模型下载脚本返回非 0，可能部分模型缺失（可重跑本脚本断点续传）"
    WARNINGS+=("模型可能不完整")
  fi
else
  warn "未找到 scripts/download_models.sh，请手动把模型放到 models/ 下"
  WARNINGS+=("缺少 download_models.sh")
fi

# ---------- 6. 配置文件 ----------
step "6/9 检查配置文件 robot.env"
ENV_FILE="$ROOT/robot.env"
ENV_EXAMPLE="$ROOT/robot.env.example"
if [ -f "$ENV_FILE" ]; then
  ok "robot.env 已存在，保留不改"
else
  if [ -f "$ENV_EXAMPLE" ]; then
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    ok "已从 robot.env.example 复制生成 robot.env"
    warn "必须编辑 $ENV_FILE 填入真实 API Key"
    WARNINGS+=("robot.env 为示例副本，需填 API Key")
  else
    warn "既没有 robot.env 也没有 robot.env.example"
    WARNINGS+=("缺少配置文件")
  fi
fi
if [ -f "$ENV_FILE" ]; then
  # 简单检查是否仍为示例占位值
  if grep -qE '^DEEPSEEK_API_KEY=(sk-x{4,}|)$' "$ENV_FILE" 2>/dev/null; then
    warn "DEEPSEEK_API_KEY 未填写（仍为示例值或空）"
    WARNINGS+=("DEEPSEEK_API_KEY 未填写")
  fi
  if grep -qE '^TEXT_INPUT_API_KEY=(change-me-to-a-strong-secret|)$' "$ENV_FILE" 2>/dev/null; then
    warn "TEXT_INPUT_API_KEY 仍为示例值 / 为空 —— HTTP 接口会拒绝所有请求"
    WARNINGS+=("TEXT_INPUT_API_KEY 未设置")
  fi
fi

# ---------- 7. 硬编码地址提醒 ----------
step "7/9 硬编码地址提醒（本脚本不修改，请自行确认）"
print_kv() { printf "        %-28s %s\n" "$1" "$2"; }

if [ -f "$ROOT/send_mqtt.sh" ]; then
  MQTT_HOST_V="$(grep -m1 '^MQTT_HOST=' "$ROOT/send_mqtt.sh" | cut -d= -f2- | tr -d '"'"'"' ')"
  MQTT_PORT_V="$(grep -m1 '^MQTT_PORT=' "$ROOT/send_mqtt.sh" | cut -d= -f2- | tr -d '"'"'"' ')"
  print_kv "send_mqtt.sh MQTT_HOST:" "${MQTT_HOST_V:-未找到}"
  print_kv "send_mqtt.sh MQTT_PORT:" "${MQTT_PORT_V:-未找到}"
fi

if [ -f "$ROOT/tts_config.json" ]; then
  TTS_SERVER_V="$("$PY" - "$ROOT/tts_config.json" <<'PYEOF' 2>/dev/null
import json,sys
try:
    d=json.load(open(sys.argv[1],encoding="utf-8"))
    print(d.get("server",""))
except Exception:
    print("<解析失败>")
PYEOF
)"
  print_kv "tts_config.json server:" "${TTS_SERVER_V:-<空>}"
fi

if [ -f "$ROOT/config.py" ]; then
  TR_URL_V="$(grep -m1 '^TRANSLATE_URL' "$ROOT/config.py" | cut -d= -f2- | tr -d '"'"'"' ')"
  print_kv "config.py TRANSLATE_URL:" "${TR_URL_V:-未找到}"
fi
info "以上地址若与新环境不符，请手动修改对应文件。"

# ---------- 8. systemd 服务 ----------
step "8/9 systemd 服务"
UNIT_SRC="$ROOT/voice-robot.service"
UNIT_DST="/etc/systemd/system/voice-robot.service"

if [ "$WITH_SERVICE" -eq 0 ]; then
  info "未指定 --service，跳过服务安装。"
  info "如需安装：bash scripts/deploy.sh --service"
else
  if [ ! -f "$UNIT_SRC" ]; then
    fail "未找到 $UNIT_SRC，无法安装服务"
    FAILED=1
  elif ! command -v systemctl >/dev/null 2>&1; then
    fail "本机没有 systemctl，无法安装 systemd 服务"
    FAILED=1
  else
    # 旧服务清理提示
    if [ -f /etc/systemd/system/robot.service ]; then
      warn "检测到旧服务 /etc/systemd/system/robot.service，建议停用并删除："
      info "  ${SUDO} systemctl disable --now robot.service && ${SUDO} rm -f /etc/systemd/system/robot.service"
      WARNINGS+=("存在旧 robot.service，需手工清理")
    fi

    if ${SUDO} cp "$UNIT_SRC" "$UNIT_DST"; then
      ok "已复制到 $UNIT_DST"
    else
      fail "复制 service 文件失败"
      FAILED=1
    fi

    ${SUDO} systemctl daemon-reload && ok "daemon-reload 完成" || warn "daemon-reload 失败"

    # robot.env 仍为示例原样时给出警告
    if [ -f "$ENV_FILE" ] && [ -f "$ENV_EXAMPLE" ] && cmp -s "$ENV_FILE" "$ENV_EXAMPLE"; then
      warn "robot.env 与 robot.env.example 完全相同（未填 API Key），服务可能无法正常对话"
      WARNINGS+=("robot.env 未填写")
    fi

    if systemctl is-active --quiet voice-robot; then
      info "服务已在运行，执行 restart"
      if ${SUDO} systemctl restart voice-robot; then
        ok "服务已重启"
      else
        fail "服务重启失败"
        FAILED=1
      fi
    else
      if ${SUDO} systemctl enable --now voice-robot; then
        ok "服务已 enable --now"
      else
        fail "服务启用失败（可能是 venv 路径 / 权限问题，见下方日志）"
        FAILED=1
      fi
    fi

    sleep 2
    if systemctl is-active --quiet voice-robot; then
      ok "服务状态：active"
      info "最近的日志（Ctrl+C 退出）：${SUDO} journalctl -u voice-robot -f"
    else
      fail "服务未处于 active 状态，查看日志定位："
      info "  ${SUDO} journalctl -u voice-robot -n 50 --no-pager"
      FAILED=1
    fi
  fi
fi

# ---------- 9. 收尾汇总 ----------
step "9/9 部署收尾"
echo "  项目目录：$ROOT"
for f in voice-robot.service scripts/deploy.sh scripts/download_models.sh; do
  [ -f "$ROOT/$f" ] && ok "存在 $f"
done

if [ "${#WARNINGS[@]}" -gt 0 ]; then
  echo
  echo "  ${C_YEL}待处理事项：${C_RST}"
  for w in "${WARNINGS[@]}"; do
    echo "    - $w"
  done
fi

cat <<'TODO'

  手工待办：
    1. 编辑 robot.env，填入 DEEPSEEK_API_KEY / TEXT_INPUT_API_KEY 等真实值
    2. 确认 send_mqtt.sh 的 MQTT_HOST、tts_config.json 的 server、
       config.py 的 TRANSLATE_URL 与当前网络一致
    3. 建议分层验证（依次排查，定位更快）：
         venv/bin/python main.py --text "今天北京天气怎么样"     # 只验 LLM 链路
         arecord -d 3 -f cd /tmp/t.wav && aplay /tmp/t.wav      # 验麦克风/扬声器
         venv/bin/python main.py --no-kws                        # 验 ASR + TTS，先跳过唤醒
         bash scripts/deploy.sh --service                        # 最后装服务
TODO

echo
if [ "$FAILED" -ne 0 ]; then
  fail "部署过程中存在失败项，请按上方提示处理"
  exit 1
fi
ok "部署脚本执行完毕"
