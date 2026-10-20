#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HY-MT2 翻译客户端（独立运行 + 模块导入）

两种使用方式：
    1) 作为模块导入::

        from translate_cli import translate, LANGUAGES
        result = translate("你好世界", target_lang="English")

    2) 命令行独立使用::

        python translate_cli.py "你好世界" --target English
        python translate_cli.py --file input.txt --target English
        echo "你好世界" | python translate_cli.py
        python translate_cli.py --list-languages
        python translate_cli.py --show-config

实现要点（社区最佳实践）：
    - 使用 /v1/chat/completions 端点，配合 llama-server --jinja 自动应用聊天模板
    - System + User 分离：system 承载翻译指令，user 承载待翻译文本
    - 采样参数：temperature=0.7, top_p=0.6, top_k=20, repetition_penalty=1.05
    - 停止标记：使用模型原生 EOS token，不用 "\\n"（会误伤前导换行）
    - 长文本：按 TRANSLATE_CHUNK_SIZE 分块，超长句允许累积到 2× 后硬切
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import OrderedDict

import requests

# ============================================================
# 内部固定常量（不对外暴露）
# ============================================================
_TIMEOUT = 60              # 单次请求超时（秒）
_MAX_TOKENS = 128          # 单次生成最大 token 数

# 采样参数（官方推荐值）
_TEMPERATURE = 0.7
_TOP_P = 0.6
_TOP_K = 20
_REPETITION_PENALTY = 1.05

# 停止标记：模型原生 EOS + 兜底
# <｜hy_end▁of▁sentence｜> 是 HY-MT2 的句末 EOS token
_STOP_TOKENS = [
    "<｜hy_end▁of▁sentence｜>",
    "<|im_end|>",
    "</s>",
]


# ============================================================
# 支持的语言列表
# ============================================================
LANGUAGES = OrderedDict([
    # ---- 33 种互译语言 ----
    ("Arabic",              ("ar", "阿拉伯语")),
    ("Bengali",             ("bn", "孟加拉语")),
    ("Chinese",             ("zh", "中文（简体）")),
    ("Traditional Chinese", ("zh-Hant", "中文（繁体）")),
    ("Czech",               ("cs", "捷克语")),
    ("Dutch",               ("nl", "荷兰语")),
    ("English",             ("en", "英语")),
    ("Filipino",            ("tl", "菲律宾语")),
    ("French",              ("fr", "法语")),
    ("German",              ("de", "德语")),
    ("Gujarati",            ("gu", "古吉拉特语")),
    ("Hebrew",              ("he", "希伯来语")),
    ("Hindi",               ("hi", "印地语")),
    ("Indonesian",          ("id", "印尼语")),
    ("Italian",             ("it", "意大利语")),
    ("Japanese",            ("ja", "日语")),
    ("Khmer",               ("km", "高棉语")),
    ("Korean",              ("ko", "韩语")),
    ("Lao",                 ("lo", "老挝语")),
    ("Malay",               ("ms", "马来语")),
    ("Burmese",             ("my", "缅甸语")),
    ("Persian",             ("fa", "波斯语")),
    ("Polish",              ("pl", "波兰语")),
    ("Portuguese",          ("pt", "葡萄牙语")),
    ("Russian",             ("ru", "俄语")),
    ("Spanish",             ("es", "西班牙语")),
    ("Tamil",               ("ta", "泰米尔语")),
    ("Telugu",              ("te", "泰卢固语")),
    ("Thai",                ("th", "泰语")),
    ("Turkish",             ("tr", "土耳其语")),
    ("Ukrainian",           ("uk", "乌克兰语")),
    ("Urdu",                ("ur", "乌尔都语")),
    ("Vietnamese",          ("vi", "越南语")),
    # ---- 5 种中国少数民族语言/方言 ----
    ("Cantonese",           ("yue", "粤语")),
    ("Tibetan",             ("bo", "藏语")),
    ("Kazakh",              ("kk", "哈萨克语")),
    ("Mongolian",           ("mn", "蒙古语")),
    ("Uyghur",              ("ug", "维吾尔语")),
])

