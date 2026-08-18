"""Regression + quality-comparison harness for the segmentation stage
(modules/image_pipeline/detector.py).

Two tiers, because they answer different questions:

  Tier 1 — mask-repair unit checks. Deterministic synthetic masks, no
  model and no photos needed, so this runs anywhere (including a fresh
  clone, where data/ is gitignored). Covers the scenarios that motivated
  the repair logic: a thin handle broken off the body, a detached lid, a
  distant speck that must still be discarded, speckle holes inside a
  white surface, and a genuine see-through opening that must survive.

  Tier 2 — before/after quality comparison on REAL photos, skipped
  automatically when data/uploads is absent. "Before" is not a copy of
  the old code: LEGACY_CONFIG below expresses the previous behaviour
  purely through PipelineConfig (no post-processing, fixed threshold,
  largest-component-only, no closing, no hole filling), so the two runs
  differ by configuration alone.

Usage:
    python scripts/verify_segmentation.py            # tier 1, plus tier 2 if photos exist
    python scripts/verify_segmentation.py --units    # tier 1 only (fast, no model load)
"""
import io
import sys
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.image_pipeline import detector  # noqa: E402
from modules.image_pipeline.config import PipelineConfig  # noqa: E402

CURRENT = PipelineConfig()

# The pre-repair behaviour, expressed only as configuration.
LEGACY_CONFIG = replace(
    CURRENT,
    post_process_mask=False,
    enable_alpha_matting=False,
    adaptive_segmentation=False,
    mask_closing_kernel=0,
    secondary_component_ratio=1.0,   # largest component only
    component_proximity_px=0,
    max_hole_fraction=0.0,           # no hole repair
)

PASS, FAIL = "PASS", "FAIL"
_results = []


def check(name, condition, detail=""):
    status = PASS if condition else FAIL
    _results.append((status, name, detail))
    print(f"  [{status}] {name}" + (f"  — {detail}" if detail else ""))
    return condition


# --------------------------------------------------------------- tier 1 --

def _canvas(h=400, w=400):
    return np.zeros((h, w), dtype=bool)


def test_thin_handle_reconnects():
    """A body and a head joined by a handle that segmentation broke: the
    head must survive. Under the old largest-component-only rule it did
    not."""
    m = _canvas()
    m[250:380, 150:250] = True          # body (largest)
    m[60:130, 170:230] = True           # head
    m[130:250, 196:204] = True          # thin handle joining them...
    m[180:190, 196:204] = False         # ...broken by a 10px gap

    legacy, legacy_dropped = detector.significant_components_mask(m, 1.0, 0)
    repaired_connectivity = detector.close_mask_gaps(m, CURRENT.mask_closing_kernel)
    repaired, repaired_dropped = detector.significant_components_mask(
        repaired_connectivity, CURRENT.secondary_component_ratio, CURRENT.component_proximity_px
    )
    repaired = repaired & m

    head = np.zeros_like(m)
    head[60:130, 170:230] = True
    legacy_keeps_head = bool((legacy & head).sum())
    repaired_keeps_head = bool((repaired & head).sum())

    check("thin handle: old rule discarded the head", not legacy_keeps_head,
          f"legacy dropped {legacy_dropped} component(s)")
    check("thin handle: repair keeps the head", repaired_keeps_head,
          f"{int((repaired & head).sum())} head px retained")
    check("thin handle: repair adds no phantom pixels outside detection",
          bool((repaired & ~m).sum() == 0))


def test_detached_part_kept_by_proximity():
    """A small lid sitting just above the body — too small for the size
    rule, close enough for the proximity rule."""
    m = _canvas()
    m[200:380, 150:250] = True          # body
    m[176:192, 180:220] = True          # small lid, ~8px above the body

    kept, dropped = detector.significant_components_mask(
        m, CURRENT.secondary_component_ratio, CURRENT.component_proximity_px
    )
    lid = np.zeros_like(m); lid[176:192, 180:220] = True
    check("detached lid is kept via proximity",
          int((kept & lid).sum()) == int(lid.sum()),
          f"{int((kept & lid).sum())}/{int(lid.sum())} lid px kept, dropped {dropped} component(s)")


