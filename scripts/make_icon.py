#!/usr/bin/env python3
"""Generate ChatLens.icns and the favicon PNGs from a programmatic design.

Run from the chatlens repo root:
    .venv/bin/python scripts/make_icon.py

Produces:
    assets/icon-1024.png        — master raster
    assets/ChatLens.iconset/    — macOS iconset folder (one PNG per @1x/@2x size)
    assets/ChatLens.icns        — bundled icon (referenced by setup.py)
    assets/favicon-512.png      — for the landing page

Design: indigo→violet→fuchsia gradient squircle with a bold rounded "C" mark
and a small chat-bubble dot inside the C's opening.
"""
from __future__ import annotations

import math
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

SIZE = 1024

# Brand palette — bumped slightly more saturated than the site so the icon
# pops on a Dock with lots of other apps.
INDIGO = (79, 70, 229)     # #4F46E5
VIOLET = (124, 58, 237)    # #7C3AED
FUCHSIA = (217, 70, 239)   # #D946EF


def _lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _gradient(size: int) -> Image.Image:
    """Diagonal 3-stop gradient. Drawn small then bicubic-upscaled for speed."""
    small = 192
    img = Image.new("RGB", (small, small))
    px = img.load()
    for y in range(small):
        for x in range(small):
            t = (x + y) / (2 * (small - 1))
            if t < 0.5:
                px[x, y] = _lerp(INDIGO, VIOLET, t * 2)
            else:
                px[x, y] = _lerp(VIOLET, FUCHSIA, (t - 0.5) * 2)
    return img.resize((size, size), Image.BICUBIC)


def _squircle_mask(size: int, radius_frac: float = 0.225) -> Image.Image:
    """Rounded-rectangle alpha mask. Close enough to macOS's superellipse at
    icon sizes."""
    radius = int(size * radius_frac)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=radius, fill=255
    )
    return mask


def _top_highlight(size: int, mask: Image.Image) -> Image.Image:
    """Soft white wash on the upper portion — gives the icon glassy depth."""
    hl = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(hl)
    # Big soft ellipse centered well above the icon so only the bottom curve
    # of the ellipse falls inside the squircle.
    d.ellipse(
        (-size * 0.2, -size * 0.55, size * 1.2, size * 0.3),
        fill=(255, 255, 255, 60),
    )
    hl = hl.filter(ImageFilter.GaussianBlur(size * 0.06))
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(hl, (0, 0), mask)
    return out


def _draw_c(canvas: Image.Image) -> None:
    """A clean monogram C: thick rounded arc with the opening at ~4-5 o'clock."""
    d = ImageDraw.Draw(canvas)
    size = canvas.size[0]
    cx = cy = size // 2

    r = int(size * 0.315)
    stroke = int(size * 0.12)
    # Pillow angles: 0° = 3 o'clock, clockwise. ~70° opening on the right.
    start, end = 35, 325
    bbox = (cx - r, cy - r, cx + r, cy + r)
    d.arc(bbox, start=start, end=end, fill=(255, 255, 255, 255), width=stroke)

    # Rounded caps at the arc endpoints (Pillow's arc uses butt caps).
    cap_r = stroke / 2
    for ang in (start, end):
        rad = math.radians(ang)
        ex = cx + r * math.cos(rad)
        ey = cy + r * math.sin(rad)
        d.ellipse(
            (ex - cap_r, ey - cap_r, ex + cap_r, ey + cap_r),
            fill=(255, 255, 255, 255),
        )


def build_master(size: int = SIZE) -> Image.Image:
    grad = _gradient(size)
    mask = _squircle_mask(size)
    bg = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    bg.paste(grad, (0, 0), mask)
    bg = Image.alpha_composite(bg, _top_highlight(size, mask))
    glyph = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    _draw_c(glyph)
    return Image.alpha_composite(bg, glyph)


# Sizes the macOS iconset needs. Names follow Apple's required naming.
ICONSET_SIZES = [
    ("icon_16x16.png",      16),
    ("icon_16x16@2x.png",   32),
    ("icon_32x32.png",      32),
    ("icon_32x32@2x.png",   64),
    ("icon_128x128.png",   128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png",   256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png",   512),
    ("icon_512x512@2x.png", 1024),
]


def build_iconset(master: Image.Image, target_dir: Path) -> None:
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True)
    for name, size in ICONSET_SIZES:
        master.resize((size, size), Image.LANCZOS).save(target_dir / name)


def build_icns(iconset_dir: Path, icns_path: Path) -> None:
    res = subprocess.run(
        ["iconutil", "-c", "icns", "-o", str(icns_path), str(iconset_dir)],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        sys.exit(f"iconutil failed: {res.stderr}")


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    assets = root / "assets"
    assets.mkdir(exist_ok=True)

    master = build_master(SIZE)
    master.save(assets / "icon-1024.png")

    iconset_dir = assets / "ChatLens.iconset"
    build_iconset(master, iconset_dir)
    build_icns(iconset_dir, assets / "ChatLens.icns")

    # Favicon for the landing page (single high-res PNG; modern browsers handle it).
    master.resize((512, 512), Image.LANCZOS).save(assets / "favicon-512.png")
    master.resize((180, 180), Image.LANCZOS).save(assets / "apple-touch-icon.png")

    print(f"✓ {assets / 'icon-1024.png'}")
    print(f"✓ {assets / 'ChatLens.icns'}")
    print(f"✓ {assets / 'favicon-512.png'}")
    print(f"✓ {assets / 'apple-touch-icon.png'}")


if __name__ == "__main__":
    main()
