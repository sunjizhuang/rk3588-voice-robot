"""线程安全事件总线，支持 SSE 多端同步与去重。"""
import queue
import threading
import time
import json

class EventBus:
    def __init__(self, history_size=50):
        self._subscribers = []
        self._lock = threading.Lock()
        self._history = []
        self._history_size = history_size

    def subscribe(self) -> queue.Queue:
        q = queue.Queue(maxsize=100)
        with self._lock:
            for msg in self._history:
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    break
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def publish(self, event_type: str, data: dict):
        # 构造消息
        msg = {"type": event_type, "data": data, "ts": time.time()}
        
        # ★ 简单的消息去重逻辑：若 2 秒内收到完全相同的 user_text 或 reply，则忽略
        with self._lock:
            if event_type in ("user_text", "reply", "asr") and self._history:
                last = self._history[-1]
                if (last["type"] == event_type and 
                    last["data"].get("text") == data.get("text") and 
                    msg["ts"] - last["ts"] < 2.0):
                    return  # 忽略重复

            self._history.append(msg)
            if len(self._history) > self._history_size:
                self._history.pop(0)

            dead = []
            for q in self._subscribers:
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                try:
                    self._subscribers.remove(q)
                except ValueError:
                    pass

event_bus = EventBus()