def test_distant_speck_still_dropped():
    """The reason the largest-component rule existed: a stray blob far
    from the product must not survive and inflate the crop box."""
    m = _canvas()
    m[200:380, 150:250] = True          # body
    m[10:18, 380:392] = True            # distant speck

    kept, dropped = detector.significant_components_mask(
        m, CURRENT.secondary_component_ratio, CURRENT.component_proximity_px
    )
    speck = np.zeros_like(m); speck[10:18, 380:392] = True
    check("distant speck is still discarded", not bool((kept & speck).any()),
          f"dropped {dropped} component(s)")


def test_speckle_holes_filled_openings_kept():
    """White/mirrored surfaces come back with speckle holes punched
    through them; a picture frame's opening is also a hole. Only the
    former may be filled."""
    m = _canvas(600, 600)
    m[100:500, 100:500] = True
    m[150:450, 150:450] = False         # genuine opening (~56% of the ring)
    ring_area = int(m.sum())
    m[120:126, 120:126] = False         # speckle hole (tiny)
    m[470:474, 300:304] = False         # speckle hole (tiny)

    filled, n = detector.fill_small_holes(m, CURRENT.max_hole_fraction)
    opening = np.zeros_like(m); opening[150:450, 150:450] = True
    check("speckle holes are filled", n == 2, f"filled {n} hole(s)")
    check("genuine opening is preserved", not bool((filled & opening).any()),
          f"opening is {300*300} px vs the {int(CURRENT.max_hole_fraction*ring_area)} px fill limit")


def test_measure_separation_flags_low_contrast():
    """White-on-white must read as low contrast; dark-on-white must not."""
    white_on_white = np.full((400, 400, 3), 255, np.uint8)
    white_on_white[120:280, 120:280] = 246          # barely-there white product
    dark_on_white = np.full((400, 400, 3), 255, np.uint8)
    dark_on_white[120:280, 120:280] = 40            # dark product
    grey_bg = np.full((400, 400, 3), 200, np.uint8)
    grey_bg[120:280, 120:280] = 250                 # white product on grey

    s_white = detector.measure_separation(Image.fromarray(white_on_white))["separation"]
    s_dark = detector.measure_separation(Image.fromarray(dark_on_white))["separation"]
    s_grey = detector.measure_separation(Image.fromarray(grey_bg))["separation"]

    check("white-on-white takes the safety net",
          s_white < CURRENT.low_contrast_separation, f"separation={s_white:.1f}")
    check("dark-on-white does not need the safety net",
          s_dark >= CURRENT.low_contrast_separation, f"separation={s_dark:.1f}")
    # White-on-grey is genuinely intermediate, and which side of the
    # threshold it lands on is a tuning choice rather than a correctness
    # property (the low-contrast branch is measured harmless either way).
    # What must always hold is the ordering.
    check("separation ranks white-on-white < white-on-grey < dark-on-white",
          s_white < s_grey < s_dark,
          f"{s_white:.1f} < {s_grey:.1f} < {s_dark:.1f}")


def test_legacy_config_reproduces_old_behaviour():
    """The A/B comparison is only meaningful if LEGACY_CONFIG really is
    the old algorithm."""
    m = _canvas()
    m[200:380, 150:250] = True
    m[176:192, 180:220] = True          # nearby lid the old rule dropped

    legacy, _ = detector.significant_components_mask(
        m, LEGACY_CONFIG.secondary_component_ratio, LEGACY_CONFIG.component_proximity_px
    )
    lid = np.zeros_like(m); lid[176:192, 180:220] = True
    unchanged_close = detector.close_mask_gaps(m, LEGACY_CONFIG.mask_closing_kernel)
    _, n_filled = detector.fill_small_holes(m, LEGACY_CONFIG.max_hole_fraction)

    check("legacy config keeps only the largest component", not bool((legacy & lid).any()))
    check("legacy config performs no closing", bool((unchanged_close == m).all()))
    check("legacy config performs no hole filling", n_filled == 0)