_LANGUAGE_ALIASES = {
    "ar": "Arabic", "bn": "Bengali",
    "zh": "Chinese", "zh-cn": "Chinese", "zh-hans": "Chinese",
    "zh-tw": "Traditional Chinese", "zh-hk": "Traditional Chinese",
    "zh-hant": "Traditional Chinese",
    "cs": "Czech", "nl": "Dutch",
    "en": "English", "en-us": "English", "en-gb": "English",
    "tl": "Filipino", "fil": "Filipino",
    "fr": "French", "de": "German",
    "gu": "Gujarati", "he": "Hebrew", "hi": "Hindi",
    "id": "Indonesian", "it": "Italian", "ja": "Japanese",
    "km": "Khmer", "ko": "Korean", "lo": "Lao",
    "ms": "Malay", "my": "Burmese", "fa": "Persian",
    "pl": "Polish", "pt": "Portuguese", "ru": "Russian",
    "es": "Spanish", "ta": "Tamil", "te": "Telugu",
    "th": "Thai", "tr": "Turkish", "uk": "Ukrainian",
    "ur": "Urdu", "vi": "Vietnamese",
    "yue": "Cantonese", "bo": "Tibetan",
    "kk": "Kazakh", "mn": "Mongolian", "ug": "Uyghur",
    # 中文名
    "阿拉伯语": "Arabic", "孟加拉语": "Bengali",
    "中文": "Chinese", "简体中文": "Chinese", "中文简体": "Chinese",
    "繁体中文": "Traditional Chinese", "中文繁体": "Traditional Chinese",
    "捷克语": "Czech", "荷兰语": "Dutch",
    "英语": "English", "英文": "English",
    "菲律宾语": "Filipino",
    "法语": "French", "德语": "German",
    "古吉拉特语": "Gujarati", "希伯来语": "Hebrew", "印地语": "Hindi",
    "印尼语": "Indonesian", "意大利语": "Italian",
    "日语": "Japanese", "日文": "Japanese",
    "高棉语": "Khmer", "韩语": "Korean", "老挝语": "Lao",
    "马来语": "Malay", "缅甸语": "Burmese", "波斯语": "Persian",
    "波兰语": "Polish", "葡萄牙语": "Portuguese", "俄语": "Russian",
    "西班牙语": "Spanish", "泰米尔语": "Tamil", "泰卢固语": "Telugu",
    "泰语": "Thai", "土耳其语": "Turkish", "乌克兰语": "Ukrainian",
    "乌尔都语": "Urdu", "越南语": "Vietnamese",
    "粤语": "Cantonese", "广东话": "Cantonese",
    "藏语": "Tibetan", "哈萨克语": "Kazakh",
    "蒙古语": "Mongolian", "维吾尔语": "Uyghur",
}


def normalize_language_name(name: str) -> str:
    """把语言名/代码/中文名归一为规范英文名。"""
    if not name:
        return name
    key = name.strip()
    for canon in LANGUAGES:
        if canon.lower() == key.lower():
            return canon
    if key.lower() in _LANGUAGE_ALIASES:
        return _LANGUAGE_ALIASES[key.lower()]
    if key in _LANGUAGE_ALIASES:
        return _LANGUAGE_ALIASES[key]
    return key


def is_supported_language(name: str) -> bool:
    if not name:
        return False
    return normalize_language_name(name) in LANGUAGES


def format_language_table() -> str:
    lines = []
    lines.append(f"HY-MT2 支持 {len(LANGUAGES)} 种语言/方言：\n")
    lines.append(f"  {'#':>3}  {'Language (规范名)':<22} {'Code':<9} 中文名")
    lines.append("  " + "-" * 60)
    for i, (name, (code, zh)) in enumerate(LANGUAGES.items(), 1):
        lines.append(f"  {i:>3}  {name:<22} {code:<9} {zh}")
    lines.append("")
    lines.append("用法示例：")
    lines.append("  python translate_cli.py \"你好\" --target English")
    lines.append("  python translate_cli.py \"你好\" --target 日语")
    lines.append("  python translate_cli.py \"你好\" --target ja")
    return "\n".join(lines)


# ============================================================
# 默认配置（config.py 缺失时使用）
# ============================================================
_DEFAULT_CONFIG = {
    "TRANSLATE_URL": "http://localhost:8788/v1/chat/completions",
    "TRANSLATE_MODEL": "hy-mt2-1.8b",
    "TRANSLATE_TARGET_LANG": "English",
    "TRANSLATE_CHUNK_SIZE": 150,
}


def _load_config() -> dict:
    cfg = dict(_DEFAULT_CONFIG)
    try:
        import config as _cfg  # type: ignore
        for key in _DEFAULT_CONFIG:
            if hasattr(_cfg, key):
                cfg[key] = getattr(_cfg, key)
    except ImportError:
        pass
    return cfg


_CFG = _load_config()


# ============================================================
# 文本切分
# ============================================================
_SENT_END_RE = re.compile(r'(?<=[。！？])\s*')


def _split_by_sentence(text: str) -> list:
    parts = [s for s in _SENT_END_RE.split(text) if s and s.strip()]
    return parts if parts else [text]

