"""Check animation artifacts with stdlib only; Pillow stays in the build script."""

import colorsys
import hashlib
import shutil
import struct
import subprocess
import zlib
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parents[1] / "docs" / "assets"


def read_gif(
    path: Path = ASSETS / "veyro-logo-animated.gif",
    size: tuple[int, int] = (640, 256),
    max_bytes: int = 1024 * 1024,
) -> tuple[list[dict], list[tuple[bytes, bytes]], bytes]:
    data = path.read_bytes()
    assert data[:6] == b"GIF89a"
    assert 0 < len(data) < max_bytes
    width, height, packed, background, aspect = struct.unpack_from("<HHBBB", data, 6)
    assert (width, height) == size
    assert background == aspect == 0
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
    assert any(b > r * 1.5 and g > 150 for r, g, b in colors)  # Teal/cyan.
    hues = [colorsys.rgb_to_hsv(r / 255, g / 255, b / 255) for r, g, b in colors]
    assert all(20 / 360 <= h <= 195 / 360 for h, s, v in hues if s > 0.1 and v > 0.25)
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


def decode_rgb(path: Path) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("FFmpeg is required for independent decoded-frame verification")
    result = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True,
        check=True,
    )
    assert not result.stderr, result.stderr.decode()
    return result.stdout


def test_independent_decoder_proves_fixed_wordmark_loop_and_clean_rasters() -> None:
    data = decode_rgb(ASSETS / "veyro-logo-animated.gif")
    frame_size = 640 * 256 * 3
    assert len(data) == 40 * frame_size
    frames = [data[start : start + frame_size] for start in range(0, len(data), frame_size)]
    assert len(set(frames)) == 40
    assert frames[0] == decode_rgb(ASSETS / "veyro-logo-animated-poster.png")
    static = decode_rgb(ASSETS / "veyro-logo.png")
    _, _, palette = read_gif()
    colors = {palette[i : i + 3] for i in range(0, len(palette), 3)}
    navy = bytes((9, 17, 29))
    for frame in frames:
        assert all(frame[i : i + 3] in colors for i in range(0, frame_size, 3))
        for y in range(256):
            start, end = (y * 640 + 240) * 3, (y + 1) * 640 * 3
            assert frame[start:end] == static[start:end], "Wordmark moved, sloped or flickered"
        assert frame[: 640 * 40 * 3] == navy * (640 * 40)
        assert frame[216 * 640 * 3 :] == navy * (40 * 640)
    # A seam must be no larger than an ordinary step, with no harsh pixel flashes.
    differences = []
    for first, second in zip(frames, frames[1:] + frames[:1], strict=True):
        delta = [abs(a - b) for a, b in zip(first, second, strict=True)]
        assert max(delta) <= 16
        differences.append(sum(delta))
    assert min(differences) > 0
    assert differences[-1] <= max(differences[:-1]) * 1.15


def test_animation_generation_is_deterministic_and_check_does_not_write(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required for the isolated, pinned animation builder")
    tools = tmp_path / "tools"
    tools.mkdir()
    source = ASSETS.parents[1] / "tools"
    for name in ("generate_brand_assets.py", "generate_animated_logo.py"):
        shutil.copyfile(source / name, tools / name)
    command = [uv, "run", "--script", str(tools / "generate_animated_logo.py")]
    output = tmp_path / "docs" / "assets"
    expected = {p.name: p.read_bytes() for p in ASSETS.glob("veyro-logo-animated*")}
    for _ in range(2):
        subprocess.run(command, capture_output=True, check=True)
        assert {p.name: p.read_bytes() for p in output.iterdir()} == expected
    subprocess.run(command + ["--check"], capture_output=True, check=True)
    gif = output / "veyro-logo-animated.gif"
    gif.write_bytes(b"stale")
    before = gif.stat().st_mtime_ns
    poster = output / "veyro-logo-animated-poster.png"
    poster.unlink()
    result = subprocess.run(command + ["--check"], capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert gif.name in result.stdout and poster.name in result.stdout
    assert gif.read_bytes() == b"stale" and gif.stat().st_mtime_ns == before
    assert not poster.exists()
