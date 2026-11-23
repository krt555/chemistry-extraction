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
    "反应": ["reaction", "synthesis", "reacted", "prepared", "preparation", "product"],
    "总结": ["synthesis", "preparation", "prepared", "experimental", "procedure"],
    "步骤": ["typical", "procedure", "experimental"],
    "条件": ["conditions", "typical", "experimental"],
    "主要": ["main", "major", "primary"],
    "作用": ["role", "effect", "essential", "promote"],
    "产率": ["yield"],
    "样品": ["sample", "product", "material"],
}

# Question words that should pull in the experimental section.
INTENT_HINTS = [
    (r"怎么|如何|步骤|流程|制备|合成方法", ["typical", "experimental", "procedure", "prepared"]),
    (r"多少|数值|是多大|几", ["was", "reached", "about"]),
    (r"作用|为什么|影响", ["essential", "promote", "effect", "control", "found"]),
    (r"总结|概括|概述|综述|整体|主要|overview|summar", [
        "synthesis", "preparation", "experimental", "procedure", "reaction", "product",
    ]),
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
        is_overview = bool(re.search(r"总结|概括|概述|综述|整体|主要|overview|summar", question, re.I))
        if is_overview:
            top_k = max(top_k, min(8, len(self.chunks)))
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
        if is_overview:
            selected: list[tuple[float, int]] = []
            selected_indexes: set[int] = set()
            seen_papers: set[int] = set()
            for item in scored:
                paper_fk = int(self.chunks[item[1]]["paper_fk"])
                if paper_fk not in seen_papers:
                    selected.append(item)
                    selected_indexes.add(item[1])
                    seen_papers.add(paper_fk)
                    if len(selected) == top_k:
                        break
            if len(selected) < top_k:
                selected.extend(
                    item for item in scored
                    if item[1] not in selected_indexes
                )
            scored = selected
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
    "你是材料化学文献助手，任务是**回答用户的问题**，不是复述或翻译原文。"
    "只能依据给定的页面内容作答，不得补充其中没有的事实。"
    "全程用中文回答：英文原文必须翻译成中文表述，化学式、试剂缩写（如 TEOS、P123、CTAB）"
    "、空间群（如 p6mm、Im3m）和数值单位保留原样。"
    "\n回答格式必须是两段："
    "\n第一段以「**直接回答**：」开头，用 1-3 句中文**正面回答用户的问题**，"
    "直接给出结论、数值或条件；不要罗列原文，也不要说“文中提到”。"
    "\n第二段以「**依据**：」开头，分条列出支撑该结论的具体内容。"
    "每条必须写明来自哪一篇论文：格式为「论文编号《标题》第 N 页：中文说明 [p.N]」。"
    "若需要引用英文原句，请在中文说明之后用括号附上，不要只给英文。"
    "\n如果多篇论文都提到该问题，请分别说明各篇的做法或数值差异。"
    "\n若页面内容确实无法回答该问题，第一段直接说明“检索到的页面中没有相关信息”，"
    "并说明可以换什么说法再问。"
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

def build_context_blocks(hits: list[dict[str, Any]], budget: int = 24000) -> str:
    """Render retrieved pages for the prompt, newest-relevant first.

    Each block names the paper explicitly so the model can tell the user which
    article an answer came from.
    """
    blocks: list[str] = []
    for index, hit in enumerate(hits, 1):
        full = " ".join(str(hit.get("text") or hit["snippet"]).split())
        if len(full) > 6000:
            full = full[:6000] + "…"
        block = (
            f"[来源 {index}] 论文编号 {hit['paper_key']}"
            f"｜标题《{hit.get('title') or '未命名'}》｜第 {hit['page']} 页\n"
            f"检索命中片段：{hit['snippet']}\n"
            f"该页完整内容：{full}"
        )
        if blocks and budget - len(block) < 0:
            break
        budget -= len(block)
        blocks.append(block)
    return "\n\n".join(blocks)


def build_question_prompt(question: str, hits: list[dict[str, Any]]) -> list[dict[str, str]]:
    context = build_context_blocks(hits)
    return [
        {"role": "system", "content": ANSWER_SYSTEM},
        {
            "role": "user",
            "content": (
                f"用户的问题：{question}\n\n"
                f"可用的论文页面内容：\n{context}\n\n"
                "请依据以上内容，用中文正面回答这个问题，"
                "并说明结论来自哪一篇论文（写出论文编号和标题）。"
            ),
        },
    ]


def citations_of(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "paper_key": hit["paper_key"],
            "title": hit.get("title", ""),
            "page": hit["page"],
            "quote": hit["snippet"],
            "score": hit["score"],
        }
        for hit in hits
    ]


def stream_answer(question: str, hits: list[dict[str, Any]]):
    """Yield the answer incrementally; raises when no LLM is configured."""
    from mkea_extract.config import LLMConfig
    from mkea_extract.llm import OpenAICompatibleLLM

    llm = OpenAICompatibleLLM(LLMConfig.from_env())
    yield from llm.stream(build_question_prompt(question, hits), max_tokens=1200)


def build_answer(question: str, hits: list[dict[str, Any]], llm_available: bool) -> dict[str, Any]:
    if not hits:
        return {
            "answer": "检索到的页面中没有相关信息。可以换个说法，或先确认该论文已完成抽取。",
            "citations": [],
            "engine": "retrieval",
        }

    citations = citations_of(hits)

    if llm_available:
        try:
            from mkea_extract.config import LLMConfig
            from mkea_extract.llm import OpenAICompatibleLLM

            llm = OpenAICompatibleLLM(LLMConfig.from_env())
            response = llm.complete(
                build_question_prompt(question, hits),
                json_mode=False,
                max_tokens=1200,
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
