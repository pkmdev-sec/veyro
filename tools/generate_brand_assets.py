#!/usr/bin/env python3
"""Build Veyro's sentinel-V logo with stdlib only; --check detects asset drift."""

from __future__ import annotations

import argparse
import struct
import zlib
from pathlib import Path

WIDTH, HEIGHT, SCALE = 160, 64, 4
ASSETS = Path(__file__).resolve().parents[1] / "docs" / "assets"
BACKGROUND = (9, 17, 29)
PALETTE = {
    "n": (*BACKGROUND, 255),
    "w": (220, 246, 236, 255),
    "a": (247, 185, 81, 255),
    "e": (68, 179, 176, 255),
    "0": (79, 207, 177, 255),
    "1": (94, 219, 185, 255),
    "2": (113, 232, 198, 255),
    "3": (135, 239, 212, 255),
    "4": (48, 167, 170, 255),
    "5": (56, 184, 181, 255),
    "6": (72, 200, 190, 255),
    "7": (91, 216, 201, 255),
}
GLYPHS = {
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
}
ICON_ORIGIN = (12, 11)
WORD_ORIGIN = (66, 23)
GLYPH_SCALE, GLYPH_ADVANCE = 3, 17


def draw_logo() -> list[list[str]]:
    pixels = [["n"] * WIDTH for _ in range(HEIGHT)]
    ox, oy = ICON_ORIGIN
    for y in range(42):
        outside = y * 17 // 42
        inside = min(21, 10 + y * 11 // 27)
        for x in range(outside, inside):
            # Ordered terraces follow the V, not random confetti.
            tone = (x + y // 3) % 4
            pixels[oy + y][ox + x] = str(tone)
            pixels[oy + y][ox + 41 - x] = str(4 + tone)

    for y, (left, right) in enumerate(((5, 8), (2, 11), (0, 13), (2, 11), (5, 8))):
        for x in range(left, right):
            pixels[22 + y][27 + x] = "e"
    for y in range(23, 26):
        for x in range(32, 35):
            pixels[y][x] = "a"

    for index, letter in enumerate("VEYRO"):
        for y, row in enumerate(GLYPHS[letter]):
            for x, bit in enumerate(row):
                if bit != "1":
                    continue
                left = WORD_ORIGIN[0] + index * GLYPH_ADVANCE + x * GLYPH_SCALE
                top = WORD_ORIGIN[1] + y * GLYPH_SCALE
                for py in range(top, top + GLYPH_SCALE):
                    pixels[py][left : left + GLYPH_SCALE] = ["w"] * GLYPH_SCALE
    return pixels


def render_svg(pixels: list[list[str]]) -> bytes:
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH * SCALE}" '
        f'height="{HEIGHT * SCALE}" viewBox="0 0 {WIDTH} {HEIGHT}" '
        'role="img" aria-labelledby="veyro-title veyro-desc" shape-rendering="crispEdges">',
        '  <title id="veyro-title">Veyro</title>',
        '  <desc id="veyro-desc">A mint and teal pixel sentinel V shelters a small amber '
        'approval beacon, beside a level VEYRO wordmark on deep navy.</desc>',
    ]
    for color, (red, green, blue, _) in PALETTE.items():
        lines.append(f'  <g fill="#{red:02x}{green:02x}{blue:02x}">')
        for y, row in enumerate(pixels):
            x = 0
            while x < WIDTH:
                if row[x] != color:
                    x += 1
                    continue
                start = x
                while x < WIDTH and row[x] == color:
                    x += 1
                lines.append(f'    <rect x="{start}" y="{y}" width="{x - start}" height="1"/>')
        lines.append("  </g>")
    lines.append("</svg>")
    return ("\n".join(lines) + "\n").encode()


def render_png(pixels: list[list[str]]) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        checksum = zlib.crc32(kind + data)
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)

    scanlines = bytearray()
    for row in pixels:
        scanline = b"\x00" + b"".join(bytes(PALETTE[color]) * SCALE for color in row)
        scanlines.extend(scanline * SCALE)
    header = struct.pack(">IIBBBBB", WIDTH * SCALE, HEIGHT * SCALE, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(scanlines, level=9))
        + chunk(b"IEND", b"")
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="check assets without writing files")
    args = parser.parse_args()
    pixels = draw_logo()
    assets = {"veyro-logo.svg": render_svg(pixels), "veyro-logo.png": render_png(pixels)}
    if args.check:
        stale = [
            name
            for name, data in assets.items()
            if not (ASSETS / name).exists() or (ASSETS / name).read_bytes() != data
        ]
        if stale:
            print("Brand assets need regeneration: " + ", ".join(stale))
            return 1
        print("Brand assets are current.")
        return 0
    ASSETS.mkdir(parents=True, exist_ok=True)
    for name, data in assets.items():
        (ASSETS / name).write_bytes(data)
    print("Generated Veyro SVG and PNG logos.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
