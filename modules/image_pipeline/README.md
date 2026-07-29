# Image Enhancement Pipeline

Local, no-paid-API pipeline that turns a raw product photo into a
professional-looking primary e-commerce listing image (white background,
centered, enhanced lighting) — **without ever altering what the product
actually looks like** (no scratches/dents removed, no parts invented, no
colors/logos changed). Only presentation is improved.

## Public API

```python
from modules import image_pipeline

enhanced, score, report = image_pipeline.process_image(photo_bytes)
```

- `enhanced.jpeg_bytes` / `.width` / `.height` / `.low_resolution`
- `score.overall` (0-100) plus `.sharpness` / `.exposure` / `.centering` / `.occupancy`
- `report.passed` / `.issues` (blocking) / `.warnings` (non-blocking)

Raises `ValueError` if no product could be detected at all, or
`image_pipeline.LowQualityImageError` (carries `.report`) if a product
was found but the resulting `score.overall` is below
`config.quality_threshold` — callers should show `.report.issues` and
prompt a retake.

All behavior is tunable via `PipelineConfig` (see `config.py`); pass a
customized instance as `process_image(bytes, config=...)`.

## Stages (in order)

1. **Import** — EXIF orientation correction.
2. **Detection** (`detector.py`) — `rembg` (`isnet-general-use`)
   segmentation, keeping only the largest connected foreground blob to
   discard stray segmentation noise, plus a heuristic 0-100 confidence
   (rembg has no native confidence score — this is a proxy, not a
   calibrated metric).
3. **Perspective correction** (`perspective.py`) — **in-plane rotation
   only**, via `cv2.minAreaRect`. Explicitly skipped unless the mask is
   "rectangular enough" (contour-area / bounding-rect-area ≥ 0.85) —
   round/oval/irregular products don't have a meaningful "correct" angle
   from a minimal bounding rectangle, and forcing one produces an
   arbitrary, wrong-looking rotation (this happened during development:
   a round steamer lid got rotated ~20° for no real reason before this
   guard was added). Full 3D perspective correction isn't attempted — it
   needs a known reference plane or multiple viewpoints that a single
   arbitrary product photo doesn't provide.
4. **Background removal + crop** (`background.py`) — crops to the kept
   mask's bounding box.
5. **Enhancement** (`enhancer.py`) — auto-contrast, a small brightness/
   contrast lift, optional denoise (`cv2.fastNlMeansDenoisingColored`,
   capped to a working resolution so it doesn't stall on huge crops),
   optional unsharp-mask sharpening. Runs on the product's RGB channels
   only, before compositing, so the white background doesn't skew the
   statistics.
6. **Reflection reduction** (`reflections.py`) — **classical hot-spot
   inpainting only, not true reflection removal.** See the module's own
   docstring for the full explanation; short version: this session tried
   installing the leading local open-source alternative (StableDelight)
   and it could not be made to work on this machine (a cascade of
   version incompatibilities ending in `numpy` needing a C compiler that
   isn't installed). What's implemented instead only softens small,
   tightly-bounded, near-white/low-saturation blobs — capped to ≤1.5% of
   the product's area each, specifically so it can't be tricked into
   "smoothing" a large legitimately bright/white surface (verified during
   development: an earlier, looser threshold was inpainting big swaths
   of a white plastic product body, visibly blurring real detail). A
   soft mid-tone mirrored reflection (e.g. a photographer's silhouette in
   a glossy black panel) will **not** be removed by this — that needs a
   generative model, which is a paid-API-only option today (see below).
7. **Centering + scaling + output canvas** (`background.py`) — scales
   the enhanced/de-glared product to fill `config.target_occupancy` of a
   `config.canvas_size` square, capped at `config.max_upscale` (default
   1.3×) so a low-resolution source is left smaller-but-sharp instead of
   blown up into visible blur (`enhanced.low_resolution` signals when
   this cap was hit), then composites it centered with a soft blurred
   drop shadow onto a solid `config.background_color` canvas.
8. **Quality validation** (`validator.py` + `diagnostics.py`) — all
   classical CV metrics (no ML quality model is available locally):
   sharpness via Laplacian variance *normalized to a fixed reference
   size first* (raw Laplacian variance is resolution-dependent, not just
   blur-dependent — verified: the same genuinely sharp photo scored ~40
   at its native ~3000px crop but 300-800 once resized to a standard
   1000px reference before scoring), exposure via *exact* 0/255 pixel
   fraction with a tolerance allowance (a naive "top-and-bottom-5-bins"
   clipping check flagged real black/white product photos as
   over/under-exposed just for containing legitimately black or white
   parts — verified on a white plastic product where ~26% of pixels sat
   at brightness 254 with a perfectly good exposure), centering/
   occupancy from the deterministic placement math stage 7 already did
   (no second segmentation pass needed).
9. **Output** — JPEG at `config.jpeg_quality` (default 92), never
   overwrites the input.

## Hardware (`hardware.py`)

Checks `onnxruntime.get_available_providers()` for
`CUDAExecutionProvider` (rembg is ONNX-based, not PyTorch — this is the
correct probe, not `torch.cuda.is_available()`). On a machine with only
the CPU `onnxruntime` package installed (this project's default), this
always resolves to `"cpu"` regardless of GPU presence — installing
`onnxruntime-gpu` instead would be required to actually exercise a CUDA
path.

## Known limitations (by design, not oversights)

- **No true reflection removal** — see stage 6 above.
- **No true 3D perspective correction** — see stage 3 above.
- **Quality thresholds are heuristic**, calibrated against a handful of
  real BaseLinker product photos during development, not a large labeled
  dataset — expect to retune `PipelineConfig.quality_threshold` and the
  sub-score weights in `validator.py` as more real photos go through it.
- **Detector confidence is a proxy**, not a calibrated score.

## Extending this later

Every stage is a plain function taking/returning PIL images (plus a
config), so a stronger backend can replace any one of them without
touching the rest — most notably `reflections.reduce_reflections()`
could be swapped for a call to a paid generative image API (e.g. OpenAI
GPT Image, discussed but not integrated as of this writing) without
changing `pipeline.py`'s orchestration.
