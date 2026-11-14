"""Render PDF pages to images and locate evidence quotes on them.

The review panel used to show a reflowed text layer, which loses the figures,
plots and table layout that matter most when checking a chemistry paper.  This
module serves the real page instead:

* ``render_page`` rasterises one page with PyMuPDF (already a project
  dependency) and caches the PNG on disk, keyed by file content and DPI;
* ``locate_quote`` returns the quote's bounding boxes in *relative* coordinates
  (0..1), so the browser can draw a highlight over the image at any zoom level
  without knowing the render DPI.

Quote matching mirrors ``mkea_extract.pdf.ground_quote``: PDF text layers break
sentences across lines, so an exact search is tried first and progressively
shorter prefixes are used as a fallback.
"""

from __future__ import annotations

import hashlib
import re
import threading
from pathlib import Path
from typing import Any

try:  # optional at import time; errors surface only when a page is requested
    import pymupdf
except ImportError:  # pragma: no cover - exercised only without the optional dep
    pymupdf = None

ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT / "webapp" / "data" / "page_cache"

DEFAULT_DPI = 150
MIN_DPI = 72
MAX_DPI = 300

# PyMuPDF documents are not thread-safe; the job threads and request handlers
# can both land here, so every document open is serialised.
_RENDER_LOCK = threading.Lock()


class PageRenderError(RuntimeError):
    """Raised when a page cannot be rasterised."""


def _require_pymupdf():
    if pymupdf is None:
        raise PageRenderError("PyMuPDF 未安装，无法渲染 PDF 页面")
    return pymupdf


def clamp_dpi(dpi: Any) -> int:
    try:
        value = int(dpi)
    except (TypeError, ValueError):
        return DEFAULT_DPI
    return max(MIN_DPI, min(MAX_DPI, value))


def _cache_key(pdf_path: Path, page: int, dpi: int) -> Path:
    stat = pdf_path.stat()
    token = hashlib.sha256(
        f"{pdf_path.resolve()}:{stat.st_mtime_ns}:{stat.st_size}:{page}:{dpi}".encode()
    ).hexdigest()[:20]
    return CACHE_DIR / f"{token}.png"


def render_page(pdf_path: str | Path, page: int, *, dpi: int = DEFAULT_DPI) -> Path:
    """Rasterise one 1-indexed page and return the cached PNG path."""
    fitz = _require_pymupdf()
    path = Path(pdf_path)
    if not path.exists():
        raise PageRenderError(f"PDF 不存在：{path}")

    dpi = clamp_dpi(dpi)
    cached = _cache_key(path, page, dpi)
    if cached.exists() and cached.stat().st_size > 0:
        return cached

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with _RENDER_LOCK:
        doc = fitz.open(path)
        try:
            if not 1 <= page <= doc.page_count:
                raise PageRenderError(f"页码超出范围：{page}")
            pixmap = doc.load_page(page - 1).get_pixmap(dpi=dpi)
            # Write to a temp name first so a crashed render never leaves a
            # truncated file that later requests would treat as a cache hit.
            # PyMuPDF infers the format from the suffix, so pass it explicitly.
            tmp = cached.with_name(cached.name + ".tmp")
            tmp.write_bytes(pixmap.tobytes("png"))
            tmp.replace(cached)
        except PageRenderError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            raise PageRenderError(f"渲染第 {page} 页失败：{exc}") from exc
        finally:
            doc.close()
    return cached


def page_size(pdf_path: str | Path, page: int) -> dict[str, float]:
    """Page dimensions in PDF points, for aspect-ratio-correct display."""
    fitz = _require_pymupdf()
    path = Path(pdf_path)
    if not path.exists():
        raise PageRenderError(f"PDF 不存在：{path}")
    with _RENDER_LOCK:
        doc = fitz.open(path)
        try:
            if not 1 <= page <= doc.page_count:
                raise PageRenderError(f"页码超出范围：{page}")
            rect = doc.load_page(page - 1).rect
            return {"width": float(rect.width), "height": float(rect.height)}
        finally:
            doc.close()


def _clean_quote(quote: str) -> str:
    """Undo the cosmetic edits that make an exact search fail."""
    text = re.sub(r"-\s*\n\s*", "", str(quote or ""))
    # Retrieval snippets are elided with ellipses; keep the longest fragment.
    parts = [part.strip() for part in re.split(r"…|\.{3,}", text) if part.strip()]
    if parts:
        text = max(parts, key=len)
    return re.sub(r"\s+", " ", text).strip()


def _search(page, needle: str) -> list[Any]:
    if len(needle) < 6:
        return []
    try:
        return list(page.search_for(needle) or [])
    except Exception:  # pragma: no cover - defensive
        return []


