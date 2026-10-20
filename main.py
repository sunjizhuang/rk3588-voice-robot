#!/usr/bin/env python3
"""RK3588 语音机器人主入口：KWS + VAD + ASR + 动作分流 + DeepSeek + 双 TTS。"""
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / 'robot.env')

import argparse
import json
import logging
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import wave

from config import (
    VAD_MODEL, ASR_ENCODER, ASR_DECODER, ASR_TOKENS,
    TTS_MODEL_DIR, TTS_DEFAULT_SID, TTS_DEFAULT_SPEED,
    KWS_ENCODER, KWS_DECODER, KWS_JOINER, KWS_TOKENS, KEYWORDS_FILE,
    ACTION_CONFIG, SEND_MQTT_SCRIPT, MQTT_DEVICE,
    CHAT_CLI, CHAT_TIMEOUT, CHAT_TTS_MODE_DEFAULT,
    TEXT_INPUT_API_KEY, TEXT_INPUT_PORT,
    ASR_DISPLAY_MODE,
)
from audio import open_mic, read_chunk
from vad import SileroVAD
from asr import ASREngine
from kws import KWSEngine
from tts import TTSEngine
from action_router import ActionRouter
from http_server import start_text_input_server
from audio_setup import setup_audio_output
from audio_player import AudioPlayer


# --------------------------------------------------------------------------- #
# 日志配置
# --------------------------------------------------------------------------- #

LOG_LEVEL = logging.INFO
LOG_FILE = ""

LOG_FORMAT = "[%(asctime)s] [%(levelname)s] %(message)s"
LOG_DATEFMT = "%H:%M:%S"


def setup_logging(level: int = LOG_LEVEL, log_file: str = LOG_FILE or None):
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATEFMT))
    root.addHandler(console)

    if log_file:
        try:
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATEFMT))
            root.addHandler(fh)
        except Exception as e:
            print(f"[Log] 无法创建日志文件 {log_file}: {e}", file=sys.stderr)


log = logging.getLogger("robot")


setup_logging()


# --------------------------------------------------------------------------- #
# 信号处理：Ctrl+C / SIGTERM 强制退出
# --------------------------------------------------------------------------- #

def _force_exit(signum, frame):
    print("\n[强制退出]")
    try:
        sys.stdout.flush()
    except Exception:
        pass
    os._exit(0)


signal.signal(signal.SIGINT, _force_exit)
signal.signal(signal.SIGTERM, _force_exit)


# --------------------------------------------------------------------------- #
# TTS 异步播报
# --------------------------------------------------------------------------- #

tts_speaking = threading.Event()


def speak_async(player, tts, text: str, sid=None, speed=None):
    def _run():
        tts_speaking.set()
        log.info("[TTS] 开始播报：%s%s", text[:30], "..." if len(text) > 30 else "")
        try:
            samples, sr = tts.synthesize(text, sid=sid, speed=speed)
            player.play(samples, sr, wait=True)
        except Exception as e:
            log.error("[TTS] 播报失败：%s", e)
        finally:
            tts_speaking.clear()
            log.info("[TTS] 播报结束")
    threading.Thread(target=_run, daemon=True).start()


def speak_streaming_async(player, tts, text, sid=None, speed=None):
    def _run():
        tts_speaking.set()
        log.info("[TTS] 开始流式播报：%s%s", text[:30], "..." if len(text) > 30 else "")
        try:
            for sent in tts._split_sentences(text):
                if not sent:
                    continue
                samples, sr = tts.synthesize(sent, sid=sid, speed=speed)
                player.play(samples, sr, wait=True)
        except Exception as e:
            log.error("[TTS] 流式播报失败：%s", e)
        finally:
            tts_speaking.clear()
            log.info("[TTS] 播报结束")
    threading.Thread(target=_run, daemon=True).start()


# --------------------------------------------------------------------------- #
# VoxCPM 输出监控 + 批量合并播放
# --------------------------------------------------------------------------- #

voxcpm_play_queue = queue.Queue()


def _concat_wavs(paths):
    """用 ffmpeg 合并多个 wav，返回临时文件路径。失败返回 None。"""
    import tempfile

    if len(paths) == 1:
        return paths[0]

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        dst = f.name

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
        if ret == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0:
            return dst
        return None
    finally:
        try:
            os.unlink(list_file)
        except OSError:
            pass


