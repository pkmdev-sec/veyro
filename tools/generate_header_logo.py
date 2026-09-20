#!/usr/bin/env python3
"""Generate the original relay-knot header logo without fonts or existing assets."""

from __future__ import annotations

import argparse
from pathlib import Path

ASSET = Path(__file__).resolve().parents[1] / "docs/assets/veyro-relay-logo.svg"


def render_svg() -> bytes:
    return b"""<svg xmlns="http://www.w3.org/2000/svg" width="720" height="200"
     viewBox="0 0 720 200" role="img" aria-labelledby="relay-title relay-desc">
  <title id="relay-title">Veyro</title>
  <desc id="relay-desc">An original relay knot: two folded mint and cyan links leave
    an open checkpoint around an amber diamond. A custom, level VEYRO wordmark
    stands beside it.</desc>
  <defs>
    <linearGradient id="mint" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#98f5d9"/>
      <stop offset="1" stop-color="#37c6ad"/>
    </linearGradient>
    <linearGradient id="cyan" x1="1" y1="1" x2="0" y2="0">
      <stop offset="0" stop-color="#86d9fa"/>
      <stop offset="1" stop-color="#409fc5"/>
    </linearGradient>
  </defs>
  <rect width="720" height="200" rx="24" fill="#09111d"/>
  <g id="relay-knot" stroke-linejoin="round">
    <path d="M40 70 L76 34 H119 L139 54 H94 L68 80 L94 106 L82 122 L40 80 Z" fill="url(#mint)"/>
    <path d="M68 80 L94 106 L82 122 L56 96 Z" fill="#238c88"/>
    <path d="M160 130 L124 166 H81 L61 146 H106 L132 120 L106 94 L118 78 L160 120 Z"
          fill="url(#cyan)"/>
    <path d="M132 120 L106 94 L118 78 L144 104 Z" fill="#357c9b"/>
    <path d="M100 90 L110 100 L100 110 L90 100 Z" fill="#f8c875"/>
  </g>
  <g id="wordmark" fill="none" stroke="#edf7f4" stroke-width="12"
     stroke-linecap="round" stroke-linejoin="round">
    <path id="letter-v" d="M248 64 L278 132 L308 64"/>
    <path id="letter-e" d="M380 64 H336 V132 H380 M336 98 H372"/>
    <path id="letter-y" d="M408 64 L436 98 L464 64 M436 98 V132"/>
    <path id="letter-r" d="M492 132 V64 H516 C552 64 552 98 516 98 H492 M516 98 L550 132"/>
    <path id="letter-o" d="M612 64 C570 64 570 132 612 132 C654 132 654 64 612 64 Z"/>
  </g>
</svg>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    data = render_svg()
    if args.check:
        if not ASSET.exists() or ASSET.read_bytes() != data:
            print("Relay logo needs regeneration.")
            return 1
        print("Relay logo is current.")
        return 0
    ASSET.parent.mkdir(parents=True, exist_ok=True)
    ASSET.write_bytes(data)
    print("Generated original 720x200 relay-knot logo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
