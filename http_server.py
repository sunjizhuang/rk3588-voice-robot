"""HTTP 服务：Web 对话界面（类微信 UI）+ SSE 推送 + 文本指令下发。"""
import json
import os
import threading
import traceback
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

from event_bus import event_bus

HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>灵境小助手</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { 
    font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
    background: #f3f5f9; height: 100vh; height: 100dvh;
    display: flex; flex-direction: column; overflow: hidden;
  }
  header {
    padding: 16px 20px; background: #07c160; color: white;
    font-size: 18px; font-weight: 600; text-align: center;
    box-shadow: 0 1px 4px rgba(0,0,0,0.1); position: relative; flex-shrink: 0;
  }
  #conn-status {
    position: absolute; right: 16px; top: 50%; transform: translateY(-50%);
    font-size: 13px; font-weight: normal; display: flex; align-items: center; gap: 4px;
  }
  .dot-conn { width: 8px; height: 8px; border-radius: 50%; background: #ccc; }
  .dot-conn.connected { background: #fff; }
  .dot-conn.disconnected { background: #f48771; }

  #chat {
    flex: 1; overflow-y: auto; padding: 20px;
    display: flex; flex-direction: column; gap: 20px;
  }

  .msg { display: flex; width: 100%; align-items: flex-start; gap: 12px; }
  .msg.bot { align-self: flex-start; }
  .msg.user { align-self: flex-end; flex-direction: row-reverse; }

  .avatar { 
    width: 32px; height: 32px; border-radius: 6px; flex-shrink: 0;
    background-size: cover; background-position: center; background-color: #eee;
  }
  @media (min-width: 768px) { .avatar { width: 40px; height: 40px; border-radius: 8px; } }
  .msg.bot .avatar { background-image: url('/static/Aetherlin.png'); border: 1px solid #e0e0e0; }
  .msg.user .avatar { background-image: url('/static/you.png'); }

  .bubble-wrapper { display: flex; flex-direction: column; gap: 4px; max-width: calc(100% - 50px); }
  @media (min-width: 768px) { .bubble-wrapper { max-width: 75%; } }
  .msg.user .bubble-wrapper { align-items: flex-end; }

  .bubble { 
    padding: 12px 16px; border-radius: 12px; font-size: 15px;
    line-height: 1.5; word-wrap: break-word; position: relative;
  }
  /* ★ 核心修改：机器人（左侧）使用纯白底色 */
  .msg.bot .bubble { 
    background: #ffffff; 
    color: #333333; 
    border: 1px solid #e5e5e5;
    border-top-left-radius: 2px; 
    box-shadow: 0 1px 3px rgba(0,0,0,0.05);
  }
  /* 用户（右侧）保持微信绿 */
  .msg.user .bubble { 
    background: #95ec69; 
    color: #1a1a1a; 
    border-top-right-radius: 2px; 
  }

  .msg.system { justify-content: center; width: 100%; margin: 10px 0; }
  .msg.system .bubble {
    background: rgba(0,0,0,0.05); color: #888; font-size: 13px;
    padding: 6px 12px; border-radius: 8px; text-align: center;
    border: none; box-shadow: none;
  }
  .time { font-size: 11px; color: #999; margin-top: 2px; }

  #status-bar {
    display: flex; justify-content: center; align-items: center; gap: 40px;
    padding: 10px; background: #f8f9fa; border-top: 1px solid #ddd;
    font-size: 14px; color: #999; flex-shrink: 0;
  }
  .status-item {
    display: flex; align-items: center; gap: 6px;
    opacity: 0.4; transition: all 0.2s;
  }
  .status-item.active { opacity: 1; color: #07c160; font-weight: 600; transform: scale(1.1); }

  #footer { 
    padding: 12px 20px; background: #fff; border-top: 1px solid #eee;
    display: flex; gap: 10px; align-items: center; flex-shrink: 0;
  }
  #input-box { 
    flex: 1; padding: 12px 16px; border: 1px solid #ddd; border-radius: 24px;
    font-size: 15px; outline: none; background: #f8f9fa;
  }
  #input-box:focus { border-color: #07c160; background: #fff; }
  #send-btn { 
    background: #07c160; color: white; border: none; padding: 0 20px;
    height: 44px; border-radius: 24px; font-size: 15px; cursor: pointer; font-weight: 600;
  }
</style>
</head>
<body>

<header>
  灵境小助手
  <div id="conn-status">
    <div class="dot-conn" id="conn-dot"></div>
    <span id="conn-text">未连接</span>
  </div>
</header>

<div id="chat"></div>

<div id="status-bar">
  <div class="status-item" id="status-listen">🎤 听</div>
  <div class="status-item" id="status-think">💭 想</div>
  <div class="status-item" id="status-speak">🔊 说</div>
</div>

<div id="footer">
  <input type="text" id="input-box" placeholder="也可输入文字指令，按 Enter 发送..." autocomplete="off">
  <button id="send-btn">发送</button>
</div>

<script>
  const chat = document.getElementById('chat');
  const inputBox = document.getElementById('input-box');
  const sendBtn = document.getElementById('send-btn');
  const connDot = document.getElementById('conn-dot');
  const connText = document.getElementById('conn-text');
  const sListen = document.getElementById('status-listen');
  const sThink = document.getElementById('status-think');
  const sSpeak = document.getElementById('status-speak');

  function setStatus(state) {
    sListen.classList.remove('active');
    sThink.classList.remove('active');
    sSpeak.classList.remove('active');
    if (state === 'listen') sListen.classList.add('active');
    if (state === 'think') sThink.classList.add('active');
    if (state === 'speak') sSpeak.classList.add('active');
  }

  function formatTime(ts) {
    if (!ts) return '';
    return new Date(ts * 1000).toLocaleTimeString('zh-CN', { hour12: false });
  }

  function addMsg(role, text, ts) {
    const div = document.createElement('div');
    div.className = 'msg ' + role;

    const avatar = document.createElement('div');
    avatar.className = 'avatar';

    const wrapper = document.createElement('div');
    wrapper.className = 'bubble-wrapper';

    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    bubble.textContent = text;
    wrapper.appendChild(bubble);

    if (ts) {
      const time = document.createElement('div');
      time.className = 'time';
      time.textContent = formatTime(ts);
      wrapper.appendChild(time);
    }

    div.appendChild(avatar);
    div.appendChild(wrapper);
    chat.appendChild(div);
    chat.scrollTop = chat.scrollHeight;
  }

  function addSystem(text) {
    const div = document.createElement('div');
    div.className = 'msg system';
    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    bubble.textContent = text;
    div.appendChild(bubble);
    chat.appendChild(div);
    chat.scrollTop = chat.scrollHeight;
  }

  let lastMsgText = "";
  const now = () => Math.floor(Date.now() / 1000);

  function connectSSE() {
    const es = new EventSource('/events');
    es.onopen = () => {
      connDot.className = 'dot-conn connected';
      connText.textContent = '已连接';
      setStatus('listen');
    };
    es.onerror = () => {
      connDot.className = 'dot-conn disconnected';
      connText.textContent = '重连中...';
      setTimeout(connectSSE, 3000);
    };
    es.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        const d = msg.data || {};
        switch (msg.type) {
          case 'status_listen': setStatus('listen'); break;
          case 'status_think': setStatus('think'); break;
          case 'status_speak': setStatus('speak'); break;
          case 'kws': addSystem('✨ ' + d.text); setStatus('listen'); break;
          case 'asr':
          case 'user_text':
            if (d.text === lastMsgText) return;
            lastMsgText = d.text;
            addMsg('user', d.text, msg.ts);
            setStatus('think');
            break;
          case 'reply':
            addMsg('bot', d.text, msg.ts);
            setStatus('speak');
            break;
          case 'tts_done': setStatus('listen'); break;
        }
      } catch (err) {}
    };
  }

  // ★ 极速发送体验：点击立刻上屏用户气泡，不等待后端
  function sendText() {
    const text = inputBox.value.trim();
    if (!text) return;
    
    inputBox.value = '';
    lastMsgText = text; // 防止 SSE 回传造成重复气泡
    addMsg('user', text, now());
    setStatus('think');
    
    fetch('/text_input', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: text })
    }).catch((e) => {
      console.error('发送异常', e);
    });
  }

  sendBtn.addEventListener('click', sendText);
  inputBox.addEventListener('keypress', (e) => {
    if (e.key === 'Enter') sendText();
  });

  connectSSE();
