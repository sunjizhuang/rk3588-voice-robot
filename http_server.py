"""HTTP 文本输入接口，带 API Key 校验。"""
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler


class TextInputHandler(BaseHTTPRequestHandler):
    tts_engine = None
    api_key = ""
    speak_func = None

    def do_POST(self):
        if self.path != "/text_input":
            self._json_response(404, {"error": "not found"})
            return
        if not self.api_key or self.headers.get("X-API-Key", "") != self.api_key:
            self._json_response(401, {"error": "unauthorized"})
            return

        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len)
        try:
            data = json.loads(body)
            text = data.get("text", "").strip()
            sid = data.get("sid", None)
            speed = data.get("speed", None)
        except Exception:
            text, sid, speed = "", None, None

        if not text:
            self._json_response(400, {"error": "text is required"})
            return
        try:
            self.speak_func(self.tts_engine, text, sid=sid, speed=speed)
            self._json_response(200, {"status": "ok", "text": text})
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _json_response(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def log_message(self, fmt, *args):
        pass


def start_text_input_server(tts_engine, api_key: str, speak_func, port: int = 8765):
    if not api_key:
        print("[TextInput] 警告：API Key 为空，接口将拒绝所有请求")
    TextInputHandler.tts_engine = tts_engine
    TextInputHandler.api_key = api_key
    TextInputHandler.speak_func = speak_func
    server = HTTPServer(("0.0.0.0", port), TextInputHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[TextInput] HTTP 接口已启动: http://0.0.0.0:{port}/text_input")
    return server

