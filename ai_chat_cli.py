#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RK3588 / Ubuntu 20.04 CLI 大模型对话工具（带 VoxCPM TTS 语音输出）
支持服务商：智谱 GLM、DeepSeek
支持模式：交互模式 / 命令行直传文本，普通输出 / 流式输出
TTS：通过独立 JSON 配置文件（默认 ./tts_config.json）启用

- 流式模式：LLM 边吐 token，边按“句末标点为主 + 逗号辅助（累积 > N）”切句，
  立即送入 TTS 生成线程池；文字照常打印，语音后台异步播放。
- 普通模式：LLM 一次性返回后按同一套规则切句，走同一套 TTS 管线。
- 语音不打断新对话：用户敲新问题时，旧语音继续播完，新对话并行处理。
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from openai import OpenAI

# ==================== 服务商配置（统一参数管理） ====================
PROVIDERS = {
    "zhipu": {
        "name": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "default_model": "glm-4-flash",
        "api_key_env": "ZHIPUAI_API_KEY",
        "params": {
            "temperature": 0.7,
            "max_tokens": 1024,
            "top_p": 1.0,
            "frequency_penalty": 0.0,
            "presence_penalty": 0.0,
        },
    },
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "default_model": "deepseek-flash",
        "api_key_env": "DEEPSEEK_API_KEY",
        "params": {
            "temperature": 0.7,
            "max_tokens": 1024,
            "top_p": 1.0,
            "frequency_penalty": 0.0,
            "presence_penalty": 0.0,
        },
    },
}
# ====================================================================

DEFAULT_PROVIDER = "zhipu"
#DEFAULT_SYSTEM = "我是灵境机器人智能助手，请简洁、准确地回答问题。"
DEFAULT_SYSTEM = (
    "我是灵境机器人智能助手，请简洁、准确地回答问题。"
    "请用纯文本回答，不要使用 Markdown 格式（不要用 *、#、`、- 等符号），"
    "不要使用中文冒号、引号、书名号。"
    "请控制在 150 字以内。"
)
DEFAULT_TTS_CONFIG = "./tts_config.json"


