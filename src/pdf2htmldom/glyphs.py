"""Stage 3a: read text that was converted to outlines, from the letter shapes.

Some producers (Publisher, Illustrator, "convert text to outlines") export text
as vector paths. The words are gone from the text layer, but the exact letter
outlines are still in the file, and those outlines are the font's own glyphs.

So instead of OCR guessing from pixels, each outlined letter is compared with
the glyphs in the font files installed on this machine. A match is exact: an
``S`` and a ``5`` are different outlines, not similar pixels. What does not
match -- a font that is not installed, letters fused into one shape -- is left
for OCR and reported. Nothing is half-guessed: a row of text is read completely
from shapes, or not at all.

How a letter is matched: its outline points must be one affine transform away
from a reference glyph's points. That single least-squares fit absorbs
everything a producer does to set text -- size, horizontal squeeze, the slant of
a synthesised italic -- and hands back the exact baseline and pen position, so
nothing about placement is estimated.

Steps, per outlined path:

1. split the path into closed contours, and the contours into rows;
2. group contours into letters (holes inside an outline, dots above a stem);
3. fit every reference glyph with the same contour structure;
4. per row, agree on one face, size, squeeze, slant and baseline, then pick
   each letter's match consistently with that;
5. recover spaces from the gaps, splitting the row where a gap is a column gap;
6. join pieces of one line that the producer drew as separate paths, putting
   back hyphens it drew as tiny rectangles.
"""

from __future__ import annotations

import math
import os
import re
import statistics
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np

from .models import BBox, DrawingBlock, Line, Span

FIT_TOL = 0.03  # max point deviation after the affine fit, as a fraction of the letter's size
STRETCH_RANGE = (0.6, 1.35)  # horizontal scale a producer may apply to text
SLANT_MAX = 0.45  # synthesised italic shear, |dx/dy|
ROW_SIZE_TOL = 0.10
ROW_STRETCH_TOL = 0.10
ROW_SLANT_TOL = 0.06
ROW_BASELINE_TOL = 0.2  # as a fraction of the row's font size
DEFAULT_FAMILIES = ("Arial", "Arial Narrow", "Calibri", "Times New Roman")
CHARSET = (
    [chr(c) for c in range(0x21, 0x7F)]
    + [chr(c) for c in range(0xA1, 0x100)]
    + list("\u2018\u2019\u201c\u201d\u2013\u2014\u2022\u2026\u221a\u20ac")
)
LOOKALIKES = ({"I", "l", "|"}, {"O", "0"})


# --- installed fonts -------------------------------------------------------------


def _family_key(name: str) -> str:
    return re.sub(r"[^a-z]", "", name.lower())


def _font_dirs() -> list[Path]:
    dirs: list[Path] = []
    if sys.platform == "win32":
        dirs.append(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts")
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    elif sys.platform == "darwin":
        dirs += [Path("/Library/Fonts"), Path("/System/Library/Fonts"), Path.home() / "Library/Fonts"]
    else:
        dirs += [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), Path.home() / ".fonts"]
    return [d for d in dirs if d.is_dir()]


@lru_cache(maxsize=1)
def _font_files() -> tuple[Path, ...]:
    return tuple(
        p for d in _font_dirs() for p in d.rglob("*") if p.suffix.lower() in (".ttf", ".otf")
    )


@lru_cache(maxsize=64)
def _family_files(family_key: str) -> tuple[str, tuple[tuple[tuple[bool, bool], Path], ...]]:
    """(display name, ((bold, italic), file)...) for one installed family.

    Only files whose name starts like the family are opened -- reading the name
    table of every installed font costs seconds; this costs milliseconds."""
    from fontTools.ttLib import TTFont

    prefix = family_key[:4]
    display = ""
    styles: dict[tuple[bool, bool], Path] = {}
    for p in _font_files():
        if not _family_key(p.stem).startswith(prefix):
            continue
        try:
            f = TTFont(str(p), lazy=True)
            family = f["name"].getDebugName(1) or ""
            style = (f["name"].getDebugName(2) or "").lower()
            f.close()
        except Exception:
            continue
        if _family_key(family) != family_key:
            continue
        display = display or family
        styles.setdefault(("bold" in style, "italic" in style or "oblique" in style), p)
    return display, tuple(styles.items())


