"""Per-paper content summary (full version + hover brief).

Two engines, same contract, mirroring the rest of the app:

* ``llm``       – when an API key is configured, the model reads the leading
                  pages plus the already-extracted fields and writes a Chinese
                  summary.
* ``heuristic`` – deterministic fallback that composes the summary from the
                  structured records and a few scored sentences of the paper.
                  Always available, no network.

The brief is what the library shows on hover, so it is hard-capped and kept to
a single line; the full summary is what the "内容总结" button reveals.
"""

from __future__ import annotations

import re
from typing import Any

from .extraction import FIELD_KEYS, FIELD_LABELS, llm_status

BRIEF_LIMIT = 90
"""Hover tooltips must stay readable: one line, no scrolling."""

# Fields worth naming in a summary, in reading order.
_HIGHLIGHT_FIELDS: tuple[str, ...] = (
    "mesostructure",
    "surfactant",
    "silica_source",
    "assembly_method",
    "solution_system",
    "synthesis_temperature",
    "surface_area",
    "pore_size",
    "morphology",
)

_BRIEF_FIELDS: tuple[str, ...] = (
    "mesostructure",
    "surfactant",
    "surface_area",
    "pore_size",
)

# Compact labels for the one-line hover brief; the full summary keeps FIELD_LABELS.
_BRIEF_LABELS: dict[str, str] = {
    "mesostructure": "结构",
    "surfactant": "模板剂",
    "surface_area": "比表面积",
    "pore_size": "孔径",
}

# Units help the brief stay self-explanatory once the label is shortened.
_BRIEF_UNITS: dict[str, str] = {
    "surface_area": " m²/g",
    "pore_size": " nm",
}

# 字段值里的英文术语转中文：规则引擎按固定词表输出，这里逐条回译，
# 保证离线总结通篇是中文，而不是中文标签 + 英文取值的混排。
_TERM_CN: tuple[tuple[re.Pattern[str], str], ...] = (
    # 形貌
    (re.compile(r"\bfilm/monolith\b", re.I), "薄膜/整体材料"),
    (re.compile(r"\bcore-?shell\b", re.I), "核壳结构"),
    (re.compile(r"\bnanoparticles?\b", re.I), "纳米颗粒"),
    (re.compile(r"\bplatelets?\b", re.I), "片状"),
    (re.compile(r"\bworm-?like\b", re.I), "蠕虫状"),
    (re.compile(r"\brod-?like\b", re.I), "棒状"),
    (re.compile(r"\bspheres?\b", re.I), "球形"),
    (re.compile(r"\bfib(?:ers|res)\b", re.I), "纤维状"),
    (re.compile(r"\bhollow\b", re.I), "空心"),
    # 介孔结构
    (re.compile(r"\b2D hexagonal\b", re.I), "二维六方"),
    (re.compile(r"\b3D hexagonal\b", re.I), "三维六方"),
    (re.compile(r"\bcubic\b", re.I), "立方"),
    (re.compile(r"\blamellar\b", re.I), "层状"),
    (re.compile(r"wormhole-?like / disordered", re.I), "虫洞状/无序"),
    # 温度程序
    (re.compile(r"\bcalcination\b|\bcalcined\b", re.I), "煅烧"),
    (re.compile(r"\baging\b|\baged\b", re.I), "老化"),
    (re.compile(r"\bhydrothermal\b", re.I), "水热"),
    (re.compile(r"\bdrying\b|\bdried\b", re.I), "干燥"),
    (re.compile(r"\bstirred\b|\bstirring\b", re.I), "搅拌"),
    (re.compile(r"(?<=\d)\s*h\b", re.I), " 小时"),
)


