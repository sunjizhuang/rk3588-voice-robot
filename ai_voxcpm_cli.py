#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VoxCPM2 命令行客户端
====================

与 `app.py` 启动的 Gradio server（默认 http://127.0.0.1:8808）交互。

支持两种调用方式：
  1) 一次性命令：带命令行参数运行，生成完成即退出。
     长文本自动走“流式模式”：拆句 → 并发生成 → 顺序播放。
     可用 --gapless 进一步减少段间间隙。
  2) 交互式 REPL：不带参数运行，进入菜单式循环，可连续多次生成。
     （REPL 始终整段生成，不走流式。）

通信方式：
  - 优先使用 `gradio_client.Client`；
  - 若未安装，则自动回退到原生 HTTP + SSE 调用。

支持模式（VoxCPM2 四种语音生成方式）：
  - Voice Design         （仅 control instruction）
  - Controllable Cloning （参考音频 + 可选 control instruction）
  - Ultimate Cloning     （参考音频 + 手动输入 transcript）
  - Voice Library        （使用服务端已注册的 voice_id，第四态）

输出：
  - 保存为 wav 到 `--out-dir`（默认 ./outputs/）
  - 若本机存在 ffplay / afplay / aplay / paplay，则自动播放一次（可用 --no-play 关闭）

此外，本模块还为 `ai_chat_cli.py` 提供可复用的动态 TTS 组件：
  - IncrementalSplitter：面向 LLM 流式输出的增量切分器
  - TTSSession / TTSRuntime：全局并发生成 + 顺序播放的运行时
