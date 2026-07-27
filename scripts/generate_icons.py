"""One-off script to generate placeholder PWA icons (run once, or replace
static/icons/*.png with your own branded icons later)."""
from PIL import Image, ImageDraw, ImageFont

SIZES = [192, 512]
BG = (17, 24, 39)       # near-black slate
FG = (56, 189, 248)     # accent cyan


def make_icon(size: int, path: str):
    img = Image.new("RGB", (size, size), BG)
    draw = ImageDraw.Draw(img)
    margin = size // 8
    draw.rounded_rectangle(
        [margin, margin, size - margin, size - margin],
        radius=size // 10,
        outline=FG,
        width=max(2, size // 40),
    )
    text = "EG"
    try:
        font = ImageFont.truetype("arialbd.ttf", size // 3)
    except Exception:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(
        ((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1]),
        text,
        fill=FG,
        font=font,
    )
    img.save(path, "PNG")


if __name__ == "__main__":
    import os

    out_dir = os.path.join(os.path.dirname(__file__), "..", "static", "icons")
    os.makedirs(out_dir, exist_ok=True)
    for s in SIZES:
        make_icon(s, os.path.join(out_dir, f"icon-{s}.png"))
    print("Icons generated in static/icons/")
