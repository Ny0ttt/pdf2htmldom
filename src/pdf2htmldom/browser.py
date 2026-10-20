"""Stage 8: browser rendering and *geometry measurement*.

The important idea here is that the browser is not only a screenshot machine.
Playwright can report the true rendered box of every element, and -- via a
zero-size inline-block probe -- the true rendered *baseline* of every line of
text. That turns refinement from a pixel-guessing exercise into an exact,
deterministic correction: measure the error, subtract it, verify.

The browser is launched once and reused. On a small VM, a cold Chromium start
alone costs more than the whole per-page budget.
"""

from __future__ import annotations

import json
from pathlib import Path

# CSS px are 1/96in, PDF points are 1/72in.
PT_PER_CSSPX = 0.75

MEASURE_JS = r"""
() => {
  const PT = 72 / 96;                      // CSS px -> pt
  const out = {};
  const els = document.querySelectorAll('[data-el]');
  for (const el of els) {
    // getBoundingClientRect is viewport-relative, but every coordinate in the IR
    // is page-relative. The page container is centred by CSS, so its own origin
    // must be subtracted or every measurement is off by the page offset.
    const pageEl = el.closest('.page');
    const origin = pageEl
      ? pageEl.getBoundingClientRect()
      : { left: 0, top: 0 };
    const r = el.getBoundingClientRect();
    const rec = {
      x: (r.left - origin.left) * PT,
      y: (r.top - origin.top) * PT,
      w: r.width * PT,
      h: r.height * PT,
      cls: el.className || '',
    };
    if (el.dataset.baseline !== undefined) {
      // The element's own box is the full block width, so the *text* extent has
      // to come from a Range over the contents.
      const range = document.createRange();
      range.selectNodeContents(el);
      const tr = range.getBoundingClientRect();
      if (tr.width > 0 || tr.height > 0) {
        rec.w = tr.width * PT;
        rec.x = (tr.left - origin.left) * PT;
      }
      // A zero-size inline-block sits exactly on the baseline of the last line.
      const probe = document.createElement('span');
      probe.style.cssText =
        'display:inline-block;width:0;height:0;vertical-align:baseline;padding:0;margin:0';
      el.appendChild(probe);
      const pr = probe.getBoundingClientRect();
      rec.baseline = (pr.bottom - origin.top) * PT;
      rec.text_left = (tr.left - origin.left) * PT;
      probe.remove();
      rec.src = el.dataset.src || 'pdf';
      rec.target_baseline = parseFloat(el.dataset.baseline);
      rec.target_x = parseFloat(el.dataset.x);
      rec.target_w = parseFloat(el.dataset.w);
    }
    out[el.dataset.el] = rec;
  }
  return out;
}
"""


class BrowserRenderer:
    """Warm Chromium. Launch once, reuse for measurement and screenshots."""

    def __init__(self, device_scale: float = 1.5) -> None:
        self.device_scale = device_scale
        self._pw = None
        self._browser = None
        self._ctx = None
        self._page = None

    def __enter__(self) -> "BrowserRenderer":
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",  # /dev/shm is tiny in containers
                    "--disable-lcd-text",
                    "--force-color-profile=srgb",
                    "--font-render-hinting=none",
                    "--hide-scrollbars",
                ]
            )
            self._ctx = self._browser.new_context(
                device_scale_factor=self.device_scale,
                viewport={"width": 1000, "height": 1400},
            )
            self._page = self._ctx.new_page()
        except BaseException:
            # __exit__ never runs when __enter__ raises. Without this, the
            # half-started Playwright loop stays alive and every later launch
            # fails with a misleading "Sync API inside the asyncio loop" error.
            self.__exit__()
            raise
        return self

    def __exit__(self, *exc) -> None:
        for closer in (self._ctx, self._browser):
            try:
                closer and closer.close()
            except Exception:
                pass
        try:
            self._pw and self._pw.stop()
        except Exception:
            pass

    def load(self, html_path: str | Path) -> None:
        self._page.goto(Path(html_path).resolve().as_uri(), wait_until="load")
        self._page.wait_for_timeout(120)
        try:
            self._page.evaluate("() => document.fonts.ready")
        except Exception:
            pass

    def measure(self) -> dict:
        return self._page.evaluate(MEASURE_JS)

    def apply_corrections(self, corrections: dict[str, dict]) -> None:
        """Apply per-line baseline and width corrections in the live DOM."""
        self._page.evaluate(
            """(corr) => {
                for (const [id, c] of Object.entries(corr)) {
                  const el = document.querySelector(`[data-el="${id}"]`);
                  if (!el) continue;
                  if (c.dy) {
                    const cur = parseFloat(el.style.top) || 0;
                    el.style.top = (cur + c.dy).toFixed(3) + 'pt';
                  }
                  if (c.ls) el.style.letterSpacing = c.ls.toFixed(3) + 'pt';
                  if (c.fs) {
                    const cur = parseFloat(el.style.fontSize) || 0;
                    if (cur > 0) el.style.fontSize = (cur * c.fs).toFixed(3) + 'pt';
                  }
                }
            }""",
            corrections,
        )

    def screenshot_element(self, selector: str, out_png: str | Path) -> tuple[int, int]:
        el = self._page.query_selector(selector)
        if el is None:
            raise RuntimeError(f"selector not found: {selector}")
        el.screenshot(path=str(out_png))
        box = el.bounding_box()
        return int(round(box["width"] * self.device_scale)), int(round(box["height"] * self.device_scale))

    def save_dom(self, out_html: str | Path) -> None:
        html = self._page.evaluate("() => '<!DOCTYPE html>\\n' + document.documentElement.outerHTML")
        Path(out_html).write_text(html, encoding="utf-8")