def _closest_cluster(boxes: list[Any], max_gap: float = 90.0) -> list[Any]:
    """Keep only the tightest group of rectangles.

    ``search_for`` reports every occurrence on the page; a quote repeated in a
    header or figure caption would otherwise scatter the highlight.  Grouping
    by vertical proximity and keeping the densest group puts the highlight
    where the sentence actually is.
    """
    if len(boxes) <= 1:
        return boxes
    ordered = sorted(boxes, key=lambda b: b.y0)
    groups: list[list[Any]] = [[ordered[0]]]
    for box in ordered[1:]:
        if box.y0 - groups[-1][-1].y0 <= max_gap:
            groups[-1].append(box)
        else:
            groups.append([box])
    # Most rectangles wins; ties go to the tighter (earlier) group.
    return max(groups, key=len)


def _merge_rects(boxes: list[Any]) -> list[Any]:
    """Join rectangles that sit on the same text line.

    ``search_for`` returns one rectangle per line fragment; neighbouring
    fragments of the same sentence would otherwise render as separate,
    ragged highlights.
    """
    if not boxes:
        return []
    ordered = sorted(boxes, key=lambda b: (round(b.y0, 1), round(b.x0, 1)))
    merged = [ordered[0]]
    for box in ordered[1:]:
        last = merged[-1]
        line_height = max(last.y1 - last.y0, box.y1 - box.y0, 1.0)
        same_line = abs(box.y0 - last.y0) <= line_height * 0.6
        # Allow a small horizontal gap so word-by-word hits join up.
        adjacent = box.x0 <= last.x1 + line_height * 1.5
        if same_line and adjacent:
            last.x0 = min(last.x0, box.x0)
            last.x1 = max(last.x1, box.x1)
            last.y0 = min(last.y0, box.y0)
            last.y1 = max(last.y1, box.y1)
        else:
            merged.append(box)
    return merged


def _norm_word(text: str) -> str:
    """Normalise one token for comparison (case/punctuation insensitive)."""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(text).lower())


def _locate_by_words(page, needle: str) -> list[Any]:
    """Find the quote as a contiguous run of words on the page.

    ``search_for`` matches a fragment *everywhere* it occurs, so searching the
    quote piece by piece used to scatter highlights across unrelated parts of
    the page.  Here we instead align the quote against the page's word stream
    and keep the single best-matching window, which is what the reader means by
    "this sentence is here".
    """
    want = [_norm_word(w) for w in needle.split(" ")]
    want = [w for w in want if w]
    if len(want) < 2:
        return []

    try:
        words = page.get_text("words") or []
    except Exception:  # pragma: no cover - defensive
        return []
    if not words:
        return []

    page_tokens = [_norm_word(w[4]) for w in words]
    wanted_set = set(want)

    # Slide a window the size of the quote and score it by how many of the
    # quote's words it contains, in order.
    span = len(want)
    best_score, best_range = 0.0, None
    limit = len(page_tokens)
    for start in range(limit):
        if page_tokens[start] != want[0] and page_tokens[start] not in wanted_set:
            continue
        # Allow the window to stretch a little: PDFs insert hyphens and numbers.
        for width in (span, int(span * 1.4) + 2):
            end = min(limit, start + width)
            window = page_tokens[start:end]
            if not window:
                continue
            matched = sum(1 for token in window if token in wanted_set)
            score = matched / max(len(want), 1)
            # Prefer windows that start exactly at the quote's first word.
            if page_tokens[start] == want[0]:
                score += 0.08
            if score > best_score:
                best_score, best_range = score, (start, end)

    if not best_range or best_score < 0.55:
        return []

    start, end = best_range
    # Keep only the words that actually belong to the quote, so trailing
    # unrelated words in the window are not highlighted.
    rects = [
        pymupdf.Rect(words[i][0], words[i][1], words[i][2], words[i][3])
        for i in range(start, end)
        if page_tokens[i] in wanted_set
    ]
    return rects


def locate_quote(
    pdf_path: str | Path, page: int, quote: str
) -> dict[str, Any]:
    """Find *quote* on *page*, returning relative boxes plus the match mode.

    Boxes are ``{x, y, w, h}`` fractions of the page, so the front-end can
    position a highlight regardless of the image's rendered size.
    """
    fitz = _require_pymupdf()
    path = Path(pdf_path)
    if not path.exists():
        raise PageRenderError(f"PDF 不存在：{path}")

    needle = _clean_quote(quote)
    if not needle:
        return {"found": False, "boxes": [], "mode": "empty"}

    with _RENDER_LOCK:
        doc = fitz.open(path)
        try:
            if not 1 <= page <= doc.page_count:
                raise PageRenderError(f"页码超出范围：{page}")
            target = doc.load_page(page - 1)
            rect = target.rect
            width = float(rect.width) or 1.0
            height = float(rect.height) or 1.0

            hits = _search(target, needle)
            mode = "exact"
            if len(hits) > 1:
                # 同一句话可能在页面上多处出现（页眉、图注、参考文献）。
                # 只保留彼此最靠近的一组，避免高亮散落到无关位置。
                hits = _closest_cluster(hits)

            # PDF text layers hyphenate and re-wrap, so a whole-sentence search
            # often fails. Fall back to aligning the quote against the page's
            # word stream, which keeps the highlight in one place.
            if not hits:
                hits = _locate_by_words(target, needle)
                if hits:
                    mode = "partial"

            if not hits:
                return {"found": False, "boxes": [], "mode": "miss"}

            hits = _merge_rects(hits)

            boxes = []
            for box in hits[:40]:
                boxes.append(
                    {
                        "x": round(max(0.0, box.x0 / width), 5),
                        "y": round(max(0.0, box.y0 / height), 5),
                        "w": round(min(1.0, (box.x1 - box.x0) / width), 5),
                        "h": round(min(1.0, (box.y1 - box.y0) / height), 5),
                    }
                )
            return {"found": True, "boxes": boxes, "mode": mode}
        finally:
            doc.close()


