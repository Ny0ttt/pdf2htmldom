"""Environment checks: is this machine set up to convert PDFs well?

Most of what pdf2htmldom depends on fails quietly rather than loudly: without
Tesseract, OCR is skipped; without a font, outlined text in that font falls back
to OCR. The conversion still finishes, just worse. ``pdf2htmldom doctor``
makes those gaps visible before they show up as a bad result.

Statuses:
  ok    -- fine
  warn  -- works, with reduced quality or untested
  fail  -- conversions will not run
"""

from __future__ import annotations

import importlib
import platform
import sys
from dataclasses import dataclass

VERIFIED_PYTHON = (3, 12)
MIN_PYTHON = (3, 10)


@dataclass
class Check:
    name: str
    status: str  # ok | warn | fail
    detail: str
    fix: str = ""


def _python() -> Check:
    v = sys.version_info
    found = f"Python {v.major}.{v.minor}.{v.micro} ({platform.system()} {platform.machine()})"
    if (v.major, v.minor) < MIN_PYTHON:
        return Check("python", "fail", found, f"Install Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer.")
    if (v.major, v.minor) != VERIFIED_PYTHON:
        return Check("python", "warn", f"{found}; the pinned versions in constraints.txt are verified on "
                     f"{VERIFIED_PYTHON[0]}.{VERIFIED_PYTHON[1]}")
    return Check("python", "ok", found)


def _packages() -> list[Check]:
    checks = []
    for module, dist in (("pymupdf", "pymupdf"), ("playwright", "playwright"), ("cv2", "opencv-python-headless"),
                         ("pydantic", "pydantic"), ("fontTools", "fonttools"), ("pytesseract", "pytesseract")):
        try:
            importlib.import_module(module)
            from importlib.metadata import version

            ver = version(dist)
        except Exception as exc:  # missing or broken install
            checks.append(Check(dist, "fail", f"cannot import {module}: {exc}",
                                'pip install -e ".[ui,dev]" -c constraints.txt'))
            continue
        if dist == "pymupdf" and int(ver.split(".")[0]) >= 2:
            checks.append(Check(dist, "warn", f"{ver}: PyMuPDF 2.x is not validated with pdf2htmldom",
                                'pip install "pymupdf<2"'))
        else:
            checks.append(Check(dist, "ok", ver))
    return checks


def _chromium() -> Check:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            ver = browser.version
            browser.close()
        return Check("chromium", "ok", f"launches (version {ver})")
    except Exception as exc:
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        fix = "python -m playwright install chromium"
        if sys.platform.startswith("linux"):
            fix = "python -m playwright install --with-deps chromium"
        return Check("chromium", "fail", f"cannot launch: {first}", fix)


def _tesseract() -> Check:
    try:
        from .ocr import _pytesseract

        tess = _pytesseract()
        ver = tess.get_tesseract_version()
        langs = sorted(tess.get_languages(config="") or [])
        return Check("tesseract", "ok", f"{ver}; languages: {', '.join(langs) or 'none'}")
    except Exception:
        return Check(
            "tesseract", "warn",
            "not found: scanned pages and unmatched outlined text will not be OCR'd",
            "Install Tesseract (Windows: winget install UB-Mannheim.TesseractOCR), "
            "or set TESSERACT_CMD to the executable.",
        )


def _fonts() -> list[Check]:
    from .glyphs import DEFAULT_FAMILIES, _family_files, _family_key

    checks = []
    for family in DEFAULT_FAMILIES:
        _display, styles = _family_files(_family_key(family))
        names = {(False, False): "regular", (True, False): "bold", (False, True): "italic", (True, True): "bold italic"}
        found = [names[s] for s in names if s in dict(styles)]
        if not styles:
            checks.append(Check(
                f"font: {family}", "warn",
                f"not installed: outlined text set in {family} falls back to OCR, "
                f"and HTML in {family} renders with a substitute",
                f"Install {family} if its licence allows.",
            ))
        elif len(styles) < 4:
            checks.append(Check(f"font: {family}", "warn", f"partial: {', '.join(found)} only"))
        else:
            checks.append(Check(f"font: {family}", "ok", ", ".join(found)))
    return checks


def run_checks() -> list[Check]:
    checks = [_python(), *_packages()]
    if all(c.status != "fail" for c in checks):  # the rest needs the packages
        checks += [_chromium(), _tesseract(), *_fonts()]
    return checks


def report(checks: list[Check]) -> str:
    label = {"ok": "  OK ", "warn": " WARN", "fail": " FAIL"}
    width = max(len(c.name) for c in checks)
    lines = []
    for c in checks:
        lines.append(f"[{label[c.status]}] {c.name:<{width}}  {c.detail}")
        if c.fix and c.status != "ok":
            lines.append(f"        {'':<{width}}  fix: {c.fix}")
    fails = sum(c.status == "fail" for c in checks)
    warns = sum(c.status == "warn" for c in checks)
    lines.append("")
    lines.append(
        "Ready." if not fails and not warns
        else f"{fails} problem(s) to fix, {warns} warning(s)." if fails
        else f"Ready, with {warns} warning(s): conversions run, some with reduced quality."
    )
    return "\n".join(lines)