def _voxcpm_player_loop(player: AudioPlayer, stop_event: threading.Event):
    """独立播放线程：批量取 + 合并 + 播放，段间零间隙。"""
    while not stop_event.is_set():
        try:
            path = voxcpm_play_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        if path is None:
            break

        # 批量取：把队列里已就绪的段一起取
        batch = [path]
        while True:
            try:
                nxt = voxcpm_play_queue.get_nowait()
            except queue.Empty:
                break
            if nxt is None:
                try:
                    voxcpm_play_queue.put_nowait(None)
                except Exception:
                    pass
                break
            batch.append(nxt)

        try:
            if len(batch) == 1:
                log.info("[VoxCPM] 播放：%s", os.path.basename(batch[0]))
                player.play_wav_file(batch[0], wait=True)
            else:
                log.info("[VoxCPM] 批量播放 %d 段", len(batch))
                merged = _concat_wavs(batch)
                if merged and merged != batch[0]:
                    player.play_wav_file(merged, wait=True)
                    try:
                        os.unlink(merged)
                    except OSError:
                        pass
                else:
                    for p in batch:
                        player.play_wav_file(p, wait=True)
                        
            log.info("[VoxCPM] 播报完成")            
        except Exception as e:
            log.error("[VoxCPM] 播放失败：%s", e)
        finally:
            for _ in batch:
                try:
                    voxcpm_play_queue.task_done()
                except Exception:
                    pass


def _watch_voxcpm_outputs(out_dir: str, stop_event: threading.Event):
    """监控 outputs 目录，新 wav 出现就入队。"""
    played = set()
    try:
        for name in os.listdir(out_dir):
            if name.lower().endswith('.wav'):
                played.add(name)
    except Exception:
        pass

    log.info("[VoxCPM-Watch] 监控目录：%s", out_dir)

    while not stop_event.is_set():
        try:
            new_files = []
            for name in os.listdir(out_dir):
                if not name.lower().endswith('.wav'):
                    continue
                if name in played:
                    continue
                path = os.path.join(out_dir, name)
                try:
                    if os.path.getsize(path) > 0:
                        with wave.open(path, 'rb') as wf:
                            wf.getparams()
                        new_files.append(path)
                        played.add(name)
                except Exception:
                    pass

            new_files.sort()
            for path in new_files:
                try:
                    voxcpm_play_queue.put_nowait(path)
                except Exception:
                    pass

        except Exception as e:
            log.error("[VoxCPM-Watch] 异常：%s", e)

        time.sleep(0.1)