# ==================== 命令行参数 ====================
def parse_args():
    parser = argparse.ArgumentParser(
        description="RK3588 CLI 大模型对话终端（支持智谱 GLM / DeepSeek / VoxCPM TTS）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python3 ai_chat_cli.py                                      # 交互模式（默认读取 ./tts_config.json）
  python3 ai_chat_cli.py --stream                             # 流式输出（+ 语音）
  python3 ai_chat_cli.py --tts "你好"                          # 强制启用 TTS
  python3 ai_chat_cli.py --no-tts "你好"                       # 禁用 TTS
  python3 ai_chat_cli.py --tts-profile robot --stream         # 切换声音 profile
  python3 ai_chat_cli.py --tts-comma-threshold 12 --stream    # 覆盖逗号辅助切分阈值 N
  python3 ai_chat_cli.py -p deepseek --stream "分析这段代码"
        """
    )
    parser.add_argument("text", nargs="*", help="直接传入要提问的文本；不传则进入交互模式")
    parser.add_argument(
        "-p", "--provider",
        choices=list(PROVIDERS.keys()),
        default=DEFAULT_PROVIDER,
        help=f"服务商，默认 {DEFAULT_PROVIDER}（可选：{', '.join(PROVIDERS.keys())}）"
    )
    parser.add_argument("--stream", action="store_true", help="启用流式输出（逐字打印）")
    parser.add_argument("-m", "--model", default=None, help="模型编码，不指定则使用服务商默认模型")
    parser.add_argument("--api-key", default=None, help="API Key；不传则读取对应环境变量")
    parser.add_argument("--system", default=DEFAULT_SYSTEM, help="系统提示词")

    # ---- 生成参数：默认 None，不传时使用服务商配置中的值 ----
    parser.add_argument("--temperature", type=float, default=None,
                        help="温度；不传则使用服务商默认值")
    parser.add_argument("--max-tokens", type=int, default=None,
                        help="单次回复最大 token 数；不传则使用服务商默认值")
    parser.add_argument("--top-p", type=float, default=None,
                        help="top_p 采样；不传则使用服务商默认值")
    parser.add_argument("--frequency-penalty", type=float, default=None,
                        help="频率惩罚；不传则使用服务商默认值")
    parser.add_argument("--presence-penalty", type=float, default=None,
                        help="存在惩罚；不传则使用服务商默认值")

    # ---- TTS 相关 ----
    tts = parser.add_mutually_exclusive_group()
    tts.add_argument("--tts", action="store_true",
                     help="强制启用 TTS（即使配置文件不存在或 enabled=false）")
    tts.add_argument("--no-tts", action="store_true",
                     help="强制禁用 TTS")
    parser.add_argument("--tts-config", default=DEFAULT_TTS_CONFIG,
                        help=f"TTS 配置文件路径，默认 {DEFAULT_TTS_CONFIG}")
    parser.add_argument("--tts-profile", default=None,
                        help="选择 TTS profile（覆盖配置中的 active_profile）")
    parser.add_argument("--tts-comma-threshold", type=int, default=None,
                        help="覆盖配置中的逗号辅助切分阈值 N")
    parser.add_argument("--tts-no-play", action="store_true",
                        help="只生成音频文件，不自动播放")

    return parser.parse_args()


def resolve_config(args):
    """
    解析最终 LLM 配置：
      - base_url / model / api_key 来自服务商配置 + 命令行覆盖
      - params（temperature/max_tokens 等）以服务商配置为基础，命令行显式传入覆盖
    """
    provider = PROVIDERS[args.provider]
    base_url = provider["base_url"]
    model = args.model or provider["default_model"]

    api_key = args.api_key or os.getenv(provider["api_key_env"])
    if not api_key:
        print(
            f"错误：未提供 {provider['name']} 的 API Key。\n"
            f"请设置环境变量 {provider['api_key_env']}，或使用 --api-key 参数。",
            file=sys.stderr
        )
        sys.exit(1)

    params = dict(provider["params"])
    overrides = {
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
        "top_p": args.top_p,
        "frequency_penalty": args.frequency_penalty,
        "presence_penalty": args.presence_penalty,
    }
    for key, value in overrides.items():
        if value is not None:
            params[key] = value

    return base_url, model, api_key, params


# ==================== TTS 配置加载 ====================
def _build_gen_request_from_profile(profile: Dict[str, Any]):
    """从 profile 字典构造 GenRequest（延迟导入 ai_voxcpm_cli）。"""
    from ai_voxcpm_cli import GenRequest
    mode = (profile.get("mode") or "design").lower()
    use_ultimate = mode == "ultimate"
    use_library = mode == "library"
    ref = profile.get("reference_wav") if mode in ("clone", "ultimate") else None
    ctrl = profile.get("control_instruction") or ""
    if use_ultimate:
        ctrl = ""
    return GenRequest(
        text="",  # 占位，每段会用实际文本覆盖
        control_instruction=ctrl,
        reference_wav=ref,
        voice_id=(profile.get("voice_id") or "") if use_library else "",
        use_ultimate_cloning=use_ultimate,
        prompt_text=(profile.get("transcript") or "") if use_ultimate else "",
        cfg_value=float(profile.get("cfg_value", 2.0)),
        do_normalize=bool(profile.get("do_normalize", False)),
        denoise=bool(profile.get("denoise", False)),
        dit_steps=int(profile.get("dit_steps", 10)),
        seed=None,
        random_seed=True,
    )


def setup_tts(args) -> Optional[Dict[str, Any]]:
    """加载配置，构建 TTSRuntime 和 tts_ctx。返回 None 表示不启用 TTS。"""
    if args.no_tts:
        print("ℹ️  TTS：已禁用（--no-tts）")
        return None

    cfg_path = Path(args.tts_config).expanduser()
    cfg: Dict[str, Any] = {}
    if cfg_path.exists():
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️  TTS 配置读取失败（{cfg_path}）：{exc}")
            if not args.tts:
                return None
    else:
        if not args.tts:
            # 无配置文件、也没 --tts → 静默不启用
            return None
        print(f"ℹ️  TTS 配置文件不存在（{cfg_path}），使用内置默认。")

    enabled = bool(cfg.get("enabled", True)) if cfg else True
    if not enabled and not args.tts:
        print(f"ℹ️  TTS：配置文件中 enabled=false，未启用（可用 --tts 强制开启）")
        return None

    # 延迟导入，避免未安装 gradio_client 时影响无 TTS 场景
    from ai_voxcpm_cli import (
        TTSRuntime, _build_backend, DEFAULT_OUT_DIR,
    )

    server = cfg.get("server") or "http://127.0.0.1:8808"
    api_name = cfg.get("api_name") or "/generate"
    backend_prefer = cfg.get("backend") or "auto"
    out_dir = Path(cfg.get("out_dir") or DEFAULT_OUT_DIR).expanduser().resolve()
    auto_play = bool(cfg.get("auto_play", True)) and not args.tts_no_play
    gapless = bool(cfg.get("gapless", True))
    workers = int(cfg.get("stream_workers", 4))
    seed = cfg.get("seed", None)

    split_cfg = cfg.get("split") or {}
    comma_threshold = (
        args.tts_comma_threshold
        if args.tts_comma_threshold is not None
        else int(split_cfg.get("comma_threshold", 20))
    )
    min_chars = int(split_cfg.get("min_chars", 4))

    profiles = cfg.get("profiles") or {}
    active = args.tts_profile or cfg.get("active_profile") or "default"
    if active not in profiles:
        if profiles:
            print(f"⚠️  profile '{active}' 不存在，回退到第一个：{list(profiles.keys())[0]}")
            active = list(profiles.keys())[0]
        else:
            profiles = {"default": {"mode": "design", "control_instruction": ""}}
            active = "default"

    try:
        backend = _build_backend(server, api_name, backend_prefer)
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️  TTS 后端初始化失败（server={server}）：{exc}")
        if not args.tts:
            return None
        raise

    runtime = TTSRuntime(
        backend=backend,
        out_dir=out_dir,
        auto_play=auto_play,
        max_workers=workers,
        gapless=gapless,
        seed=seed,
    )
    runtime.start()

    base_req = _build_gen_request_from_profile(profiles[active])

    print(
        f"🔊 TTS 已启用：server={server} profile={active} "
        f"N={comma_threshold} gapless={gapless} play={auto_play} seed={runtime.seed}"
    )

    return {
        "runtime": runtime,
        "base_req": base_req,
        "comma_threshold": comma_threshold,
        "min_chars": min_chars,
    }


# ==================== 对话核心 ====================
def chat_once(client, model, messages, params, stream, tts_ctx=None):
    """调用一次 API。若有 tts_ctx，则把回复送进 TTS 管线。

    返回值为本轮助手的完整回复文本（无论是否流式）。
    """
    request_kwargs = dict(model=model, messages=messages, stream=stream, **params)

    tts_session = None
    splitter = None
    if tts_ctx is not None:
        from ai_voxcpm_cli import IncrementalSplitter
        tts_session = tts_ctx["runtime"].new_session(tts_ctx["base_req"])
        splitter = IncrementalSplitter(
            comma_threshold=tts_ctx["comma_threshold"],
            min_chars=tts_ctx["min_chars"],
        )

    if stream:
        response = client.chat.completions.create(**request_kwargs)
        collected: List[str] = []
        for chunk in response:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta and delta.content:
                print(delta.content, end="", flush=True)
                collected.append(delta.content)
                if splitter is not None:
                    for seg in splitter.feed(delta.content):
                        tts_session.push(seg)
        print()
        if tts_session is not None:
            for seg in splitter.flush():
                tts_session.push(seg)
            tts_session.finish()
        return "".join(collected)

    # 普通模式
    response = client.chat.completions.create(**request_kwargs)
    reply = response.choices[0].message.content or ""
    print(reply)
    if tts_session is not None:
        from ai_voxcpm_cli import split_text_for_streaming
        for seg in split_text_for_streaming(
            reply,
            max_chars=tts_ctx["comma_threshold"],
            min_chars=tts_ctx["min_chars"],
        ):
            tts_session.push(seg)
        tts_session.finish()
    return reply


def interactive(client, args, model, params, tts_ctx=None):
    """交互模式：多轮对话，保留上下文。"""
    provider_name = PROVIDERS[args.provider]["name"]
    history = []

    print("=" * 60)
    print(f"  RK3588 大模型 CLI  |  {provider_name}")
    print(f"  模型: {model}  |  流式: {'开' if args.stream else '关'}  |  "
          f"TTS: {'开' if tts_ctx else '关'}")
    print(f"  参数: temperature={params['temperature']}, "
          f"max_tokens={params['max_tokens']}, top_p={params['top_p']}")
    print("  输入 exit / quit / :q 退出")
    print("=" * 60)

    while True:
        try:
            user_input = input("\n你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", ":q"):
            print("再见！")
            break

        messages = [{"role": "system", "content": args.system}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_input})

        print("助手: ", end="", flush=True)
        try:
            reply = chat_once(
                client, model, messages, params, args.stream, tts_ctx=tts_ctx
            )
            history.append({"role": "user", "content": user_input})
            history.append({"role": "assistant", "content": reply})
        except Exception as e:  # noqa: BLE001
            print(f"\n[请求出错] {e}", file=sys.stderr)


def main():
    args = parse_args()
    base_url, model, api_key, params = resolve_config(args)

    client = OpenAI(api_key=api_key, base_url=base_url)

    # 先加载 TTS（可能因 server 不可用而返回 None）
    tts_ctx = setup_tts(args)

    try:
        if args.text:
            user_text = " ".join(args.text)
            messages = [
                {"role": "system", "content": args.system},
                {"role": "user", "content": user_text},
            ]
            try:
                chat_once(client, model, messages, params, args.stream, tts_ctx=tts_ctx)
            except Exception as e:  # noqa: BLE001
                print(f"[请求出错] {e}", file=sys.stderr)
                sys.exit(1)
        else:
            interactive(client, args, model, params, tts_ctx=tts_ctx)
    finally:
        if tts_ctx is not None:
            print("\n🔊 等待语音播放完成…")
            try:
                tts_ctx["runtime"].shutdown(wait=True)
            except Exception as exc:  # noqa: BLE001
                print(f"⚠️  TTS 关闭异常：{exc}")


if __name__ == "__main__":
    main()

