"""常驻 sounddevice OutputStream 播放器，小缓冲，精确阻塞。"""
import logging
import threading
import wave

import numpy as np
import sounddevice as sd

log = logging.getLogger("robot")


class AudioPlayer:
    def __init__(self, samplerate: int = 48000, channels: int = 1,
                 device: int = None, blocksize: int = 1024,
                 latency: str = 'low'):
        self.samplerate = samplerate
        self.channels = channels
        self.device = device
        self.blocksize = blocksize
        self._lock = threading.Lock()

        self.stream = sd.OutputStream(
            samplerate=samplerate,
            channels=channels,
            dtype='float32',
            device=device,
            blocksize=blocksize,
            latency=latency,
        )
        self.stream.start()
        log.info("[AudioPlayer] OutputStream 已启动: sr=%d, ch=%d, "
                 "blocksize=%d, latency=%s, device=%s",
                 samplerate, channels, blocksize, latency, device)

    def play(self, samples: np.ndarray, src_rate: int, wait: bool = True):
        if src_rate != self.samplerate:
            n_out = int(len(samples) * self.samplerate / src_rate)
            indices = np.linspace(0, len(samples) - 1, n_out)
            samples = np.interp(indices, np.arange(len(samples)),
                                samples).astype(np.float32)

        samples = np.clip(samples, -1.0, 1.0)

        with self._lock:
            try:
                for i in range(0, len(samples), self.blocksize):
                    chunk = samples[i:i + self.blocksize]
                    self.stream.write(chunk)
            except Exception as e:
                log.error("[AudioPlayer] 写入失败：%s", e)

    def play_wav_file(self, path: str, wait: bool = True):
        try:
            with wave.open(path, 'rb') as wf:
                nchannels = wf.getnchannels()
                sampwidth = wf.getsampwidth()
                framerate = wf.getframerate()
                nframes = wf.getnframes()
                data = wf.readframes(nframes)
        except Exception as e:
            log.error("[AudioPlayer] 读取 wav 失败 %s: %s", path, e)
            return

        if sampwidth == 2:
            samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        elif sampwidth == 4:
            samples = np.frombuffer(data, dtype=np.int32).astype(np.float32) / 2147483648.0
        elif sampwidth == 1:
            samples = (np.frombuffer(data, dtype=np.uint8).astype(np.float32) - 128) / 128.0
        else:
            log.error("[AudioPlayer] 不支持的位深：%d", sampwidth)
            return

        if nchannels > 1:
            samples = samples.reshape(-1, nchannels).mean(axis=1)

        self.play(samples, framerate, wait=wait)

    def stop(self):
        try:
            self.stream.stop()
            self.stream.close()
            log.info("[AudioPlayer] OutputStream 已关闭")
        except Exception:
            pass

