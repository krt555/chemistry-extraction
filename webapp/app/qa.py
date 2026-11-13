"""Citation-grounded question answering over stored papers.

Retrieval is deterministic BM25-style scoring over page chunks, so answers
always carry a real page number.  When an LLM is configured the retrieved
chunks are summarised into a natural-language answer; otherwise the top chunks
are returned as an extractive answer.  Either way the citation list is produced
by retrieval, never by the model, which is what makes the citations verifiable.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "is", "are", "was", "were", "be", "to",
    "in", "on", "for", "with", "by", "at", "as", "that", "this", "it", "from",
    "we", "our", "their", "its", "which", "what", "how", "why", "when",
    "是", "的", "了", "吗", "呢", "和", "与", "在", "有", "多少", "什么", "哪些",
    "这个", "那个", "请问", "一下", "怎么", "如何",
}

SYNONYMS = {
    "比表面积": ["bet", "surface", "area", "m2/g"],
    "表面积": ["bet", "surface", "area"],
    "孔径": ["pore", "size", "diameter", "nm", "bjh"],
    "孔道": ["pore", "channel", "mesopore"],
    "孔容": ["pore", "volume", "cm3/g"],
    "表面活性剂": ["surfactant", "p123", "f127", "ctab", "template", "copolymer"],
    "模板剂": ["template", "surfactant", "structure-directing"],
    "硅源": ["teos", "silica", "source", "tmos", "orthosilicate"],
    "煅烧": ["calcined", "calcination", "550", "air"],
    "焙烧": ["calcined", "calcination"],
    "水热": ["hydrothermal", "autoclave", "aged"],
    "老化": ["aged", "aging", "stirred"],
    "形貌": ["morphology", "sem", "tem", "particles", "spherical", "rod-like"],
    "介孔": ["mesoporous", "mesostructure", "mesopore"],
    "结构": ["structure", "hexagonal", "cubic", "p6mm", "im3m"],
    "温度": ["temperature", "°c", "heated"],
    "时间": ["time", "hours"],
    "酸": ["acid", "hcl", "ph", "acidic"],
    "碱": ["base", "naoh", "ammonia", "basic"],
    "盐": ["salt", "nacl", "kcl"],
    "溶剂": ["solvent", "ethanol", "etoh", "water"],
    "合成": ["synthesis", "synthesised", "synthesized", "preparation", "prepared"],
    "制备": ["preparation", "prepared", "synthesis"],
    "步骤": ["typical", "procedure", "experimental"],
    "条件": ["conditions", "typical", "experimental"],
    "作用": ["role", "effect", "essential", "promote"],
    "产率": ["yield"],
    "样品": ["sample", "product", "material"],
}

# Question words that should pull in the experimental section.
INTENT_HINTS = [
    (r"怎么|如何|步骤|流程|制备|合成方法", ["typical", "experimental", "procedure", "prepared"]),
    (r"多少|数值|是多大|几", ["was", "reached", "about"]),
    (r"作用|为什么|影响", ["essential", "promote", "effect", "control", "found"]),
]


def tokenize(text: str) -> list[str]:
    text = text.lower()
    tokens = re.findall(r"[a-z0-9][a-z0-9\-/\.]*|[\u4e00-\u9fff]+", text)
    out: list[str] = []
    for token in tokens:
        if token in STOPWORDS or len(token) < 2:
            continue
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            # Index Chinese by bigrams so partial phrases still match.
            if len(token) == 2:
                out.append(token)
            else:
                out.extend(token[i : i + 2] for i in range(len(token) - 1))
            out.extend(SYNONYMS.get(token, []))
        else:
            out.append(token)
    return out


def expand_query(question: str) -> list[str]:
    tokens = tokenize(question)
    for phrase, extra in SYNONYMS.items():
        if phrase in question:
            tokens.extend(extra)
    for pattern, extra in INTENT_HINTS:
        if re.search(pattern, question):
            tokens.extend(extra)
    return tokens


class Retriever:
    """BM25 over (paper, page) chunks."""

    def __init__(self, chunks: list[dict[str, Any]]) -> None:
        self.chunks = chunks
        self.tokens = [tokenize(chunk["text"]) for chunk in chunks]
        self.lengths = [len(item) or 1 for item in self.tokens]
        self.avg_len = sum(self.lengths) / max(1, len(self.lengths))
        self.freqs = [Counter(item) for item in self.tokens]
        self.df: Counter = Counter()
        for item in self.freqs:
            self.df.update(item.keys())
        self.n = max(1, len(chunks))

    def search(self, question: str, top_k: int = 4) -> list[dict[str, Any]]:
        query = expand_query(question)
        if not query or not self.chunks:
            return []
        k1, b = 1.5, 0.75
        scored: list[tuple[float, int]] = []
        for index, freq in enumerate(self.freqs):
            score = 0.0
            for term in query:
                tf = freq.get(term, 0)
                if not tf:
                    continue
                idf = math.log(1 + (self.n - self.df[term] + 0.5) / (self.df[term] + 0.5))
                norm = tf * (k1 + 1) / (
                    tf + k1 * (1 - b + b * self.lengths[index] / self.avg_len)
                )
                score += idf * norm
            if score > 0:
                scored.append((score, index))
        scored.sort(reverse=True)
        results = []
        for score, index in scored[:top_k]:
            chunk = dict(self.chunks[index])
            chunk["score"] = round(score, 3)
            chunk["snippet"] = best_snippet(chunk["text"], query)
            results.append(chunk)
        return results


def best_snippet(text: str, query: list[str], width: int = 300) -> str:
    """Return a window centred on the densest cluster of query terms."""
    flat = " ".join(text.split())
    if len(flat) <= width:
        return flat
    lowered = flat.lower()

    # Rare terms carry the question's intent ("calcined"), common ones do not.
    weights = {term: 1.0 / (1 + lowered.count(term)) for term in set(query)}
    positions: list[tuple[int, float]] = []
    for term, weight in weights.items():
        start = lowered.find(term)
        while start != -1:
            positions.append((start, weight))
            start = lowered.find(term, start + 1)
    if not positions:
        return flat[:width].strip() + "…"

    # Score each hit by the weighted mass of other hits within half a window.
    half = width // 2
    best_centre, best_score = positions[0][0], -1.0
    for centre, _ in positions:
        score = sum(w for pos, w in positions if abs(pos - centre) <= half)
        if score > best_score:
            best_score, best_centre = score, centre

    start = max(0, min(best_centre - half + 40, len(flat) - width))
    # Snap to a word boundary so the quote reads naturally.
    if start > 0:
        space = flat.find(" ", start)
        if 0 <= space < start + 25:
            start = space + 1
    snippet = flat[start : start + width].strip()
    return ("…" if start else "") + snippet + ("…" if start + width < len(flat) else "")


ANSWER_SYSTEM = (
    "你是材料化学文献助手。只能依据给定的页面片段回答，不得补充片段之外的事实。"
    "全程用中文作答，英文术语、化学式与数值保留原样即可。"
    "回答必须分两段：第一段以「**内容总结**：」开头，用 1-3 句中文概括检索到的片段讲了什么；"
    "第二段以「**详细回答**：」开头，给出针对问题的具体结论，每个结论句末标注来源，格式为 [p.页码]。"
    "若片段不足以回答，直接说明“检索到的页面中没有相关信息”。"
)


def _extractive_digest(question: str, hits: list[dict[str, Any]]) -> str:
    """One Chinese sentence describing what the retrieved pages cover.

    The offline path must not invent an answer, so the digest only states
    which papers/pages matched and which topics their text touches — all of
    it derived from the retrieved snippets, never from the model.
    """
    papers = sorted({hit["paper_key"] for hit in hits})
    pages = sorted({hit["page"] for hit in hits})
    page_list = "、".join(str(p) for p in pages)
    # 中文与半角编号/数字相接处补一个空格，保证两种分支读起来都顺。
    if len(papers) == 1:
        where = f"共在 {papers[0]} 的第 {page_list} 页中"
    else:
        where = f"共在 {len(papers)} 篇论文的第 {page_list} 页中"

    blob = " ".join(hit["snippet"] for hit in hits).lower()
    topics = [name for pattern, name in _DIGEST_TOPICS if re.search(pattern, blob)]
    what = "、".join(topics[:4]) if topics else "该问题相关的合成与表征描述"
    return (
        f"**内容总结**：围绕「{question.strip()}」，{where}检索到相关原文，"
        f"内容涉及{what}。"
    )


# 片段主题 -> 中文说法，用于抽取式回答的开头概括。
_DIGEST_TOPICS: tuple[tuple[str, str], ...] = (
    (r"surface area|bet\b|m2/g|m²/g", "比表面积"),
    (r"pore (size|diameter|volume)|bjh|\bnm\b", "孔径/孔容"),
    (r"surfactant|p123|f127|ctab|template|copolymer", "模板剂与表面活性剂"),
    (r"teos|silica source|tmos|orthosilicate", "硅源"),
    (r"calcin|aged|aging|hydrothermal|autoclave|°c", "温度程序与水热/煅烧条件"),
    (r"hexagonal|cubic|p6mm|im3m|mesostructur", "介孔结构与空间群"),
    (r"morpholog|sem|tem|spher|fiber|rod-like|film", "产物形貌"),
    (r"\bhcl\b|\bph\b|acid|naoh|ammonia", "酸碱条件"),
)


def build_answer(question: str, hits: list[dict[str, Any]], llm_available: bool) -> dict[str, Any]:
    if not hits:
        return {
            "answer": "检索到的页面中没有相关信息。可以换个说法，或先确认该论文已完成抽取。",
            "citations": [],
            "engine": "retrieval",
        }

    citations = [
        {
            "paper_key": hit["paper_key"],
            "title": hit.get("title", ""),
            "page": hit["page"],
            "quote": hit["snippet"],
            "score": hit["score"],
        }
        for hit in hits
    ]

    if llm_available:
        try:
            from mkea_extract.config import LLMConfig
            from mkea_extract.llm import OpenAICompatibleLLM

            context = "\n\n".join(
                f"[片段 {i}] 论文 {hit['paper_key']} 第 {hit['page']} 页：\n{hit['snippet']}"
                for i, hit in enumerate(hits, 1)
            )
            llm = OpenAICompatibleLLM(LLMConfig.from_env())
            response = llm.complete(
                [
                    {"role": "system", "content": ANSWER_SYSTEM},
                    {
                        "role": "user",
                        "content": f"问题：{question}\n\n可用片段：\n{context}\n\n请作答。",
                    },
                ],
                json_mode=False,
                max_tokens=900,
            )
            text = response.text.strip()
            if text:
                return {"answer": text, "citations": citations, "engine": "llm"}
        except Exception as exc:  # degrade to extractive answer
            _ = exc

    lines = [
        _extractive_digest(question, hits),
        "",
        f"**详细回答**：以下是最相关的 {len(hits)} 段原文（未配置 AI，当前为抽取式回答，直接返回原文片段）：",
    ]
    for hit in hits:
        lines.append(f"· {hit['snippet']} [p.{hit['page']}]")
    return {"answer": "\n".join(lines), "citations": citations, "engine": "retrieval"}
