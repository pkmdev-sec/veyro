"""Compare generated image assets by their stable, meaningful content."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_HEADER = struct.Struct(">IIBBBBB")
PNG_CHANNELS = {0: 1, 2: 3, 4: 2, 6: 4}

PngContent = tuple[tuple[int, int, int, int], bytes]


def canonical_png(data: bytes) -> PngContent:
    """Return PNG dimensions, mode, and unfiltered pixels, ignoring encoding choices."""
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError("invalid PNG signature")

    offset = len(PNG_SIGNATURE)
    header: tuple[int, int, int, int, int, int, int] | None = None
    compressed = bytearray()
    finished = False
    while offset < len(data):
        if offset + 12 > len(data):
            raise ValueError("truncated PNG chunk")
        length = struct.unpack_from(">I", data, offset)[0]
        end = offset + 12 + length
        if end > len(data):
            raise ValueError("truncated PNG payload")
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        checksum = struct.unpack_from(">I", data, offset + 8 + length)[0]
        if zlib.crc32(kind + payload) != checksum:
            raise ValueError("invalid PNG checksum")

        if kind == b"IHDR":
            if header is not None or offset != len(PNG_SIGNATURE) or length != PNG_HEADER.size:
                raise ValueError("invalid PNG header")
            header = PNG_HEADER.unpack(payload)
        elif kind == b"IDAT":
            if header is None or finished:
                raise ValueError("invalid PNG image data")
            compressed.extend(payload)
        elif kind == b"IEND":
            if length or header is None or not compressed:
                raise ValueError("invalid PNG end")
            finished = True
            offset = end
            break
        elif kind[:1].isupper():
            raise ValueError(f"unsupported critical PNG chunk: {kind!r}")
        offset = end

    if not finished or offset != len(data) or header is None:
        raise ValueError("incomplete PNG")
    width, height, bit_depth, color_type, compression, filtering, interlace = header
    if width <= 0 or height <= 0:
        raise ValueError("invalid PNG dimensions")
    if bit_depth != 8 or color_type not in PNG_CHANNELS:
        raise ValueError("unsupported PNG mode")
    if (compression, filtering, interlace) != (0, 0, 0):
        raise ValueError("unsupported PNG encoding")

    decoder = zlib.decompressobj()
    raster = decoder.decompress(bytes(compressed)) + decoder.flush()
    if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ValueError("invalid PNG compression stream")

    channels = PNG_CHANNELS[color_type]
    row_size = width * channels
    if len(raster) != height * (row_size + 1):
        raise ValueError("invalid PNG raster size")

    pixels = bytearray()
    previous = bytes(row_size)
    for y in range(height):
        start = y * (row_size + 1)
        filter_type = raster[start]
        encoded = raster[start + 1 : start + row_size + 1]
        row = bytearray(row_size)
        for x, value in enumerate(encoded):
            left = row[x - channels] if x >= channels else 0
            above = previous[x]
            upper_left = previous[x - channels] if x >= channels else 0
            if filter_type == 0:
                predictor = 0
            elif filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = above
            elif filter_type == 3:
                predictor = (left + above) // 2
            elif filter_type == 4:
                estimate = left + above - upper_left
                left_distance = abs(estimate - left)
                above_distance = abs(estimate - above)
                upper_left_distance = abs(estimate - upper_left)
                if left_distance <= above_distance and left_distance <= upper_left_distance:
                    predictor = left
                elif above_distance <= upper_left_distance:
                    predictor = above
                else:
                    predictor = upper_left
            else:
                raise ValueError("unsupported PNG row filter")
            row[x] = (value + predictor) & 0xFF
        pixels.extend(row)
        previous = row

    return (width, height, bit_depth, color_type), bytes(pixels)


def asset_is_current(path: Path, expected: bytes) -> bool:
    """Use visual PNG equality and exact equality for deterministic formats."""
    try:
        actual = path.read_bytes()
    except OSError:
        return False
    if actual == expected:
        return True
    if path.suffix.lower() != ".png":
        return False

    expected_content = canonical_png(expected)
    try:
        return canonical_png(actual) == expected_content
    except (ValueError, zlib.error):
        return False


def assets_needing_regeneration(directory: Path, assets: dict[str, bytes]) -> list[str]:
    return [name for name, data in assets.items() if not asset_is_current(directory / name, data)]
