"""Check animation artifacts with stdlib only; Pillow stays in the build script."""

import hashlib
import struct
import zlib
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "docs" / "assets"


def read_gif() -> tuple[list[dict], list[tuple[bytes, bytes]], bytes]:
    data = (ASSETS / "veyro-logo-animated.gif").read_bytes()
    assert data[:6] == b"GIF89a"
    assert 0 < len(data) < 1024 * 1024
    width, height, packed, background, aspect = struct.unpack_from("<HHBBB", data, 6)
    assert (width, height, background, aspect) == (640, 256, 0, 0)
    assert packed & 0x80
    offset = 13 + 3 * 2 ** ((packed & 7) + 1)
    palette = data[13:offset]
    frames, applications = [], []
    control = None

    def subblocks() -> bytes:
        nonlocal offset
        blocks = bytearray()
        while True:
            size = data[offset]
            offset += 1
            if size == 0:
                return bytes(blocks)
            block = data[offset : offset + size]
            assert len(block) == size
            blocks.extend(block)
            offset += size

    while data[offset] != 0x3B:
        marker = data[offset]
        offset += 1
        if marker == 0x21:
            label = data[offset]
            offset += 1
            if label == 0xF9:
                assert data[offset] == 4 and data[offset + 5] == 0
                flags, delay, transparent = struct.unpack_from("<BHB", data, offset + 1)
                assert not flags & 1  # All frames have an opaque, consistent backdrop.
                assert flags >> 2 & 7 == 1
                control = (delay, transparent)
                offset += 6
            elif label == 0xFF:
                size = data[offset]
                offset += 1
                name = data[offset : offset + size]
                offset += size
                applications.append((name, subblocks()))
            else:
                subblocks()
            continue
        assert marker == 0x2C
        x, y, w, h, flags = struct.unpack_from("<HHHHB", data, offset)
        offset += 9
        assert 0 <= x < x + w <= width and 0 <= y < y + h <= height
        if flags & 0x80:
            local_size = 3 * 2 ** ((flags & 7) + 1)
            assert data[offset : offset + local_size] == palette
            offset += local_size
        assert 2 <= data[offset] <= 8
        offset += 1
        compressed = subblocks()
        assert control is not None and compressed
        frames.append({"bounds": (x, y, w, h), "delay": control[0], "data": compressed})
        control = None
    assert offset + 1 == len(data)
    return frames, applications, palette


def test_gif_has_small_smooth_infinite_loop() -> None:
    frames, applications, _ = read_gif()
    assert len(frames) == 40
    assert frames[0]["bounds"] == (0, 0, 640, 256)
    assert {frame["delay"] for frame in frames} == {10}
    assert sum(frame["delay"] for frame in frames) == 400
    assert applications == [(b"NETSCAPE2.0", b"\x01\x00\x00")]
    assert len({hashlib.sha256(frame["data"]).digest() for frame in frames}) == len(frames)


def test_voxel_palette_has_many_colors_and_a_fixed_dark_backdrop() -> None:
    _, _, palette = read_gif()
    colors = {tuple(palette[i : i + 3]) for i in range(0, len(palette), 3)}
    assert tuple(palette[:3]) == (9, 17, 29)
    assert len(colors) >= 80
    assert any(g > r * 1.5 and g > 180 for r, g, b in colors)  # Mint.
    assert any(b > r * 1.5 and b > 180 for r, g, b in colors)  # Blue.
    assert any(r > 230 and 140 < g < 220 and b < 130 for r, g, b in colors)  # Amber.


def test_poster_is_a_valid_full_size_rgb_png() -> None:
    data = (ASSETS / "veyro-logo-animated-poster.png").read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    offset = 8
    chunks = []
    while offset < len(data):
        size = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + size]
        crc = struct.unpack_from(">I", data, offset + 8 + size)[0]
        assert zlib.crc32(kind + payload) == crc
        chunks.append((kind, payload))
        offset += 12 + size
    assert offset == len(data)
    assert chunks[0] == (b"IHDR", struct.pack(">IIBBBBB", 640, 256, 8, 2, 0, 0, 0))
    assert chunks[-1] == (b"IEND", b"")
    raster = zlib.decompress(b"".join(payload for kind, payload in chunks if kind == b"IDAT"))
    assert len(raster) == 256 * (640 * 3 + 1)
    assert all(raster[y * (640 * 3 + 1)] <= 4 for y in range(256))
