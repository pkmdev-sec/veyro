#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow==12.3.0"]
# ///
"""Render the Veyro voxel logo; --check verifies deterministic shipped assets."""

from __future__ import annotations

import argparse
import math
from io import BytesIO
from pathlib import Path

from generate_brand_assets import HEIGHT, WIDTH, draw_logo
from PIL import Image, ImageDraw

ASSETS = Path(__file__).resolve().parents[1] / "docs" / "assets"
SIZE = (640, 256)
FRAMES = 40
FRAME_MS = 100
BACKGROUND = (9, 17, 29)
# Each material has several hues, with separate dark side and bright top facets.
MATERIALS = {
    "t": ((32, 122, 153), (35, 153, 169), (52, 120, 192)),
    "m": ((65, 209, 174), (102, 233, 190), (60, 183, 207)),
    "w": ((123, 220, 236), (176, 239, 219), (113, 194, 235)),
    "a": ((255, 182, 74), (244, 154, 69), (255, 209, 112)),
}
LEVELS = (0.43, 0.55, 0.68, 0.79, 0.90, 1.0, 1.09)
COLORS = [BACKGROUND, (17, 31, 46), (36, 60, 77), (11, 23, 37)] + [
    tuple(min(255, round(channel * level)) for channel in hue)
    for hues in MATERIALS.values()
    for hue in hues
    for level in LEVELS
]
PALETTE = [channel for color in COLORS for channel in color]
PALETTE += [0] * (768 - len(PALETTE))


def render_frame(index: int, pixels: list[list[str]]) -> Image.Image:
    phase = math.tau * index / FRAMES
    yaw = -0.20 + 0.045 * math.sin(phase)
    tilt = -0.24 + 0.035 * math.cos(phase)
    cy, sy, ct, st = math.cos(yaw), math.sin(yaw), math.cos(tilt), math.sin(tilt)

    def project(x: float, y: float, z: float) -> tuple[int, int]:
        x, y = x - WIDTH / 2, y - HEIGHT / 2
        depth = -x * sy + z * cy
        return round(322 + 5.7 * (x * cy + z * sy)), round(126 + 5.7 * (y * ct - depth * st))

    image = Image.new("P", SIZE, 0)
    image.putpalette(PALETTE)
    draw = ImageDraw.Draw(image)
    # Keep the static logo's clipped plate behind its raised, individually colored cells.
    outline = [(4, 0), (100, 0), (104, 4), (104, 36), (100, 40), (4, 40), (0, 36), (0, 4)]
    draw.polygon([project(x, y, -0.6) for x, y in outline], fill=3)
    draw.polygon([project(x, y, 0) for x, y in outline], fill=1, outline=2)

    cells = [
        (x, y, color)
        for y, row in enumerate(pixels)
        for x, color in enumerate(row)
        if color in MATERIALS
    ]
    # Back-to-front ordering for the fixed-sign camera angles; no faces change visibility.
    cells.sort(key=lambda cell: -cell[0] * sy + cell[1] * st)
    for x, y, material in cells:
        seed = (x * 73 + y * 151 + x * y * 19) % 997
        hue = seed % 3
        base = 4 + list(MATERIALS).index(material) * 21 + hue * 7
        sweep = math.cos(phase - x / WIDTH * math.tau)
        level = 4 + (seed % 5 == 0) + (sweep > 0.80)
        depth = 0.75 + (seed % 4) * 0.12
        left, top, right, bottom = x + 0.065, y + 0.065, x + 0.935, y + 0.935
        a, b, c, d = [
            project(px, py, depth)
            for px, py in ((left, top), (right, top), (right, bottom), (left, bottom))
        ]
        back_a, back_b, back_c = [
            project(px, py, 0) for px, py in ((left, top), (right, top), (right, bottom))
        ]
        draw.polygon([back_a, back_b, b, a], fill=base + min(6, level + 1))
        draw.polygon([back_b, back_c, c, b], fill=base + 1)
        draw.polygon([a, b, c, d], fill=base + level)
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
        stale = [
            name
            for name, data in assets.items()
            if not (ASSETS / name).exists() or (ASSETS / name).read_bytes() != data
        ]
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
