"""Minimal OpenAI-compatible chat client (stdlib only).

Tested against OpenCode Go's ``/inference/go/openai/v1`` endpoint, but any
provider exposing ``POST /chat/completions`` works.  The client sends the
OpenCode session header so prompt caching and routing behave well.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .config import LLMConfig


class LLMError(RuntimeError):
    """Raised when the LLM request or JSON response cannot be completed."""


@dataclass
class LLMResponse:
    text: str
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    latency_s: float = 0.0
    cost: float | None = None


def extract_json(text: str) -> dict[str, Any] | None:
    """Extract the first JSON object from a possibly fenced model response."""

    if not text:
        return None
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    decoder = json.JSONDecoder()
    for match in re.finditer(r"[\{\[]", cleaned):
        try:
            data, _ = decoder.raw_decode(cleaned[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            return {"records": data}
    return None


def _max_tokens_limit(body: str) -> int | None:
    """Pull the accepted upper bound out of a provider's 400 response.

    Providers disagree on how large ``max_tokens`` may be and answer with
    messages like ``Range of max_tokens should be [1, 32768]``.  Reading the
    limit back lets one retry succeed instead of degrading the whole job to
    the rule engine.
    """
    patterns = (
        r"max_tokens\s+should\s+be\s*\[\s*\d+\s*,\s*(\d+)\s*\]",
        r"max_tokens.{0,40}?(?:less than or equal to|at most|maximum of|<=)\s*(\d+)",
        r"max_tokens.{0,40}?support.{0,20}?(\d+)",
    )
    for pattern in patterns:
        match = re.search(pattern, body, re.I)
        if match:
            try:
                limit = int(match.group(1))
            except ValueError:
                continue
            if limit > 0:
                return limit
    return None


class OpenAICompatibleLLM:
    def __init__(self, config: LLMConfig):
        self.config = config

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        json_mode: bool = True,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.config.max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        start = time.time()
        last_error: Exception | None = None
        data: dict[str, Any] | None = None
        # 参数自愈（缩小 max_tokens、去掉 response_format）属于修正请求，
        # 不应占用为网络抖动准备的重试额度，各自最多发生一次。
        budget = self.config.max_retries
        attempt = 0
        fixed_max_tokens = False
        fixed_response_format = False
        while attempt < budget:
            try:
                data = self._post(payload)
                break
            except urllib.error.HTTPError as exc:
                body = _read_error(exc)
                # 服务商拒绝过大的 max_tokens：按报错里给出的上限重试，
                # 而不是让整次抽取退回规则引擎。
                if exc.code == 400 and not fixed_max_tokens:
                    limit = _max_tokens_limit(body)
                    if limit and payload.get("max_tokens", 0) > limit:
                        payload["max_tokens"] = limit
                        fixed_max_tokens = True
                        budget += 1
                        last_error = LLMError(
                            f"HTTP 400 (retrying with max_tokens={limit}): {body}"
                        )
                        attempt += 1
                        continue
                if exc.code == 400 and "response_format" in payload and not fixed_response_format:
                    payload.pop("response_format", None)
                    fixed_response_format = True
                    budget += 1
                    last_error = LLMError(f"HTTP 400 (retrying without response_format): {body}")
                    attempt += 1
                    continue
                if exc.code in {401, 403}:
                    raise LLMError(
                        f"LLM authentication failed (HTTP {exc.code}): {body}\n"
                        "Check MKEA_LLM_API_KEY and MKEA_LLM_BASE_URL in .env."
                    ) from exc
                last_error = LLMError(f"HTTP {exc.code}: {body}")
                if exc.code not in {408, 409, 429, 500, 502, 503, 504}:
                    raise last_error from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = LLMError(f"network error: {exc}")
            attempt += 1
            if attempt < budget:
                time.sleep(min(2**attempt, 8))
        if data is None:
            raise LLMError(f"LLM request failed after retries: {last_error}")

        try:
            content = _content_text(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected LLM response: {str(data)[:800]}") from exc
        usage = data.get("usage") or {}
        cost = usage.get("cost")
        return LLMResponse(
            text=content,
            model=data.get("model", self.config.model),
            usage=usage,
            latency_s=time.time() - start,
            cost=float(cost) if isinstance(cost, (int, float)) else None,
        )

    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> tuple[dict[str, Any], LLMResponse]:
        response = self.complete(
            messages, json_mode=True, max_tokens=max_tokens, temperature=temperature
        )
        data = extract_json(response.text)
        if data is not None:
            return data, response

        repair = messages + [
            {"role": "assistant", "content": response.text[:4000]},
            {
                "role": "user",
                "content": "Your previous answer was not valid JSON. Return the same result as strict JSON only.",
            },
        ]
        repaired = self.complete(
            repair, json_mode=False, max_tokens=max_tokens, temperature=temperature
        )
        data = extract_json(repaired.text)
        if data is None:
            raise LLMError(f"model did not return JSON after repair: {repaired.text[:800]}")
        repaired.usage = _merge_usage(response.usage, repaired.usage)
        repaired.cost = (response.cost or 0.0) + (repaired.cost or 0.0)
        repaired.latency_s += response.latency_s
        return data, repaired

    def check(self) -> LLMResponse:
        """Send a tiny request to verify endpoint, key and model."""

        response = self.complete(
            [
                {"role": "system", "content": "You reply with compact JSON only."},
                {"role": "user", "content": 'Return {"ok": true}.'},
            ],
            json_mode=True,
            max_tokens=512,
        )
        if extract_json(response.text) is None:
            raise LLMError(f"endpoint responded, but not with JSON: {response.text[:300]}")
        return response

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.config.base_url}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.api_key}",
                "User-Agent": self.config.user_agent,
                "x-opencode-session": self.config.session_id,
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.config.timeout_s) as response:
            return json.loads(response.read().decode("utf-8"))


def _read_error(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", "replace")[:800]
    except Exception:  # pragma: no cover - defensive for broken error bodies
        return str(exc)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return str(content or "")


def _merge_usage(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for key in set(first) | set(second):
        a, b = first.get(key), second.get(key)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            merged[key] = a + b
        else:
            merged[key] = b if b is not None else a
    return merged
