#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import sys
import time
import wave
import queue
import threading
import subprocess

# ============ 配置 ============
DEFAULT_WATCH_DIR = "/root/ljjs_rk3588/rk3588-voice-robot/outputs"
POLL_INTERVAL = 0.5          # 扫描间隔（调小，一轮能同时看到多个文件）
STABLE_CHECKS = 2            # 跨轮次累计：连续几次大小不变认为写完
APLAY_CMD = "aplay"

# ============ 全局 ============
play_queue = queue.Queue()
played_set = set()           # (path, size, mtime)
played_lock = threading.Lock()

# 跨轮次记录每个文件的大小稳定状态：path -> (last_size, stable_count)
stable_state = {}
stable_lock = threading.Lock()


# ============ 播放去重 ============
def is_played(path):
    try:
        st = os.stat(path)
        key = (os.path.abspath(path), st.st_size, st.st_mtime)
    except OSError:
        return False
    with played_lock:
        return key in played_set


def mark_played(path):
    try:
        st = os.stat(path)
        key = (os.path.abspath(path), st.st_size, st.st_mtime)
    except OSError:
        return
    with played_lock:
        played_set.add(key)


# ============ 文件名时间解析 ============
# 匹配 voxcpm_0001_044_20260924_163352_462939.wav 中的时间部分
# 捕获：年 月 日 时 分 秒 微秒
TIME_RE = re.compile(r'(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})_(\d{6})')


def extract_time_key(name):
    """
    从文件名解析时间，返回 (年,月,日,时,分,秒,微秒)。
    解析失败返回 None。
    """
    m = TIME_RE.search(name)
    if not m:
        return None
    return tuple(int(x) for x in m.groups())


def sort_key(path):
    """
    排序键：
      1) 优先用文件名中的时间戳（7 段）
      2) 解析不到时用 mtime 兜底
      3) 最后用文件名兜底，保证稳定排序
    """
    name = os.path.basename(path)
    t = extract_time_key(name)
    if t is not None:
        return (0, t, name)          # 有时间：优先级最高
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0
    return (1, mtime, name)          # 没时间：按 mtime


# ============ 完整性检查（非阻塞，跨轮次累计） ============
def is_wav_complete(path):
    """
    非阻塞检查：每轮只 getsize 一次，不 sleep。
    跨轮次累计大小连续 STABLE_CHECKS 次不变，且能正常打开读取。
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return False

    with stable_lock:
        last_size, stable = stable_state.get(path, (-1, 0))
        if size == last_size and size > 0:
            stable += 1
        else:
            stable = 0
        stable_state[path] = (size, stable)

    if stable < STABLE_CHECKS:
        return False

    # 大小连续 STABLE_CHECKS 次不变，再验证能否正常打开
    try:
        with wave.open(path, 'rb') as wf:
            wf.getparams()
        return True
    except Exception:
        return False


def clear_stable_state(path):
    """文件入队后清理稳定状态，避免残留"""
    with stable_lock:
        stable_state.pop(path, None)


# ============ 常驻 aplay 播放器 ============
class ResidentAplay:
    def __init__(self):
        self.proc = None
        self.params = None   # (nchannels, sampwidth, framerate)

    def _start(self, nchannels, sampwidth, framerate):
        # sampwidth: 1->U8, 2->S16_LE, 3->S24_3LE, 4->S32_LE
        fmt_map = {1: 'U8', 2: 'S16_LE', 3: 'S24_3LE', 4: 'S32_LE'}
        fmt = fmt_map.get(sampwidth, 'S16_LE')
        cmd = [
            APLAY_CMD,
            '-t', 'raw',
            '-f', fmt,
            '-r', str(framerate),
            '-c', str(nchannels),
            '-',
        ]
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.params = (nchannels, sampwidth, framerate)

    def ensure_params(self, nchannels, sampwidth, framerate):
        if self.proc is None or self.proc.poll() is not None:
            self._start(nchannels, sampwidth, framerate)
            return
        if self.params != (nchannels, sampwidth, framerate):
            # 参数变化，重启 aplay
            self.stop()
            self._start(nchannels, sampwidth, framerate)

    def play_wav(self, path):
        with wave.open(path, 'rb') as wf:
            nchannels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()
            self.ensure_params(nchannels, sampwidth, framerate)

            # 分块读取写入 aplay stdin，避免大文件占内存
            chunk_frames = 4096
            while True:
                data = wf.readframes(chunk_frames)
                if not data:
                    break
                try:
                    self.proc.stdin.write(data)
                    self.proc.stdin.flush()
                except BrokenPipeError:
                    # aplay 挂了，重启后重试当前块
                    self._start(nchannels, sampwidth, framerate)
                    self.proc.stdin.write(data)
                    self.proc.stdin.flush()

    def stop(self):
        if self.proc:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=2)
            except Exception:
                self.proc.kill()
            self.proc = None
            self.params = None


# ============ 播放线程 ============
def player_loop():
    player = ResidentAplay()
    try:
        while True:
            path = play_queue.get()
            if path is None:
                break
            try:
                player.play_wav(path)
            except Exception as e:
                print(f"[播放失败] {path}: {e}", file=sys.stderr)
            finally:
                play_queue.task_done()
    finally:
        player.stop()


# ============ 监控线程 ============
def watch_loop(watch_dir):
    # 启动基线：已有文件全部标记为已播放，不重复播
    for name in os.listdir(watch_dir):
        if name.lower().endswith('.wav'):
            mark_played(os.path.join(watch_dir, name))

    while True:
        try:
            # 1. 先收集本轮所有"已写完且未播放"的文件（非阻塞）
            pending = []
            for name in os.listdir(watch_dir):
                if not name.lower().endswith('.wav'):
                    continue
                path = os.path.join(watch_dir, name)
                if is_played(path):
                    continue
                if is_wav_complete(path):
                    pending.append(path)

            # 2. 按文件名中的时间戳排序（不是 completion_time）
            pending.sort(key=sort_key)

            # 3. 排序后依次入队
            for path in pending:
                mark_played(path)
                clear_stable_state(path)
                print(f"[入队] {path}")
                play_queue.put(path)

        except Exception as e:
            print(f"[监控异常] {e}", file=sys.stderr)
        time.sleep(POLL_INTERVAL)


def main():
    watch_dir = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_WATCH_DIR
    if not os.path.isdir(watch_dir):
        print(f"目录不存在: {watch_dir}", file=sys.stderr)
        sys.exit(1)

    t_player = threading.Thread(target=player_loop, daemon=True)
    t_player.start()

    try:
        watch_loop(watch_dir)
    except KeyboardInterrupt:
        play_queue.put(None)
        print("\n退出")


if __name__ == '__main__':
    main()