def _find_cut_position(text: str, min_pos: int, max_pos: int):
    """在 [0, max_pos) 内找最靠后的标点作为切分点。

    优先级：句末标点 > 逗号/分号/顿号 > None
    """
    end = min(len(text), max_pos)
    if end <= 0:
        return None

    # 1. 从后往前找句末标点
    for i in range(end - 1, -1, -1):
        if text[i] in "。！？":
            return i + 1

    # 2. 从后往前找逗号/分号/顿号/冒号
    for i in range(end - 1, -1, -1):
        if text[i] in "，,、；;：:":
            return i + 1

    return None

def _split_overlong_sentence(sent: str, chunk_size: int) -> list:
    """处理单句超长，优先按标点切分。"""
    if len(sent) <= chunk_size:
        return [sent]

    hard_limit = chunk_size * 2
    result = []
    remaining = sent

    while len(remaining) > chunk_size:
        cut = _find_cut_position(remaining, 0, hard_limit)
        if cut is None or cut <= 0:
            cut = chunk_size  # 真的没标点，硬切
        result.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()

    if remaining:
        result.append(remaining)

    return result

def _split_long_text(text: str, chunk_size: int) -> list:
    sentences = _split_by_sentence(text)
    chunks = []
    current = ""
    for sent in sentences:
        if len(current) + len(sent) <= chunk_size:
            current += sent
            continue
        if current.strip():
            chunks.append(current.strip())
            current = ""
        if len(sent) <= chunk_size:
            current = sent
        else:
            chunks.extend(_split_overlong_sentence(sent, chunk_size))
    if current.strip():
        chunks.append(current.strip())

    # ← 加这两行
    print(f"[DEBUG] 分块结果 ({len(chunks)} 块):", file=sys.stderr)
    for i, c in enumerate(chunks, 1):
        print(f"  [{i}] ({len(c)}字符) {c!r}", file=sys.stderr)

    return chunks