def _to_chinese(value: str) -> str:
    """Translate the rule engine's English vocabulary into Chinese."""
    text = _clean(value)
    for pattern, replacement in _TERM_CN:
        text = pattern.sub(replacement, text)
    # "fibers; spheres" 这类多值用顿号串联更像中文
    return re.sub(r"\s*;\s*", "、", text)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _truncate(text: str, limit: int = BRIEF_LIMIT) -> str:
    text = _clean(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip("，。；、,; ") + "…"


def _field_pairs(records: list[dict[str, Any]], keys: tuple[str, ...]) -> list[tuple[str, str]]:
    """First non-empty value per field across all records, label included."""
    pairs: list[tuple[str, str]] = []
    for key in keys:
        if key not in FIELD_KEYS:
            continue
        for record in records:
            value = _clean((record.get("fields") or {}).get(key) or "")
            if value:
                pairs.append((FIELD_LABELS[key], value))
                break
    return pairs


# 研究主题的中文归纳：离线引擎不翻译句子，而是按主题关键词给出中文结论。
# 每条规则命中后输出一句完整中文，避免总结里夹带英文长句。
_TOPIC_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bcatalys|catalytic\b", re.I), "材料被用于催化相关应用"),
    (re.compile(r"drug\s+(delivery|release)|biomedic", re.I), "材料面向药物负载与缓释等生物医学应用"),
    (re.compile(r"\badsorpt|\badsorb|removal of", re.I), "材料被用于吸附/分离性能考察"),
    (re.compile(r"hydrogen evolution|electrochemic|\bHER\b", re.I), "材料被用于电化学/析氢性能考察"),
    (re.compile(r"\bsensing\b|\bsensor\b", re.I), "材料被用于传感性能考察"),
    (re.compile(r"\benergy storage|\bsupercapacitor|\bbattery\b", re.I), "材料被用于储能相关应用"),
    (re.compile(r"large[- ]pore|pore size.{0,30}(expan|enlarg|tun)", re.I), "工作重点在于扩大并调控孔径"),
    (re.compile(r"\bthermal(ly)?\s+stab|hydrothermal stab", re.I), "文中强调材料的热/水热稳定性"),
    (re.compile(r"\bmorpholog(y|ies)\s+control|control.{0,20}morpholog", re.I), "工作重点在于产物形貌的调控"),
    (re.compile(r"\bthin\s+films?\b|\bcoating\b", re.I), "产物以薄膜/涂层形式制备"),
    (re.compile(r"\bdoped?\b|\bdoping\b|\bincorporat", re.I), "骨架中引入了杂原子或客体组分"),
)


def _topics(pages: list[str], limit: int = 3) -> list[str]:
    """Chinese topic statements derived from the paper, not translated quotes."""
    text = " ".join(pages[:3])
    found: list[str] = []
    for pattern, statement in _TOPIC_RULES:
        if pattern.search(text) and statement not in found:
            found.append(statement)
        if len(found) >= limit:
            break
    return found


def _brief_from_fields(records: list[dict[str, Any]]) -> str:
    """One-line hover brief: compact labels, short values, at most 3 items."""
    parts: list[str] = []
    for key in _BRIEF_FIELDS:
        for record in records:
            value = _clean((record.get("fields") or {}).get(key) or "")
            if not value:
                continue
            # Multi-valued heuristic output ("fibers; spheres") only needs its head.
            value = re.split(r"\s*[;；]\s*", value)[0]
            value = _truncate(_to_chinese(value), 28)
            parts.append(f"{_BRIEF_LABELS[key]} {value}{_BRIEF_UNITS.get(key, '')}")
            break
        if len(parts) >= 3:
            break
    return "，".join(parts)


def _overview(records: list[dict[str, Any]]) -> str:
    """Opening line: what was made, by which route, with which template."""
    def first(key: str) -> str:
        for record in records:
            value = _clean((record.get("fields") or {}).get(key) or "")
            if value:
                return _to_chinese(value)
        return ""

    structure = first("mesostructure")
    assembly = first("assembly_method")
    surfactant = first("surfactant")
    silica = first("silica_source")

    clauses: list[str] = []
    if assembly:
        clauses.append(f"采用{assembly}路线")
    if surfactant:
        clauses.append(f"以 {surfactant} 为模板剂")
    if silica:
        clauses.append(f"以 {silica} 为硅源")

    product = f"制备了{structure}介孔材料" if structure else "制备了介孔材料"
    if not clauses:
        return f"本文{product}。"
    return f"本文{'、'.join(clauses)}，{product}。"