# --- reference glyphs ------------------------------------------------------------


def _ordered(contours: list[np.ndarray]) -> list[np.ndarray]:
    """Canonical contour order inside a letter. Sorted by vertical position
    first, because a slant moves points sideways but never up or down."""
    pts = np.vstack(contours)
    lo, hi = pts.min(0), pts.max(0)
    span = np.where(hi - lo > 1e-9, hi - lo, 1.0)

    def key(c):
        m = (c.mean(0) - lo) / span
        return (len(c), round(float(m[1]), 1), round(float(m[0]), 1))

    return sorted(contours, key=key)


@dataclass
class _Ref:
    ch: str
    family: str
    bold: bool
    italic: bool
    pinv: np.ndarray  # pseudo-inverse of [points 1], for the affine fit
    design: np.ndarray  # [points 1], to evaluate the fit
    advance: float
    space: float
    upem: int
    ascent: float  # fraction of the em
    descent: float  # fraction of the em, positive
    sig: tuple[int, ...]  # points per contour, in canonical order


def _load_family(family: str, styles) -> list[_Ref]:
    from fontTools.pens.basePen import BasePen
    from fontTools.ttLib import TTFont

    class _Seg(BasePen):
        """Each contour as its segment end points, which survive PDF export as-is."""

        def __init__(self, gs):
            super().__init__(gs)
            self.contours: list[list[tuple[float, float]]] = []
            self.cur: list[tuple[float, float]] | None = None

        def _moveTo(self, p):
            self.cur = [p]

        def _lineTo(self, p):
            self.cur.append(p)

        def _curveToOne(self, a, b, p):
            self.cur.append(p)

        def _qCurveToOne(self, a, p):
            self.cur.append(p)

        def _closePath(self):
            if self.cur and len(self.cur) > 1:
                if self.cur[0] == self.cur[-1]:
                    self.cur.pop()
                self.contours.append(self.cur)
            self.cur = None

        _endPath = _closePath

    refs: list[_Ref] = []
    for (bold, italic), path in styles:
        try:
            font = TTFont(str(path))
        except Exception:
            continue
        gs, cmap, upem = font.getGlyphSet(), font.getBestCmap() or {}, font["head"].unitsPerEm
        hhea = font["hhea"]
        space = gs[cmap[32]].width if 32 in cmap else upem * 0.25
        for ch in CHARSET:
            name = cmap.get(ord(ch))
            if not name:
                continue
            pen = _Seg(gs)
            gs[name].draw(pen)
            if not pen.contours:
                continue
            contours = _ordered([np.array([(x, -y) for x, y in c], float) for c in pen.contours])  # y down
            pts = np.vstack(contours)
            design = np.hstack([pts, np.ones((len(pts), 1))])
            refs.append(
                _Ref(ch, family, bold, italic, np.linalg.pinv(design), design,
                     float(gs[name].width), float(space), upem, hhea.ascent / upem, -hhea.descent / upem,
                     tuple(len(c) for c in contours))
            )
        font.close()
    return refs


@lru_cache(maxsize=32)
def _family_refs(family_key: str) -> tuple[_Ref, ...]:
    display, styles = _family_files(family_key)
    return tuple(_load_family(display, styles)) if styles else ()


def reference(families: tuple[str, ...]) -> dict[tuple[int, ...], list[_Ref]]:
    """Reference glyphs for these families, indexed by contour structure."""
    by_sig: dict[tuple[int, ...], list[_Ref]] = {}
    for fam in families:
        for r in _family_refs(_family_key(fam)):
            by_sig.setdefault(r.sig, []).append(r)
    return by_sig