# ============================================================
# 核心翻译（最佳实践：Chat 端点 + System/User 分离）
# ============================================================
def _call_llama(prompt: str) -> str:
    """单次请求 llama-server（completions 端点）。"""
    resp = requests.post(
        _CFG["TRANSLATE_URL"],
        json={
            "model": _CFG["TRANSLATE_MODEL"],
            "prompt": prompt,
            "temperature": 0.1,
            "top_p": 0.7,
            "top_k": 10,
            "repetition_penalty": 1.05,
            "max_tokens": _MAX_TOKENS,
            "stop": ["<|im_end|>"],
        },
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["text"].strip()

    # 严格截断：
    # 1. 取第一个非空段
    for para in content.split("\n"):
        para = para.strip()
        if para:
            content = para
            break

    # 2. 取到第一个句末标点
    m = re.match(r'^(.+?[.!?。！？])', content)
    if m:
        return m.group(1).strip()

    return content


def _translate_one(text: str, target_lang: str) -> str:
    """严格翻译：只翻译原文，不回答、不推理、不解释、不总结、不扩写。"""
    src = text.strip()
    # 关键：若原文无句末标点，补一个句号
    # 关键修复：归一化句末标点
    # 中文模型只认 。！？，需把 . ! ? 也归一化
    if src:
        last = src[-1]
        if last == ".":
            src = src[:-1] + "。"
        elif last == "!":
            src = src[:-1] + "！"
        elif last == "?":
            src = src[:-1] + "？"
        elif last not in "。！？":
            # 完全没有句末标点，补中文句号
            src = src + "。"

    prompt = (
        f"You are a translation engine. "
        f"Translate the following text into {target_lang}. "
        f"Only output the translated result without any additional explanation. "
     #   f"Do not answer questions, do not explain, do not summarize, do not expand.\n\n"
        f"{src}"
    )

    print(f"[DEBUG] 发送给模型: {src!r}", file=sys.stderr)    
    return _call_llama(prompt)


def translate(
    text: str,
    target_lang: str = None,
    source_lang: str = "auto",
    strict_language: bool = False,
) -> str:
    """翻译文本。

    Args:
        text:             待翻译文本
        target_lang:      目标语言（支持规范英文名 / ISO 代码 / 中文名）
        source_lang:      源语言（HY-MT2 自动识别，此参数仅保留接口一致性）
        strict_language:  True 时对未识别的目标语言抛异常

    Returns:
        译文
    """
    text = (text or "").strip()
    if not text:
        return ""

    target_lang = target_lang or _CFG["TRANSLATE_TARGET_LANG"]

    canonical = normalize_language_name(target_lang)
    if not is_supported_language(canonical):
        msg = (
            f"不支持的目标语言: {target_lang!r}。"
            f"运行 `python translate_cli.py --list-languages` 查看支持的语言。"
        )
        if strict_language:
            raise ValueError(msg)
        print(f"[translate] 警告: {msg}", file=sys.stderr)
    else:
        target_lang = canonical

    chunk_size = _CFG["TRANSLATE_CHUNK_SIZE"]

    # 短文本：一次请求
    if len(text) <= chunk_size:
        try:
            result = _translate_one(text, target_lang)
            if not result:
                print(f"[translate] 空返回，保留原文: {text!r}", file=sys.stderr)
                return text
            return result
        except Exception as e:
            print(f"[translate] 请求失败: {e}", file=sys.stderr)
            return text

    # 长文本：分块 + 逐块翻译
    chunks = _split_long_text(text, chunk_size)
    translated = []
    for i, chunk in enumerate(chunks):
        try:
            translated.append(_translate_one(chunk, target_lang))
        except Exception as e:
            print(f"[translate] chunk {i+1}/{len(chunks)} 失败: {e}", file=sys.stderr)
            translated.append(chunk)

    joiner = " " if target_lang.lower() in ("english", "en") else ""
    return joiner.join(translated)


# ============================================================
# 命令行入口
# ============================================================
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="HY-MT2 翻译客户端（本地 llama-server）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python translate_cli.py "你好世界"
  python translate_cli.py "你好世界" --target Japanese
  python translate_cli.py "你好世界" --target 日语
  python translate_cli.py "你好世界" --target ja
  python translate_cli.py --file input.txt --target English -o out.txt
  echo "你好" | python translate_cli.py
  python translate_cli.py --list-languages
  python translate_cli.py --show-config
        """,
    )
    p.add_argument("text_pos", nargs="?", help="待翻译文本（位置参数）")
    p.add_argument("-t", "--text", dest="text_opt", help="待翻译文本（选项参数）")
    p.add_argument("-f", "--file", help="从文件读取文本")
    p.add_argument("-T", "--target", default=None, help="目标语言（默认 English）")
    p.add_argument("-S", "--source", default="auto", help="源语言（默认 auto）")
    p.add_argument("--url", default=None, help="llama-server 地址（临时覆盖）")
    p.add_argument("--model", default=None, help="模型名（临时覆盖）")
    p.add_argument("-o", "--output", help="输出文件（默认 stdout）")
    p.add_argument("-q", "--quiet", action="store_true", help="不打印 info 日志")
    p.add_argument("--strict-language", action="store_true",
                   help="目标语言不在支持列表时报错退出（默认只警告）")
    p.add_argument("--list-languages", action="store_true",
                   help="列出所有支持的语言并退出")
    p.add_argument("--show-config", action="store_true", help="显示当前配置并退出")
    return p


def _read_input(args) -> str:
    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            return f.read()
    if args.text_pos:
        return args.text_pos
    if args.text_opt:
        return args.text_opt
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.list_languages:
        print(format_language_table())
        return 0

    if args.show_config:
        print(json.dumps(_CFG, indent=2, ensure_ascii=False))
        return 0

    if args.url:
        _CFG["TRANSLATE_URL"] = args.url
    if args.model:
        _CFG["TRANSLATE_MODEL"] = args.model

    text = _read_input(args)
    if not text.strip():
        parser.error("没有输入文本。请通过位置参数、--text、--file 或 stdin 提供。")

    target_lang = args.target or _CFG["TRANSLATE_TARGET_LANG"]
    canonical = normalize_language_name(target_lang)
    chunk_size = _CFG["TRANSLATE_CHUNK_SIZE"]

    if not args.quiet:
        if is_supported_language(canonical):
            code, zh = LANGUAGES[canonical]
            print(f"[info] 目标语言: {canonical} ({code}, {zh})", file=sys.stderr)
        else:
            print(f"[info] 目标语言: {target_lang}（未在支持列表中）", file=sys.stderr)
        print(f"[info] 输入长度: {len(text)} 字符", file=sys.stderr)
        if len(text) > chunk_size:
            print(
                f"[info] 长文本模式（阈值 {chunk_size}，"
                f"超长句允许累积到 {chunk_size * 2}）",
                file=sys.stderr,
            )

    try:
        result = translate(
            text,
            target_lang=target_lang,
            source_lang=args.source,
            strict_language=args.strict_language,
        )
    except ValueError as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"[error] 翻译失败: {e}", file=sys.stderr)
        return 1

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(result)
        if not args.quiet:
            print(f"[info] 已写入: {args.output}", file=sys.stderr)
    else:
        print(result)

    return 0


if __name__ == "__main__":
    sys.exit(main())

