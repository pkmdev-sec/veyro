#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow==12.3.0"]
# ///
"""Render one front-facing animated overview of Veyro's reviewed-control path."""

from __future__ import annotations

import argparse
import math
from io import BytesIO
from itertools import pairwise
from pathlib import Path

from generate_brand_assets import SCALE, WORD_ORIGIN, draw_logo, render_png
from PIL import Image, ImageDraw, ImageFont

ASSETS = Path(__file__).resolve().parents[1] / "docs" / "assets"
SIZE = (1120, 1000)
FRAMES = 64
FRAME_MS = 100
BG = "#09111d"
PANEL = "#141f30"
BORDER = "#304157"
WHITE = "#eff5fc"
MUTED = "#aebdd0"
MINT = "#66e9be"
BLUE = "#7cc8ff"
AMBER = "#ffca77"
RED = "#ff959a"


def font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.load_default(size=size)


def build_scene() -> tuple[Image.Image, list[list[tuple[int, int]]]]:
    image = Image.new("RGB", SIZE, BG)
    draw = ImageDraw.Draw(image)

    def label(x, y, value, size=26, fill=WHITE, anchor="la"):
        bounds = draw.textbbox((x, y), value, font=font(size), anchor=anchor)
        assert 0 <= bounds[0] < bounds[2] <= SIZE[0], value
        assert 0 <= bounds[1] < bounds[3] <= SIZE[1], value
        draw.text((x, y), value, font=font(size), fill=fill, anchor=anchor)

    def card(box, title, detail, color=BLUE):
        draw.rounded_rectangle(box, radius=18, fill=PANEL, outline=BORDER, width=2)
        x, y, _, _ = box
        draw.rounded_rectangle((x + 18, y + 20, x + 23, y + 59), radius=2, fill=color)
        label(x + 42, y + 16, title, 31, color)
        label(x + 42, y + 57, detail, 22, MUTED)

    def arrow(points, color=BORDER, width=4):
        draw.line(points, fill=color, width=width, joint="curve")
        x, y = points[-1]
        px, py = points[-2]
        angle = math.atan2(y - py, x - px)
        wing = [
            (
                x - 12 * math.cos(angle) + sign * 7 * math.sin(angle),
                y - 12 * math.sin(angle) - sign * 7 * math.cos(angle),
            )
            for sign in (-1, 1)
        ]
        draw.polygon([(x, y), *wing], fill=color)

    logo = Image.open(BytesIO(render_png(draw_logo()))).convert("RGBA")
    icon = logo.crop((0, 0, WORD_ORIGIN[0] * SCALE, logo.height))
    icon = icon.resize((icon.width // 2, icon.height // 2), Image.Resampling.NEAREST)
    image.paste(icon, (24, 20), icon)
    label(174, 44, "VEYRO", 66, WHITE)
    label(1080, 38, "localjev + Qwen3-14B", 29, MINT, "ra")
    label(1080, 79, "Keep your agent. Keep control.", 25, MUTED, "ra")
    draw.line((40, 174, 1080, 174), fill=BORDER, width=2)

    label(40, 196, "YOUR NATIVE AGENTS", 22, MUTED)
    for box, name in [
        ((40, 232, 363, 292), "Prime Agent"),
        ((398, 232, 721, 292), "OpenCode"),
        ((756, 232, 1080, 292), "Codex hooks"),
    ]:
        draw.rounded_rectangle(box, radius=12, fill=PANEL, outline=BORDER, width=2)
        label((box[0] + box[2]) // 2, 247, name, 29, WHITE, "ma")
    label(40, 305, "Native terminals and permissions stay intact", 22, MUTED)

    card(
        (40, 364, 690, 464),
        "Observe structured metadata",
        "Metadata only; no terminal scraping",
        BLUE,
    )
    card((770, 364, 1080, 464), "Read-only", "Default: no controls", MINT)
    arrow([(560, 335), (560, 362)], BLUE)
    arrow([(692, 414), (768, 414)], MINT)

    draw.rounded_rectangle((40, 520, 1080, 712), radius=20, outline=BORDER, width=2)
    label(64, 535, "OPT-IN REVIEWED PROPOSAL", 22, AMBER)
    card((64, 575, 427, 680), "Policy first", "Capabilities + boundaries", BLUE)
    card((542, 575, 1056, 680), "localjev / Qwen3-14B", "Ollama inference on your machine", MINT)
    arrow([(360, 466), (360, 518)], AMBER)
    label(450, 597, "review", 20, MUTED)
    arrow([(429, 640), (540, 640)], MINT)
    label(
        64, 690, "Forbidden actions stay blocked. Model scores never grant permission.", 20, MUTED
    )

    label(260, 748, "APPROVE, RESERVE, THEN RECHECK", 22, AMBER)
    card((40, 789, 353, 895), "Exact approval", "Human + request digest", AMBER)
    card((402, 789, 715, 895), "Claim + recheck", "No duplicate dispatch", AMBER)
    card((764, 789, 1080, 895), "Allowed control", "Prime / OpenCode", MINT)
    arrow([(920, 714), (920, 729), (195, 729), (195, 787)], AMBER)
    arrow([(355, 842), (400, 842)], AMBER)
    arrow([(717, 842), (762, 842)], MINT)

    draw.line((40, 934, 1080, 934), fill=BORDER, width=2)
    label(40, 951, "One proposal. No unattended autopilot.", 23, WHITE)
    label(1080, 952, "Codex: observation only", 23, MUTED, "ra")
    paths = [
        [(560, 335), (560, 362)],
        [(692, 414), (768, 414)],
        [(360, 466), (360, 518)],
        [(429, 640), (540, 640)],
        [(920, 714), (920, 729), (195, 729), (195, 787)],
        [(355, 842), (400, 842)],
        [(717, 842), (762, 842)],
    ]
    return image, paths


def point_on_path(points: list[tuple[int, int]], progress: float) -> tuple[float, float]:
    segments = [(a, b, math.dist(a, b)) for a, b in pairwise(points)]
    remaining = progress * sum(length for _, _, length in segments)
    for a, b, length in segments:
        if remaining <= length:
            t = remaining / length
            return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
        remaining -= length
    return points[-1]


def render_assets() -> dict[str, bytes]:
    base, paths = build_scene()
    palette = base.quantize(colors=248)
    colors = palette.getpalette()
    assert colors is not None
    colors[744:768] = [
        102,
        233,
        190,
        124,
        200,
        255,
        255,
        202,
        119,
        255,
        149,
        154,
        239,
        245,
        252,
        174,
        189,
        208,
        48,
        65,
        87,
        11,
        17,
        29,
    ]
    palette.putpalette(colors)
    frames = []
    for index in range(FRAMES):
        image = base.copy()
        draw = ImageDraw.Draw(image)
        # Only connection markers move. All text, cards, and brand geometry stay front-facing.
        markers = [(0, (index % 16) / 16, BLUE), (1, (index % 32) / 32, MINT)]
        reviewed = index / FRAMES * 5
        stage = min(4, int(reviewed))
        markers.append((stage + 2, reviewed - stage, AMBER if stage != 1 else MINT))
        for path_index, progress, color in markers:
            x, y = point_on_path(paths[path_index], progress)
            draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill=color)
        frames.append(image.quantize(palette=palette, dither=Image.Dither.NONE))
    data = BytesIO()
    frames[0].save(
        data,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=FRAME_MS,
        loop=0,
        disposal=1,
        optimize=False,
    )
    return {"veyro-supervision.gif": data.getvalue()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    assets = render_assets()
    if args.check:
        stale = [
            name
            for name, data in assets.items()
            if not (ASSETS / name).exists() or (ASSETS / name).read_bytes() != data
        ]
        if stale:
            print("Diagram needs regeneration: " + ", ".join(stale))
            return 1
        print("Animated diagram is current.")
        return 0
    ASSETS.mkdir(parents=True, exist_ok=True)
    for name, data in assets.items():
        (ASSETS / name).write_bytes(data)
        print(f"Generated {name}: {SIZE[0]}x{SIZE[1]}, {FRAMES} frames, {len(data):,} bytes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
