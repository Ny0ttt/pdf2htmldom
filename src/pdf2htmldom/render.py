"""Stage 6: IR -> HTML + CSS.

How the layout rule from the spec is implemented:

* The page is a fixed-size container measured in ``pt``. CSS ``pt`` is 1/72in,
  identical to a PDF point, so the IR's coordinates are emitted verbatim and
  there is no scaling arithmetic anywhere.
* Typography becomes reusable classes; geometry stays inline, because positions
  are unique by nature and classes would not deduplicate them.
* Each visual line is its own absolutely positioned ``<span>`` with
  ``white-space: pre``, nested inside the semantic element for its paragraph.
  The semantic tag therefore never moves content, DOM order stays equal to
  reading order, and the text remains real, selectable and editable.
* Text is never clipped. ``overflow: visible`` everywhere so that a mis-sized
  box shows up as a measurable error instead of silently truncating.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field

from .models import (
    BBox,
    Document,
    BoxRowBlock,
    DrawingBlock,
    FrameBlock,
    ImageBlock,
    Line,
    Page,
    Span,
    TableBlock,
    TextBlock,
)

# --- font substitution -------------------------------------------------------

# System fonts first. Arial and Times New Roman are metric-compatible with the
# base-14 Helvetica and Times, so advance widths (and line placement) still match.
MONO_STACK = "Consolas,'Courier New',monospace"
SERIF_STACK = "'Times New Roman',Times,serif"
SANS_STACK = "Arial,Helvetica,sans-serif"
NARROW_STACK = "'Arial Narrow','Liberation Sans Narrow','Helvetica Condensed',Arial,sans-serif"
CALIBRI_STACK = "Calibri,Carlito,Arial,sans-serif"


@dataclass
class RenderInfo:
    targets: dict[str, dict] = field(default_factory=dict)
    font_substitutions: dict[str, str] = field(default_factory=dict)
    classes: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    text_chars: int = 0


def css_family(span: Span) -> str:
    name = span.font.split("+")[-1].lower()
    if span.mono or "mono" in name or "courier" in name:
        return MONO_STACK
    if "narrow" in name or "condensed" in name:
        return NARROW_STACK
    if "calibri" in name:
        return CALIBRI_STACK
    if span.serif or any(k in name for k in ("times", "serif", "georgia", "garamond", "book", "roman")):
        return SERIF_STACK
    return SANS_STACK


def _key(span: Span) -> str:
    return "|".join(
        [
            css_family(span),
            f"{span.size:.2f}",
            span.color,
            "b" if span.bold else "",
            "i" if span.italic else "",
        ]
    )


def _style_for(span: Span) -> str:
    parts = [f"font-family:{css_family(span)}", f"font-size:{span.size:.2f}pt", f"color:{span.color}"]
    if span.bold:
        parts.append("font-weight:700")
    if span.italic:
        parts.append("font-style:italic")
    return ";".join(parts)


class _ClassPool:
    """Deduplicates typography into reusable classes (spec section 6)."""

    def __init__(self) -> None:
        self._by_key: dict[str, str] = {}
        self.css: list[str] = []
        self.substitutions: dict[str, str] = {}

    def get(self, span: Span) -> str:
        k = _key(span)
        if k not in self._by_key:
            name = f"t{len(self._by_key) + 1}"
            self._by_key[k] = name
            self.css.append(f".{name}{{{_style_for(span)}}}")
            if span.font.split("+")[-1].lower() not in css_family(span).lower():
                self.substitutions.setdefault(span.font, css_family(span).split(",")[0].strip("'"))
        return self._by_key[k]


# --- geometry helpers --------------------------------------------------------


def _render_span(s: Span, pool: _ClassPool, fs: float = 1.0) -> str:
    """Render one styled run.

    A size correction has to be applied to the span itself: the span's class sets
    its own ``font-size``, which overrides anything inherited from the line, so a
    correction written only on the line would be silently discarded.
    """
    style = f' style="font-size:{s.size * fs:.3f}pt"' if abs(fs - 1.0) > 0.001 else ""
    return f'<span class="{pool.get(s)}"{style}>{html.escape(s.text)}</span>'


def _line_top(line: Line) -> float:
    """Top edge that puts the rendered baseline on the PDF's baseline.

    CSS places a line box's baseline at ``half-leading + ascent`` from the top.
    With ``line-height: 1`` the box height equals the font size, so the ascent
    fraction from the PDF's own span metrics is the right offset. Residual
    error from font substitution is measured and corrected in the browser pass.
    """
    return line.baseline - line.ascender * line.size


def _target(el_id: str, bbox: BBox, **extra) -> dict:
    return {"x": bbox.x0, "y": bbox.y0, "w": bbox.width, "h": bbox.height, **extra}


# --- element renderers -------------------------------------------------------


def _render_text_block(
    b: TextBlock, pool: _ClassPool, info: RenderInfo, corrections: dict[str, dict] | None = None
) -> str:
    if b.kind == "heading" and b.level:
        tag = f"h{min(b.level, 6)}"
    elif b.list_style == "ul":
        tag = "ul"
    elif b.list_style == "ol":
        tag = "ol"
    else:
        tag = "p"

    item_open, item_close = ("", "")
    if tag in ("ul", "ol"):
        item_open, item_close = "<li>", "</li>"

    corrections = corrections or {}
    lines_html: list[str] = []
    for ln in b.lines:
        # Corrections from the browser measurement pass are applied here, so the
        # exported HTML is already right and needs no JavaScript to be correct.
        fix = corrections.get(ln.id, {})
        fs = float(fix.get("fs", 1.0))
        spans = "".join(
            _render_span(s, pool, fs) for s in ln.spans
        )
        info.text_chars += len(ln.text)
        # The block is the positioned ancestor, so line offsets are relative to it.
        # data-* attributes stay in absolute page coordinates so the browser pass
        # can compare against the IR without knowing the nesting.
        left = ln.origin_x - b.bbox.x0
        top = _line_top(ln) - b.bbox.y0 + float(fix.get("dy", 0.0))
        extra = f";letter-spacing:{fix['ls']:.3f}pt" if fix.get("ls") else ""
        lines_html.append(
            f'<span class="ln" data-el="{ln.id}" data-src="{ln.source}" data-baseline="{ln.baseline:.3f}" '
            f'data-x="{ln.origin_x:.3f}" data-w="{ln.bbox.width:.3f}" '
            f'style="left:{left:.3f}pt;top:{top:.3f}pt;font-size:{ln.size * fs:.2f}pt{extra}">{spans}</span>'
        )
        info.targets[ln.id] = _target(
            ln.id, ln.bbox, baseline=ln.baseline, kind="line", size=ln.size, text=ln.text
        )

    info.targets[b.id] = _target(b.id, b.bbox, kind=b.kind, tag=tag)
    return (
        f'<{tag} class="blk" data-el="{b.id}" data-kind="{b.kind}" '
        f'style="left:{b.bbox.x0:.3f}pt;top:{b.bbox.y0:.3f}pt;'
        f'width:{max(b.bbox.width, 1):.3f}pt;height:{max(b.bbox.height, 1):.3f}pt">'
        f"{item_open}{''.join(lines_html)}{item_close}</{tag}>"
    )


def _render_table(t: TableBlock, pool: _ClassPool, info: RenderInfo) -> str:
    """A real <table> with real cells, positioned at its measured box."""
    col_w = [t.col_edges[i + 1] - t.col_edges[i] for i in range(len(t.col_edges) - 1)]
    row_h = [t.row_edges[i + 1] - t.row_edges[i] for i in range(len(t.row_edges) - 1)]

    by_pos: dict[tuple[int, int], list] = {}
    for c in t.cells:
        by_pos.setdefault((c.row, c.col), []).append(c)

    rows_html: list[str] = []
    for r in range(t.n_rows):
        cells_html: list[str] = []
        for c in range(t.n_cols):
            cell_list = by_pos.get((r, c))
            if not cell_list:
                continue
            cell = cell_list[0]
            attrs = ""
            if cell.colspan > 1:
                attrs += f' colspan="{cell.colspan}"'
            if cell.rowspan > 1:
                attrs += f' rowspan="{cell.rowspan}"'
            tag = "th" if cell.is_header else "td"

            inner: list[str] = []
            if cell.lines:
                first = cell.lines[0]
                # Padding places the cell's first baseline where the PDF put it.
                pad_top = max(0.0, (first.baseline - first.ascender * first.size) - cell.bbox.y0)
                pad_left = max(0.0, first.origin_x - cell.bbox.x0)
                text = "<br>".join(
                    "".join(f'<span class="{pool.get(s)}">{html.escape(s.text)}</span>' for s in ln.spans)
                    for ln in cell.lines
                )
                info.text_chars += sum(len(ln.text) for ln in cell.lines)
                inner.append(
                    f'<div class="cellpad" style="padding:{pad_top:.2f}pt 0 0 {pad_left:.2f}pt">{text}</div>'
                )
            cells_html.append(f'<{tag}{attrs} data-el="{cell.bbox.x0:.0f}-{cell.bbox.y0:.0f}">{ "".join(inner) }</{tag}>')
        rows_html.append(f"<tr>{''.join(cells_html)}</tr>")

    cols = "".join(f'<col style="width:{w:.3f}pt">' for w in col_w)
    info.targets[t.id] = _target(t.id, t.bbox, kind="table", rows=t.n_rows, cols=t.n_cols)

    return (
        f'<table class="tbl" data-el="{t.id}" style="left:{t.bbox.x0:.3f}pt;top:{t.bbox.y0:.3f}pt;'
        f'width:{t.bbox.width:.3f}pt;height:{t.bbox.height:.3f}pt;'
        f'--rule:{t.rule_width:.2f}pt">'
        f"<colgroup>{cols}</colgroup><tbody>{''.join(rows_html)}</tbody></table>"
    )


def _render_drawing(d: DrawingBlock, info: RenderInfo) -> str:
    info.targets[d.id] = _target(d.id, d.bbox, kind="drawing")
    thin_h = d.bbox.height <= 1.6 and d.bbox.width > 8
    thin_v = d.bbox.width <= 1.6 and d.bbox.height > 8
    if thin_h:
        return (
            f'<div class="rule-h" data-el="{d.id}" style="left:{d.bbox.x0:.3f}pt;top:{d.bbox.y0:.3f}pt;'
            f'width:{d.bbox.width:.3f}pt;height:{max(d.bbox.height, 0.5):.3f}pt;'
            f'background:{d.fill or d.stroke or "#000"}"></div>'
        )
    if thin_v:
        return (
            f'<div class="rule-v" data-el="{d.id}" style="left:{d.bbox.x0:.3f}pt;top:{d.bbox.y0:.3f}pt;'
            f'width:{max(d.bbox.width, 0.5):.3f}pt;height:{d.bbox.height:.3f}pt;'
            f'background:{d.fill or d.stroke or "#000"}"></div>'
        )
    if d.path_d:
        # Real vector geometry, rendered as SVG at 1:1 in page points.
        fill = d.fill or "none"
        rule = "evenodd" if d.even_odd else "nonzero"
        stroke = f' stroke="{d.stroke}" stroke-width="{d.width:.2f}"' if d.stroke else ""
        return (
            f'<svg class="vec" data-el="{d.id}" '
            f'style="left:{d.bbox.x0:.3f}pt;top:{d.bbox.y0:.3f}pt;'
            f'width:{d.bbox.width:.3f}pt;height:{d.bbox.height:.3f}pt" '
            f'viewBox="{d.bbox.x0:.3f} {d.bbox.y0:.3f} {d.bbox.width:.3f} {d.bbox.height:.3f}" '
            f'preserveAspectRatio="none">'
            f'<path d="{d.path_d}" fill="{fill}" fill-rule="{rule}"{stroke}/></svg>'
        )
    bg = d.fill or "transparent"
    border = f"1pt solid {d.stroke}" if d.stroke and d.fill is None else "none"
    return (
        f'<div class="drw" data-el="{d.id}" style="left:{d.bbox.x0:.3f}pt;top:{d.bbox.y0:.3f}pt;'
        f'width:{d.bbox.width:.3f}pt;height:{d.bbox.height:.3f}pt;'
        f'background:{bg};border:{border}"></div>'
    )


def _render_box_row(b: BoxRowBlock, info: RenderInfo) -> str:
    info.targets[b.id] = _target(b.id, b.bbox, kind="boxrow", count=b.count)
    return (
        f'<div class="box-row" data-el="{b.id}" style="left:{b.bbox.x0:.3f}pt;top:{b.bbox.y0:.3f}pt;'
        f'--box-w:{b.box_width:.3f}pt;--box-h:{b.box_height:.3f}pt;'
        f'--rule:{b.rule_width:.3f}pt;--rule-c:{b.color}">'
        + '<div class="form-box"></div>' * b.count
        + "</div>"
    )


def _render_frame(f: FrameBlock, info: RenderInfo) -> str:
    info.targets[f.id] = _target(f.id, f.bbox, kind="frame")
    return (
        f'<div class="frame" data-el="{f.id}" style="left:{f.bbox.x0:.3f}pt;top:{f.bbox.y0:.3f}pt;'
        f'width:{f.bbox.width:.3f}pt;height:{f.bbox.height:.3f}pt;'
        f'border:{f.rule_width:.3f}pt solid {f.color}{"" if f.top else ";border-top:0"}"></div>'
    )


def _render_image(im: ImageBlock, info: RenderInfo) -> str:
    info.targets[im.id] = _target(im.id, im.bbox, kind="image")
    return (
        f'<img class="img" data-el="{im.id}" src="assets/{im.asset}" alt="" '
        f'style="left:{im.bbox.x0:.3f}pt;top:{im.bbox.y0:.3f}pt;'
        f'width:{im.bbox.width:.3f}pt;height:{im.bbox.height:.3f}pt">'
    )


# --- page + document ---------------------------------------------------------


BASE_CSS = """
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:#e9e9ee}
body{font-kerning:none;-webkit-font-smoothing:antialiased}
.page{position:relative;background:#fff;overflow:visible;margin:0 auto}
.blk{position:absolute;margin:0;padding:0;overflow:visible;font-weight:400}
.blk>ul,.blk>ol,.blk>li{list-style:none;margin:0;padding:0}
.ln{position:absolute;white-space:pre;line-height:1;display:block;overflow:visible;
  font-size:inherit}
.tbl{position:absolute;border-collapse:collapse;table-layout:fixed;overflow:visible}
.tbl td,.tbl th{padding:0;vertical-align:top;overflow:visible;
  border:var(--rule) solid #000;font-weight:400;text-align:left}
.cellpad{display:block}
.tbl th .cellpad{font-weight:700}
.drw,.rule-h,.rule-v,.img,.vec,.frame{position:absolute}
.vec{overflow:visible}
.img{object-fit:fill}
.box-row{position:absolute;display:flex}
.form-box{flex:none;width:var(--box-w);height:var(--box-h);border:var(--rule) solid var(--rule-c)}
.form-box+.form-box{margin-left:calc(-1 * var(--rule))}
"""


def render_document(
    doc: Document,
    pages: list[Page] | None = None,
    corrections: dict[str, dict] | None = None,
) -> tuple[str, str, RenderInfo]:
    pool = _ClassPool()
    info = RenderInfo()
    page_html: list[str] = []
    print_rules: list[str] = []

    for page in pages if pages is not None else doc.pages:
        body: list[str] = []
        for el in sorted(page.elements, key=lambda e: e.reading_order):
            if isinstance(el, TextBlock):
                body.append(_render_text_block(el, pool, info, corrections))
            elif isinstance(el, TableBlock):
                body.append(_render_table(el, pool, info))
            elif isinstance(el, DrawingBlock):
                body.append(_render_drawing(el, info))
            elif isinstance(el, ImageBlock):
                body.append(_render_image(el, info))
            elif isinstance(el, BoxRowBlock):
                body.append(_render_box_row(el, info))
            elif isinstance(el, FrameBlock):
                body.append(_render_frame(el, info))
        page_html.append(
            f'<div class="page" data-page="{page.number}" '
            f'style="width:{page.width:.3f}pt;height:{page.height:.3f}pt">{"".join(body)}</div>'
        )
        print_rules.append(
            f"@media print{{.page[data-page=\"{page.number}\"]{{page-break-after:always}}}}"
        )

    css = "\n".join(
        [
            BASE_CSS,
            "\n".join(pool.css),
            f"@page{{size:{doc.pages[0].width:.3f}pt {doc.pages[0].height:.3f}pt;margin:0}}",
            "\n".join(print_rules),
        ]
    )
    html_out = (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        f"<title>{html.escape(doc.source_file)}</title>\n"
        '<link rel="stylesheet" href="styles.css">\n</head>\n<body>\n'
        + "\n".join(page_html)
        + "\n</body>\n</html>\n"
    )
    info.font_substitutions = pool.substitutions
    return html_out, css, info


# --- clean output -------------------------------------------------------------
#
# What the user gets. Same geometry as the measurement render above, written for
# people: one element per text line in page coordinates, semantic wrappers that
# never move anything, five utility classes instead of one class per style, and
# no data-* attributes (those only exist for the browser measuring pass).
#
# Text lines use ``white-space: nowrap`` rather than ``pre``, with every space
# that nowrap would collapse written as &nbsp;. That keeps the exact spacing and
# survives a code formatter re-wrapping long lines in the source, which under
# ``pre`` would turn into real line breaks on the page.

_FAMILY_CLASS = {NARROW_STACK: "n", SERIF_STACK: "s", MONO_STACK: "m", CALIBRI_STACK: "c", SANS_STACK: ""}
_COLOR_CLASS = {"#ffffff": "w", "#ff0000": "r"}

CLEAN_CSS = f"""*{{box-sizing:border-box}}
html,body{{margin:0;background:#e9e9ee}}
body{{font-kerning:none;-webkit-font-smoothing:antialiased}}

/* Every coordinate is in PDF points from the page's top-left corner. */
.page{{position:relative;margin:0 auto;background:#fff;color:#000;font-family:{SANS_STACK}}}
.page :where(p,h1,h2,h3,h4,h5,h6,ol,ul,li){{margin:0;padding:0;font-size:inherit}}
.page :where(h1,h2,h3,h4,h5,h6){{font-weight:400}}
.page :where(ol,ul){{list-style:none}}

/* Everything is placed absolutely. A .grp is a semantic wrapper (paragraph,
   list, heading) pinned to the page origin, so its children use page
   coordinates too. */
.page>*,.grp>*{{position:absolute;line-height:1;white-space:nowrap}}
.grp{{left:0;top:0}}

/* Text styles */
.b{{font-weight:700}}
.i{{font-style:italic}}
.n{{font-family:{NARROW_STACK}}}
.s{{font-family:{SERIF_STACK}}}
.m{{font-family:{MONO_STACK}}}
.c{{font-family:{CALIBRI_STACK}}}
.w{{color:#fff}}
.r{{color:#f00}}

/* Shapes */
.rule{{background:#000}}
.frame{{border:var(--rule,0.5pt) solid var(--rule-c,#000)}}
.bracket{{border:var(--rule,0.5pt) solid var(--rule-c,#000);border-top:0}}

/* Form boxes: one .box-row per field, one .form-box per character */
.box-row{{display:flex;--rule:0.5pt;--rule-c:#808080}}
.form-box{{flex:none;width:var(--box-w);height:var(--box-h);border:var(--rule) solid var(--rule-c)}}
.form-box+.form-box{{margin-left:calc(-1 * var(--rule))}}

/* Tables */
.tbl{{border-collapse:collapse;table-layout:fixed}}
.tbl td,.tbl th{{padding:0;vertical-align:top;font-weight:400;text-align:left}}
.tbl th{{font-weight:700}}
/* Default cell border at zero specificity, so any class or inline style on a
   cell overrides it. Each side has its own variable, falling back to --rule:
   style="--bb:3pt" changes one side only. */
:where(.tbl) :where(td,th){{border-style:solid;border-color:var(--rule-c,#000);
  border-width:var(--bt,var(--rule)) var(--br,var(--rule)) var(--bb,var(--rule)) var(--bl,var(--rule))}}
/* Hide a line. "hidden" beats the neighbouring cell's border, so a shared line really disappears. */
.no-l{{border-left:hidden}}
.no-r{{border-right:hidden}}
.no-lr{{border-left:hidden;border-right:hidden}}
.no-t{{border-top:hidden}}
.no-b{{border-bottom:hidden}}
.no-tb{{border-top:hidden;border-bottom:hidden}}
.no-x{{border:hidden}}
.cellpad{{display:block}}

.page{{break-after:page}}
.page:last-child{{break-after:auto}}
"""


def _num(v: float) -> str:
    """Shortest exact-enough form: 12.500 -> 12.5, 8.000 -> 8."""
    return f"{v:.3f}".rstrip("0").rstrip(".")


def _span_style(s: Span) -> dict:
    return {
        "fam": _FAMILY_CLASS.get(css_family(s), ""),
        "b": s.bold,
        "i": s.italic,
        "color": s.color.lower(),
    }


def _dominant(spans: list[Span]) -> Span:
    return max(spans, key=lambda s: len(s.text.strip()))


def _style_attrs(st: dict, base: dict | None = None) -> tuple[list[str], list[str]]:
    """Classes and inline declarations for ``st``, relative to ``base``."""
    base = base or {"fam": "", "b": False, "i": False, "color": "#000000"}
    cls: list[str] = []
    css: list[str] = []
    if st["fam"] != base["fam"]:
        if st["fam"]:
            cls.append(st["fam"])
        else:
            css.append(f"font-family:{SANS_STACK}")
    if st["b"] != base["b"]:
        cls.append("b") if st["b"] else css.append("font-weight:400")
    if st["i"] != base["i"]:
        cls.append("i") if st["i"] else css.append("font-style:normal")
    if st["color"] != base["color"]:
        if st["color"] in _COLOR_CLASS:
            cls.append(_COLOR_CLASS[st["color"]])
        else:
            css.append(f"color:{st['color']}")
    return cls, css


class _Spaces:
    """Escapes text and turns spaces nowrap would collapse into &nbsp;."""

    def __init__(self) -> None:
        self.prev_space = True  # a line starts as if after a space

    def __call__(self, text: str) -> str:
        out: list[str] = []
        for ch in text:
            if ch == " ":
                out.append("&nbsp;" if self.prev_space else " ")
                self.prev_space = True
            else:
                out.append(html.escape(ch))
                self.prev_space = False
        return "".join(out)


def _inline(ln: Line, base: dict, size: float, fs: float) -> str:
    """The inside of one line: runs in the base style are bare text, others
    become <b>, <i> or a small <span> with only what differs."""
    spans = [s.model_copy() for s in ln.spans]
    while spans and not spans[-1].text.rstrip():
        spans.pop()
    if spans:
        spans[-1] = spans[-1].model_copy(update={"text": spans[-1].text.rstrip()})
    esc = _Spaces()
    parts: list[str] = []
    for s in spans:
        text = esc(s.text)
        if not text:
            continue
        cls, css = _style_attrs(_span_style(s), base)
        if abs(s.size * fs - size) > 0.01:
            css.append(f"font-size:{_num(s.size * fs)}pt")
        if not cls and not css:
            parts.append(text)
        elif not css and sorted(cls) in (["b"], ["i"], ["b", "i"]):
            if "i" in cls:
                text = f"<i>{text}</i>"
            if "b" in cls:
                text = f"<b>{text}</b>"
            parts.append(text)
        else:
            c = f' class="{" ".join(cls)}"' if cls else ""
            st = f' style="{";".join(css)}"' if css else ""
            parts.append(f"<span{c}{st}>{text}</span>")
    return "".join(parts)


def _clean_line(ln: Line, tag: str, corrections: dict[str, dict]) -> str:
    fix = corrections.get(ln.id, {})
    fs = float(fix.get("fs", 1.0))
    size = ln.size * fs
    base = _span_style(_dominant(ln.spans))
    cls, css = _style_attrs(base)
    top = _line_top(ln) + float(fix.get("dy", 0.0))
    css = [f"left:{_num(ln.origin_x)}pt", f"top:{_num(top)}pt", f"font-size:{_num(size)}pt"] + css
    if fix.get("ls"):
        css.append(f"letter-spacing:{_num(fix['ls'])}pt")
    c = f' class="{" ".join(cls)}"' if cls else ""
    return f'<{tag}{c} style="{";".join(css)}">{_inline(ln, base, size, fs)}</{tag}>'


def _clean_text_block(b: TextBlock, corrections: dict[str, dict]) -> str:
    if b.kind == "heading" and b.level:
        tag = f"h{min(b.level, 6)}"
    elif b.list_style in ("ul", "ol"):
        tag = b.list_style
    else:
        tag = "p"

    if tag in ("ul", "ol"):
        if len(b.lines) == 1:
            return f'<{tag} class="grp">{_clean_line(b.lines[0], "li", corrections)}</{tag}>'
        lines = "\n    ".join(_clean_line(ln, "span", corrections) for ln in b.lines)
        return f'<{tag} class="grp">\n  <li class="grp">\n    {lines}\n  </li>\n</{tag}>'
    if len(b.lines) == 1:
        return _clean_line(b.lines[0], tag, corrections)
    lines = "\n  ".join(_clean_line(ln, "span", corrections) for ln in b.lines)
    return f'<{tag} class="grp">\n  {lines}\n</{tag}>'


def _geom(bb: BBox) -> str:
    return (
        f"left:{_num(bb.x0)}pt;top:{_num(bb.y0)}pt;"
        f"width:{_num(bb.width)}pt;height:{_num(bb.height)}pt"
    )


def _clean_drawing(d: DrawingBlock) -> str:
    color = (d.fill or d.stroke or "#000000").lower()
    thin_h = d.bbox.height <= 1.6 and d.bbox.width > 8
    thin_v = d.bbox.width <= 1.6 and d.bbox.height > 8
    if thin_h or thin_v:
        bb = BBox(
            x0=d.bbox.x0, y0=d.bbox.y0,
            x1=d.bbox.x0 + max(d.bbox.width, 0.5), y1=d.bbox.y0 + max(d.bbox.height, 0.5),
        )
        bg = "" if color == "#000000" else f";background:{color}"
        return f'<div class="rule" style="{_geom(bb)}{bg}"></div>'
    if d.path_d:
        fill = d.fill or "none"
        rule = "evenodd" if d.even_odd else "nonzero"
        stroke = f' stroke="{d.stroke}" stroke-width="{d.width:.2f}"' if d.stroke else ""
        return (
            f'<svg style="{_geom(d.bbox)};overflow:visible" '
            f'viewBox="{_num(d.bbox.x0)} {_num(d.bbox.y0)} {_num(d.bbox.width)} {_num(d.bbox.height)}" '
            f'preserveAspectRatio="none"><path d="{d.path_d}" fill="{fill}" fill-rule="{rule}"{stroke}/></svg>'
        )
    if d.fill:
        return f'<div style="{_geom(d.bbox)};background:{d.fill}"></div>'
    stroke = (d.stroke or "#000000").lower()
    extra = ";--rule:1pt" + ("" if stroke == "#000000" else f";--rule-c:{stroke}")
    return f'<div class="frame" style="{_geom(d.bbox)}{extra}"></div>'


def _clean_frame(f: FrameBlock) -> str:
    extra = "" if abs(f.rule_width - 0.5) < 0.01 else f";--rule:{_num(f.rule_width)}pt"
    if f.color.lower() != "#000000":
        extra += f";--rule-c:{f.color}"
    cls = "frame" if f.top else "bracket"
    return f'<div class="{cls}" style="{_geom(f.bbox)}{extra}"></div>'


def _clean_box_row(b: BoxRowBlock) -> str:
    extra = "" if abs(b.rule_width - 0.5) < 0.01 else f";--rule:{_num(b.rule_width)}pt"
    if b.color.lower() != "#808080":
        extra += f";--rule-c:{b.color}"
    style = (
        f"left:{_num(b.bbox.x0)}pt;top:{_num(b.bbox.y0)}pt;"
        f"--box-w:{_num(b.box_width)}pt;--box-h:{_num(b.box_height)}pt{extra}"
    )
    box = '<div class="form-box"></div>'
    if b.count == 1:
        return f'<div class="box-row" style="{style}">{box}</div>'
    rows = [box * min(10, b.count - i) for i in range(0, b.count, 10)]
    return f'<div class="box-row" style="{style}">\n  ' + "\n  ".join(rows) + "\n</div>"


def _clean_table(t: TableBlock) -> str:
    col_w = [t.col_edges[i + 1] - t.col_edges[i] for i in range(len(t.col_edges) - 1)]
    by_pos = {(c.row, c.col): c for c in t.cells}
    rows_html: list[str] = []
    for r in range(t.n_rows):
        cells: list[str] = []
        for c in range(t.n_cols):
            cell = by_pos.get((r, c))
            if cell is None:
                continue
            attrs = (f' colspan="{cell.colspan}"' if cell.colspan > 1 else "") + (
                f' rowspan="{cell.rowspan}"' if cell.rowspan > 1 else ""
            )
            tag = "th" if cell.is_header else "td"
            inner = ""
            if cell.lines:
                first = cell.lines[0]
                pad_top = max(0.0, (first.baseline - first.ascender * first.size) - cell.bbox.y0)
                pad_left = max(0.0, first.origin_x - cell.bbox.x0)
                texts = []
                for ln in cell.lines:
                    base = _span_style(_dominant(ln.spans))
                    cls, css = _style_attrs(base)
                    css.insert(0, f"font-size:{_num(ln.size)}pt")
                    c_attr = f' class="{" ".join(cls)}"' if cls else ""
                    texts.append(f'<span{c_attr} style="{";".join(css)}">{_inline(ln, base, ln.size, 1.0)}</span>')
                inner = (
                    f'<div class="cellpad" style="padding:{_num(pad_top)}pt 0 0 {_num(pad_left)}pt">'
                    + "<br>".join(texts) + "</div>"
                )
            cells.append(f"<{tag}{attrs}>{inner}</{tag}>")
        rows_html.append("  <tr>" + "".join(cells) + "</tr>")
    cols = "".join(f'<col style="width:{_num(w)}pt">' for w in col_w)
    return (
        f'<table class="tbl" style="{_geom(t.bbox)};--rule:{_num(t.rule_width)}pt">\n'
        f"  <colgroup>{cols}</colgroup>\n" + "\n".join(rows_html) + "\n</table>"
    )


def render_clean_document(
    doc: Document, corrections: dict[str, dict] | None = None
) -> tuple[str, str]:
    """The human-facing HTML + CSS. Geometry is identical to ``render_document``."""
    corrections = corrections or {}
    pages_html: list[str] = []
    for page in doc.pages:
        body: list[str] = []
        for el in sorted(page.elements, key=lambda e: e.reading_order):
            if isinstance(el, TextBlock):
                body.append(_clean_text_block(el, corrections))
            elif isinstance(el, TableBlock):
                body.append(_clean_table(el))
            elif isinstance(el, DrawingBlock):
                body.append(_clean_drawing(el))
            elif isinstance(el, ImageBlock):
                body.append(f'<img src="assets/{el.asset}" alt="" style="{_geom(el.bbox)}">')
            elif isinstance(el, BoxRowBlock):
                body.append(_clean_box_row(el))
            elif isinstance(el, FrameBlock):
                body.append(_clean_frame(el))
        inner = "\n".join(body).replace("\n", "\n  ")
        pages_html.append(
            f'<div class="page" id="page-{page.number}" '
            f'style="width:{_num(page.width)}pt;height:{_num(page.height)}pt">\n  {inner}\n</div>'
        )
    css = CLEAN_CSS + f"@page{{size:{_num(doc.pages[0].width)}pt {_num(doc.pages[0].height)}pt;margin:0}}\n"
    title = html.escape(doc.source_file.rsplit(".", 1)[0])
    html_out = (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        f"<title>{title}</title>\n"
        '<link rel="stylesheet" href="styles.css">\n</head>\n<body>\n'
        + "\n".join(pages_html)
        + "\n</body>\n</html>\n"
    )
    return html_out, css
