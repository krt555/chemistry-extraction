"""Translate English paper text into Chinese on demand.

The corpus is English while the users are Chinese-speaking, so both the
evidence panel and the assistant need a "译" action.  Translation is a pure
pass-through to the configured LLM:

* no key configured -> a clear, honest refusal (never a fake translation);
* chemical formulae, reagent abbreviations, space groups and numbers must stay
  as written, which is what the prompt enforces;
* results are cached in-process, keyed by the text hash, because reviewers
  click the same page repeatedly.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
_SRC = ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

MAX_CHARS = 6000
_CACHE_LIMIT = 256
_cache: "OrderedDict[str, str]" = OrderedDict()
_CACHE_LOCK = threading.Lock()

TRANSLATE_SYSTEM = (
    "你是材料化学领域的专业翻译。把用户给出的英文论文片段翻译成简体中文。"
    "要求："
    "\n1. 忠实翻译，不增删内容，不做评论和总结；"
    "\n2. 化学式（如 SiO2、TEOS）、试剂与聚合物缩写（如 P123、F127、CTAB）、"
    "空间群（如 p6mm、Im3m、Ia3d）、仪器与方法缩写（如 BET、XRD、TEM、SAXS）"
    "、数字和单位一律保留原样，不要翻译也不要换算；"
    "\n3. 保留原文的段落结构；表格行请逐行翻译，保持 | 分隔；"
    "\n4. 只输出译文本身，不要加「译文：」之类的前缀，也不要重复英文原文。"
)


class TranslateError(RuntimeError):
    """Raised when the text cannot be translated."""


def _key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cached(key: str) -> str | None:
    with _CACHE_LOCK:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    return None


def _remember(key: str, value: str) -> None:
    with _CACHE_LOCK:
        _cache[key] = value
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_LIMIT:
            _cache.popitem(last=False)


def looks_english(text: str) -> bool:
    """Rough check so we can skip translating text that is already Chinese."""
    sample = str(text or "")
    if not sample.strip():
        return False
    han = sum(1 for ch in sample if "\u4e00" <= ch <= "\u9fff")
    latin = sum(1 for ch in sample if ch.isascii() and ch.isalpha())
    return latin > han * 2


def translate(text: str) -> dict[str, Any]:
    """Translate one chunk of English text into Chinese.

    Returns ``{"text", "cached", "truncated"}``; raises ``TranslateError``
    when no LLM is configured or the call fails.
    """
def _provider() -> str:
    """Which translation backend to use (env: MKEA_TRANSLATE_PROVIDER)."""
    return (os.environ.get("MKEA_TRANSLATE_PROVIDER") or "llm").strip().lower()


def _http_json(url: str, *, data: bytes | None = None, headers: dict | None = None) -> Any:
    request = urllib.request.Request(
        url, data=data, headers=headers or {}, method="POST" if data else "GET"
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def _translate_mymemory(text: str) -> str:
    """Free, key-less endpoint. Short inputs only; good enough as a fallback."""
    out: list[str] = []
    # MyMemory caps a request at roughly 500 bytes, so send it sentence by sentence.
    for chunk in _split_chunks(text, 450):
        params = urllib.parse.urlencode({"q": chunk, "langpair": "en|zh-CN"})
        payload = _http_json(f"https://api.mymemory.translated.net/get?{params}")
        piece = ((payload or {}).get("responseData") or {}).get("translatedText") or ""
        if not piece:
            raise TranslateError("MyMemory 没有返回译文")
        out.append(piece)
    return " ".join(out)


def _translate_deepl(text: str) -> str:
    key = os.environ.get("MKEA_DEEPL_API_KEY", "").strip()
    if not key:
        raise TranslateError("使用 DeepL 需要先设置 MKEA_DEEPL_API_KEY")
    host = "api-free.deepl.com" if key.endswith(":fx") else "api.deepl.com"
    body = urllib.parse.urlencode(
        {"text": text, "target_lang": "ZH", "source_lang": "EN"}
    ).encode()
    payload = _http_json(
        f"https://{host}/v2/translate",
        data=body,
        headers={
            "Authorization": f"DeepL-Auth-Key {key}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    parts = [item.get("text", "") for item in (payload or {}).get("translations", [])]
    if not any(parts):
        raise TranslateError("DeepL 没有返回译文")
    return "\n".join(parts)


def _split_chunks(text: str, limit: int) -> list[str]:
    """Split on sentence boundaries so each piece fits a provider's limit."""
    sentences = re.split(r"(?<=[.!?;])\s+", text)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        while len(sentence) > limit:
            chunks.append(sentence[:limit])
            sentence = sentence[limit:]
        if len(current) + len(sentence) + 1 > limit:
            if current:
                chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current)
    return chunks


