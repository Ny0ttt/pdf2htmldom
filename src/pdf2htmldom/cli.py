"""Command line entry point.

    python -m pdf2htmldom.cli build fixtures/table_basic.pdf -o out/table_basic
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .pipeline import build


def _pages(value: str | None) -> list[int] | None:
    if not value:
        return None
    out: list[int] = []
    for part in value.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pdf2htmldom", description="PDF -> native HTML reconstruction")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="reconstruct a PDF into an HTML package")
    b.add_argument("pdf")
    b.add_argument("-o", "--out", required=True)
    b.add_argument("--pages", help="e.g. 1,3-5 (default: all)")
    b.add_argument("--scale", type=float, default=1.5, help="device scale factor for validation renders")
    b.add_argument("--max-iters", type=int, default=3, help="refinement loop cap")
    b.add_argument("--single-file", action="store_true", help="also emit standalone.html")
    b.add_argument("--no-ocr", action="store_true", help="disable OCR (faster, text layer only)")
    b.add_argument("--ocr-dpi", type=int, default=300)
    b.add_argument(
        "--fit-ocr-size",
        action="store_true",
        help="also fit OCR line width by scaling type (experimental: the size change "
             "moves the baseline, so it does not yet converge reliably)",
    )
    b.add_argument("--json", action="store_true", help="print the report as JSON")

    sub.add_parser(
        "doctor",
        help="check this machine's setup: Python, packages, Chromium, Tesseract, fonts",
    )

    args = ap.parse_args(argv)

    if args.cmd == "doctor":
        from .doctor import report, run_checks

        checks = run_checks()
        print(report(checks))
        return 1 if any(c.status == "fail" for c in checks) else 0

    if args.cmd == "build":
        t0 = time.time()
        result = build(
            args.pdf,
            args.out,
            device_scale=args.scale,
            max_iters=args.max_iters,
            page_numbers=_pages(args.pages),
            single_file=args.single_file,
            skip_ocr=args.no_ocr,
            ocr_dpi=args.ocr_dpi,
            fit_ocr_size=args.fit_ocr_size,
        )
        elapsed = time.time() - t0
        report = json.loads((Path(args.out) / "report.json").read_text(encoding="utf-8"))
        if args.json:
            print(json.dumps(report, indent=2))
            return 0

        print(f"\n  {args.pdf} -> {args.out}   ({elapsed:.1f}s total)")
        for p in report["pages"]:
            print(
                f"  page {p['page']}: ssim={p['ssim']:.4f} ink_recall={p['ink_recall']:.4f} "
                f"ink_precision={p['ink_precision']:.4f} "
                f"line_err mean={p['line_error_mean_pt']:.2f}pt max={p['line_error_max_pt']:.2f}pt"
            )
        tp = report["text_preservation"]
        print(f"\n  text preserved: {tp['preserved_pct']:.3f}%  ({tp['matched_chars']}/{tp['pdf_chars']} chars)")
        if tp["missing_samples"]:
            print(f"  missing samples: {tp['missing_samples']}")
        print("\n  acceptance targets:")
        for name, a in report["acceptance"].items():
            mark = "PASS" if a["pass"] else "MISS"
            print(f"    [{mark}] {name:<24} value={a['value']}  target={a['target']}")
        if report["warnings"]:
            print("\n  warnings:")
            for w in report["warnings"][:10]:
                print(f"    - {w}")
        print()
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