def text_in_rect(
    pdf_path: str | Path, page: int, rect: dict[str, float]
) -> str:
    """Extract the text inside a relative rectangle of one page.

    ``rect`` uses the same 0..1 fractions the front-end draws with, so the
    caller never has to know the render DPI.
    """
    fitz = _require_pymupdf()
    path = Path(pdf_path)
    if not path.exists():
        raise PageRenderError(f"PDF 不存在：{path}")

    with _RENDER_LOCK:
        doc = fitz.open(path)
        try:
            if not 1 <= page <= doc.page_count:
                raise PageRenderError(f"页码超出范围：{page}")
            target = doc.load_page(page - 1)
            bounds = target.rect
            width = float(bounds.width) or 1.0
            height = float(bounds.height) or 1.0

            x0 = max(0.0, float(rect.get("x", 0.0))) * width
            y0 = max(0.0, float(rect.get("y", 0.0))) * height
            x1 = x0 + max(0.0, float(rect.get("w", 0.0))) * width
            y1 = y0 + max(0.0, float(rect.get("h", 0.0))) * height
            if x1 - x0 < 2 or y1 - y0 < 2:
                raise PageRenderError("选区太小，请重新框选")

            clip = fitz.Rect(x0, y0, min(x1, width), min(y1, height))
            # "words" keeps reading order and lets us rebuild lines ourselves;
            # a raw clip extraction would glue columns together.
            words = target.get_text("words", clip=clip) or []
            if not words:
                return ""

            # 选区横跨版心又很扁时，一行会同时命中左右栏，读起来是串行的。
            # 只在这种窄长选区里做分栏；整页/大块选区按 PyMuPDF 的阅读顺序即可，
            # 否则侧边水印之类的竖排文字会被误判成一栏，反而打乱全文。
            region_w = clip.x1 - clip.x0
            region_h = clip.y1 - clip.y0
            xs = sorted(float(w[0]) for w in words)
            split = None
            narrow_band = region_h < height * 0.25 and region_w > width * 0.45
            if narrow_band and len(xs) >= 8:
                gaps = sorted(
                    ((right - left, (left + right) / 2) for left, right in zip(xs, xs[1:])),
                    reverse=True,
                )
                widest, centre = gaps[0]
                runner_up = gaps[1][0] if len(gaps) > 1 else 0.0
                # 真正的栏缝明显突出于普通词距
                if widest > max(width * 0.04, runner_up * 1.25):
                    left_n = sum(1 for x in xs if x < centre)
                    if left_n >= 3 and len(xs) - left_n >= 3:
                        split = centre

            def rebuild(items: list[Any]) -> list[str]:
                lines: dict[int, list[tuple[float, str]]] = {}
                for word in items:
                    x_start, y_start, text = float(word[0]), float(word[1]), word[4]
                    lines.setdefault(int(round(y_start / 3.0)), []).append((x_start, text))
                rows = []
                for key in sorted(lines):
                    row = " ".join(text for _, text in sorted(lines[key]))
                    if row.strip():
                        rows.append(row.strip())
                return rows

            if split is None:
                # get_text("words") 已按阅读顺序返回，直接拼接最稳妥
                parts = [str(w[4]) for w in words]
                joined = " ".join(part for part in parts if part.strip())
            else:
                left = [w for w in words if float(w[0]) < split]
                right = [w for w in words if float(w[0]) >= split]
                joined = " ".join(rebuild(left) + rebuild(right))

            # Re-join hyphenated line breaks so the translator sees whole words.
            return re.sub(r"(\w)-\s+(\w)", r"\1\2", joined).strip()
        finally:
            doc.close()


def clear_cache(max_files: int = 4000) -> int:
    """Trim the render cache when it grows too large.

    Cache keys include the PDF's mtime and size, so stale entries are never
    served; they only occupy space. Oldest files are dropped first.
    """
    if not CACHE_DIR.exists():
        return 0
    files = sorted(CACHE_DIR.glob("*.png"), key=lambda item: item.stat().st_mtime)
    removed = 0
    for item in files[: max(0, len(files) - max_files)]:
        item.unlink(missing_ok=True)
        removed += 1
    return removed


__all__ = [
    "DEFAULT_DPI",
    "PageRenderError",
    "clamp_dpi",
    "clear_cache",
    "locate_quote",
    "page_size",
    "render_page",
    "text_in_rect",
]
