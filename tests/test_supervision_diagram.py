from __future__ import annotations

import shutil
import struct
import subprocess
from pathlib import Path

import pytest
from test_animated_brand import read_gif

ROOT = Path(__file__).resolve().parents[1]
DIAGRAM = ROOT / "docs/assets/veyro-supervision.gif"


def test_single_diagram_is_bounded_and_loops():
    frames, applications, _ = read_gif(DIAGRAM, (1120, 1200), max_bytes=2_000_000)
    assert len(frames) == 120
    assert frames[0]["bounds"] == (0, 0, 1120, 1200)
    assert {frame["delay"] for frame in frames} == {4}
    assert applications == [(b"NETSCAPE2.0", b"\x01\x00\x00")]


def frame_hashes(filters: str | None = None) -> list[str]:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("FFmpeg is required for independent animation decoding")
    command = [ffmpeg, "-v", "error", "-i", str(DIAGRAM)]
    if filters:
        command += ["-vf", filters]
    result = subprocess.run(
        command + ["-f", "framemd5", "-"], capture_output=True, text=True, check=True, timeout=30
    )
    assert not result.stderr
    return [
        line.rsplit(",", 1)[1].strip()
        for line in result.stdout.splitlines()
        if line and not line.startswith("#")
    ]


def test_decoded_diagram_animates_only_connectors_not_text_or_geometry():
    hashes = frame_hashes()
    assert len(hashes) == len(set(hashes)) == 120
    # Mask just the seven connector corridors. The remaining scene must never move or flicker.
    corridors = [
        (552, 327, 17, 42),
        (684, 406, 92, 17),
        (352, 458, 17, 69),
        (421, 632, 127, 17),
        (912, 706, 17, 89),
        (347, 834, 62, 17),
        (709, 834, 61, 17),
    ]
    filters = ",".join(
        f"drawbox=x={x}:y={y}:w={w}:h={h}:color=black:t=fill" for x, y, w, h in corridors
    )
    stable = frame_hashes(filters)
    assert len(stable) == 120 and len(set(stable)) == 1


def test_diagram_generator_is_current_and_nonwriting():
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required for the isolated diagram builder")
    assets = [DIAGRAM, DIAGRAM.with_suffix(".png")]
    before = [(asset.read_bytes(), asset.stat().st_mtime_ns) for asset in assets]
    subprocess.run(
        [uv, "run", "--script", str(ROOT / "tools/generate_supervision_diagram.py"), "--check"],
        capture_output=True,
        check=True,
        timeout=60,
    )
    assert [(asset.read_bytes(), asset.stat().st_mtime_ns) for asset in assets] == before


def test_still_diagram_has_expected_dimensions():
    data = DIAGRAM.with_suffix(".png").read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert data[12:16] == b"IHDR"
    assert struct.unpack(">II", data[16:24]) == (1120, 1200)


def test_diagram_names_both_qwen_variants_and_release_status():
    generator = (ROOT / "tools/generate_supervision_diagram.py").read_text()
    assert '"Qwen3 14B"' in generator
    assert '"In main: localjev assessor"' in generator
    assert '"Qwen3 4B Instruct"' in generator
    assert '"Experimental: not shipped"' in generator
