"""动作分流 + MQTT 发送 + subprocess 调 ai_chat_cli.py（含 VoxCPM 降级）。"""
import json
import subprocess
import sys

from action_mapping import WORD_ID_TO_ACTION_STR, UNSUPPORTED_WORD_IDS


class ActionRouter:
    def __init__(self, config_path: str,
                 send_mqtt_script: str = "./send_mqtt.sh",
                 device: str = "lite3",
                 chat_cli: str = "ai_chat_cli.py",
                 chat_timeout: int = 120,
                 chat_tts_mode: str = "voxcpm"):
        self.send_mqtt_script = send_mqtt_script
        self.device = device
        self.chat_cli = chat_cli
        self.chat_timeout = chat_timeout
        self.chat_tts_mode = chat_tts_mode

        with open(config_path, "r", encoding="utf-8") as f:
            self.action_map: dict = json.load(f)

        # 按 key 长度降序，保证最长匹配优先
        self.sorted_keys = sorted(self.action_map.keys(), key=len, reverse=True)

    # ---------- 匹配 ----------
    def match(self, text: str):
        """最长包含匹配。返回 (action_key, word_id) 或 (None, None)。"""
        if not text:
            return None, None
        for key in self.sorted_keys:
            if key in text:
                return key, self.action_map[key]
        return None, None

    # ---------- 分流 ----------
    def route(self, text: str):
        """
        返回:
          ("action", action_str)       命中已支持动作
          ("unsupported", action_key)  命中未支持动作
          ("chat", None)               未命中，走 DeepSeek
        """
        key, word_id = self.match(text)
        if key is None:
            return "chat", None
        if word_id in UNSUPPORTED_WORD_IDS or word_id not in WORD_ID_TO_ACTION_STR:
            return "unsupported", key
        return "action", WORD_ID_TO_ACTION_STR[word_id]

    # ---------- MQTT ----------
    def send_action(self, action_str: str) -> bool:
        payload = json.dumps({
            "cmd": "action", "action": action_str, "device": self.device,
        }, ensure_ascii=False)
        try:
            ret = subprocess.run(
                [self.send_mqtt_script, payload],
                capture_output=True, text=True, timeout=5,
            )
            if ret.returncode == 0:
                print(f"[MQTT] 已发送: {payload}")
                return True
            print(f"[MQTT] 发送失败: {ret.stderr.strip()}")
            return False
        except subprocess.TimeoutExpired:
            print("[MQTT] 发送超时")
            return False
        except Exception as e:
            print(f"[MQTT] 异常: {e}")
            return False

    # ---------- DeepSeek 查询 ----------
    def _build_cmd(self, text: str, no_tts: bool):
        cmd = [sys.executable, self.chat_cli, "--stream"]
        if no_tts:
            cmd.append("--no-tts")
        cmd.append(text)
        return cmd

    def _run(self, text: str, no_tts: bool):
        try:
            ret = subprocess.run(
                self._build_cmd(text, no_tts),
                capture_output=True, text=True, timeout=self.chat_timeout,
            )
            return ret.returncode, ret.stdout or "", ret.stderr or ""
        except subprocess.TimeoutExpired:
            return -1, "", "timeout"
        except Exception as e:
            return -1, "", str(e)

    def chat(self, text: str):
        """
        返回 (answer, error, tts_action)
          tts_action: "voxcpm_played" | "need_sherpa"
        """
        if self.chat_tts_mode == "voxcpm":
            rc, out, err = self._run(text, no_tts=False)
            if rc == 0:
                answer = self._clean_stdout(out)
                print("[ChatCLI] VoxCPM 模式成功（已自动播报）")
                return answer, "", "voxcpm_played"

            print(f"[ChatCLI] VoxCPM 模式失败（rc={rc}），降级到 sherpa-onnx")
            rc2, out2, err2 = self._run(text, no_tts=True)
            if rc2 == 0:
                return self._clean_stdout(out2), "", "need_sherpa"
            return "", (err2 or err or "查询失败").strip(), "need_sherpa"

        rc, out, err = self._run(text, no_tts=True)
        if rc == 0:
            return self._clean_stdout(out), "", "need_sherpa"
        return "", (err or "查询失败").strip(), "need_sherpa"

    def _clean_stdout(self, raw: str) -> str:
        """过滤启动/提示行，保留回复正文。"""
        lines = []
        for line in (raw or "").splitlines():
            s = line.strip()
            if not s:
                continue
            if s[0] in "ℹ🔊⚠✅❌":
                continue
            if s.startswith("助手:"):
                s = s[len("助手:"):].strip()
            lines.append(s)
        return "\n".join(lines) if lines else ""