def _translate_llm(text: str) -> str:
    from mkea_extract.config import ConfigError, LLMConfig
    from mkea_extract.llm import LLMError, OpenAICompatibleLLM

    try:
        config = LLMConfig.from_env()
    except ConfigError as exc:
        raise TranslateError(f"翻译需要先连接 AI 服务：{exc}") from exc

    try:
        response = OpenAICompatibleLLM(config).complete(
            [
                {"role": "system", "content": TRANSLATE_SYSTEM},
                {"role": "user", "content": text},
            ],
            json_mode=False,
            max_tokens=4096,
        )
    except LLMError as exc:
        raise TranslateError(f"翻译失败：{exc}") from exc
    except Exception as exc:  # pragma: no cover - defensive
        raise TranslateError(f"翻译失败：{exc}") from exc
    return response.text.strip()


_BACKENDS = {
    "llm": _translate_llm,
    "mymemory": _translate_mymemory,
    "deepl": _translate_deepl,
}


def available_providers() -> list[dict[str, Any]]:
    """Describe each backend so the UI can explain what is usable right now."""
    # .env is the canonical place for the key; from_env() loads it on demand,
    # so checking os.environ alone would report a configured key as missing.
    has_llm = False
    try:
        from mkea_extract.config import LLMConfig

        has_llm = bool(LLMConfig.from_env().api_key)
    except Exception:
        has_llm = bool(os.environ.get("MKEA_LLM_API_KEY"))
    return [
        {
            "id": "llm",
            "label": "AI 服务（已连接的大模型）",
            "ready": has_llm,
            "note": "质量最好，会保留化学式与数值；需要在「连接 AI 服务」里配置密钥。",
        },
        {
            "id": "mymemory",
            "label": "MyMemory（免费，无需密钥）",
            "ready": True,
            "note": "免费公共接口，无需任何配置；长文本会分句发送，质量一般。",
        },
        {
            "id": "deepl",
            "label": "DeepL",
            "ready": bool(os.environ.get("MKEA_DEEPL_API_KEY")),
            "note": "需要设置环境变量 MKEA_DEEPL_API_KEY。",
        },
    ]


def translate(text: str, provider: str | None = None) -> dict[str, Any]:
    """Translate one chunk of English text into Chinese.

    Returns ``{"text", "cached", "truncated", "provider"}``; raises
    ``TranslateError`` when the selected backend is unusable.
    """
    source = str(text or "").strip()
    if not source:
        raise TranslateError("没有可翻译的内容")

    truncated = len(source) > MAX_CHARS
    if truncated:
        source = source[:MAX_CHARS]

    name = (provider or _provider()).lower()
    backend = _BACKENDS.get(name)
    if backend is None:
        raise TranslateError(
            f"不支持的翻译服务：{name}（可选 {', '.join(_BACKENDS)}）"
        )

    key = _key(f"{name}\n{source}")
    hit = _cached(key)
    if hit is not None:
        return {"text": hit, "cached": True, "truncated": truncated, "provider": name}

    try:
        result = backend(source).strip()
    except TranslateError:
        raise
    except urllib.error.HTTPError as exc:
        raise TranslateError(f"翻译服务返回错误（HTTP {exc.code}）") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TranslateError(f"无法连接翻译服务：{exc}") from exc
    except Exception as exc:  # pragma: no cover - defensive
        raise TranslateError(f"翻译失败：{exc}") from exc

    if not result:
        raise TranslateError("翻译服务返回了空结果")

    _remember(key, result)
    return {"text": result, "cached": False, "truncated": truncated, "provider": name}


def stream_translate(text: str, provider: str | None = None):
    """Yield the translation incrementally.

    Only the LLM backend can stream; the HTTP translators return one block, so
    they emit a single chunk.  Finished text is cached exactly like the
    blocking path, so a second view is instant.
    """
    source = str(text or "").strip()
    if not source:
        raise TranslateError("没有可翻译的内容")
    if len(source) > MAX_CHARS:
        source = source[:MAX_CHARS]

    name = (provider or _provider()).lower()
    if name not in _BACKENDS:
        raise TranslateError(
            f"不支持的翻译服务：{name}（可选 {', '.join(_BACKENDS)}）"
        )

    key = _key(f"{name}\n{source}")
    hit = _cached(key)
    if hit is not None:
        yield hit
        return

    if name != "llm":
        result = translate(source, name)
        yield result["text"]
        return

    from mkea_extract.config import ConfigError, LLMConfig
    from mkea_extract.llm import LLMError, OpenAICompatibleLLM

    try:
        config = LLMConfig.from_env()
    except ConfigError as exc:
        raise TranslateError(f"翻译需要先连接 AI 服务：{exc}") from exc

    collected: list[str] = []
    try:
        for piece in OpenAICompatibleLLM(config).stream(
            [
                {"role": "system", "content": TRANSLATE_SYSTEM},
                {"role": "user", "content": source},
            ],
            max_tokens=4096,
        ):
            collected.append(piece)
            yield piece
    except LLMError as exc:
        raise TranslateError(f"翻译失败：{exc}") from exc

    whole = "".join(collected).strip()
    if whole:
        _remember(key, whole)


__all__ = [
    "MAX_CHARS",
    "TranslateError",
    "available_providers",
    "looks_english",
    "stream_translate",
    "translate",
]