"""

from __future__ import annotations

import argparse
import json
import os
import queue as _queue
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_SERVER = "http://192.168.199.7:8808"
DEFAULT_API_NAME = "/generate"
DEFAULT_CFG = 2.0
DEFAULT_DIT_STEPS = 10
DEFAULT_OUT_DIR = "outputs"
DEFAULT_TARGET_TEXT = (
    "VoxCPM2 is a creative multilingual TTS model from ModelBest, "
    "designed to generate highly realistic speech."
)

# 流式切分默认值
DEFAULT_MAX_CHARS = 40
DEFAULT_MIN_CHARS = 4
DEFAULT_STREAM_WORKERS = 4

PLAYERS = ["ffplay", "afplay", "aplay", "paplay"]

# Gradio /generate 接口的输入顺序（必须与 app.py 中 _generate 签名完全一致）
GENERATE_INPUT_ORDER = [
    "text",
    "control_instruction",
    "reference_wav",
    "voice_id",          # ← 与 app.py 的 voice_id 位置一致
    "use_prompt_text",
    "prompt_text",
    "cfg_value",
    "do_normalize",
    "denoise",
    "dit_steps",
    "seed",
]


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _to_filedata(path: Optional[str]) -> Optional[Dict[str, Any]]:
    """把本地文件路径转成 Gradio 新版 FileData 结构。"""
    if not path:
        return None
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"参考音频不存在：{p}")

    suffix = p.suffix.lower()
    mime = {
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".flac": "audio/flac",
        ".ogg": "audio/ogg",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
    }.get(suffix, "application/octet-stream")

    return {
        "path": str(p),
        "url": None,
        "size": p.stat().st_size,
        "orig_name": p.name,
        "mime_type": mime,
        "meta": {"_type": "gradio.FileData"},
    }


def _friendly_error(raw: str) -> str:
    """把服务器返回的错误包装成用户可读的中文提示。"""
    if not raw:
        return "生成失败（服务器未返回具体错误）"
    err = str(raw)

    # 服务端 voice_library.get() 抛 KeyError('音色不存在: xxx')
    if "音色不存在" in err:
        m = re.search(r"音色不存在[:：]\s*([^\s'\"\)\]，,]+)", err)
        vid = m.group(1) if m else "?"
        return (
            f"❌ 服务端音色库中找不到 voice_id='{vid}'。\n"
            f"   请检查拼写，或在服务端运行：  voxcpm voice list\n"
            f"   原始错误：{err}"
        )

    # 兜底识别 KeyError
    if "KeyError" in err and ("voice" in err.lower() or "音色" in err):
        return (
            "❌ 音色 ID 不存在。\n"
            "   请检查拼写，或在服务端运行：  voxcpm voice list\n"
            f"   原始错误：{err}"
        )

    # 服务端音色库尚未初始化
    if "VoiceLibrary" in err and ("未初始化" in err or "只读" in err):
        return (
            "❌ 服务端音色库未就绪。\n"
            "   请在服务端确认模型已加载（首次注册/生成会自动加载），"
            "或检查 voices/ 目录是否存在。\n"
            f"   原始错误：{err}"
        )

    return err


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class GenRequest:
    """一次生成请求。"""
    text: str = DEFAULT_TARGET_TEXT
    control_instruction: str = ""
    reference_wav: Optional[str] = None
    voice_id: str = ""                     # 服务端音色库 ID（library 模式使用）
    use_ultimate_cloning: bool = False
    prompt_text: str = ""
    cfg_value: float = DEFAULT_CFG
    do_normalize: bool = False
    denoise: bool = False
    dit_steps: int = DEFAULT_DIT_STEPS
    seed: Optional[int] = None
    random_seed: bool = True

    @property
    def effective_voice_id(self) -> str:
        """客户端裁决：reference_wav 优先于 voice_id（与服务器一致）。"""
        if self.reference_wav:
            return ""
        return (self.voice_id or "").strip()

    def to_payload(self) -> List[Any]:
        """按 server 接口的输入顺序构造 payload（11 项）。"""
        if self.random_seed or self.seed is None:
            seed = random.randint(0, 2**32 - 1)
        else:
            seed = int(self.seed)

        use_prompt_text = bool(self.use_ultimate_cloning)
        actual_prompt_text = (self.prompt_text or "").strip() if use_prompt_text else ""
        actual_control = "" if use_prompt_text else (self.control_instruction or "").strip()

        return [
            self.text,
            actual_control,
            _to_filedata(self.reference_wav),
            self.effective_voice_id,
            use_prompt_text,
            actual_prompt_text,
            float(self.cfg_value),
            bool(self.do_normalize),
            bool(self.denoise),
            int(self.dit_steps),
            int(seed),
        ]

    def describe(self) -> str:
        if self.use_ultimate_cloning:
            mode = "Ultimate Cloning"
        elif self.reference_wav:
            mode = "Controllable Cloning"
        elif self.voice_id:
            mode = "Voice Library"
        else:
            mode = "Voice Design"
        return (
            f"mode={mode}, cfg={self.cfg_value}, steps={self.dit_steps}, "
            f"normalize={self.do_normalize}, denoise={self.denoise}, "
            f"random_seed={self.random_seed}, "
            f"ref={self.reference_wav or '-'}, voice_id={self.voice_id or '-'}"
        )


@dataclass
class GenResult:
    ok: bool
    audio_path: Optional[str] = None
    sample_rate: Optional[int] = None
    seed: Optional[int] = None
    raw: Any = None
    error: Optional[str] = None
    elapsed: float = 0.0
    backend: str = ""


# ---------------------------------------------------------------------------
# 后端：gradio_client
# ---------------------------------------------------------------------------

class GradioClientBackend:
    name = "gradio_client"

    def __init__(self, server: str, api_name: str = DEFAULT_API_NAME) -> None:
        from gradio_client import Client  # 延迟导入，便于回退
        self.server = server.rstrip("/")
        self.api_name = api_name
        self._client = Client(self.server)

    def submit(self, payload: List[Any], out_dir: Path, prefix: str = "voxcpm") -> GenResult:
        t0 = time.time()
        try:
            result = self._client.predict(*payload, api_name=self.api_name)
        except Exception as exc:  # noqa: BLE001
            return GenResult(ok=False, error=f"{type(exc).__name__}: {exc}",
                             elapsed=time.time() - t0, backend=self.name)

        audio_info = result[0] if isinstance(result, (tuple, list)) and result else result
        seed_value = None
        if isinstance(result, (tuple, list)) and len(result) >= 2:
            try:
                seed_value = int(result[1])
            except (TypeError, ValueError):
                seed_value = None

        sr, src_path = _parse_audio_info(audio_info)
        if not src_path:
            return GenResult(ok=False, error=f"无法从返回结果解析音频路径：{audio_info!r}",
                             elapsed=time.time() - t0, backend=self.name)

        saved = _save_audio(src_path, out_dir, prefix=prefix, ext=".wav")
        return GenResult(
            ok=True, audio_path=saved, sample_rate=sr, seed=seed_value,
            raw=result, elapsed=time.time() - t0, backend=self.name,
        )

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 后端：原生 HTTP + SSE
# ---------------------------------------------------------------------------

class HttpSseBackend:
    """不依赖 gradio_client，直接调用 /gradio_api/call/<api_name>。"""
    name = "http_sse"

    def __init__(self, server: str, api_name: str = DEFAULT_API_NAME, timeout: float = 600.0) -> None:
        self.server = server.rstrip("/")
        self.api_name = api_name.lstrip("/")
        self.timeout = timeout

    def _post_event(self, payload: List[Any]) -> str:
        url = f"{self.server}/gradio_api/call/{self.api_name}"
        body = json.dumps({"data": payload}).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        event_id = data.get("event_id")
        if not event_id:
            raise RuntimeError(f"未拿到 event_id，返回：{data!r}")
        return str(event_id)

    def _stream_result(self, event_id: str) -> List[Any]:
        url = f"{self.server}/gradio_api/call/{self.api_name}/{event_id}"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            event_type = None
            for raw_line in resp:
                line = raw_line.decode("utf-8").strip()
                if not line:
                    continue
                if line.startswith("event:"):
                    event_type = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    data_str = line.split(":", 1)[1].strip()
                    if event_type == "complete":
                        parsed = json.loads(data_str)
                        if isinstance(parsed, dict):
                            return parsed.get("data", [])
                        if isinstance(parsed, list):
                            return parsed
                        return [parsed]
                    if event_type == "error":
                        raise RuntimeError(f"server error: {data_str}")
        raise RuntimeError("SSE 流结束但未收到 complete 事件")

    def submit(self, payload: List[Any], out_dir: Path, prefix: str = "voxcpm") -> GenResult:
        t0 = time.time()
        try:
            event_id = self._post_event(payload)
            data = self._stream_result(event_id)
        except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError, OSError) as exc:
            return GenResult(ok=False, error=f"{type(exc).__name__}: {exc}",
                             elapsed=time.time() - t0, backend=self.name)

        audio_info = data[0] if data else None
        seed_value = None
        if len(data) >= 2:
            try:
                seed_value = int(data[1])
            except (TypeError, ValueError):
                seed_value = None

        sr, src_path = _parse_audio_info(audio_info)
        if not src_path:
            return GenResult(ok=False, error=f"无法从返回结果解析音频路径：{audio_info!r}",
                             elapsed=time.time() - t0, backend=self.name)

        saved = _save_audio(src_path, out_dir, prefix=prefix, ext=".wav")
        return GenResult(
            ok=True, audio_path=saved, sample_rate=sr, seed=seed_value,
            raw=data, elapsed=time.time() - t0, backend=self.name,
        )

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# 工具函数（续）
# ---------------------------------------------------------------------------

def _parse_audio_info(audio_info: Any) -> Tuple[Optional[int], Optional[str]]:
    """把 server 返回的 audio 字段解析成 (sample_rate, file_path)。"""
    #print(f"[debug] audio_info = {audio_info!r}")

    if audio_info is None:
        return None, None
    if isinstance(audio_info, dict):
        # 优先用可下载的 URL（跨机场景必须走 URL）
        # 没有 URL 时才退回 path（同机场景）
        path = audio_info.get("url") or audio_info.get("path") or audio_info.get("name")
        sr = audio_info.get("sample_rate") or audio_info.get("orig_sample_rate")
        try:
            sr = int(sr) if sr is not None else None
        except (TypeError, ValueError):
            sr = None
        return sr, path
    if isinstance(audio_info, (tuple, list)):
        if len(audio_info) >= 2:
            sr, path = audio_info[0], audio_info[1]
            try:
                sr = int(sr)
            except (TypeError, ValueError):
                sr = None
            if isinstance(path, dict):
                # 同样优先取 url
                path = path.get("url") or path.get("path")
            return sr, path
        if len(audio_info) == 1:
            return _parse_audio_info(audio_info[0])
    if isinstance(audio_info, str):
        return None, audio_info
    return None, None


def _save_audio(src_path: str, out_dir: Path, prefix: str = "voxcpm", ext: str = ".wav") -> str:
    """把 server 返回的临时音频复制到 out_dir，返回保存后的路径。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    dst = out_dir / f"{prefix}_{stamp}{ext}"

    src = Path(src_path)
    if src.exists():
        shutil.copyfile(src, dst)
        return str(dst.resolve())

    if str(src_path).startswith(("http://", "https://")):
        with urllib.request.urlopen(src_path, timeout=120) as resp, open(dst, "wb") as f:
            shutil.copyfileobj(resp, f)
        return str(dst.resolve())

    raise FileNotFoundError(f"找不到生成的音频文件：{src_path}")


