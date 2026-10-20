"""Stage 3: OCR and text recovery.

Written for a case the fixtures never had: a PDF whose visible text is drawn as
*vector outlines* while a thin text layer covers only a handful of elements. The
form that prompted this had 260 text spans, 233 of them whitespace-only, and 913
real characters -- against ~113 words rendered as filled paths.

The rule the spec sets is followed exactly: OCR supplies text and coordinates,
and the PDF's own text layer always wins where it exists, because it is exact and
OCR is a guess. Reconciliation is coverage-based rather than per-word guessing:
any OCR word landing inside a real text span is discarded as a duplicate.

The output is ordinary ``Line`` objects, so everything downstream -- table
detection, structure inference, positioning, refinement -- is unchanged.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
import pymupdf
from PIL import Image

# Windows installers don't always put Tesseract on PATH (a per-user winget
# install lands under LOCALAPPDATA), so look in the usual places too.
_TESSERACT_CANDIDATES = [
    os.environ.get("TESSERACT_CMD", ""),
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Tesseract-OCR" / "tesseract.exe"),
]


def _pytesseract():
    import pytesseract

    if not shutil.which(pytesseract.pytesseract.tesseract_cmd):
        for cand in _TESSERACT_CANDIDATES:
            if cand and Path(cand).is_file():
                pytesseract.pytesseract.tesseract_cmd = cand
                break
    return pytesseract

# A word is dropped if its centre falls inside a real text span, expanded by this
# much, because the PDF's own characters are authoritative.
COVER_TOL = 2.0

# Colour distance from the background above which a pixel counts as ink.
INK_DIST = 100.0
# Stroke thickness / font size at or above which a line is treated as bold.
# Measured on Arial: regular 0.10-0.13, bold 0.135-0.17.
BOLD_STROKE_RATIO = 0.133
# Measured width / natural Arial width. Arial Narrow sets at ~0.82 of Arial;
# below the lower bound the OCR box itself is wrong, so don't trust it.
NARROW_WIDTH_RANGE = (0.70, 0.845)

# Ink height -> font size. The divisor depends on what the word actually
# contains: a word's ink height is its x-height, its cap height, or the full
# ascender+descender span, depending on which letters it has. One constant for
# all three overestimates lowercase-with-descenders by ~50%, which throws the
# width fit badly off. Ratios are for Arial/Helvetica: x-height 0.519,
# cap height 0.716, ascender 0.75, descender 0.21.
X_HEIGHT_RATIO = 0.52
CAP_HEIGHT_RATIO = 0.72
ASC_DESC_RATIO = 0.95
SIZE_FROM_HEIGHT = CAP_HEIGHT_RATIO  # fallback only

DESCENDERS = set("gjpqyQ@,_")
ASCENDERS = set("bdfhklt0123456789")


def _height_ratio(text: str) -> float:
    """Ink-height-to-em ratio implied by the characters present."""
    if any(ch in DESCENDERS for ch in text):
        return ASC_DESC_RATIO
    if any(ch in ASCENDERS or ch.isupper() for ch in text):
        return CAP_HEIGHT_RATIO
    return X_HEIGHT_RATIO


def _estimate_size(words: list) -> float:
    """Estimate the em size from per-word ink heights.

    Each word yields its own estimate and the median across the line is taken,
    which is robust to a single badly-boxed word.
    """
    est: list[float] = []
    for text, bbox, _conf in words:
        h = bbox[3] - bbox[1]
        ratio = _height_ratio(text)
        if h > 0 and ratio > 0:
            est.append(h / ratio)
    if not est:
        return 8.0
    est.sort()
    return max(4.0, est[len(est) // 2])


@dataclass
class OcrLine:
    text: str
    bbox: tuple[float, float, float, float]  # pt
    baseline: float
    size: float
    conf: float
    words: list[tuple[str, tuple[float, float, float, float]]] = field(default_factory=list)
    color: str = "#000000"
    bold: bool = False
    narrow: bool = False


def available_languages() -> list[str]:
    try:
        return list(_pytesseract().get_languages(config=""))
    except Exception:
        return []


def _has_descender(word: str) -> bool:
    return any(ch in DESCENDERS for ch in word)


def ocr_page(
    page: pymupdf.Page,
    *,
    dpi: int = 300,
    lang: str = "eng",
    min_conf: float = 35.0,
) -> tuple[list[OcrLine], dict]:
    """OCR a rendered page into line-level primitives in PDF points."""
    pytesseract = _pytesseract()
    from pytesseract import Output

    scale = dpi / 72.0
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    pixels = np.asarray(img)

    try:
        data = pytesseract.image_to_data(img, lang=lang, output_type=Output.DICT, config="--psm 3")
    except Exception as exc:  # missing language pack, tesseract failure, ...
        return [], {"error": str(exc), "dpi": dpi, "lang": lang}

    groups: dict[tuple, list] = {}
    n_words = 0
    n_words_rejected = 0
    for i, raw in enumerate(data["text"]):
        text = (raw or "").strip()
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if not text or conf < min_conf:
            continue
        n_words += 1
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        left, top = data["left"][i] / scale, data["top"][i] / scale
        w, h = data["width"][i] / scale, data["height"][i] / scale
        groups.setdefault(key, []).append((text, (left, top, left + w, top + h), conf))

    lines: list[OcrLine] = []
    for key in sorted(groups):
        # Tesseract groups by visual line, which on a form merges separate fields
        # ("A3. Telefon Bimbit" and "A4. Telefon Pejabat" land on one row). Split
        # wherever the horizontal gap exceeds a couple of ems so each field keeps
        # its own position.
        words = sorted(groups[key], key=lambda w: w[1][0])
        for run in _split_at_gaps(words):
            line = _build_line(run)
            # Tesseract emits fragments for dust, rules and stray marks. A line
            # too small to hold a character is noise, and its bbox is not a width
            # target anything should be fitted to.
            x0, y0, x1, y1 = line.bbox
            if (x1 - x0) < 3.0 or (y1 - y0) < 3.0 or len(line.text.strip()) < 2:
                n_words_rejected += 1
                continue
            if not (4.0 <= line.size <= 40.0):
                n_words_rejected += 1
                continue
            # A checkbox or rule read as "|_|" has no letters or digits in it.
            if not any(ch.isalnum() for ch in line.text):
                n_words_rejected += 1
                continue
            line.text = _fix_pipe_i(line.text)
            line.color, line.bold = _style_from_pixels(pixels, line, scale)
            line.narrow = _is_narrow(line)
            lines.append(line)

    return lines, {
        "dpi": dpi,
        "lang": lang,
        "words": n_words,
        "lines": len(lines),
        "rejected_fragments": n_words_rejected,
    }


def ocr_regions(
    page: pymupdf.Page,
    rects: list[tuple[float, float, float, float]],
    *,
    dpi: int = 600,
    lang: str = "eng",
    min_conf: float = 30.0,
) -> tuple[list[OcrLine], dict]:
    """OCR only the given rectangles, each read as one line of text.

    Used for the few outlined shapes glyph matching could not read (a symbol
    font that is not installed). Page-level OCR would drop a lone character as
    noise, or merge it into a line the text layer already covers; reading the
    region on its own keeps it. High DPI because these regions are small.
    """
    if not rects:
        return [], {}
    pytesseract = _pytesseract()
    from pytesseract import Output

    scale = dpi / 72.0
    lines: list[OcrLine] = []
    for x0, y0, x1, y1 in rects:
        # Tight sideways: the neighbours are already read, and must not be read twice.
        pad_y = max(2.0, 0.4 * (y1 - y0))
        clip = pymupdf.Rect(x0 - 0.3, y0 - pad_y, x1 + 0.3, y1 + pad_y) & page.rect
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip, alpha=False)
        img = Image.new("RGB", (pix.width + 40, pix.height), "white")  # quiet margin for Tesseract
        img.paste(Image.frombytes("RGB", (pix.width, pix.height), pix.samples), (20, 0))
        single = (x1 - x0) <= 1.3 * (y1 - y0)
        try:
            data = pytesseract.image_to_data(
                img, lang=lang, output_type=Output.DICT, config="--psm 10" if single else "--psm 7"
            )
        except Exception as exc:
            return lines, {"error": str(exc)}
        words = []
        for i, raw in enumerate(data["text"]):
            text = (raw or "").strip()
            try:
                conf = float(data["conf"][i])
            except (TypeError, ValueError):
                conf = -1.0
            if not text or conf < min_conf:
                continue
            left = clip.x0 + (data["left"][i] - 20) / scale
            top = clip.y0 + data["top"][i] / scale
            words.append((text, (left, top, left + data["width"][i] / scale, top + data["height"][i] / scale), conf))
        if not words:
            continue
        line = _build_line(sorted(words, key=lambda w: w[1][0]))
        line.text = _fix_pipe_i(line.text)
        if single:
            # A region one letter wide holds one character; Tesseract's extras
            # around a lone symbol (a trailing comma, a quote) are noise.
            alnum = [ch for ch in line.text if ch.isalnum()]
            line.text = alnum[0] if alnum else line.text[:1]
        pixels = np.asarray(img)
        dx = clip.x0 - 20 / scale
        local = OcrLine(
            text=line.text,
            bbox=(line.bbox[0] - dx, line.bbox[1] - clip.y0, line.bbox[2] - dx, line.bbox[3] - clip.y0),
            baseline=line.baseline, size=line.size, conf=line.conf,
        )
        line.color, line.bold = _style_from_pixels(pixels, local, scale)
        line.narrow = _is_narrow(line)
        lines.append(line)
    return lines, {"regions": len(rects), "lines": len(lines)}


def _fix_pipe_i(text: str) -> str:
    """Tesseract reads a sans-serif capital I standing alone as "|"."""
    return re.sub(r"(?<!\S)\|(?!\S)", "I", text)


def _style_from_pixels(pixels: np.ndarray, line: OcrLine, scale: float) -> tuple[str, bool]:
    """Read a line's text colour and weight back off the rendered page.

    Colour: the background is the median of a ring just outside the line's box;
    ink is whatever differs strongly from it, and the strongest half of that ink
    (stroke cores, not anti-aliased edges) gives the colour. This is what keeps
    white-on-blue headings white and red warnings red.

    Weight: stroke thickness relative to font size, via a distance transform.
    Arial regular sits around 0.10-0.13 em, bold around 0.14-0.17 em.
    """
    x0, y0, x1, y1 = (int(round(v * scale)) for v in line.bbox)
    pad = 3
    h, w = pixels.shape[:2]
    crop = pixels[max(0, y0 - pad):min(h, y1 + pad), max(0, x0 - pad):min(w, x1 + pad)].astype(np.int32)
    if crop.shape[0] < 3 or crop.shape[1] < 3:
        return "#000000", False
    ring = np.concatenate([crop[0], crop[-1], crop[:, 0], crop[:, -1]])
    bg = np.median(ring, axis=0)
    dist = np.sqrt(((crop - bg) ** 2).sum(axis=-1))
    mask = dist > INK_DIST
    if mask.sum() < 10:
        return "#000000", False

    core = crop[dist >= np.percentile(dist[mask], 50)]
    r, g, b = (int(v) for v in np.median(core, axis=0))
    if max(r, g, b) < 60:
        r = g = b = 0
    elif min(r, g, b) > 200:
        r = g = b = 255
    color = f"#{r:02x}{g:02x}{b:02x}"

    dt = cv2.distanceTransform(mask.astype(np.uint8), cv2.DIST_L2, 3)
    stroke = 2.0 * float(np.percentile(dt[mask], 90))
    bold = stroke / (line.size * scale) >= BOLD_STROKE_RATIO
    return color, bold


_HELV = {False: pymupdf.Font("helv"), True: pymupdf.Font("hebo")}


def _is_narrow(line: OcrLine) -> bool:
    """A condensed face (Arial Narrow) is ~18% narrower than Arial at the same
    size. Without detecting it, the width fit can only squeeze letter-spacing,
    which crushes the words together."""
    natural = _HELV[line.bold].text_length(line.text, fontsize=line.size)
    if natural <= 0:
        return False
    ratio = (line.bbox[2] - line.bbox[0]) / natural
    return NARROW_WIDTH_RANGE[0] <= ratio <= NARROW_WIDTH_RANGE[1]


def _split_at_gaps(words: list, gap_em: float = 1.8) -> list[list]:
    if not words:
        return []
    em = max(1.0, _estimate_size(words))
    runs: list[list] = [[words[0]]]
    for prev, cur in zip(words, words[1:]):
        if cur[1][0] - prev[1][2] > gap_em * em:
            runs.append([cur])
        else:
            runs[-1].append(cur)
    return runs


def _build_line(words: list) -> OcrLine:
    """One run of words on one visual line."""
    x0 = min(w[1][0] for w in words)
    y0 = min(w[1][1] for w in words)
    x1 = max(w[1][2] for w in words)
    y1 = max(w[1][3] for w in words)
    size = _estimate_size(words)

    # A word without descenders sits on the baseline, so its ink bottom *is* the
    # baseline. Using only those avoids the classic OCR baseline error.
    clean = [w for w in words if not _has_descender(w[0])]
    baseline = sum(w[1][3] for w in clean) / len(clean) if clean else y1 - 0.212 * size

    return OcrLine(
        text=" ".join(w[0] for w in words),
        bbox=(x0, y0, x1, y1),
        baseline=baseline,
        size=size,
        conf=sum(w[2] for w in words) / len(words),
        words=[(w[0], w[1]) for w in words],
    )


def reconcile(
    ocr_lines: list[OcrLine], text_bboxes: list[tuple[float, float, float, float]]
) -> tuple[list[OcrLine], int]:
    """Drop OCR output that the PDF's own text layer already covers exactly.

    Returns the surviving lines and how many were discarded as duplicates.
    """
    boxes = [
        pymupdf.Rect(b[0] - COVER_TOL, b[1] - COVER_TOL, b[2] + COVER_TOL, b[3] + COVER_TOL)
        for b in text_bboxes
    ]
    def covered(bbox) -> bool:
        cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
        return any(b.x0 <= cx <= b.x1 and b.y0 <= cy <= b.y1 for b in boxes)

    kept: list[OcrLine] = []
    dropped = 0
    for line in ocr_lines:
        # Per word, not per line: a heading can be half real text and half
        # outlines ("BORANG ... JAM /" + an outlined italic tail), and dropping
        # the whole OCR line would lose the tail.
        words = [w for w in line.words if not covered(w[1])]
        if not words:
            dropped += 1
            continue
        if len(words) < len(line.words):
            if not any(ch.isalnum() for w in words for ch in w[0]):
                dropped += 1
                continue
            rebuilt = _build_line([(t, b, line.conf) for t, b in words])
            rebuilt.color, rebuilt.bold = line.color, line.bold
            rebuilt.narrow = _is_narrow(rebuilt)
            line = rebuilt
        kept.append(line)
    return kept, dropped
