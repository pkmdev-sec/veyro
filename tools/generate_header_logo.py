#!/usr/bin/env python3
"""Generate Veyro's original, font-free architectural aperture header."""

from __future__ import annotations

import argparse
from pathlib import Path

ASSET = Path(__file__).resolve().parents[1] / "docs/assets/veyro-relay-logo.svg"
BACKGROUND = "#10121c"
IVORY = "#f2f0e9"
LAVENDER = "#baaafa"
GLACIER = "#91d6e6"
MINT = "#93dec4"
AMBER = "#edc88d"

# These are filled outlines, not a font or a reusable glyph alphabet.
WORDMARK = (
    "M248 64 H267 L283 113 L299 64 H318 L292 136 H274 Z",
    "M337 64 H390 V80 H354 V92 H385 V108 H354 V120 H390 V136 H337 Z",
    "M407 64 H427 L442 87 L457 64 H477 L451 103 V136 H433 V103 Z",
    "M495 64 H540 L558 78 V99 L545 111 L565 136 H544 L526 112 H513 V136 H495 Z "
    "M513 80 V96 H535 L540 91 V85 L535 80 Z",
    "M598 64 H633 L650 81 V119 L633 136 H598 L581 119 V81 Z "
    "M604 80 L599 85 V115 L604 120 H627 L632 115 V85 L627 80 Z",
)


def material_tiles() -> list[str]:
    """Cut a fixed four-unit material grid to the front face, leaving its aperture clear."""
    tiles = []
    for row, y in enumerate(range(40, 152, 4)):
        for column, x in enumerate(range(64, 176, 4)):
            # The four corners must stay inside the chamfered outer silhouette.
            corners = ((x, y), (x + 3.4, y), (x, y + 3.4), (x + 3.4, y + 3.4))
            if not all(
                px + py >= 111 and px - py <= 120 and px + py <= 322 and py - px <= 81
                for px, py in corners
            ):
                continue
            if x < 147 and x + 3.4 > 93 and y < 123 and y + 3.4 > 69:
                continue
            if x < 130 and x + 3.4 > 108 and y + 3.4 > 135:
                continue
            shade = "#ffffff" if (row * 7 + column * 11) % 9 < 5 else BACKGROUND
            opacity = (7 + (row * 13 + column * 17) % 9) / 100
            tiles.append(
                f'    <rect x="{x}" y="{y}" width="3.4" height="3.4" '
                f'fill="{shade}" opacity="{opacity:.2f}"/>'
            )
    return tiles


def render_svg() -> bytes:
    lines = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="720" height="200"',
        '     viewBox="0 0 720 200" role="img" aria-labelledby="relay-title relay-desc">',
        '  <title id="relay-title">Veyro</title>',
        '  <desc id="relay-desc">A dimensional tiled aperture with an open center and a small',
        "    amber checkpoint. Lavender, glacier and mint surfaces stand beside a custom,",
        "    level ivory VEYRO wordmark.</desc>",
        "  <defs>",
        '    <radialGradient id="ambient" cx="0.19" cy="0.46" r="0.6">',
        f'      <stop offset="0" stop-color="{LAVENDER}" stop-opacity="0.09"/>',
        f'      <stop offset="1" stop-color="{BACKGROUND}" stop-opacity="0"/>',
        "    </radialGradient>",
        '    <linearGradient id="face" x1="0" y1="0" x2="0.9" y2="1">',
        f'      <stop offset="0" stop-color="{LAVENDER}"/>',
        f'      <stop offset="0.58" stop-color="{GLACIER}"/>',
        f'      <stop offset="1" stop-color="{MINT}"/>',
        "    </linearGradient>",
        "  </defs>",
        f'  <rect width="720" height="200" rx="24" fill="{BACKGROUND}"/>',
        '  <rect width="720" height="200" rx="24" fill="url(#ambient)"/>',
        '  <g id="aperture">',
        '    <path d="M77 47 H164 L187 70 V153 L176 164 H77 L66 153 V58 Z '
        'M103 80 V130 H150 V80 Z" fill="#293449" fill-rule="evenodd"/>',
        '    <path d="M178 58 L187 70 V153 L176 164 L168 154 L178 144 Z" fill="#35475a"/>',
        '    <path d="M73 38 H158 L178 58 V144 L168 154 H73 L62 143 V49 Z '
        'M94 70 V122 H146 V70 Z" fill="url(#face)" fill-rule="evenodd"/>',
        '    <path d="M94 70 H146 V122 H137 V79 H94 Z" fill="#35475a"/>',
        '    <path d="M94 70 H146 L137 79 H94 Z" fill="#293449"/>',
        '    <g id="material-tiles">',
        *material_tiles(),
        "    </g>",
        '    <path d="M73 38 H158 L178 58 H174 L156 42 H75 L66 51 V143 '
        'L62 143 V49 Z" fill="#f2f0e9" opacity="0.34"/>',
        '    <path d="M94 122 H137 L146 118 V122 Z" fill="#f2f0e9" opacity="0.4"/>',
        f'    <rect x="109" y="135" width="20" height="19" fill="{BACKGROUND}"/>',
        f'    <rect x="112" y="136" width="14" height="8" fill="{AMBER}"/>',
        '    <path d="M112 144 H126 L130 148 H116 Z" fill="#8a755c"/>',
        "  </g>",
        f'  <g id="wordmark" fill="{IVORY}" fill-rule="evenodd">',
        *(f'    <path d="{outline}"/>' for outline in WORDMARK),
        "  </g>",
        "</svg>",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    data = render_svg()
    if args.check:
        if not ASSET.exists() or ASSET.read_bytes() != data:
            print("Aperture logo needs regeneration.")
            return 1
        print("Aperture logo is current.")
        return 0
    ASSET.parent.mkdir(parents=True, exist_ok=True)
    ASSET.write_bytes(data)
    print("Generated original 720x200 aperture logo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