def _cleanup_old_outputs(out_dir: Path, prefix: str = "voxcpm", keep: int = 0) -> int:
    """删除 out_dir 下旧的 {prefix}_*.wav 文件（本次运行开始前调用）。

    按修改时间倒序，保留最近 keep 个；keep=0 表示全删。
    只匹配本客户端自己生成的 wav，不动其他文件。
    """
    if not out_dir.exists():
        return 0
    files = sorted(
        out_dir.glob(f"{prefix}_*.wav"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    to_delete = files[keep:] if keep > 0 else files
    n = 0
    for p in to_delete:
        try:
            p.unlink()
            n += 1
        except OSError as exc:
            print(f"⚠️  删除旧文件失败：{p}（{exc}）")
    return n


def _pick_player() -> Optional[str]:
    for p in PLAYERS:
        if shutil.which(p):
            return p
    return None


def _player_cmd(player: str, path: str) -> List[str]:
    if player == "ffplay":
        return [player, "-nodisp", "-autoexit", "-loglevel", "quiet", path]
    return [player, path]


def _play_audio(path: str) -> None:
    """用于单段/REPL：起播放器并等待其结束。"""
    player = _pick_player()
    if not player:
        print("ℹ️  未找到可用的音频播放器（ffplay / afplay / aplay / paplay），跳过自动播放。")
        return
    try:
        subprocess.run(_player_cmd(player, path), check=False)
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️  自动播放失败：{exc}")


def _play_audio_blocking(path: str) -> None:
    """阻塞式播放，播完才返回。用于流式顺序播放。"""
    player = _pick_player()
    if not player:
        print("ℹ️  未找到可用播放器，跳过播放。")
        return
    try:
        subprocess.run(_player_cmd(player, path), check=False)
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️  播放失败：{exc}")


# ---------------------------------------------------------------------------
# Gapless：把多段 wav 拼成一个临时文件再播
# ---------------------------------------------------------------------------

def _probe_wav_params(path: str) -> Optional[Tuple[int, int, int]]:
    """返回 (sample_rate, channels, sampwidth)，失败返回 None。"""
    try:
        import wave
        with wave.open(path, "rb") as w:
            return (w.getframerate(), w.getnchannels(), w.getsampwidth())
    except Exception:  # noqa: BLE001
        return None


def _concat_wavs_ffmpeg(paths: List[str], dst: str) -> bool:
    """用 ffmpeg concat demuxer 拼接。成功返回 True。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False

    list_file = dst + ".list.txt"
    try:
        with open(list_file, "w", encoding="utf-8") as f:
            for p in paths:
                safe = str(Path(p).resolve()).replace("'", "'\\''")
                f.write(f"file '{safe}'\n")

        cmd = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0",
            "-i", list_file,
            "-c", "copy",
            dst,
        ]
        ret = subprocess.run(cmd, check=False).returncode
        if ret == 0 and Path(dst).exists() and Path(dst).stat().st_size > 0:
            return True

        cmd_reenc = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0",
            "-i", list_file,
            "-c:a", "pcm_s16le",
            dst,
        ]
        ret = subprocess.run(cmd_reenc, check=False).returncode
        return ret == 0 and Path(dst).exists() and Path(dst).stat().st_size > 0
    except Exception:  # noqa: BLE001
        return False
    finally:
        try:
            Path(list_file).unlink(missing_ok=True)
        except OSError:
            pass


def _concat_wavs_python(paths: List[str], dst: str) -> bool:
    """纯 Python wave 拼接。要求参数一致，否则失败。"""
    try:
        import wave
        params = [_probe_wav_params(p) for p in paths]
        if any(p is None for p in params):
            return False
        if len(set(params)) != 1:
            return False

        with wave.open(dst, "wb") as out:
            with wave.open(paths[0], "rb") as first:
                out.setnchannels(first.getnchannels())
                out.setsampwidth(first.getsampwidth())
                out.setframerate(first.getframerate())
            for p in paths:
                with wave.open(p, "rb") as w:
                    out.writeframes(w.readframes(w.getnframes()))
        return Path(dst).exists() and Path(dst).stat().st_size > 0
    except Exception:  # noqa: BLE001
        return False


def _concat_wavs(paths: List[str], dst: str) -> bool:
    """拼接多段 wav 到 dst，成功返回 True。ffmpeg 优先，失败回退 Python。"""
    if not paths:
        return False
    if len(paths) == 1:
        try:
            shutil.copyfile(paths[0], dst)
            return True
        except OSError:
            return False
    if _concat_wavs_ffmpeg(paths, dst):
        return True
    return _concat_wavs_python(paths, dst)


def _cleanup_tmp_dir(tmp_dir: Path) -> None:
    try:
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
    except OSError:
        pass


def _build_backend(server: str, api_name: str, prefer: str) -> Any:
    """构建后端。prefer: auto | gradio | http"""
    prefer = (prefer or "auto").lower()
    if prefer in ("auto", "gradio"):
        try:
            return GradioClientBackend(server, api_name=api_name)
        except Exception as exc:  # noqa: BLE001
            if prefer == "gradio":
                raise
            print(f"⚠️  gradio_client 不可用（{type(exc).__name__}: {exc}），回退到 HTTP/SSE。")
    return HttpSseBackend(server, api_name=api_name)


def _print_result(res: GenResult, auto_play: bool) -> None:
    if not res.ok:
        print(f"\n{_friendly_error(res.error)}\n"
              f"   （backend={res.backend}, {res.elapsed:.1f}s）\n")
        return
    print("\n✅ 生成成功")
    print(f"   后端    : {res.backend}")
    print(f"   音频    : {res.audio_path}")
    print(f"   采样率  : {res.sample_rate}")
    print(f"   实际seed: {res.seed}")
    print(f"   耗时    : {res.elapsed:.1f}s\n")
    if auto_play:
        _play_audio(res.audio_path or "")


# ---------------------------------------------------------------------------
# 文本切分（流式用）
# ---------------------------------------------------------------------------

_SENT_END_RE = re.compile(r"(?<=[。！？!?\.])\s*|\n+")
_SUB_SPLIT_RE = re.compile(r"(?<=[，,、；;])")


def _hard_split_by_len(text: str, max_chars: int) -> List[str]:
    return [text[i:i + max_chars] for i in range(0, len(text), max_chars)] or [text]


def _split_long_sentence(sent: str, max_chars: int) -> List[str]:
    if len(sent) <= max_chars:
        return [sent]

    parts = [p for p in _SUB_SPLIT_RE.split(sent) if p]
    if len(parts) <= 1:
        return _hard_split_by_len(sent, max_chars)

    out: List[str] = []
    buf = ""
    for p in parts:
        if not buf:
            buf = p
        elif len(buf) + len(p) <= max_chars:
            buf += p
        else:
            out.append(buf)
            buf = p
    if buf:
        out.append(buf)

    final: List[str] = []
    for seg in out:
        if len(seg) > max_chars:
            final.extend(_hard_split_by_len(seg, max_chars))
        else:
            final.append(seg)
    return final


def split_text_for_streaming(
    text: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    min_chars: int = DEFAULT_MIN_CHARS,
) -> List[str]:
    """把长文本切成适合流式生成的句子列表。

    规则：
      1. 先按句末标点（。！？.!? 及换行）切分，保留标点；
      2. 相邻过短句（< min_chars）合并到前一句；
      3. 超过 max_chars 的句子，再在逗号/分号处切一刀，尽量 <= max_chars；
      4. 若仍超长且无标点，按长度硬切。
    """
    if not text or not text.strip():
        return []

    raw = _SENT_END_RE.split(text)
    sents = [s.strip() for s in raw if s and s.strip()]
    if not sents:
        return []

    merged: List[str] = []
    for s in sents:
        if merged and len(s) < min_chars:
            merged[-1] = merged[-1] + s
        else:
            merged.append(s)

    result: List[str] = []
    for s in merged:
        result.extend(_split_long_sentence(s, max_chars))

    return result


# ---------------------------------------------------------------------------
# 流式流水线：生成并行、播放串行（独立播放线程；可选 gapless 拼接）
# ---------------------------------------------------------------------------

class StreamingPipeline:
    """按句拆分的流水线：后台并行生成，独立播放线程按序播放。

    gapless=False（默认）：
        每段一个播放器实例，播完取下一段。

    gapless=True：
        播放线程每次从队列取一段后，再非阻塞地“尽量多取”当前已排队的连续段，
        拼成一个临时 wav，用单个播放器实例一次播完 → 消除段间间隙。
        若拼接失败（参数不一致等），自动退回逐段播。

    两种模式下：
      - 生成并行，主线程按序 result() 并 put 进 FIFO 队列；
      - 播放顺序严格正确；
      - 下一段已就绪则立即接上，未就绪则播放线程阻塞等待（方案 C）；
      - 所有段共享同一个 seed。
    """

    _SENTINEL = object()

    def __init__(
        self,
        backend: Any,
        out_dir: Path,
        base_req: GenRequest,
        sentences: List[str],
        auto_play: bool,
        max_workers: int = DEFAULT_STREAM_WORKERS,
        prefix: str = "voxcpm",
        gapless: bool = False,
    ) -> None:
        self.backend = backend
        self.out_dir = out_dir
        self.base_req = base_req
        self.sentences = sentences
        self.auto_play = auto_play
        self.max_workers = max(1, min(max_workers, len(sentences)))
        self.prefix = prefix
        self.gapless = bool(gapless) and auto_play

        if base_req.random_seed or base_req.seed is None:
            self.fixed_seed = random.randint(0, 2**32 - 1)
            print(f"▶ 流式模式：本次统一 seed = {self.fixed_seed}")
        else:
            self.fixed_seed = int(base_req.seed)

        self.results: List[Optional[GenResult]] = [None] * len(sentences)

        self._play_q: "_queue.Queue[Any]" = _queue.Queue()
        self._play_thread: Optional[threading.Thread] = None

        self._tmp_dir = out_dir / ".gapless_tmp"
        self._tmp_seq = 0

    def _make_payload(self, text: str) -> List[Any]:
        req = GenRequest(
            text=text,
            control_instruction=self.base_req.control_instruction,
            reference_wav=self.base_req.reference_wav,
            voice_id=self.base_req.voice_id,
            use_ultimate_cloning=self.base_req.use_ultimate_cloning,
            prompt_text=self.base_req.prompt_text,
            cfg_value=self.base_req.cfg_value,
            do_normalize=self.base_req.do_normalize,
            denoise=self.base_req.denoise,
            dit_steps=self.base_req.dit_steps,
            seed=self.fixed_seed,
            random_seed=False,
        )
        return req.to_payload()

    def _submit_one(self, idx: int, text: str) -> GenResult:
        try:
            payload = self._make_payload(text)
        except FileNotFoundError as exc:
            return GenResult(ok=False, error=str(exc), backend=self.backend.name)
        prefix = f"{self.prefix}_{idx + 1:03d}"
        return self.backend.submit(payload, self.out_dir, prefix=prefix)

    # ---- 播放线程 ----

    def _drain_ready(self, first: str) -> List[str]:
        """取出 first，再非阻塞地尽量多取当前已排队的段（不跨 SENTINEL）。"""
        batch = [first]
        while True:
            try:
                item = self._play_q.get_nowait()
            except _queue.Empty:
                break
            if item is self._SENTINEL:
                self._play_q.put(self._SENTINEL)
                break
            batch.append(item)
        return batch

    def _play_one(self, path: str) -> None:
        _play_audio_blocking(path)

    def _play_batch_gapless(self, batch: List[str]) -> None:
        if not batch:
            return
        if len(batch) == 1:
            self._play_one(batch[0])
            return

        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        self._tmp_seq += 1
        tmp = self._tmp_dir / f"merged_{self._tmp_seq:04d}.wav"

        if _concat_wavs(batch, str(tmp)):
            print(f"🎵 gapless：拼接 {len(batch)} 段 → 单实例播放")
            self._play_one(str(tmp))
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        else:
            print("ℹ️  gapless：拼接失败，退回逐段播放。")
            for p in batch:
                self._play_one(p)

    def _play_worker(self) -> None:
        while True:
            item = self._play_q.get()
            try:
                if item is self._SENTINEL:
                    return
                if not self.gapless:
                    self._play_one(item)
                    continue
                batch = self._drain_ready(item)
                self._play_batch_gapless(batch)
            finally:
                self._play_q.task_done()

    def _start_player(self) -> None:
        if not self.auto_play:
            return
        self._play_thread = threading.Thread(
            target=self._play_worker, name="voxcpm-player", daemon=True
        )
        self._play_thread.start()

    def _stop_player(self) -> None:
        if self._play_thread is None:
            return
        self._play_q.put(self._SENTINEL)
        self._play_q.join()
        self._play_thread.join()
        self._play_thread = None
        if self.gapless:
            _cleanup_tmp_dir(self._tmp_dir)

    # ---- 主流程 ----

    def run(self) -> int:
        n = len(self.sentences)
        mode = "gapless 拼接" if self.gapless else "逐段"
        print(f"\n▶ 流式模式：共 {n} 段，后台并发生成（workers={self.max_workers}），顺序播放（{mode}）\n")

        self._start_player()
        ok_count = 0
        futures: List[Future] = []

        try:
            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                for i, sent in enumerate(self.sentences):
                    futures.append(pool.submit(self._submit_one, i, sent))

                for i, fut in enumerate(futures):
                    preview = self.sentences[i]
                    if len(preview) > 20:
                        preview = preview[:20] + "…"
                    print(f"⏳ [{i + 1}/{n}] 等待第 {i + 1} 段生成：{preview!r}")
                    res = fut.result()
                    self.results[i] = res

                    if not res.ok:
                        print(f"❌ [{i + 1}/{n}] 生成失败（{res.elapsed:.1f}s）：{_friendly_error(res.error)}")
                        continue

                    ok_count += 1
                    print(f"✅ [{i + 1}/{n}] 生成完成（{res.elapsed:.1f}s）→ {res.audio_path}")
                    if self.auto_play and res.audio_path:
                        self._play_q.put(res.audio_path)
        finally:
            self._stop_player()

        print(f"\n▶ 流式完成：成功 {ok_count}/{n} 段。\n")
        return ok_count


# ---------------------------------------------------------------------------
# 供 ai_chat_cli.py 复用的动态 TTS 管线
# ---------------------------------------------------------------------------

_SENT_END_CHARS = set("。！？.!?")
_COMMA_CHARS = set("，,、；;")
_NEWLINE_CHARS = set("\n")


class IncrementalSplitter:
    """增量切分器：边接收 LLM 流式输出，边切出适合 TTS 的片段。

    规则（与 split_text_for_streaming 一致）：
      1. 遇到句末标点（。！？.!?）或换行 → 立即切；
      2. 缓冲区累积字符数 > comma_threshold 后，遇到逗号/分号（，,、；;）→ 切；
      3. flush() 时把剩余文本按 split_text_for_streaming 兜底切分。
    """

    def __init__(self, comma_threshold: int = DEFAULT_MAX_CHARS,
                 min_chars: int = DEFAULT_MIN_CHARS):
        self.comma_threshold = max(1, int(comma_threshold))
        self.min_chars = max(1, int(min_chars))
        self._buf = ""

    def feed(self, delta: str) -> List[str]:
        if not delta:
            return []
        self._buf += delta
        out: List[str] = []
        while True:
            cut = self._find_cut()
            if cut is None:
                break
            idx, _ = cut
            seg = self._buf[: idx + 1].strip()
            self._buf = self._buf[idx + 1:]
            if seg:
                out.append(seg)
        return out

    def flush(self) -> List[str]:
        text = self._buf.strip()
        self._buf = ""
        if not text:
            return []
        return split_text_for_streaming(
            text, max_chars=self.comma_threshold, min_chars=self.min_chars
        )

    def _find_cut(self) -> Optional[Tuple[int, str]]:
        buf = self._buf
        n = len(buf)
        allow_comma = n > self.comma_threshold
        for i, ch in enumerate(buf):
            if ch in _SENT_END_CHARS or ch in _NEWLINE_CHARS:
                return (i, ch)
            if allow_comma and ch in _COMMA_CHARS:
                return (i, ch)
        return None


class TTSSession:
    """一轮对话的 TTS 会话。句子 push 后由 runtime 统一生成、按序播放。"""

    def __init__(self, runtime: "TTSRuntime", base_req: GenRequest, seq: int) -> None:
        self.runtime = runtime
        self.base_req = base_req
        self.seq = seq
        self.seed = runtime.seed
        self._idx = 0
        self._futures: List[Tuple[int, Future]] = []
        self._closed = False

    def push(self, text: str) -> None:
        if self._closed:
            raise RuntimeError("session 已 finish，不能再 push")
        text = (text or "").strip()
        if not text:
            return
        idx = self._idx
        self._idx += 1
        fut = self.runtime._executor.submit(self._gen_one, idx, text)
        self._futures.append((idx, fut))

    def _gen_one(self, idx: int, text: str) -> GenResult:
        req = GenRequest(
            text=text,
            control_instruction=self.base_req.control_instruction,
            reference_wav=self.base_req.reference_wav,
            voice_id=self.base_req.voice_id,
            use_ultimate_cloning=self.base_req.use_ultimate_cloning,
            prompt_text=self.base_req.prompt_text,
            cfg_value=self.base_req.cfg_value,
            do_normalize=self.base_req.do_normalize,
            denoise=self.base_req.denoise,
            dit_steps=self.base_req.dit_steps,
            seed=self.seed,
            random_seed=False,
        )
        prefix = f"voxcpm_{self.seq:04d}_{idx + 1:03d}"
        try:
            payload = req.to_payload()
        except FileNotFoundError as exc:
            return GenResult(ok=False, error=str(exc), backend=self.runtime.backend.name)
        return self.runtime.backend.submit(payload, self.runtime.out_dir, prefix=prefix)

    def finish(self) -> None:
        """标记本轮结束；收集任务提交到 runtime 的串行收集线程。"""
        if self._closed:
            return
        self._closed = True
        self.runtime._collect_executor.submit(self._collect_and_queue)

    def _collect_and_queue(self) -> None:
        """按 push 顺序 result futures，把音频路径按序放入全局播放队列。"""
        for idx, fut in self._futures:
            try:
                res = fut.result()
            except Exception as exc:  # noqa: BLE001
                print(f"⚠️  TTS 生成异常 [{self.seq}-{idx + 1}]：{exc}")
                continue
            if not res.ok:
                print(f"⚠️  TTS 生成失败 [{self.seq}-{idx + 1}]：{_friendly_error(res.error)}")
                continue
            if res.audio_path:
                self.runtime._play_q.put((self.seq, res.audio_path))


class TTSRuntime:
    """全局 TTS 运行时：并发生成 + 串行收集 + 串行播放。

    用法：
        runtime = TTSRuntime(backend, out_dir, auto_play=True, ...)
        runtime.start()
        session = runtime.new_session(base_req)
        session.push("第一句。")
        session.push("第二句。")
        session.finish()
        ...
        runtime.shutdown(wait=True)
    """

    _SENTINEL = object()

    def __init__(
        self,
        backend: Any,
        out_dir: Path,
        auto_play: bool = True,
        max_workers: int = DEFAULT_STREAM_WORKERS,
        gapless: bool = True,
        seed: Optional[int] = None,
        prefix: str = "voxcpm",
    ) -> None:
        self.backend = backend
        self.out_dir = Path(out_dir)
        self.auto_play = bool(auto_play)
        self.gapless = bool(gapless) and self.auto_play
        self.prefix = prefix
        self.seed = int(seed) if seed is not None else random.randint(0, 2**32 - 1)

        self._executor = ThreadPoolExecutor(max_workers=max(1, int(max_workers)),
                                            thread_name_prefix="voxcpm-gen")
        self._collect_executor = ThreadPoolExecutor(max_workers=1,
                                                    thread_name_prefix="voxcpm-collect")
        self._play_q: "_queue.Queue[Any]" = _queue.Queue()
        self._play_thread: Optional[threading.Thread] = None
        self._session_seq = 0
        self._started = False
        self._tmp_dir = self.out_dir / ".gapless_tmp"
        self._tmp_seq = 0

    # ---- 生命周期 ----

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        if self.auto_play:
            self._play_thread = threading.Thread(
                target=self._play_worker, name="voxcpm-player", daemon=True
            )
            self._play_thread.start()

    def new_session(self, base_req: GenRequest) -> TTSSession:
        self._session_seq += 1
        return TTSSession(self, base_req, seq=self._session_seq)

    def shutdown(self, wait: bool = True) -> None:
        """等待所有生成/收集任务完成，再停播放线程。"""
        try:
            self._collect_executor.shutdown(wait=True)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._executor.shutdown(wait=True)
        except Exception:  # noqa: BLE001
            pass
        if self._play_thread is not None:
            self._play_q.put(self._SENTINEL)
            if wait:
                self._play_thread.join()
            self._play_thread = None
        if self.gapless:
            _cleanup_tmp_dir(self._tmp_dir)

    # ---- 播放 ----

    def _play_one(self, path: str) -> None:
        _play_audio_blocking(path)

    def _play_batch_gapless(self, batch: List[str]) -> None:
        if not batch:
            return
        if len(batch) == 1:
            self._play_one(batch[0])
            return
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        self._tmp_seq += 1
        tmp = self._tmp_dir / f"merged_{self._tmp_seq:04d}.wav"
        if _concat_wavs(batch, str(tmp)):
            self._play_one(str(tmp))
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        else:
            for p in batch:
                self._play_one(p)

    def _play_worker(self) -> None:
        pending: Optional[Tuple[int, str]] = None
        while True:
            if pending is not None:
                item = pending
                pending = None
            else:
                item = self._play_q.get()
            try:
                if item is self._SENTINEL:
                    return
                seq, path = item
                if not self.gapless:
                    self._play_one(path)
                    continue
                # gapless：只把「同一 session」的连续段拼一起，不跨轮
                batch = [path]
                while True:
                    try:
                        nxt = self._play_q.get_nowait()
                    except _queue.Empty:
                        break
                    if nxt is self._SENTINEL:
                        self._play_q.put(self._SENTINEL)
                        break
                    nseq, npath = nxt
                    if nseq != seq:
                        pending = nxt
                        break
                    batch.append(npath)
                self._play_batch_gapless(batch)
            finally:
                self._play_q.task_done()


# ---------------------------------------------------------------------------
# 交互式 REPL（保持整段生成，不走流式）
# ---------------------------------------------------------------------------

REPL_HELP = """\
可用命令：
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
"""


def _prompt(label: str, default: str = "") -> str:
    s = input(f"{label}{f' [{default}]' if default else ''}: ").strip()
    return s if s else default


def _prompt_float(label: str, default: float) -> float:
    while True:
        s = input(f"{label} [{default}]: ").strip()
        if not s:
            return default
        try:
            return float(s)
        except ValueError:
            print("  ⚠️  请输入数字。")


def _prompt_int(label: str, default: int) -> int:
    while True:
        s = input(f"{label} [{default}]: ").strip()
        if not s:
            return default
        try:
            return int(s)
        except ValueError:
            print("  ⚠️  请输入整数。")


def _prompt_bool(label: str, default: bool) -> bool:
    d = "y" if default else "n"
    while True:
        s = input(f"{label} (y/n) [{d}]: ").strip().lower()
        if not s:
            return default
        if s in ("y", "yes", "1", "true", "on"):
            return True
        if s in ("n", "no", "0", "false", "off"):
            return False
        print("  ⚠️  请输入 y 或 n。")


def _select_mode() -> str:
    print("\n请选择生成模式：")
    print("  1) Voice Design         （仅 Control Instruction）")
    print("  2) Controllable Cloning （参考音频 + 可选 Control Instruction）")
    print("  3) Ultimate Cloning     （参考音频 + 手动输入 transcript）")
    print("  4) Voice Library        （使用服务端已注册的音色 ID）")
    while True:
        s = input("输入 1/2/3/4 [1]: ").strip() or "1"
        if s in ("1", "2", "3", "4"):
            return {"1": "design", "2": "clone", "3": "ultimate", "4": "library"}[s]
        print("  ⚠️  请输入 1、2、3 或 4。")


def _collect_interactively(req: GenRequest, mode: str) -> GenRequest:
    if mode == "design":
        req.reference_wav = None
        req.voice_id = ""
        req.use_ultimate_cloning = False
        req.prompt_text = ""
        req.control_instruction = _prompt("Control Instruction（音色/风格描述）", req.control_instruction)
        req.text = _prompt("Target Text", req.text or DEFAULT_TARGET_TEXT)
    elif mode == "clone":
        req.voice_id = ""
        ref = _prompt("参考音频路径", req.reference_wav or "")
        req.reference_wav = ref or None
        req.use_ultimate_cloning = False
        req.prompt_text = ""
        req.control_instruction = _prompt("Control Instruction（可选）", req.control_instruction)
        req.text = _prompt("Target Text", req.text or DEFAULT_TARGET_TEXT)
    elif mode == "ultimate":
        req.voice_id = ""
        ref = _prompt("参考音频路径", req.reference_wav or "")
        if not ref:
            print("  ⚠️  极致克隆模式需要参考音频。")
        req.reference_wav = ref or None
        req.use_ultimate_cloning = True
        req.control_instruction = ""
        req.prompt_text = _prompt("参考音频 transcript（手动输入）", req.prompt_text)
        req.text = _prompt("Target Text（续写内容）", req.text or DEFAULT_TARGET_TEXT)
    elif mode == "library":
        req.reference_wav = None
        req.use_ultimate_cloning = False
        req.prompt_text = ""
        vid = _prompt("Voice ID（服务端已注册的音色 ID）", req.voice_id or "")
        req.voice_id = vid.strip()
        if not req.voice_id:
            print("  ⚠️  Voice Library 模式需要 voice_id。")
        req.control_instruction = _prompt("Control Instruction（可选）", req.control_instruction)
        req.text = _prompt("Target Text", req.text or DEFAULT_TARGET_TEXT)

    if _prompt_bool("是否调整高级参数", False):
        req.cfg_value = _prompt_float("CFG", req.cfg_value)
        req.dit_steps = _prompt_int("LocDiT steps", req.dit_steps)
        req.do_normalize = _prompt_bool("文本规范化", req.do_normalize)
        req.denoise = _prompt_bool("参考音频降噪", req.denoise)
        req.random_seed = _prompt_bool("使用随机 seed", req.random_seed)
        if not req.random_seed:
            s = _prompt("固定 seed", str(req.seed or 0))
            try:
                req.seed = int(s)
            except ValueError:
                print("  ⚠️  seed 无效，改为随机。")
                req.random_seed = True
                req.seed = None
    return req


def _validate(req: GenRequest) -> Tuple[Optional[str], Optional[str]]:
    """返回 (error, warning)，均为 Optional[str]。"""
    if not (req.text or "").strip():
        return "Target Text 不能为空。", None
    if req.reference_wav and not Path(req.reference_wav).exists():
        return f"参考音频不存在：{req.reference_wav}", None
    if req.use_ultimate_cloning and not req.reference_wav:
        return "极致克隆模式必须提供参考音频。", None
    if req.use_ultimate_cloning and not (req.prompt_text or "").strip():
        return "极致克隆模式必须提供参考音频 transcript。", None

    warning = None
    if req.reference_wav and (req.voice_id or "").strip():
        warning = (
            f"检测到 reference_wav 与 voice_id 同时存在，"
            f"按服务器规则 reference_wav 优先，"
            f"voice_id='{req.voice_id}' 将被忽略。"
        )
    return None, warning


def _repl_run(backend_holder: Dict[str, Any], req: GenRequest, out_dir: Path,
              auto_play: bool, server_holder: Dict[str, str], api_name: str,
              prefer: str) -> None:
    err, warn = _validate(req)
    if warn:
        print(f"⚠️  {warn}")
    if err:
        print(f"⚠️  {err}")
        return
    try:
        payload = req.to_payload()
    except FileNotFoundError as exc:
        print(f"⚠️  {exc}")
        return
    print(f"\n▶ 提交生成：{req.describe()}")
    res = backend_holder["backend"].submit(payload, out_dir)
    _print_result(res, auto_play)
    if res.ok and res.seed is not None:
        req.seed = res.seed


def repl(backend_holder: Dict[str, Any], server_holder: Dict[str, str],
         api_name: str, prefer: str, out_dir: Path, auto_play: bool) -> int:
    req = GenRequest()
    print("\n===== VoxCPM2 交互式客户端 =====")
    print(f"server = {server_holder['url']}  api = {api_name}")
    print("输入 :h 查看帮助，:q 退出。\n")

    while True:
        try:
            line = input("voxcpm> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue

        if line in (":q", ":quit", ":exit"):
            break
        if line in (":h", ":help", "?"):
            print(REPL_HELP)
            continue
        if line == ":show":
            print(f"  当前参数：{req.describe()}")
            print(f"  text      = {req.text[:80]!r}")
            print(f"  control   = {req.control_instruction!r}")
            print(f"  ref       = {req.reference_wav}")
            print(f"  voice_id  = {req.voice_id!r}")
            print(f"  effective = {req.effective_voice_id!r}")
            print(f"  ultimate  = {req.use_ultimate_cloning}")
            print(f"  transcript= {req.prompt_text!r}")
            print(f"  cfg       = {req.cfg_value}")
            print(f"  steps     = {req.dit_steps}")
            print(f"  normalize = {req.do_normalize}")
            print(f"  denoise   = {req.denoise}")
            print(f"  seed      = {'random' if req.random_seed else req.seed}")
            continue
        if line == ":mode":
            mode = _select_mode()
            req = _collect_interactively(req, mode)
            continue
        if line == ":run":
            _repl_run(backend_holder, req, out_dir, auto_play, server_holder, api_name, prefer)
            continue
        if line.startswith(":server"):
            parts = line.split(maxsplit=1)
            new_url = parts[1].strip() if len(parts) > 1 else ""
            if not new_url:
                print("  用法：:server http://host:port")
                continue
            try:
                backend_holder["backend"].close()
            except Exception:  # noqa: BLE001
                pass
            try:
                backend_holder["backend"] = _build_backend(new_url, api_name, prefer)
                server_holder["url"] = new_url
                print(f"  ✅ 已切换 server → {new_url}（backend={backend_holder['backend'].name}）")
            except Exception as exc:  # noqa: BLE001
                print(f"  ❌ 切换失败：{exc}")
                backend_holder["backend"] = _build_backend(server_holder["url"], api_name, prefer)
            continue

        for key, attr, caster in (
            (":ref",    "reference_wav",        lambda v: v or None),
            (":text",   "text",                 str),
            (":ctrl",   "control_instruction",  str),
            (":trans",  "prompt_text",          str),
            (":cfg",    "cfg_value",            float),
            (":steps",  "dit_steps",            int),
            (":seed",   "seed",                 None),
            (":norm",   "do_normalize",         None),
            (":denoise","denoise",              None),
        ):
            if line == key or line.startswith(key + " "):
                val = line[len(key):].strip()
                try:
                    if key == ":seed":
                        if val.lower() in ("rand", "random", ""):
                            req.random_seed = True
                            req.seed = None
                        else:
                            req.seed = int(val)
                            req.random_seed = False
                    elif key in (":norm", ":denoise"):
                        b = val.lower() in ("on", "1", "true", "y", "yes")
                        setattr(req, attr, b)
                    elif key == ":ref":
                        req.reference_wav = caster(val)
                        # 一旦设置参考音频，清空 voice_id（互斥）
                        if req.reference_wav:
                            req.voice_id = ""
                    else:
                        setattr(req, attr, caster(val))
                    print(f"  ✅ {key} 已更新。")
                except (TypeError, ValueError) as exc:
                    print(f"  ⚠️  值无效：{exc}")
                break
        else:
            print("  未知命令。输入 :h 查看帮助。")

    try:
        backend_holder["backend"].close()
    except Exception:  # noqa: BLE001
        pass
    print("再见 👋")
    return 0


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="VoxCPM2 命令行客户端（对接 app.py 启动的 Gradio server）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--server", default=os.environ.get("VOXCPM_SERVER", DEFAULT_SERVER),
                   help="server 地址，如 http://127.0.0.1:8808")
    p.add_argument("--port", type=int, default=None,
                   help="覆盖 server 端口（会替换 --server 中的端口）")
    p.add_argument("--api-name", default=DEFAULT_API_NAME,
                   help="Gradio API 名称，server 端为 /generate")
    p.add_argument("--backend", choices=["auto", "gradio", "http"], default="auto",
                   help="通信后端：auto 优先 gradio_client，失败回退 HTTP/SSE")

    p.add_argument("--text", default=None, help="目标文本")
    p.add_argument("--control", default="", help="Control Instruction")
    p.add_argument("--ref", default=None, help="参考音频路径")
    p.add_argument("--voice-id", default="",
                   help="服务端音色库 ID（与 --ref 同时给时，--ref 优先）")
    p.add_argument("--transcript", default="", help="参考音频 transcript（极致克隆用）")
    p.add_argument("--ultimate", action="store_true",
                   help="开启极致克隆模式（参考音频 + transcript）")

    p.add_argument("--cfg", type=float, default=DEFAULT_CFG, help="CFG 引导强度")
    p.add_argument("--steps", type=int, default=DEFAULT_DIT_STEPS, help="LocDiT 步数")
    p.add_argument("--normalize", action="store_true", help="开启文本规范化")
    p.add_argument("--denoise", action="store_true", help="开启参考音频降噪")
    p.add_argument("--seed", type=int, default=None, help="固定 seed")
    p.add_argument("--random-seed", action="store_true",
                   help="每次请求前随机 seed（与 --seed 互斥，优先 --random-seed）")

    p.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="音频输出目录")
    p.add_argument("--no-play", action="store_true", help="不自动播放生成的音频")

    # ---- 流式相关 ----
    p.add_argument("--no-stream", action="store_true",
                   help="禁用流式拆句（即使 --text 很长也整段生成）")
    p.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS,
                   help="流式模式下每段最大字符数（超出在逗号/分号处再切）")
    p.add_argument("--stream-workers", type=int, default=DEFAULT_STREAM_WORKERS,
                   help="流式模式并发生成的线程数")
    p.add_argument("--gapless", action="store_true",
                   help="把已就绪的连续段拼成一个临时 wav，用单个播放器实例连续播放，减少段间间隙")

    return p


def _apply_port(server: str, port: Optional[int]) -> str:
    if port is None:
        return server
    server = server.rstrip("/")
    if "://" in server:
        scheme, rest = server.split("://", 1)
        host = rest.split("/", 1)[0]
        host = host.split(":", 1)[0]
        return f"{scheme}://{host}:{port}"
    return f"http://{server.split(':')[0]}:{port}"


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    server = _apply_port(args.server, args.port)
    out_dir = Path(args.out_dir).expanduser().resolve()
    auto_play = not args.no_play

    one_shot = any([
        args.text is not None,
        args.control,
        args.ref,
        args.voice_id,
        args.transcript,
        args.ultimate,
        args.seed is not None,
        args.random_seed,
        args.normalize,
        args.denoise,
        args.cfg != DEFAULT_CFG,
        args.steps != DEFAULT_DIT_STEPS,
    ])

    try:
        backend = _build_backend(server, args.api_name, args.backend)
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 无法初始化后端（server={server}）：{type(exc).__name__}: {exc}")
        return 1

    backend_holder: Dict[str, Any] = {"backend": backend}
    server_holder: Dict[str, str] = {"url": server}

    if not one_shot:
        return repl(backend_holder, server_holder, args.api_name,
                    args.backend, out_dir, auto_play)

    # ---- 一次性命令 ----
    req = GenRequest(
        text=args.text if args.text is not None else DEFAULT_TARGET_TEXT,
        control_instruction=args.control or "",
        reference_wav=args.ref,
        voice_id=args.voice_id or "",
        use_ultimate_cloning=bool(args.ultimate),
        prompt_text=args.transcript or "",
        cfg_value=args.cfg,
        do_normalize=bool(args.normalize),
        denoise=bool(args.denoise),
        dit_steps=args.steps,
        seed=args.seed,
        random_seed=bool(args.random_seed) or args.seed is None,
    )

    err, warn = _validate(req)
    if warn:
        print(f"⚠️  {warn}")
    if err:
        print(f"❌ 参数错误：{err}")
        try:
            backend.close()
        except Exception:  # noqa: BLE001
            pass
        return 1

    print(f"▶ server = {server}  backend = {backend.name}")
    print(f"▶ {req.describe()}")

    # ---- 本次运行开始前：清理旧的 voxcpm_*.wav ----
    removed = _cleanup_old_outputs(out_dir, prefix="voxcpm", keep=0)
    if removed:
        print(f"🧹 已清理旧输出 {removed} 个（{out_dir}）")

    # ---- 决定是否走流式 ----
    sentences: List[str] = []
    if not args.no_stream and args.text is not None:
        sentences = split_text_for_streaming(
            req.text, max_chars=args.max_chars, min_chars=DEFAULT_MIN_CHARS
        )

    use_stream = len(sentences) > 1

    if use_stream:
        try:
            pipeline = StreamingPipeline(
                backend=backend,
                out_dir=out_dir,
                base_req=req,
                sentences=sentences,
                auto_play=auto_play,
                max_workers=args.stream_workers,
                gapless=args.gapless,
            )
            ok_count = pipeline.run()
        finally:
            try:
                backend.close()
            except Exception:  # noqa: BLE001
                pass
        return 0 if ok_count > 0 else 1

    # ---- 单段（原有逻辑）----
    try:
        payload = req.to_payload()
    except FileNotFoundError as exc:
        print(f"❌ 参数错误：{exc}")
        try:
            backend.close()
        except Exception:  # noqa: BLE001
            pass
        return 1

    res = backend.submit(payload, out_dir)
    try:
        backend.close()
    except Exception:  # noqa: BLE001
        pass

    if not res.ok:
        print(f"\n{_friendly_error(res.error)}\n"
              f"   （backend={res.backend}, {res.elapsed:.1f}s）\n")
        return 1

    _print_result(res, auto_play)
    return 0


if __name__ == "__main__":
    sys.exit(main())