def families_for(pdf_font_names: list[str]) -> tuple[str, ...]:
    """The defaults, plus installed families the PDF names ("BCDEEE+Arial-BoldMT")."""
    found: list[str] = list(DEFAULT_FAMILIES)
    keys = {_family_key(f) for f in found}
    for name in pdf_font_names:
        base = re.split(r"[-,]", name.split("+")[-1])[0]
        base = re.sub(r"(PSMT|PS|MT)$", "", base)
        k = _family_key(base)
        if k and k not in keys and _family_files(k)[1]:
            found.append(base)
            keys.add(k)
    return tuple(found)


# --- outlined paths -> letters ---------------------------------------------------


def _path_contours(items: list) -> list[np.ndarray]:
    """Closed subpaths of one drawing, as segment end points (page coords)."""
    contours: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] | None = None
    prev = None
    for it in items:
        kind = it[0]
        if kind == "re":
            r = it[1]
            contours.append([(r.x0, r.y0), (r.x1, r.y0), (r.x1, r.y1), (r.x0, r.y1)])
            prev = None
            continue
        if kind not in ("l", "c"):
            continue
        p0, pend = it[1], it[-1]
        if prev is None or abs(p0.x - prev.x) > 0.01 or abs(p0.y - prev.y) > 0.01:
            if cur and len(cur) > 1:
                contours.append(cur)
            cur = [(p0.x, p0.y)]
        cur.append((pend.x, pend.y))
        prev = pend
    if cur and len(cur) > 1:
        contours.append(cur)
    out = []
    for c in contours:
        if len(c) > 2 and abs(c[0][0] - c[-1][0]) < 0.01 and abs(c[0][1] - c[-1][1]) < 0.01:
            c = c[:-1]
        if len(c) >= 2:
            out.append(np.array(c, float))
    return out


def _box(c: np.ndarray) -> tuple[float, float, float, float]:
    return float(c[:, 0].min()), float(c[:, 1].min()), float(c[:, 0].max()), float(c[:, 1].max())


def _rows(contours: list[np.ndarray]) -> list[list[np.ndarray]]:
    """Lines of text: contours whose vertical extents overlap form one row.

    Marks above the letters do not overlap them vertically -- the dot of an i
    in "in", an accent -- so a row much shorter than the tallest letter joins
    the row nearest to it. A path of a few contours is one mark (":", "=") and
    is never split."""
    if len(contours) <= 4:
        return [contours]
    spans = sorted(((_box(c)[1], _box(c)[3], c) for c in contours), key=lambda t: t[0])
    rows: list[list] = []
    for y0, y1, c in spans:
        if rows and y0 < rows[-1][1]:
            rows[-1][1] = max(rows[-1][1], y1)
            rows[-1][2].append(c)
        else:
            rows.append([y0, y1, [c]])
    tallest = max(y1 - y0 for y0, y1, _ in spans)
    i = 0
    while len(rows) > 1 and i < len(rows):
        y0, y1, members = rows[i]
        if y1 - y0 <= 0.35 * tallest:
            gaps = []
            if i > 0:
                gaps.append((y0 - rows[i - 1][1], i - 1))
            if i + 1 < len(rows):
                gaps.append((rows[i + 1][0] - y1, i + 1))
            gap, j = min(gaps)
            if gap <= 3 * (y1 - y0):
                rows[j][0], rows[j][1] = min(rows[j][0], y0), max(rows[j][1], y1)
                rows[j][2].extend(members)
                del rows[i]
                continue
        i += 1
    return [m for _, _, m in rows]


