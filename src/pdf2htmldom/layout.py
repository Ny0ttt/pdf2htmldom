"""Stage 4: deterministic structure inference.

Groups extracted lines into paragraphs, headings, headers/footers and lists, and
assigns reading order. Deliberately conservative: when the evidence is thin we
fall back to "one positioned paragraph per line group", because the spec ranks
visual fidelity above semantic elegance.
"""

from __future__ import annotations

import re
import statistics

import pymupdf

from .models import BBox, DrawingBlock, Line, TextBlock

BULLET_RE = re.compile(r"^\s*[\u2022\u25cf\u25aa\u25e6\u2023\u2043\u00b7\u2013\u2014]\s+")
ORDERED_RE = re.compile(r"^\s*(\d{1,3}|[a-zA-Z]|[ivxIVX]{1,4})[.)]\s+")


def body_size(lines: list[Line]) -> float:
    """The document's dominant text size, weighted by how much text uses it."""
    weights: dict[float, int] = {}
    for ln in lines:
        for s in ln.spans:
            key = round(s.size, 1)
            weights[key] = weights.get(key, 0) + len(s.text)
    if not weights:
        return 10.0
    return max(weights.items(), key=lambda kv: kv[1])[0]


def classify_region(block: BBox, page_height: float, page_width: float) -> str | None:
    """Header/footer detection by position, refined by repetition across pages."""
    if block.y1 < page_height * 0.075:
        return "header"
    if block.y0 > page_height * 0.925:
        return "footer"
    return None


def _same_run(prev: Line, cur: Line) -> bool:
    """Should ``cur`` continue the same paragraph as ``prev``?"""
    size_prev = prev.size
    size_cur = cur.size
    if abs(size_prev - size_cur) > 0.6:
        return False
    gap = cur.baseline - prev.baseline
    if gap <= 0:
        return False
    if not (0.85 * size_cur <= gap <= 1.85 * size_cur):
        return False
    # A large indent change starts a new block, unless the previous line filled
    # the measure (in which case it was a justified wrap, not a new paragraph).
    indent_delta = cur.origin_x - prev.origin_x
    if abs(indent_delta) > 14.0:
        return False
    return True


def infer_blocks(
    lines: list[Line],
    page_height: float,
    page_width: float,
    page_no: int,
) -> tuple[list[TextBlock], list[str]]:
    warnings: list[str] = []
    if not lines:
        return [], warnings

    ordered = sorted(lines, key=lambda ln: (round(ln.baseline, 1), ln.origin_x))

    # Multi-column detection: two well-separated x-clusters is the tell.
    if len(ordered) >= 6:
        xs = sorted(ln.origin_x for ln in ordered)
        mid = page_width / 2
        left = [x for x in xs if x < mid - 20]
        right = [x for x in xs if x > mid + 20]
        if len(left) > 0.3 * len(xs) and len(right) > 0.3 * len(xs):
            warnings.append(
                f"page {page_no}: two-column layout suspected "
                f"({len(left)} left / {len(right)} right lines) -- reading order is provisional"
            )

    groups: list[list[Line]] = [[ordered[0]]]
    for ln in ordered[1:]:
        if _same_run(groups[-1][-1], ln):
            groups[-1].append(ln)
        else:
            groups.append([ln])

    blocks: list[TextBlock] = []
    for gi, group in enumerate(groups):
        bbox = group[0].bbox
        for ln in group[1:]:
            bbox = bbox.union(ln.bbox)
        text = "\n".join(ln.text for ln in group)
        list_style = "none"
        if BULLET_RE.match(group[0].text):
            list_style = "ul"
        elif ORDERED_RE.match(group[0].text):
            list_style = "ol"
        blocks.append(
            TextBlock(
                id=f"p{page_no}-b{gi}",
                bbox=bbox,
                lines=group,
                list_style=list_style,
                confidence=1.0,
            )
        )

    for b in blocks:
        region = classify_region(b.bbox, page_height, page_width)
        if region:
            b.kind = region  # type: ignore[assignment]

    return blocks, warnings


def assign_heading_levels(blocks: list[TextBlock], body: float) -> None:
    """Heading levels, document-wide.

    * Text on a coloured section bar is a section heading (h2), set by
      ``merge_bar_headings``. The largest of those, if clearly larger than the
      rest, is the document title (h1).
    * Without bars, unusually large text ranks by size from h1, as in any
      ordinary document.
    * With bars, the bars own h1/h2, and size alone is weak evidence (an OCR
      misread can be huge, a form label can be large). Only bold text at least
      as large as the section headings becomes a sub-heading (h3).
    """
    bar = [b for b in blocks if b.kind == "heading" and b.level == 2 and b.lines]
    if bar:
        top = max(bar, key=lambda b: b.lines[0].size)
        others = [b.lines[0].size for b in bar if b is not top]
        if not others or top.lines[0].size >= max(others) + 1.0:
            top.level = 1
        smallest = min(b.lines[0].size for b in bar)
        for b in blocks:
            if b.kind in ("header", "footer", "heading") or not b.lines:
                continue
            dominant = [max(ln.spans, key=lambda s: len(s.text.strip())) for ln in b.lines if ln.spans]
            if dominant and all(s.bold for s in dominant) and b.lines[0].size >= smallest - 0.5:
                b.kind, b.level, b.list_style = "heading", 3, "none"
        return

    sizes = sorted(
        {
            round(b.lines[0].size, 1)
            for b in blocks
            if b.kind not in ("header", "footer", "heading") and b.lines
        },
        reverse=True,
    )
    heading_sizes = [s for s in sizes if s >= body * 1.12]
    rank = {s: i + 1 for i, s in enumerate(heading_sizes[:6])}

    for b in blocks:
        if b.kind in ("header", "footer", "heading") or not b.lines:
            continue
        size = round(b.lines[0].size, 1)
        if size in rank:
            b.kind = "heading"
            b.level = rank[size]
        elif b.kind == "para":
            b.level = None


