"""Stage 8 (evaluation half): measured, not vibes.

Three independent signals, because each one lies in a different way:

* ``ssim`` -- a coarse global gate. It is dominated by large uniform areas, so a
  mostly-white page scores well even when every word is misplaced. Never trust
  it alone.
* ``ink recall / precision`` -- the honest content check. Recall catches *dropped
  content* (the failure SSIM hides), precision catches invented or displaced ink.
* ``line position error`` -- the most meaningful number for a text document, and
  the one the acceptance targets are written against.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import cv2
import numpy as np

INK_THRESHOLD = 200  # grayscale value below which a pixel counts as ink
DILATE_PX = 2  # ~1pt of tolerance at a 2x render


@dataclass
class Comparison:
    ssim: float
    ink_recall: float
    ink_precision: float
    ref_ink_px: int
    gen_ink_px: int
    line_errors: list[dict] = field(default_factory=list)
    width_issues: list[dict] = field(default_factory=list)

    @property
    def mean_line_error(self) -> float:
        return float(np.mean([e["err"] for e in self.line_errors])) if self.line_errors else 0.0

    @property
    def max_line_error(self) -> float:
        return float(np.max([e["err"] for e in self.line_errors])) if self.line_errors else 0.0

    def summary(self) -> dict:
        return {
            "ssim": round(self.ssim, 4),
            "ink_recall": round(self.ink_recall, 4),
            "ink_precision": round(self.ink_precision, 4),
            "ref_ink_px": self.ref_ink_px,
            "gen_ink_px": self.gen_ink_px,
            "line_error_mean_pt": round(self.mean_line_error, 3),
            "line_error_max_pt": round(self.max_line_error, 3),
            "lines_measured": len(self.line_errors),
            "width_issues": len(self.width_issues),
        }


def _gray(img: np.ndarray) -> np.ndarray:
    if img.ndim == 3:
        img = cv2.cvtColor(img[:, :, :3], cv2.COLOR_BGR2GRAY)
    return img


def ssim(a: np.ndarray, b: np.ndarray) -> float:
    """Standard SSIM (Wang et al.) with an 11x11 Gaussian window, computed in
    NumPy/OpenCV rather than pulling in scikit-image."""
    a = _gray(a).astype(np.float64)
    b = _gray(b).astype(np.float64)
    c1 = (0.01 * 255) ** 2
    c2 = (0.03 * 255) ** 2
    k, sigma = (11, 11), 1.5
    mu_a = cv2.GaussianBlur(a, k, sigma)
    mu_b = cv2.GaussianBlur(b, k, sigma)
    mu_aa, mu_bb, mu_ab = mu_a * mu_a, mu_b * mu_b, mu_a * mu_b
    sig_aa = cv2.GaussianBlur(a * a, k, sigma) - mu_aa
    sig_bb = cv2.GaussianBlur(b * b, k, sigma) - mu_bb
    sig_ab = cv2.GaussianBlur(a * b, k, sigma) - mu_ab
    num = (2 * mu_ab + c1) * (2 * sig_ab + c2)
    den = (mu_aa + mu_bb + c1) * (sig_aa + sig_bb + c2)
    return float(np.mean(num / den))


def ink_masks(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    ga, gb = _gray(a), _gray(b)
    return ga < INK_THRESHOLD, gb < INK_THRESHOLD


def ink_scores(ref: np.ndarray, gen: np.ndarray) -> tuple[float, float]:
    """Recall/precision of generated ink against the reference, with a small
    dilation so that sub-pixel and anti-aliasing differences do not count as
    errors.

    The dilation is applied to the mask being *searched*, and the intersection is
    taken with the mask being *judged*, so both scores are true fractions in
    0..1. Recall is the number that matters: it is what catches lost text.
    """
    ref_ink, gen_ink = ink_masks(ref, gen)
    kernel = np.ones((DILATE_PX * 2 + 1, DILATE_PX * 2 + 1), np.uint8)
    ref_d = cv2.dilate(ref_ink.astype(np.uint8), kernel).astype(bool)
    gen_d = cv2.dilate(gen_ink.astype(np.uint8), kernel).astype(bool)
    ref_n = int(ref_ink.sum())
    gen_n = int(gen_ink.sum())
    recall = float((ref_ink & gen_d).sum() / ref_n) if ref_n else 1.0
    precision = float((gen_ink & ref_d).sum() / gen_n) if gen_n else 0.0
    return recall, precision


def diff_image(ref: np.ndarray, gen: np.ndarray) -> np.ndarray:
    """Visual diff: red = only in the original, blue = only in the generated."""
    ref_ink, gen_ink = ink_masks(ref, gen)
    h = min(ref.shape[0], gen.shape[0])
    w = min(ref.shape[1], gen.shape[1])
    canvas = np.full((h, w, 3), 255, np.uint8)
    canvas[ref_ink[:h, :w]] = (60, 60, 220)  # red-ish (BGR)
    canvas[gen_ink[:h, :w]] = (220, 120, 60)  # blue-ish
    both = ref_ink[:h, :w] & gen_ink[:h, :w]
    canvas[both] = (40, 40, 40)
    return canvas


def compare(
    ref_img: np.ndarray,
    gen_img: np.ndarray,
    measurements: dict,
    *,
    width_tol_pt: float = 1.5,
) -> Comparison:
    h = min(ref_img.shape[0], gen_img.shape[0])
    w = min(ref_img.shape[1], gen_img.shape[1])
    ref_c, gen_c = ref_img[:h, :w], gen_img[:h, :w]

    recall, precision = ink_scores(ref_c, gen_c)
    ref_ink, gen_ink = ink_masks(ref_c, gen_c)

    line_errors: list[dict] = []
    width_issues: list[dict] = []
    for el_id, m in measurements.items():
        if "target_baseline" not in m:
            continue
        dx = m.get("text_left", m["x"]) - m["target_x"]
        dy = m["baseline"] - m["target_baseline"]
        err = math.hypot(dx, dy)
        line_errors.append(
            {"id": el_id, "dx": round(dx, 3), "dy": round(dy, 3), "err": round(err, 3)}
        )
        if "target_w" in m and m["target_w"] > 0:
            dw = m["w"] - m["target_w"]
            if abs(dw) > width_tol_pt:
                width_issues.append(
                    {
                        "id": el_id,
                        "target_w": round(m["target_w"], 2),
                        "rendered_w": round(m["w"], 2),
                        "delta_pt": round(dw, 2),
                    }
                )

    return Comparison(
        ssim=ssim(ref_c, gen_c),
        ink_recall=recall,
        ink_precision=precision,
        ref_ink_px=int(ref_ink.sum()),
        gen_ink_px=int(gen_ink.sum()),
        line_errors=sorted(line_errors, key=lambda e: -e["err"]),
        width_issues=width_issues,
    )
