"""音频输出设备自动选择。"""
import logging
import os
import subprocess

import sounddevice as sd

log = logging.getLogger("robot")


# 输出设备优先级（按 name 关键词匹配）
OUTPUT_PRIORITY = [
    "usb",              # USB 扬声器
    "es8388",           # 板载 ES8388
    "rockchip,es8388",  # 别名
    "hdmi",             # HDMI
    "pulse",            # PulseAudio 虚拟
    "default",          # 系统默认
]

# sounddevice 关键词 → PulseAudio sink 名关键词
PULSE_SINK_MAP = [
    ("usb", "usb"),
    ("es8388", "es8388"),
    ("hdmi", "hdmi"),
]

# 默认音量（可被环境变量 PULSE_VOLUME 覆盖）
DEFAULT_PULSE_VOLUME = "80%"


def find_output_devices():
    """返回所有有输出通道的 sounddevice 设备。"""
    devices = []
    try:
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_output_channels", 0) > 0:
                devices.append({
                    "index": i,
                    "name": d.get("name", ""),
                    "channels": d.get("max_output_channels", 0),
                })
    except Exception as e:
        log.warning("[Audio] 查询 sounddevice 设备失败：%s", e)
    return devices


def list_pulse_sinks():
    """返回 PulseAudio sink 列表 [(index, name), ...]。"""
    sinks = []
    try:
        result = subprocess.run(
            ["pactl", "list", "short", "sinks"],
            capture_output=True, text=True, timeout=3,
        )
        for line in result.stdout.strip().split("\n"):
            parts = line.split("\t")
            if len(parts) >= 2:
                sinks.append((parts[0], parts[1]))
    except Exception as e:
        log.warning("[Audio] 查询 PulseAudio sink 失败：%s", e)
    return sinks


def find_pulse_sink_for_device(dev_name: str) -> str:
    """根据 sounddevice 设备名找对应的 PulseAudio sink 名。"""
    name_lower = dev_name.lower()
    sinks = list_pulse_sinks()
    for keyword, sink_kw in PULSE_SINK_MAP:
        if keyword in name_lower:
            for _, sink_name in sinks:
                if sink_kw in sink_name.lower():
                    return sink_name
    return ""


def set_pulse_default_sink(sink_name: str) -> bool:
    if not sink_name:
        return False
    try:
        subprocess.run(
            ["pactl", "set-default-sink", sink_name],
            check=True, timeout=3,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    except Exception as e:
        log.warning("[Audio] 设置默认 sink 失败：%s", e)
        return False


def unmute_and_set_volume(sink_name: str, volume: str = None):
    """取消静音并设置 PulseAudio sink 音量。"""
    if not sink_name:
        return
    volume = volume or os.environ.get("PULSE_VOLUME", DEFAULT_PULSE_VOLUME)

    try:
        subprocess.run(
            ["pactl", "set-sink-mute", sink_name, "0"],
            check=False, timeout=3,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["pactl", "set-sink-volume", sink_name, volume],
            check=False, timeout=3,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        log.info("[Audio] 已取消静音并设置音量：%s → %s", sink_name, volume)
    except Exception as e:
        log.warning("[Audio] 设置音量失败：%s", e)


def setup_audio_output() -> str:
    """
    扫描输出设备，按优先级选一个，切 PulseAudio 默认 sink，取消静音并设置音量。
    返回选中的 sink 名（空字符串表示没找到，用系统默认）。
    """
    devices = find_output_devices()
    log.info("[Audio] 发现 %d 个输出设备：", len(devices))
    for d in devices:
        log.debug("  [%d] %s (%d ch)", d["index"], d["name"], d["channels"])

    if not devices:
        log.warning("[Audio] 没有可用输出设备")
        return ""

    for keyword in OUTPUT_PRIORITY:
        for d in devices:
            if keyword in d["name"].lower():
                sink = find_pulse_sink_for_device(d["name"])
                if sink:
                    if set_pulse_default_sink(sink):
                        log.info("[Audio] 选中输出：%s → sink=%s",
                                 d["name"], sink)
                        unmute_and_set_volume(sink)
                        return sink
                    else:
                        log.warning("[Audio] 切换 sink 失败：%s", sink)
                else:
                    log.warning("[Audio] 设备 %s 无对应 PulseAudio sink",
                                d["name"])

    # 兜底：给 PulseAudio 当前第一个 sink 也取消静音
    log.warning("[Audio] 未匹配到优先级设备，使用 PulseAudio 默认 sink")
    sinks = list_pulse_sinks()
    if sinks:
        unmute_and_set_volume(sinks[0][1])
    return ""

