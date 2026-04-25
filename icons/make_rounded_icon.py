#!/usr/bin/env python3
"""
Applies macOS squircle rounded corners to the icon PNG and regenerates
the .icns file via iconutil.
"""

import os
import math
import shutil
from PIL import Image, ImageDraw

SRC = os.path.join(os.path.dirname(__file__), "PhotoCDLogo.png")
ICONSET_DIR = os.path.join(os.path.dirname(__file__), "PhotoCDLogo.iconset")
ICNS_OUT = os.path.join(os.path.dirname(__file__), "PhotoCDLogo.icns")

ICON_SIZES = [
    ("icon_16x16.png",      16),
    ("icon_16x16@2x.png",   32),
    ("icon_32x32.png",      32),
    ("icon_32x32@2x.png",   64),
    ("icon_128x128.png",    128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png",    256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png",    512),
    ("icon_512x512@2x.png", 1024),
]


def make_squircle_mask(size: int, n: float = 5.0) -> Image.Image:
    """Generate a superellipse (squircle) alpha mask at the given size."""
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    cx = cy = size / 2.0
    r = size / 2.0

    # Rasterise the superellipse row by row for accuracy.
    pixels = []
    for y in range(size):
        ny = abs((y - cy) / r)
        # max x where point is inside the superellipse
        inside_x = r * (1.0 - ny ** n) ** (1.0 / n) if ny <= 1.0 else 0.0
        x_lo = int(cx - inside_x)
        x_hi = int(cx + inside_x)
        for x in range(size):
            if x_lo <= x <= x_hi:
                pixels.append(255)
            else:
                pixels.append(0)
    mask.putdata(pixels)
    return mask


def apply_rounded_corners(img: Image.Image, n: float = 5.0) -> Image.Image:
    size = img.size[0]  # assume square
    img = img.convert("RGBA")
    mask = make_squircle_mask(size, n)
    img.putalpha(mask)
    return img


def main():
    src = Image.open(SRC).convert("RGBA")

    if os.path.exists(ICONSET_DIR):
        shutil.rmtree(ICONSET_DIR)
    os.makedirs(ICONSET_DIR)

    for filename, px in ICON_SIZES:
        resized = src.resize((px, px), Image.LANCZOS)
        rounded = apply_rounded_corners(resized)
        rounded.save(os.path.join(ICONSET_DIR, filename))
        print(f"  {filename} ({px}x{px})")

    print(f"\nRunning iconutil…")
    ret = os.system(f'iconutil -c icns "{ICONSET_DIR}" -o "{ICNS_OUT}"')
    if ret == 0:
        print(f"Done → {ICNS_OUT}")
    else:
        print("iconutil failed — check that Xcode command-line tools are installed.")

    shutil.rmtree(ICONSET_DIR)


if __name__ == "__main__":
    main()
