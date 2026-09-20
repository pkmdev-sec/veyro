"""Verify the shipped brand assets, including vector/raster pixel parity."""

import re
import shutil
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "docs" / "assets"
GENERATOR = ROOT / "tools" / "generate_brand_assets.py"
SVG_NS = "{http://www.w3.org/2000/svg}"


def test_svg_is_accessible_and_self_contained() -> None:
    data = (ASSETS / "veyro-logo.svg").read_bytes()
    assert b"<!" not in data
    svg = ET.fromstring(data)
    assert svg.tag == SVG_NS + "svg"
    assert svg.attrib == {
        "width": "832",
        "height": "320",
        "viewBox": "0 0 104 40",
        "role": "img",
        "aria-labelledby": "veyro-title veyro-desc",
        "shape-rendering": "crispEdges",
    }
    title = svg.find(SVG_NS + "title")
    description = svg.find(SVG_NS + "desc")
    assert title is not None and title.text == "Veyro"
    assert description is not None
    assert description.text and "approval gate" in description.text
    assert description.text and "three coding-agent nodes" in description.text
    assert svg.attrib["aria-labelledby"].split() == [title.attrib["id"], description.attrib["id"]]
    attributes = {
        "title": {"id"},
        "desc": {"id"},
        "g": {"fill"},
        "rect": {"x", "y", "width", "height"},
    }
    for element in list(svg.iter())[1:]:
        assert element.tag.startswith(SVG_NS)
        tag = element.tag.removeprefix(SVG_NS)
        assert tag in attributes
        assert set(element.attrib) == attributes[tag]
        if tag == "g":
            assert re.fullmatch(r"#[0-9a-f]{6}", element.attrib["fill"])
        if tag == "rect":
            x, y, width, height = (
                int(element.attrib[key]) for key in ("x", "y", "width", "height")
            )
            assert 0 <= x < x + width <= 104
            assert 0 <= y < y + height <= 40


def test_png_integrity_and_exact_integer_scaled_svg_parity() -> None:
    data = (ASSETS / "veyro-logo.png").read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    offset = 8
    chunks = []
    while offset < len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        crc = struct.unpack_from(">I", data, offset + 8 + length)[0]
        assert len(payload) == length
        assert zlib.crc32(kind + payload) == crc
        chunks.append((kind, payload))
        offset += 12 + length
    assert offset == len(data)
    assert [kind for kind, _ in chunks] == [b"IHDR", b"IDAT", b"IEND"]
    assert chunks[-1][1] == b""
    assert struct.unpack(">IIBBBBB", chunks[0][1]) == (832, 320, 8, 6, 0, 0, 0)
    decoder = zlib.decompressobj()
    raster = decoder.decompress(chunks[1][1]) + decoder.flush()
    assert decoder.eof and not decoder.unused_data and not decoder.unconsumed_tail
    assert len(raster) == 320 * (832 * 4 + 1)

    pixels = [[bytes(4) for _ in range(104)] for _ in range(40)]
    svg = ET.parse(ASSETS / "veyro-logo.svg").getroot()
    for group in svg.findall(SVG_NS + "g"):
        color = bytes.fromhex(group.attrib["fill"][1:]) + b"\xff"
        for rect in group:
            x, y, width, height = (int(rect.attrib[key]) for key in ("x", "y", "width", "height"))
            for row in pixels[y : y + height]:
                row[x : x + width] = [color] * width
    expected = b"".join((b"\x00" + b"".join(pixel * 8 for pixel in row)) * 8 for row in pixels)
    assert raster == expected
    assert pixels[0][0] == bytes(4)
    assert pixels[20][50][3] == 255


def test_shipped_assets_pass_check() -> None:
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_generation_is_deterministic_and_check_detects_drift_without_writing(
    tmp_path: Path,
) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    script = tools / GENERATOR.name
    shutil.copyfile(GENERATOR, script)
    command = [sys.executable, str(script)]
    subprocess.run(command, check=True, capture_output=True)
    output = tmp_path / "docs" / "assets"
    expected = {path.name: path.read_bytes() for path in ASSETS.glob("veyro-logo.*")}
    assert set(expected) == {"veyro-logo.svg", "veyro-logo.png"}
    assert {path.name: path.read_bytes() for path in output.iterdir()} == expected
    subprocess.run(command, check=True, capture_output=True)
    assert {path.name: path.read_bytes() for path in output.iterdir()} == expected

    (output / "veyro-logo.svg").unlink()
    png = output / "veyro-logo.png"
    png.write_bytes(b"stale")
    before = png.stat().st_mtime_ns
    result = subprocess.run(command + ["--check"], capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert "veyro-logo.svg" in result.stdout and "veyro-logo.png" in result.stdout
    assert not (output / "veyro-logo.svg").exists()
    assert png.read_bytes() == b"stale" and png.stat().st_mtime_ns == before
    subprocess.run(command, check=True, capture_output=True)
    subprocess.run(command + ["--check"], check=True, capture_output=True)
    assert {path.name: path.read_bytes() for path in output.iterdir()} == expected