def _letters(contours: list[np.ndarray]) -> tuple[list[list[np.ndarray]], list[tuple[np.ndarray, list[int]]]]:
    """Contours of one row grouped into letters.

    A contour inside another's box is a hole (A, e, o) or part of a compound
    glyph (%). A mark above or below another contour, overlapping it
    horizontally, is a dot or accent (i, j, !, :, =).

    A mark over two letters at once -- the dot of an italic i leaning over the
    next letter -- cannot be placed by position. Those come back separately
    with their candidate letters, and matching decides: dot + stem is an "i",
    dot + "p" is nothing.

    Returns (letters, [(mark, indices of candidate letters)]).
    """
    boxes = [_box(c) for c in contours]
    parent = list(range(len(contours)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    eps = 0.05
    stacked_with: dict[int, list[int]] = {}
    for i, a in enumerate(boxes):
        for j in range(i + 1, len(boxes)):
            b = boxes[j]
            if b[0] > a[2] + eps or a[0] > b[2] + eps:
                continue  # no horizontal overlap: separate letters
            inside = (
                (b[0] >= a[0] - eps and b[2] <= a[2] + eps and b[1] >= a[1] - eps and b[3] <= a[3] + eps)
                or (a[0] >= b[0] - eps and a[2] <= b[2] + eps and a[1] >= b[1] - eps and a[3] <= b[3] + eps)
            )
            if inside:
                parent[find(i)] = find(j)
                continue
            x_overlap = min(a[2], b[2]) - max(a[0], b[0])
            if x_overlap >= 0.5 * min(a[2] - a[0], b[2] - b[0]) and (a[3] <= b[1] + eps or b[3] <= a[1] + eps):
                # the smaller of the two is the mark
                mark, base = (i, j) if (a[3] - a[1]) * (a[2] - a[0]) <= (b[3] - b[1]) * (b[2] - b[0]) else (j, i)
                stacked_with.setdefault(mark, []).append(base)

    pending: list[tuple[int, list[int]]] = []
    for mark, bases in stacked_with.items():
        roots = {find(b) for b in bases}
        if len(roots) == 1:
            parent[find(mark)] = roots.pop()
        else:
            pending.append((mark, sorted(roots)))

    loose = {m for m, _ in pending}
    groups: dict[int, list[np.ndarray]] = {}
    for i, c in enumerate(contours):
        if i not in loose:
            groups.setdefault(find(i), []).append(c)
    order = sorted(groups, key=lambda r: min(_box(c)[0] for c in groups[r]))
    index = {r: k for k, r in enumerate(order)}
    marks = [(contours[m], [index[find(r)] for r in roots if find(r) in index]) for m, roots in pending]
    return [groups[r] for r in order], marks


@dataclass
class _Match:
    ref: _Ref
    error: float
    size: float  # font size, pt
    stretch: float  # horizontal scale
    slant: float  # dx per unit of height going down (synthesised italic is negative)
    baseline: float
    origin: float  # pen x before this letter

    @property
    def face(self) -> tuple[str, bool, bool]:
        return self.ref.family, self.ref.bold, self.ref.italic


@dataclass
class _Letter:
    box: tuple[float, float, float, float]
    points: int
    matches: list[_Match] = field(default_factory=list)
    chosen: _Match | None = None
    alternatives: set[str] = field(default_factory=set)


def _match_letter(group: list[np.ndarray], ref: dict) -> _Letter:
    contours = _ordered(group)
    pts = np.vstack(contours)
    box = (float(pts[:, 0].min()), float(pts[:, 1].min()), float(pts[:, 0].max()), float(pts[:, 1].max()))
    letter = _Letter(box=box, points=len(pts))
    scale = max(box[2] - box[0], box[3] - box[1], 1e-6)
    variants = [pts]
    if len(contours) == 1:  # a contour may run the other way round
        c = contours[0]
        variants.append(np.roll(c[::-1], 1, axis=0))
    for r in ref.get(tuple(len(c) for c in contours), ()):
        for p in variants:
            m = r.pinv @ p  # 3x2 affine: [x y 1] (font units) -> page
            err = float(np.abs(r.design @ m - p).max()) / scale
            if err > FIT_TOL:
                continue
            a, b, c_, d = m[0, 0], m[1, 0], m[0, 1], m[1, 1]
            if d <= 0 or a <= 0 or abs(c_) > 0.03 * d:
                continue
            stretch, slant = a / d, b / d
            if not (STRETCH_RANGE[0] <= stretch <= STRETCH_RANGE[1]) or abs(slant) > SLANT_MAX:
                continue
            letter.matches.append(_Match(r, err, d * r.upem, stretch, slant, m[2, 1], m[2, 0]))
            break
    return letter


def _read_row(contours: list[np.ndarray], ref: dict) -> list[_Letter]:
    """Letters of one row, with ambiguous marks attached where they make a letter."""
    groups, marks = _letters(contours)
    letters = [_match_letter(g, ref) for g in groups]
    for mark, candidates in marks:
        for k in candidates:
            trial = _match_letter(groups[k] + [mark], ref)
            if trial.matches:
                groups[k] = groups[k] + [mark]
                letters[k] = trial
                break
        else:
            letters.append(_match_letter([mark], ref))  # no home: it will not match, and the row goes to OCR
    return sorted(letters, key=lambda lt: lt.box[0])


# --- rows -> lines ---------------------------------------------------------------


def _fits(m: _Match, p: tuple[float, float, float, float]) -> bool:
    size, stretch, slant, baseline = p
    return (
        abs(m.size / size - 1) <= ROW_SIZE_TOL
        and abs(m.stretch / stretch - 1) <= ROW_STRETCH_TOL
        and abs(m.slant - slant) <= ROW_SLANT_TOL
        and abs(m.baseline - baseline) <= ROW_BASELINE_TOL * size
    )


def _choose(letters: list[_Letter]) -> tuple[float, float, float, float] | None:
    """Agree on the row's face and geometry, then pick each letter's match.

    Every face found among the well-structured letters is a hypothesis; the one
    under which the most letters fit wins (ties: the least squeezed). Letters
    whose own face differs (a bold word in a regular line) may keep it if their
    geometry agrees. None if any letter has no consistent match."""
    if not letters or any(not lt.matches for lt in letters):
        return None
    anchors = [lt for lt in letters if lt.points >= 8] or letters
    best = None
    for face in {m.face for lt in anchors for m in lt.matches}:
        picks = [
            min((m for m in lt.matches if m.face == face), key=lambda m: m.error, default=None)
            for lt in anchors
        ]
        picks = [m for m in picks if m is not None]
        if not picks:
            continue
        params = (
            statistics.median(m.size for m in picks),
            statistics.median(m.stretch for m in picks),
            statistics.median(m.slant for m in picks),
            statistics.median(m.baseline for m in picks),
        )
        fitted = sum(any(m.face == face and _fits(m, params) for m in lt.matches) for lt in letters)
        score = (fitted, -abs(math.log(params[1])), -abs(params[2]))
        if best is None or score > best[0]:
            best = (score, face, params)
    if best is None:
        return None
    _, face, params = best
    return _assign(letters, params, face)


def _assign(letters: list[_Letter], params, face=None):
    """Pick each letter's match consistent with the row's geometry."""
    size = params[0]

    def cost(m: _Match) -> float:
        return (
            m.error
            + abs(m.size / size - 1)
            + abs(m.stretch / params[1] - 1)
            + abs(m.slant - params[2])
            + abs(m.baseline - params[3]) / size
            + (0.0 if face is None or m.face == face else 0.05)
        )

    for lt in letters:
        fitting = sorted((m for m in lt.matches if _fits(m, params)), key=cost)
        if not fitting:
            return None
        lt.chosen = fitting[0]
        lt.alternatives = {m.ref.ch for m in fitting if cost(m) <= cost(fitting[0]) + 0.02}
    return params


def _disambiguate(chars: list[str], letters: list[_Letter | None]) -> list[str]:
    """Identical shapes (I / l / |, O / 0) decided by the neighbouring letters."""
    out = list(chars)
    for i, lt in enumerate(letters):
        if lt is None or len(lt.alternatives) < 2:
            continue
        prev = next((c for c in reversed(out[:i]) if c != " "), "")
        nxt = next((c for c in out[i + 1:] if c != " "), "")
        if lt.alternatives <= LOOKALIKES[0]:
            out[i] = "l" if (prev.islower() or nxt.islower()) else "I"
        elif lt.alternatives <= LOOKALIKES[1]:
            out[i] = "0" if (prev.isdigit() or nxt.isdigit()) else "O"
    return out


def _advance(m: _Match, size: float, stretch: float) -> float:
    return m.ref.advance * size / m.ref.upem * stretch


def _space(m: _Match, size: float, stretch: float) -> float:
    return m.ref.space * size / m.ref.upem * stretch


def _render_face(m: _Match, stretch: float, slant: float) -> tuple[str, bool]:
    """Font name and italic flag the HTML should use for this letter's face.

    A producer's synthetic styles are kept visible: a slanted upright face is
    italic, and Arial squeezed to Arial Narrow's width (about 82%) renders as
    Arial Narrow -- otherwise the browser draws it a fifth too wide."""
    family = m.ref.family
    if _family_key(family) == "arial" and 0.76 <= stretch <= 0.88:
        family = "Arial Narrow"
    return family, m.ref.italic or abs(slant) >= 0.08


def _make_line(letters: list[_Letter], chars: list[str], owners: list, params, color: str, line_id: str) -> Line:
    size, stretch, slant, baseline = params
    first = letters[0].chosen
    x1 = letters[-1].chosen.origin + _advance(letters[-1].chosen, size, stretch)
    bbox = BBox(x0=first.origin, y0=baseline - first.ref.ascent * size, x1=x1, y1=baseline + first.ref.descent * size)
    spans: list[Span] = []
    face = None
    for ch, lt in zip(chars, owners):
        if lt is not None and lt.chosen.face != face:
            r = lt.chosen.ref
            family, italic = _render_face(lt.chosen, stretch, slant)
            spans.append(
                Span(
                    text="", font=family, size=size, color=color, bold=r.bold, italic=italic,
                    serif="times" in r.family.lower(), bbox=bbox,
                    origin=(lt.chosen.origin, baseline), ascender=r.ascent, descender=-r.descent,
                )
            )
            face = lt.chosen.face
        spans[-1].text += ch
    return Line(id=line_id, bbox=bbox, baseline=baseline, origin_x=first.origin, spans=spans, source="glyph")


def _row_lines(letters: list[_Letter], params, color: str, next_id) -> tuple[list[Line], int]:
    """Spaces from gaps; a gap wider than ~2 spaces starts a new line, so a
    column gap is placed exactly instead of approximated with spaces."""
    size, stretch = params[0], params[1]
    ends = [lt.chosen.origin + _advance(lt.chosen, size, stretch) for lt in letters]
    gaps = [letters[i + 1].chosen.origin - ends[i] for i in range(len(letters) - 1)]
    tracking = min(statistics.median(gaps), 0.15 * size) if gaps else 0.0

    pieces: list[list[int]] = [[0]]
    spaces_before: dict[int, int] = {}
    for i in range(1, len(letters)):
        space = _space(letters[i].chosen, size, stretch)
        extra = gaps[i - 1] - tracking
        if extra >= 2.5 * space:
            pieces.append([i])
            continue
        if extra >= 0.4 * space:
            spaces_before[i] = 1
        pieces[-1].append(i)

    lines: list[Line] = []
    ambiguous = 0
    for piece in pieces:
        chars: list[str] = []
        owners: list = []
        for i in piece:
            if spaces_before.get(i):
                chars.append(" ")
                owners.append(None)
            chars.append(letters[i].chosen.ref.ch)
            owners.append(letters[i])
        chars = _disambiguate(chars, owners)
        ambiguous += sum(1 for i in piece if len(letters[i].alternatives) > 1)
        lines.append(_make_line([letters[i] for i in piece], chars, owners, params, color, next_id()))
    return lines, ambiguous


def _distance(a: tuple, b: tuple) -> float:
    """Horizontal gap between two boxes (0 when they overlap)."""
    return max(0.0, max(a[0], b[0]) - min(a[2], b[2]))


def _hex(color) -> str:
    if color is None:
        return "#000000"
    r, g, b = (max(0, min(255, round(float(c) * 255))) for c in color[:3])
    return f"#{r:02x}{g:02x}{b:02x}"


# --- joining pieces of one line ----------------------------------------------------


def join_pieces(lines: list[Line], drawings: list) -> tuple[list[Line], set[str]]:
    """Producers draw a line as several paths (one per word or style), and draw
    hyphens as tiny filled rectangles. Join pieces that sit on one baseline with
    a word-sized gap, and turn a hyphen-shaped rectangle in such a gap into "-".

    Returns the joined lines and the ids of drawings consumed as hyphens."""
    dashes = [
        d for d in drawings
        if isinstance(d, DrawingBlock) and d.fill and d.is_rect and not d.path_d
        and d.bbox.width <= 6 and d.bbox.height <= 1.6
    ]
    used: set[str] = set()
    # Bands of lines sharing a baseline, each read left to right.
    bands: list[list[Line]] = []
    for ln in sorted(lines, key=lambda ln: ln.baseline):
        if bands and ln.baseline - bands[-1][0].baseline <= 0.15 * max(ln.size, 1.0):
            bands[-1].append(ln)
        else:
            bands.append([ln])
    lines = [ln for band in bands for ln in sorted(band, key=lambda ln: ln.origin_x)]
    def joinable(ln: Line) -> bool:
        # A lone text-layer fragment (a "-" the producer kept as real text
        # between two outlined words) bridges pieces like any other piece.
        return ln.source == "glyph" or (ln.source == "pdf" and len(ln.text.strip()) <= 3)

    out: list[Line] = []
    for ln in lines:
        prev = out[-1] if out else None
        if prev is not None and joinable(prev) and joinable(ln) and "glyph" in (prev.source, ln.source):
            size = prev.size
            space = 0.278 * size
            gap = ln.origin_x - prev.bbox.x1
            # One baseline is one line; sizes may differ (capitals in 10pt, the
            # rest in 8pt is common), and each span keeps its own size.
            same_line = abs(ln.baseline - prev.baseline) <= 0.15 * size
            if same_line and -0.3 * size <= gap <= 2.5 * space:
                dash = next(
                    (
                        d for d in dashes
                        if d.id not in used
                        and prev.bbox.x1 - 0.5 <= d.bbox.x0 and d.bbox.x1 <= ln.origin_x + 0.5
                        and prev.baseline - 0.45 * size <= d.bbox.cy <= prev.baseline - 0.1 * size
                    ),
                    None,
                )
                joined = [s.model_copy() for s in prev.spans]
                if dash is not None:
                    used.add(dash.id)
                    pre = " " if dash.bbox.x0 - prev.bbox.x1 >= 0.4 * space else ""
                    post = " " if ln.origin_x - dash.bbox.x1 >= 0.4 * space else ""
                    joined[-1].text += f"{pre}-{post}"
                elif gap >= 0.4 * space and not joined[-1].text.endswith(" "):
                    joined[-1].text += " "
                incoming = [s.model_copy() for s in ln.spans]
                if ln.source == "pdf":
                    if ln.text.startswith(" ") and not joined[-1].text.endswith(" "):
                        joined[-1].text += " "
                    incoming[0].text = incoming[0].text.lstrip()
                    incoming[-1].text = incoming[-1].text.rstrip() + " "
                for s in incoming:
                    last = joined[-1]
                    if (s.font, s.bold, s.italic, s.color) == (last.font, last.bold, last.italic, last.color) \
                            and abs(s.size - last.size) < 0.05:
                        last.text += s.text
                    else:
                        joined.append(s.model_copy())
                out[-1] = prev.model_copy(
                    update={"spans": joined, "bbox": prev.bbox.union(ln.bbox), "source": "glyph"}
                )
                continue
        out.append(ln)
    return out, used


@dataclass
class GlyphResult:
    lines: list[Line] = field(default_factory=list)
    unread: list[tuple[float, float, float, float]] = field(default_factory=list)  # left for OCR
    families: set[str] = field(default_factory=set)
    ambiguous: int = 0  # letters decided by context, not by shape
    dash_drawings: set[str] = field(default_factory=set)


def recover_outlined_text(
    paths: list[dict],
    page_no: int,
    pdf_font_names: list[str] | None = None,
    text_bboxes: list[tuple[float, float, float, float]] | None = None,
    drawings: list | None = None,
) -> GlyphResult:
    """Read outlined text paths back into ``Line``s."""
    result = GlyphResult()
    if not paths:
        return result
    try:
        ref = reference(families_for(pdf_font_names or []))
    except ImportError:  # fontTools missing: everything goes to OCR as before
        result.unread = [tuple(p["rect"]) for p in paths]
        return result

    counter = iter(range(10**9))
    lines: list[Line] = []

    def next_id() -> str:
        return f"p{page_no}-g{next(counter)}"

    covered = [(b[0] - 1, b[1] - 1, b[2] + 1, b[3] + 1) for b in (text_bboxes or [])]

    # Every row of every path, matched letter by letter.
    rows: list[tuple[list[_Letter], tuple, str]] = []
    for p in paths:
        if p.get("fill_opacity") == 0:
            continue
        color = _hex(p.get("fill") if p.get("fill") is not None else p.get("color"))
        for row_contours in _rows(_path_contours(p.get("items", []))):
            letters = _read_row(row_contours, ref)
            if not letters:
                continue
            boxes = [lt.box for lt in letters]
            rect = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                    max(b[2] for b in boxes), max(b[3] for b in boxes))
            cx, cy = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
            if any(c[0] <= cx <= c[2] and c[1] <= cy <= c[3] for c in covered):
                continue  # the text layer already carries this row
            rows.append((letters, rect, color))

    # Rows long enough to agree on their own geometry first; then one- and
    # two-letter rows (a lone "/", an apostrophe, a word drawn as its own path)
    # take it from the row they sit beside. Alone, such a row cannot tell a
    # slanted "I" from "/", or "’" from ",".
    solved: list[tuple[tuple, tuple]] = []  # (rect, params)
    pending = []
    for letters, rect, color in rows:
        if len(letters) < 3:
            pending.append((letters, rect, color))
            continue
        params = _choose(letters)
        if params is None:
            result.unread.append(rect)
            continue
        solved.append((rect, params))
        row_lines, ambiguous = _row_lines(letters, params, color, next_id)
        lines.extend(row_lines)
        result.ambiguous += ambiguous

    for letters, rect, color in pending:
        params = None
        for nrect, nparams in sorted(solved, key=lambda s: _distance(rect, s[0])):
            size = nparams[0]
            if _distance(rect, nrect) > 4 * size:
                break
            if min(rect[3], nrect[3]) - max(rect[1], nrect[1]) <= 0:
                continue  # not on the same line
            params = _assign(letters, nparams)
            if params is not None:
                break
        params = params or _choose(letters)
        if params is None:
            result.unread.append(rect)
            continue
        row_lines, ambiguous = _row_lines(letters, params, color, next_id)
        lines.extend(row_lines)
        result.ambiguous += ambiguous

    result.lines, result.dash_drawings = join_pieces(lines, drawings or [])
    result.families = {s.font for ln in result.lines for s in ln.spans}  # as rendered
    return result