def check_models(use_kws: bool):
    files = [VAD_MODEL, ASR_ENCODER, ASR_DECODER, ASR_TOKENS,
             f"{TTS_MODEL_DIR}/model.onnx", f"{TTS_MODEL_DIR}/tokens.txt"]
    if use_kws:
        files += [KWS_ENCODER, KWS_DECODER, KWS_JOINER, KWS_TOKENS, KEYWORDS_FILE]
    missing = [f for f in files if not Path(f).is_file()]
    if missing:
        for f in missing:
            log.error("模型文件不存在：%s", f)
        sys.exit(1)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def main(start_http=False, http_port=TEXT_INPUT_PORT,
         text_input="", use_kws=True, chat_tts="voxcpm",
         asr_display_mode=ASR_DISPLAY_MODE,
         asr_only=False):
    check_models(use_kws)

    setup_audio_output()

    player = AudioPlayer(samplerate=48000, channels=1, device=None)

    from config import BASE_DIR
    tts_cfg_path = str(BASE_DIR / "tts_config.json")
    out_dir = "outputs"
    try:
        if os.path.isfile(tts_cfg_path):
            with open(tts_cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            out_dir = cfg.get("out_dir") or "outputs"
    except Exception:
        pass
    out_dir_abs = os.path.abspath(os.path.expanduser(out_dir))
    os.makedirs(out_dir_abs, exist_ok=True)

    stop_event = threading.Event()

    threading.Thread(
        target=_watch_voxcpm_outputs,
        args=(out_dir_abs, stop_event),
        daemon=True,
    ).start()

    threading.Thread(
        target=_voxcpm_player_loop,
        args=(player, stop_event),
        daemon=True,
    ).start()

    log.info("初始化 VAD...")
    vad = SileroVAD(VAD_MODEL)

    log.info("初始化 ASR...")
    asr = ASREngine()
    asr_stream = asr.create_stream()

    log.info("初始化 TTS（sherpa-onnx 本地）...")
    tts = TTSEngine(TTS_MODEL_DIR, default_sid=TTS_DEFAULT_SID,
                    default_speed=TTS_DEFAULT_SPEED)
    log.info("[TTS] 说话人数量: %d", tts.num_speakers)

    log.info("初始化 ActionRouter（chat TTS 模式：%s）...", chat_tts)
    router = ActionRouter(
        config_path=ACTION_CONFIG,
        send_mqtt_script=SEND_MQTT_SCRIPT,
        device=MQTT_DEVICE,
        chat_cli=CHAT_CLI,
        chat_timeout=CHAT_TIMEOUT,
        chat_tts_mode=chat_tts,
    )

    kws, kws_stream = None, None
    if use_kws:
        log.info("初始化 KWS（唤醒词：灵境灵境）...")
        kws = KWSEngine()
        kws_stream = kws.create_stream()

    if start_http:
        start_text_input_server(tts, TEXT_INPUT_API_KEY, speak_async, http_port)

    def handle_asr_text(text: str):
        if asr_only:
            return

        route_type, payload = router.route(text)

        if route_type == "action":
            log.info("[Route] 动作指令：%s", payload)
            ok = router.send_action(payload)
            if not ok:
                log.warning("[MQTT] 动作发送失败：%s", payload)
            speak_async(player, tts, "好的")

        elif route_type == "unsupported":
            log.warning("[Route] 未支持动作：%s", payload)
            speak_async(player, tts, "这个动作暂不支持")

        else:
            log.info("[Route] 走 DeepSeek（TTS 模式：%s）", router.chat_tts_mode)
            answer, error, tts_action = router.chat(text)
            if error:
                log.error("[ChatCLI] 错误：%s", error)
                speak_async(player, tts, "查询失败，请稍后再试")
            else:
                log.info("[回复] \033[36m%s\033[0m", answer)
                if tts_action == "need_sherpa":
                    speak_streaming_async(player, tts, answer)
                else:
                    tts_speaking.set()
                    time.sleep(0.3)
                    tts_speaking.clear()

    if text_input:
        try:
            log.info("[TextInput] 查询：%s", text_input)
            route_type, payload = router.route(text_input)
            if route_type == "action":
                router.send_action(payload)
                samples, sr = tts.synthesize("好的")
                player.play(samples, sr, wait=True)
            elif route_type == "unsupported":
                samples, sr = tts.synthesize("这个动作暂不支持")
                player.play(samples, sr, wait=True)
            else:
                answer, error, tts_action = router.chat(text_input)
                if error:
                    log.error("[ChatCLI] 错误：%s", error)
                    samples, sr = tts.synthesize("查询失败，请稍后再试")
                    player.play(samples, sr, wait=True)
                else:
                    log.info("回复：%s", answer)
                    if tts_action == "need_sherpa":
                        samples, sr = tts.synthesize(answer)
                        player.play(samples, sr, wait=True)
        finally:
            stop_event.set()
            try:
                voxcpm_play_queue.put_nowait(None)
            except Exception:
                pass
            try:
                player.stop()
            except Exception as e:
                log.warning("[AudioPlayer] 停止异常：%s", e)
        return

    print()
    log.info("===== 机器人已启动 =====")
    if use_kws:
        log.info("请先说「灵境灵境」唤醒")
    else:
        log.info("无需唤醒，直接说话")
    log.info("ASR 显示模式：%s", asr_display_mode)
    log.info("ASR-Only：%s", "是" if asr_only else "否")
    log.info("（按 Ctrl+C 退出）")
    print()

    awakened = not use_kws
    _last_tts = None
    _last_awakened = None

    _stream_started = False
    _last_stream_text = ""

    try:
        with open_mic() as mic:
            while True:
                chunk_16k = read_chunk(mic)

                cur_tts = tts_speaking.is_set()
                if cur_tts != _last_tts or awakened != _last_awakened:
                    log.debug("[状态] tts_speaking=%s, awakened=%s", cur_tts, awakened)
                    _last_tts = cur_tts
                    _last_awakened = awakened

                if tts_speaking.is_set():
                    vad.reset()
                    asr_stream = asr.create_stream()
                    if kws:
                        kws.reset(kws_stream)
                    _stream_started = False
                    _last_stream_text = ""
                    continue

                if use_kws and not awakened:
                    kws.accept(kws_stream, chunk_16k)
                    result = kws.get_result(kws_stream)
                    if result:
                        print()
                        log.info("[KWS] 唤醒：%s", result)
                        kws.reset(kws_stream)
                        awakened = True
                        if not asr_only:
                            speak_async(player, tts, "我在")
                    continue

                if asr_display_mode == "sentence":
                    vad.feed(chunk_16k)
                    if vad.has_speech():
                        t_vad_trigger = time.time()
                        vad.flush()
                        while vad.has_speech():
                            audio, start_sec, duration = vad.pop_speech()
                            if audio is None:
                                break
                            vad.pop()
                            if duration < 0.3:
                                continue

                            t_send_asr = time.time()
                            log.info("[TIMING] 送 ASR：音频=%.2fs, t=%.3f", duration, t_send_asr)

                            #import soundfile as sf
                            #sf.write(f"/tmp/vad_seg_{int(time.time())}.wav", audio, 16000)

                            text = asr.decode(asr_stream, audio).strip()

                            t_asr_done = time.time()

                            t_speech_start = vad.speech_start_time
                            t_speech_end = vad.speech_end_time

                            if t_speech_start is not None:
                                total_latency = t_asr_done - t_speech_start
                                speech_duration = (
                                    (t_speech_end - t_speech_start)
                                    if t_speech_end else 0
                                )
                                after_speech = (
                                    (t_asr_done - t_speech_end)
                                    if t_speech_end else 0
                                )
                                asr_time = t_asr_done - t_send_asr
                                vad_wait = t_send_asr - t_vad_trigger

                                log.info(
                                    "[TIMING] 语音开始(t=%.3f) → ASR输出(t=%.3f) | "
                                    "总延迟=%.3fs | 说话=%.2fs | 说完后=%.3fs "
                                    "(VAD等静音≈%.3fs + ASR=%.3fs)",
                                    t_speech_start, t_asr_done,
                                    total_latency,
                                    speech_duration,
                                    after_speech,
                                    vad_wait,
                                    asr_time,
                                )

                            if text:
                                log.info("[ASR] \033[32m%s\033[0m", text)
                                handle_asr_text(text)

                        vad.reset()

                        if use_kws:
                            awakened = False

                else:
                    vad.feed(chunk_16k)

                    if vad.has_speech() or _stream_started:
                        _stream_started = True
                        text = asr.decode_stream(asr_stream, chunk_16k)

                        if text and text != _last_stream_text:
                            sys.stdout.write(f"\r[ASR] {text}")
                            sys.stdout.flush()
                            _last_stream_text = text

                        if asr.is_endpoint(asr_stream) or vad.has_speech():
                            if _last_stream_text:
                                sys.stdout.write("\n")
                                sys.stdout.flush()
                                log.info("[ASR] %s", _last_stream_text)
                                handle_asr_text(_last_stream_text)

                            asr_stream = asr.create_stream()
                            vad.reset()
                            _stream_started = False
                            _last_stream_text = ""

                            if use_kws:
                                awakened = False

    except KeyboardInterrupt:
        print()
        log.info("已退出。")
    except Exception as e:
        log.exception("主循环异常：%s", e)
    finally:
        stop_event.set()
        try:
            voxcpm_play_queue.put_nowait(None)
        except Exception:
            pass
        try:
            player.stop()
        except Exception as e:
            log.warning("[AudioPlayer] 停止异常：%s", e)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", type=str, default="")
    parser.add_argument("--http", action="store_true")
    parser.add_argument("--port", type=int, default=TEXT_INPUT_PORT)
    parser.add_argument("--no-kws", action="store_true")
    parser.add_argument("--chat-tts", choices=["voxcpm", "sherpa"],
                        default=CHAT_TTS_MODE_DEFAULT,
                        help="DeepSeek 结果的 TTS 引擎，默认 voxcpm")
    parser.add_argument("--asr-mode", choices=["sentence", "streaming"],
                        default=ASR_DISPLAY_MODE,
                        help="ASR 显示模式，默认 sentence（整句）")
    parser.add_argument("--asr-only", action="store_true",
                        help="只显示 ASR 文本，不调 MQTT / DeepSeek / TTS")
    args = parser.parse_args()
    main(start_http=args.http, http_port=args.port,
         text_input=args.text, use_kws=not args.no_kws,
         chat_tts=args.chat_tts,
         asr_display_mode=args.asr_mode,
         asr_only=args.asr_only)