# --------------------------------------------------------------- tier 2 --

REAL_SCENARIOS = [
    ("white product / white bg",      "data/uploads/577-D3/photo_1.jpg"),
    ("white product / white bg",      "data/uploads/409-D3/photo_1.jpg"),
    ("glossy chrome / white bg",      "data/uploads/781-B4/photo_1.jpg"),
    ("glossy pouch / white bg",       "data/uploads/1143-A1/photo_1.jpg"),
    ("thin handle / white bg",        "data/uploads/1006-J2/photo_1.jpg"),
    ("dark product / white bg",       "data/uploads/1064-A3/photo_1.jpg"),
    ("dark product / white bg",       "data/uploads/1290-C3/photo_1.jpg"),
]


def _edge_quality(alpha):
    opaque = alpha > 10
    total = int(opaque.sum())
    if total == 0:
        return 100
    soft = int((opaque & (alpha < 245)).sum())
    return max(0, min(100, int(100 - soft / total * 400)))


def _holes(mask):
    from scipy import ndimage
    filled = ndimage.binary_fill_holes(mask)
    _, n = ndimage.label(filled & ~mask)
    return n


def run_real_photo_comparison(root: Path):
    available = [(label, root / p) for label, p in REAL_SCENARIOS if (root / p).exists()]
    if not available:
        print("\nTier 2 skipped: no photos under data/uploads (expected in a fresh clone).")
        return

    print(f"\n--- Tier 2: before/after on {len(available)} real photos ---")
    print(f"{'scenario':<26} {'sku':<9} {'edge B>A':>10} {'holes B>A':>11} "
          f"{'area delta':>11} {'sep':>6} {'lowC':>5} {'secs':>6}")
    print("-" * 92)

    regressions = []
    for label, path in available:
        image = Image.open(io.BytesIO(path.read_bytes()))
        image = image.convert("RGB")

        legacy_rgba, legacy_mask, _, _ = detector.detect(image, use_gpu=False, config=LEGACY_CONFIG)
        legacy_alpha = np.array(legacy_rgba.getchannel("A"))

        t0 = time.perf_counter()
        new_rgba, new_mask, _, info = detector.detect(image, use_gpu=False, config=CURRENT)
        secs = time.perf_counter() - t0
        new_alpha = np.array(new_rgba.getchannel("A"))

        e_before, e_after = _edge_quality(legacy_alpha), _edge_quality(new_alpha)
        h_before, h_after = _holes(legacy_mask), _holes(new_mask)
        area_delta = int(new_mask.sum()) - int(legacy_mask.sum())
        pct = area_delta / max(1, int(legacy_mask.sum())) * 100

        print(f"{label:<26} {path.parent.name:<9} {e_before:>4} > {e_after:<3} "
              f"{h_before:>5} > {h_after:<3} {pct:>+10.2f}% {info['separation']:>6.0f} "
              f"{str(info['low_contrast']):>5} {secs:>5.1f}s")

        if e_after < e_before - 2:
            regressions.append(f"{path.parent.name}: edge quality {e_before} -> {e_after}")
        if h_after > h_before:
            regressions.append(f"{path.parent.name}: holes {h_before} -> {h_after}")

    check("no photo regressed on edge quality or hole count", not regressions,
          "; ".join(regressions) if regressions else "all photos same or better")


def main():
    units_only = "--units" in sys.argv
    root = Path(__file__).resolve().parent.parent

    print("--- Tier 1: mask-repair unit checks ---")
    test_thin_handle_reconnects()
    test_detached_part_kept_by_proximity()
    test_distant_speck_still_dropped()
    test_speckle_holes_filled_openings_kept()
    test_measure_separation_flags_low_contrast()
    test_legacy_config_reproduces_old_behaviour()

    if not units_only:
        run_real_photo_comparison(root)

    failed = [r for r in _results if r[0] == FAIL]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed.")
    if failed:
        for _, name, detail in failed:
            print(f"  FAILED: {name} — {detail}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