def compute_corrections(
    measurements: dict,
    *,
    max_letter_spacing: float = 0.6,
    max_baseline_dy: float = 40.0,
    size_fit_threshold: float = 0.03,
    size_fit_bounds: tuple[float, float] = (0.85, 1.15),
    levers: tuple[str, ...] = ("dy", "ls", "fs"),
) -> dict[str, dict]:
    """Turn measured geometry into exact deltas.

    Three levers, chosen by the size of the error:

    * ``dy`` -- baseline error is a pure translation, so one pass removes it.
    * ``fs`` -- a *large* width error means the font size is wrong, not the
      tracking. Width scales linearly with font size, so the fix is the ratio.
      This is the lever that makes OCR-derived lines converge, since their size
      can only be estimated from ink height.
    * ``ls`` -- a *small* width error is tracking. CSS adds letter-spacing after
      every character, so the value needed is ``error / nchars``.
    """
    corrections: dict[str, dict] = {}
    for el_id, m in measurements.items():
        if "target_baseline" not in m:
            continue
        dy = m["target_baseline"] - m["baseline"]
        rec: dict = {}
        if "dy" not in levers and "ls" not in levers and "fs" not in levers:
            continue
        # A huge delta means the measurement itself is wrong (wrong coordinate
        # space, element not rendered), not that the layout is off. Correcting it
        # would silently produce garbage, so refuse and let it show up as an error.
        if abs(dy) > max_baseline_dy:
            continue
        if "dy" in levers and abs(dy) > 0.02:
            rec["dy"] = dy

        target_w = m.get("target_w", 0.0)
        if target_w > 0 and m["w"] > 1:
            err = target_w - m["w"]
            # Only fit size when the target is trustworthy. An OCR line's bbox
            # can span words Tesseract wrongly grouped, and fitting to that would
            # blow the type up or shrink it away; the bound keeps a bad target
            # from doing damage while still absorbing estimator error.
            # Only OCR lines are size-fitted. A PDF text-layer line has an exact
            # font size already; scaling it to match a bbox would be fitting
            # measurement noise and it measurably destroys ink coverage.
            if (
                "fs" in levers
                and m.get("src") == "ocr"
                and abs(err) / target_w > size_fit_threshold
                and target_w >= 20
            ):
                ratio = max(size_fit_bounds[0], min(size_fit_bounds[1], target_w / m["w"]))
                if abs(ratio - 1.0) > 0.005:
                    rec["fs"] = ratio
            elif "ls" in levers and abs(err) > 0.5:
                # Approximate character count from the width and typical advance.
                nchars = max(1, int(round(m["w"] / 5.0)))
                ls = max(-max_letter_spacing, min(max_letter_spacing, err / nchars))
                if abs(ls) > 0.01:
                    rec["ls"] = ls
        if rec:
            corrections[el_id] = rec
    return corrections


def write_report(path: str | Path, payload: dict) -> None:
    Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
