#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow==12.3.0"]
# ///
"""Render a four-second sentinel light orbit; --check verifies shipped assets."""

from __future__ import annotations

import argparse
import math
from io import BytesIO
from pathlib import Path

from generate_brand_assets import BACKGROUND, HEIGHT, PALETTE, SCALE, WIDTH, draw_logo
from generated_asset_checks import assets_needing_regeneration
from PIL import Image, ImageDraw

ASSETS = Path(__file__).resolve().parents[1] / "docs" / "assets"
SIZE = (WIDTH * SCALE, HEIGHT * SCALE)
FRAMES, FRAME_MS = 40, 100
MATERIALS = "01234567ea"
LEVELS = tuple(0.45 + index * 0.03 for index in range(21))
COLORS = [BACKGROUND, PALETTE["w"][:3], (16, 42, 49)] + [
    tuple(min(255, round(channel * level)) for channel in PALETTE[material][:3])
    for material in MATERIALS
    for level in LEVELS
]
GIF_PALETTE = [channel for color in COLORS for channel in color]
GIF_PALETTE += [0] * (768 - len(GIF_PALETTE))


def render_frame(index: int, pixels: list[list[str]]) -> Image.Image:
    phase = math.tau * index / FRAMES
    image = Image.new("P", SIZE, 0)
    image.putpalette(GIF_PALETTE)
    draw = ImageDraw.Draw(image)
    cells = [
        (x, y, color)
        for y, row in enumerate(pixels)
        for x, color in enumerate(row)
        if color in MATERIALS
    ]
    # A fixed shallow extrusion; only the light moves, never the camera or text.
    for x, y, _ in cells:
        px, py = x * SCALE, y * SCALE
        draw.rectangle((px + 2, py + 2, px + 5, py + 5), fill=2)
    for x, y, material in cells:
        px, py = x * SCALE, y * SCALE
        base = 3 + MATERIALS.index(material) * len(LEVELS)
        light = 15 + round(3 * math.sin(phase - x / 18 + y / 26))
        draw.rectangle((px, py, px + 3, py + 3), fill=base + light)
        draw.line((px, py, px + 3, py), fill=base + min(20, light + 2))
        draw.line((px + 3, py + 1, px + 3, py + 3), fill=base + light - 8)
    for y, row in enumerate(pixels):
        for x, color in enumerate(row):
            if color == "w":
                px, py = x * SCALE, y * SCALE
                draw.rectangle((px, py, px + 3, py + 3), fill=1)
    return image


def render_assets() -> dict[str, bytes]:
    pixels = draw_logo()
    frames = [render_frame(index, pixels) for index in range(FRAMES)]
    gif, poster = BytesIO(), BytesIO()
    frames[0].save(
        gif,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=FRAME_MS,
        loop=0,
        optimize=False,
        disposal=1,
    )
    frames[0].convert("RGB").save(poster, format="PNG", optimize=False)
    return {
        "veyro-logo-animated.gif": gif.getvalue(),
        "veyro-logo-animated-poster.png": poster.getvalue(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify assets without changing files")
    args = parser.parse_args()
    assets = render_assets()
    if args.check:
        stale = assets_needing_regeneration(ASSETS, assets)
        if stale:
            print("Animated assets need regeneration: " + ", ".join(stale))
            return 1
        print("Animated assets are current.")
        return 0
    ASSETS.mkdir(parents=True, exist_ok=True)
    for name, data in assets.items():
        (ASSETS / name).write_bytes(data)
    print(
        f"Generated {FRAMES} frames, {FRAMES * FRAME_MS / 1000:g}s loop, "
        f"{len(assets['veyro-logo-animated.gif']):,} GIF bytes."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
