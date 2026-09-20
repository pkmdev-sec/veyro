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

from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageFont

ASSETS = Path(__file__).resolve().parents[1] / "docs" / "assets"
SIZE = (1120, 1200)
FRAMES = 120
FRAME_MS = 40
BG = "#10121c"
PANEL = "#191c2a"
BORDER = "#363c52"
WHITE = "#f2f0e9"
MUTED = "#a6abc1"
MINT = "#93dec4"
BLUE = "#91d6e6"
AMBER = "#edc88d"
LAVENDER = "#baaafa"


def font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.load_default(size=size)


def build_scene() -> tuple[Image.Image, list[list[tuple[int, int]]]]:
    # Ambient light is static: depth without moving the camera or washing over text.
    light = Image.new("RGB", SIZE, BG)
    glow = ImageDraw.Draw(light)
    glow.ellipse((480, 370, 1130, 850), fill="#25283e")
    glow.ellipse((-200, -160, 580, 260), fill="#232034")
    image = light.filter(ImageFilter.GaussianBlur(100))
    draw = ImageDraw.Draw(image)
    for y in range(22, SIZE[1], 24):
        for x in range(22, SIZE[0], 24):
            draw.point((x, y), fill="#292c3d")

    def label(x, y, value, size=26, fill=WHITE, anchor="la"):
        bounds = draw.textbbox((x, y), value, font=font(size), anchor=anchor)
        assert 0 <= bounds[0] < bounds[2] <= SIZE[0], value
        assert 0 <= bounds[1] < bounds[3] <= SIZE[1], value
        draw.text((x, y), value, font=font(size), fill=fill, anchor=anchor)

    def card(box, title, detail, color=BLUE, step=None):
        x, y, right, bottom = box
        for text, size, margin in [(title, 30, 50 if step else 16), (detail, 22, 16)]:
            assert draw.textbbox((x + 38, y), text, font=font(size))[2] <= right - margin, text
        draw.rounded_rectangle((x, y + 5, right, bottom + 5), radius=16, fill="#0b0d15")
        draw.rounded_rectangle(box, radius=16, fill=PANEL, outline=BORDER, width=1)
        draw.line((x + 20, y + 1, right - 20, y + 1), fill="#454960", width=1)
        draw.rounded_rectangle((x + 18, y + 21, x + 22, y + 54), radius=2, fill=color)
        label(x + 38, y + 15, title, 30, WHITE)
        label(x + 38, y + 58, detail, 22, MUTED)
        if step:
            label(right - 18, y + 20, step, 17, color, "ra")

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

    label(40, 26, "EXISTING-SESSION SUPERVISION", 19, MUTED)
    label(38, 59, "Review locally.", 48, WHITE)
    label(38, 113, "Stay in control.", 43, LAVENDER)
    label(1080, 36, "YOUR LOCAL DECISION LAYER", 18, MUTED, "ra")
    label(1080, 70, "localjev", 36, LAVENDER, "ra")
    label(1080, 122, "Qwen3-14B / Ollama", 24, WHITE, "ra")
    draw.line((40, 180, 1080, 180), fill=BORDER, width=1)

    label(40, 196, "01 / KEEP YOUR NATIVE AGENT", 20, MUTED)
    for box, name in [
        ((40, 232, 363, 292), "Prime Agent"),
        ((398, 232, 721, 292), "OpenCode"),
        ((756, 232, 1080, 292), "Codex hooks"),
    ]:
        draw.rounded_rectangle(box, radius=12, fill=PANEL, outline=BORDER, width=1)
        label((box[0] + box[2]) // 2, 247, name, 29, WHITE, "ma")
    label(40, 305, "Native terminals and permissions stay intact", 22, MUTED)

    card(
        (40, 364, 690, 464),
        "Observe metadata",
        "Metadata only; no terminal scraping",
        BLUE,
        step="02",
    )
    card((770, 364, 1080, 464), "Read-only", "No calls. No controls.", MINT)
    arrow([(560, 335), (560, 362)], BLUE)
    arrow([(692, 414), (768, 414)], MINT)

    draw.rounded_rectangle(
        (40, 520, 1080, 712), radius=20, fill="#161927", outline="#45405c", width=1
    )
    label(64, 535, "OPT-IN / REVIEW ONE PROPOSAL", 20, LAVENDER)
    card((64, 575, 427, 680), "Policy first", "Capabilities + boundaries", BLUE, step="03")
    card(
        (542, 575, 1056, 680),
        "localjev + Qwen3-14B",
        "Assessment stays on your machine",
        LAVENDER,
        step="04",
    )
    arrow([(360, 466), (360, 518)], AMBER)
    label(450, 597, "review", 19, MUTED)
    arrow([(429, 640), (540, 640)], MINT)
    label(
        64, 690, "Forbidden actions stay blocked. Model scores never grant permission.", 20, MUTED
    )

    label(40, 750, "EXPLICIT APPROVAL. NO BLIND RETRIES.", 20, AMBER)
    card((40, 789, 353, 895), "Allowed control", "Prime Agent / OpenCode", MINT, step="07")
    card((402, 789, 715, 895), "Claim + recheck", "No duplicate dispatch", AMBER, step="06")
    card((764, 789, 1080, 895), "Exact approval", "Human + request digest", AMBER, step="05")
    arrow([(920, 714), (920, 787)], AMBER)
    arrow([(762, 842), (717, 842)], AMBER)
    arrow([(400, 842), (355, 842)], MINT)

    draw.line((40, 934, 1080, 934), fill=BORDER, width=1)
    label(40, 951, "One proposal. No unattended autopilot.", 23, WHITE)
    label(1080, 952, "Codex: observation only", 23, MUTED, "ra")
    draw.line((40, 995, 1080, 995), fill=BORDER, width=1)
    label(40, 1010, "MODEL VARIANTS / RELEASE STATUS", 20, MUTED)
    card(
        (40, 1050, 540, 1150),
        "Qwen3 14B",
        "In main: localjev assessor",
        MINT,
    )
    card(
        (580, 1050, 1080, 1150),
        "Qwen3 4B Instruct",
        "Experimental: not shipped",
        AMBER,
    )
    label(
        40,
        1168,
        "4B is a lower-memory development profile, not an alternate assessor in this release.",
        22,
        MUTED,
    )
    paths = [
        [(560, 335), (560, 362)],
        [(692, 414), (768, 414)],
        [(360, 466), (360, 518)],
        [(429, 640), (540, 640)],
        [(920, 714), (920, 787)],
        [(762, 842), (717, 842)],
        [(400, 842), (355, 842)],
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
    accents = [MINT, BLUE, AMBER, LAVENDER, WHITE, MUTED, BORDER, BG]
    colors[744:768] = [channel for color in accents for channel in ImageColor.getrgb(color)]
    palette.putpalette(colors)
    frames = []
    for index in range(FRAMES):
        image = base.copy()
        draw = ImageDraw.Draw(image)
        # Only connection markers move. All text, cards, and brand geometry stay front-facing.
        markers = [(0, (index % 20) / 20, BLUE), (1, (index % 40) / 40, MINT)]
        reviewed = index / FRAMES * 5
        stage = min(4, int(reviewed))
        markers.append((stage + 2, reviewed - stage, AMBER if stage != 1 else LAVENDER))
        for path_index, progress, color in markers:
            x, y = point_on_path(paths[path_index], progress)
            draw.ellipse((x - 7, y - 7, x + 7, y + 7), fill="#333747")
            draw.rounded_rectangle((x - 3, y - 3, x + 3, y + 3), radius=1, fill=color)
            draw.point((round(x), round(y)), fill=WHITE)
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
    still = BytesIO()
    base.save(still, format="PNG")
    return {"veyro-supervision.gif": data.getvalue(), "veyro-supervision.png": still.getvalue()}


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
        print("Supervision diagrams are current.")
        return 0
    ASSETS.mkdir(parents=True, exist_ok=True)
    for name, data in assets.items():
        (ASSETS / name).write_bytes(data)
        print(f"Generated {name}: {len(data):,} bytes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