def merge_bar_headings(blocks: list[TextBlock], drawings: list) -> list[TextBlock]:
    """Text sitting on a filled section bar is that section's heading.

    A bar often carries two text runs (a Malay title and its English
    translation, or a PDF line plus an OCR line), which layout splits into two
    blocks. They are one heading, so they are merged into one block.
    """
    bars = [
        d.bbox
        for d in drawings
        if isinstance(d, DrawingBlock) and d.fill and not d.path_d
        and d.bbox.width >= 100 and 8 <= d.bbox.height <= 40
    ]
    out: list[TextBlock] = []
    by_bar: dict[int, TextBlock] = {}
    for b in sorted(blocks, key=lambda b: (b.bbox.y0, b.bbox.x0)):
        hit = next(
            (i for i, r in enumerate(bars) if r.x0 <= b.bbox.cx <= r.x1 and r.y0 <= b.bbox.cy <= r.y1),
            None,
        )
        if hit is None or b.kind in ("header", "footer"):
            out.append(b)
            continue
        if hit in by_bar:
            head = by_bar[hit]
            head.lines.extend(b.lines)
            head.bbox = head.bbox.union(b.bbox)
            continue
        b.kind, b.level, b.list_style = "heading", 2, "none"
        by_bar[hit] = b
        out.append(b)
    return out


_HELV: dict[bool, pymupdf.Font] = {}


def _text_width(text: str, bold: bool = False) -> float:
    """Advance width at 1pt, in Helvetica metrics (Arial-compatible)."""
    if bold not in _HELV:
        _HELV[bold] = pymupdf.Font("hebo" if bold else "helv")
    return _HELV[bold].text_length(text, fontsize=1.0)


def merge_stray_fragments(lines: list[Line]) -> tuple[list[Line], int]:
    """Fold short text-layer fragments into the OCR line they belong to.

    Where OCR reads a line the PDF also partly carries as text, the text layer
    can keep a lone piece -- a dash, a bullet -- on the same baseline, inside the
    OCR line's span. It is part of that sentence, so it is inserted at the word
    gap closest to where it sits, instead of floating as its own element.
    """
    hosts = [ln for ln in lines if ln.source in ("ocr", "glyph")]
    merged: set[str] = set()
    for frag in lines:
        text = frag.text.strip()
        if frag.id in merged or frag.source not in ("pdf", "ocr") or not text or len(text) > 3:
            continue
        host = next(
            (
                o for o in hosts
                if o is not frag and o.id not in merged
                and abs(o.baseline - frag.baseline) <= 0.3 * max(o.size, 1.0) + 1.0
                and o.bbox.x0 + 2 < frag.bbox.x0 and frag.bbox.x1 < o.bbox.x1 - 2
            ),
            None,
        )
        if host is None:
            continue
        full_text = host.text
        # Where each character starts, measured span by span at the span's own
        # size and weight (a line can mix 10pt bold with 8pt italic).
        centres: list[float] = []
        pen = 0.0
        for span in host.spans:
            for ch in span.text:
                w = _text_width(ch, span.bold) * span.size
                centres.append(pen + w / 2)
                pen += w
        scale = host.bbox.width / (pen or 1.0)
        gaps = [k for k, ch in enumerate(full_text) if ch == " "]
        if not gaps:
            continue
        mid = (frag.bbox.x0 + frag.bbox.x1) / 2
        k = min(gaps, key=lambda k: abs(host.bbox.x0 + centres[k] * scale - mid))
        # An OCR'd region filled a gap the shape reader left as a space ("( )"),
        # so it replaces that space; a text-layer piece is inserted beside it.
        new = text if frag.source == "ocr" else " " + text + " "
        pos = 0
        for span in host.spans:
            if pos <= k < pos + len(span.text):
                i = k - pos
                span.text = span.text[:i] + new + span.text[i + 1:]
                break
            pos += len(span.text)
        merged.add(frag.id)
    return [ln for ln in lines if ln.id not in merged], len(merged)


def assign_reading_order(blocks: list[TextBlock]) -> None:
    """DOM order == reading order. Visual placement is CSS only."""
    for i, b in enumerate(sorted(blocks, key=lambda b: (round(b.bbox.y0, 1), b.bbox.x0))):
        b.reading_order = i
