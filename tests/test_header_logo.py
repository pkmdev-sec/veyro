from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSET = ROOT / "docs/assets/veyro-relay-logo.svg"
GENERATOR = ROOT / "tools/generate_header_logo.py"


def test_new_logo_is_self_contained_vector_art_with_accessible_wordmark():
    data = ASSET.read_bytes()
    svg = ET.fromstring(data)
    assert svg.attrib["viewBox"] == "0 0 720 200"
    assert svg.attrib["role"] == "img"
    elements = list(svg.iter())
    ids = {node.attrib["id"] for node in elements if "id" in node.attrib}
    assert set(svg.attrib["aria-labelledby"].split()) <= ids
    assert {"aperture", "material-tiles", "wordmark"} <= ids
    assert {node.tag.rsplit("}", 1)[-1] for node in elements} <= {
        "svg",
        "title",
        "desc",
        "defs",
        "linearGradient",
        "radialGradient",
        "stop",
        "rect",
        "g",
        "path",
    }
    assert not any("href" in key for node in elements for key in node.attrib)
    assert not any("transform" in node.attrib for node in elements)
    assert all(data != old.read_bytes() for old in ASSET.parent.glob("veyro-logo*"))


def test_wordmark_has_five_filled_level_outlines_with_open_counters():
    svg = ET.fromstring(ASSET.read_bytes())
    wordmark = svg.find(".//*[@id='wordmark']")
    assert wordmark is not None
    assert wordmark.attrib["fill"] != "none"
    assert wordmark.attrib["fill-rule"] == "evenodd"
    assert len(wordmark) == 5
    previous_right = 0.0
    for outline in wordmark:
        # Inspect actual coordinates rather than relying on letter names or font metadata.
        commands = re.findall(r"([MLHVZ])([^MLHVZ]*)", outline.attrib["d"])
        assert "".join(command + values for command, values in commands) == outline.attrib["d"]
        points = []
        x = y = 0.0
        for command, values in commands:
            coordinates = [float(value) for value in values.split()]
            if command in {"M", "L"}:
                x, y = coordinates
            elif command == "H":
                (x,) = coordinates
            elif command == "V":
                (y,) = coordinates
            else:
                continue
            points.append((x, y))
        left, right = min(x for x, _ in points), max(x for x, _ in points)
        assert left > previous_right
        assert 40 <= right - left <= 80
        assert min(y for _, y in points) == 64
        assert max(y for _, y in points) == 136
        assert outline.attrib["d"].endswith("Z")
        assert "stroke" not in outline.attrib
        previous_right = right
    assert wordmark[-2].attrib["d"].count("M") == 2
    assert wordmark[-1].attrib["d"].count("M") == 2


def test_fine_material_tiles_leave_the_aperture_and_checkpoint_open():
    svg = ET.fromstring(ASSET.read_bytes())
    tiles = svg.find(".//*[@id='material-tiles']")
    assert tiles is not None and len(tiles) > 100
    for tile in tiles:
        x, y, width, height = (float(tile.attrib[key]) for key in ("x", "y", "width", "height"))
        assert 0 < width <= 4 and 0 < height <= 4
        assert 62 <= x < x + width <= 178
        assert 38 <= y < y + height <= 154
        assert x + width <= 94 or x >= 146 or y + height <= 70 or y >= 122
        assert x + width <= 109 or x >= 129 or y + height <= 135
        assert 0 < float(tile.attrib["opacity"]) <= 0.2


def test_header_logo_regenerates_without_reading_existing_art(tmp_path):
    spec = importlib.util.spec_from_file_location("header_logo", GENERATOR)
    assert spec is not None and spec.loader is not None
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    assert generator.render_svg() == ASSET.read_bytes()
    isolated = tmp_path / "tools/generate_header_logo.py"
    isolated.parent.mkdir()
    isolated.write_bytes(GENERATOR.read_bytes())
    output = tmp_path / "docs/assets/veyro-relay-logo.svg"
    result = subprocess.run([sys.executable, str(isolated), "--check"], capture_output=True)
    assert result.returncode == 1 and not output.exists()
    result = subprocess.run([sys.executable, str(isolated)], capture_output=True, check=True)
    assert result.returncode == 0
    assert output.read_bytes() == ASSET.read_bytes()
    before = output.stat().st_mtime_ns
    subprocess.run([sys.executable, str(isolated), "--check"], capture_output=True, check=True)
    assert output.stat().st_mtime_ns == before
    output.write_bytes(b"stale")
    result = subprocess.run([sys.executable, str(isolated), "--check"], capture_output=True)
    assert result.returncode == 1 and output.read_bytes() == b"stale"


def test_diagram_does_not_import_or_embed_logo_art():
    source = (ROOT / "tools/generate_supervision_diagram.py").read_text()
    assert "generate_brand_assets" not in source
    assert "draw_logo" not in source
    assert "veyro-relay-logo" not in source
    assert '"VEYRO"' not in source
