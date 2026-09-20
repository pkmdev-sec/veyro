from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from test_animated_brand import read_gif

ROOT = Path(__file__).resolve().parents[1]
DIAGRAM = ROOT / "docs/assets/veyro-supervision.gif"


def test_single_diagram_is_bounded_and_loops():
    frames, applications, _ = read_gif(DIAGRAM, (1120, 1000), max_bytes=2_000_000)
    assert len(frames) == 120
    assert frames[0]["bounds"] == (0, 0, 1120, 1000)
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
    before = (DIAGRAM.read_bytes(), DIAGRAM.stat().st_mtime_ns)
    subprocess.run(
        [uv, "run", "--script", str(ROOT / "tools/generate_supervision_diagram.py"), "--check"],
        capture_output=True,
        check=True,
        timeout=60,
    )
    assert (DIAGRAM.read_bytes(), DIAGRAM.stat().st_mtime_ns) == before
