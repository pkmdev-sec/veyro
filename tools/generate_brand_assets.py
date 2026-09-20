#!/usr/bin/env python3
"""Build Veyro's original pixel logo. Run with --check to detect asset drift."""

from __future__ import annotations

import argparse
import struct
import zlib
from pathlib import Path

WIDTH, HEIGHT, SCALE = 104, 40, 8
ASSETS = Path(__file__).resolve().parents[1] / "docs" / "assets"
PALETTE = {
    ".": (0, 0, 0, 0),
    "b": (51, 79, 93, 255),
    "n": (16, 29, 43, 255),
    "s": (11, 21, 34, 255),
    "t": (43, 111, 117, 255),
    "m": (113, 239, 200, 255),
    "w": (226, 250, 241, 255),
    "a": (255, 195, 105, 255),
}
GLYPHS = {
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
}


def draw_logo() -> list[list[str]]:
    pixels = [["."] * WIDTH for _ in range(HEIGHT)]

    def rect(x: int, y: int, width: int, height: int, color: str) -> None:
        for row in pixels[y : y + height]:
            row[x : x + width] = [color] * width

    # A one-pixel rim keeps the clipped navy plate legible on either page theme.
    for y in range(HEIGHT):
        inset = max(0, 4 - min(y, HEIGHT - 1 - y))
        rect(inset, y, WIDTH - 2 * inset, 1, "b")
        if 0 < y < HEIGHT - 1:
            rect(inset + 1, y, WIDTH - 2 * inset - 2, 1, "s" if y >= 36 else "n")

    # The eye feeds one approval gate, then fans out to three native agents.
    rect(18, 18, 1, 12, "t")
    rect(8, 30, 21, 1, "t")
    for x in (8, 18, 28):
        rect(x, 30, 1, 3, "t")
        rect(x - 2, 32, 5, 3, "t")
        rect(x - 1, 32, 3, 2, "m")
    rect(15, 22, 7, 7, "a")
    rect(16, 23, 5, 5, "n")
    for x, y in ((16, 25), (17, 26), (18, 25), (19, 24), (20, 23)):
        rect(x, y, 1, 1, "a")

    eye_spans = (
        (9, 16),
        (6, 19),
        (4, 21),
        (2, 23),
        (1, 24),
        (0, 25),
        (1, 24),
        (2, 23),
        (4, 21),
        (6, 19),
        (9, 16),
    )
    for dy, (left, right) in enumerate(eye_spans):
        rect(6 + left, 7 + dy, right - left, 1, "m")
        if 1 < dy < 9:
            rect(8 + left, 7 + dy, right - left - 4, 1, "t")
    rect(15, 8, 7, 9, "m")
    rect(17, 10, 3, 5, "n")
    rect(16, 9, 2, 2, "w")

    # The hand-drawn 5x7 alphabet is geometry, not a font dependency.
    for index, letter in enumerate("VEYRO"):
        for y, row in enumerate(GLYPHS[letter]):
            for x, bit in enumerate(row):
                if bit == "1":
                    rect(39 + index * 12 + x * 2, 12 + y * 2, 2, 2, "w")
    rect(39, 30, 34, 1, "t")
    rect(76, 30, 14, 1, "m")
    rect(93, 30, 4, 1, "a")
    return pixels


def render_svg(pixels: list[list[str]]) -> bytes:
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH * SCALE}" '
        f'height="{HEIGHT * SCALE}" viewBox="0 0 {WIDTH} {HEIGHT}" '
        'role="img" aria-labelledby="veyro-title veyro-desc" shape-rendering="crispEdges">',
        '  <title id="veyro-title">Veyro</title>',
        '  <desc id="veyro-desc">Pixel-art watchful eye connected through an amber approval '
        "gate to three coding-agent nodes, beside the VEYRO wordmark "
        "on a clipped navy plate.</desc>",
    ]
    for color, (red, green, blue, _) in PALETTE.items():
        if color == ".":
            continue
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