def _results_line(records: list[dict[str, Any]]) -> str:
    """Closing line: the characterisation numbers, in Chinese."""
    def first(key: str) -> str:
        for record in records:
            value = _clean((record.get("fields") or {}).get(key) or "")
            if value:
                return _to_chinese(value)
        return ""

    parts: list[str] = []
    area = first("surface_area")
    pore = first("pore_size")
    morphology = first("morphology")
    if area:
        parts.append(f"比表面积约 {area} m²/g")
    if pore:
        parts.append(f"孔径约 {pore} nm")
    if morphology:
        parts.append(f"产物形貌为{morphology}")
    return f"表征结果：{'，'.join(parts)}。" if parts else ""


def _heuristic_summary(
    title: str,
    pages: list[str],
    records: list[dict[str, Any]],
) -> dict[str, str]:
    lines: list[str] = []
    pairs = _field_pairs(records, _HIGHLIGHT_FIELDS)

    if records:
        lines.append(_overview(records))
        lines.append(f"全文共抽取到 {len(records)} 条样品记录。")
    if pairs:
        lines.append("关键合成条件：")
        # 字段值一并中文化，避免「产物形态：fibers; spheres」这类中英混排
        lines.extend(f"· {label}：{_to_chinese(value)}" for label, value in pairs)

    results = _results_line(records)
    if results:
        lines.append(results)

    topics = _topics(pages)
    if topics:
        lines.append("研究要点：")
        lines.extend(f"· {topic}。" for topic in topics)

    if not lines:
        has_text = any(page.strip() for page in pages)
        lines.append(
            "未能从正文中提取出合成条件或研究要点；可在「文献库」先运行抽取，或改用 AI 模式重新生成总结。"
            if has_text
            else "暂无可用于总结的文本内容（可能是纯图片 PDF，或尚未解析出正文）。"
        )

    brief = _brief_from_fields(records) or (_clean(title) or "暂无总结")

    return {
        "summary": "\n".join(lines),
        "brief": _truncate(brief),
        "engine": "heuristic",
    }


_SYSTEM = (
    "你是材料化学文献助手。只能依据给定的论文片段与已抽取字段作答，不得补充片段之外的事实。"
    "用中文输出 JSON，包含两个键："
    "summary —— 完整总结，3 到 6 行，先一句话概括研究目标，再写合成路线与关键条件，最后写表征结果；"
    "brief —— 一句话速览，不超过 40 个汉字，突出材料体系与最关键的 1-2 个指标。"
    "不要编造页码，不要输出 JSON 之外的内容。"
)


def _llm_summary(
    title: str,
    pages: list[str],
    records: list[dict[str, Any]],
    max_chars: int,
) -> dict[str, str] | None:
    from mkea_extract.config import LLMConfig
    from mkea_extract.llm import OpenAICompatibleLLM, extract_json

    body = _clean(" ".join(pages))[:max_chars]
    if not body:
        return None

    pairs = _field_pairs(records, FIELD_KEYS)
    known = "\n".join(f"- {label}：{value}" for label, value in pairs) or "（尚无抽取结果）"

    llm = OpenAICompatibleLLM(LLMConfig.from_env())
    response = llm.complete(
        [
            {"role": "system", "content": _SYSTEM},
            {
                "role": "user",
                "content": (
                    f"论文标题：{_clean(title) or '（未知）'}\n"
                    f"样品记录数：{len(records)}\n"
                    f"已抽取字段：\n{known}\n\n"
                    f"论文正文片段：\n{body}\n\n"
                    "请输出 JSON。"
                ),
            },
        ],
        json_mode=True,
        max_tokens=900,
    )
    data = extract_json(response.text) or {}
    summary = _clean(data.get("summary") or "")
    brief = _clean(data.get("brief") or "")
    if not summary:
        return None
    return {
        "summary": summary,
        "brief": _truncate(brief or summary),
        "engine": "llm",
    }


def build_summary(
    *,
    title: str,
    pages: list[str],
    records: list[dict[str, Any]],
    max_chars: int = 24000,
) -> dict[str, str]:
    """Summarise one paper, degrading to the offline composer on any failure."""
    pages = [page or "" for page in pages]
    if llm_status()["available"]:
        try:
            result = _llm_summary(title, pages, records, max_chars)
            if result:
                return result
        except Exception:  # noqa: BLE001 - a summary must never break the app
            pass
    return _heuristic_summary(title, pages, records)


__all__ = ["BRIEF_LIMIT", "build_summary"]