</script>
</body>
</html>"""


class WebHandler(BaseHTTPRequestHandler):
    tts_engine = None
    api_key = ""
    on_text_input = None

    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Key")

    def do_OPTIONS(self):
        self.send_response(200)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/":
            self._html_response(HTML_PAGE)
            return

        if parsed.path == "/events":
            self._sse_stream()
            return

        if parsed.path.startswith("/static/"):
            filename = parsed.path.split("/")[-1]
            if "/" in filename or ".." in filename:
                self.send_response(400)
                self.end_headers()
                return
            filepath = os.path.join("assets", filename)
            try:
                with open(filepath, "rb") as f:
                    data = f.read()
                self.send_response(200)
                if filename.endswith(".png"): self.send_header("Content-Type", "image/png")
                elif filename.endswith(".jpg") or filename.endswith(".jpeg"): self.send_header("Content-Type", "image/jpeg")
                self.end_headers()
                self.wfile.write(data)
            except FileNotFoundError:
                self.send_response(404)
                self.end_headers()
            return

        self._json_response(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/text_input":
            self._json_response(404, {"error": "not found"})
            return

        has_api_key = self.headers.get("X-API-Key", "") != ""
        if has_api_key:
            if not self.api_key or self.headers.get("X-API-Key", "") != self.api_key:
                self._json_response(401, {"error": "unauthorized"})
                return

        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len)
        try:
            data = json.loads(body)
            text = data.get("text", "").strip()
        except Exception:
            text = ""

        if not text:
            self._json_response(400, {"error": "text is required"})
            return

        try:
            if self.on_text_input:
                self.on_text_input(text)
            self._json_response(200, {"status": "ok", "text": text})
        except Exception as e:
            traceback.print_exc()
            self._json_response(500, {"error": str(e)})

    def _html_response(self, html: str):
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _sse_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self._send_cors_headers()
        self.end_headers()

        q = event_bus.subscribe()
        try:
            self.wfile.write(b": init\n\n")
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=15)
                    data = json.dumps(msg, ensure_ascii=False)
                    self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except Exception:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            event_bus.unsubscribe(q)

    def _json_response(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def log_message(self, fmt, *args):
        pass


def start_web_server(tts_engine, api_key: str, on_text_input, port: int = 8765):
    WebHandler.tts_engine = tts_engine
    WebHandler.api_key = api_key
    WebHandler.on_text_input = on_text_input

    server = ThreadingHTTPServer(("0.0.0.0", port), WebHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[WebServer] Web 界面已启动: http://0.0.0.0:{port}/")
    return server

