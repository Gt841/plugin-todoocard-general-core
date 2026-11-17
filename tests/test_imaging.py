from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from todoocard_core import imaging

STRIPES = [
    (0, 0, 0, 0),
    (255, 255, 255, 1),
    (255, 255, 0, 2),
    (255, 0, 0, 3),
    (0, 0, 255, 5),
    (0, 255, 0, 6),
]


def stripe_image() -> Image.Image:
    w, h = imaging.CARD_W, imaging.CARD_H
    band = h // len(STRIPES)
    rows = []
    for y in range(h):
        index = min(len(STRIPES) - 1, y // band)
        r, g, b, _code = STRIPES[index]
        rows.append(bytes([r, g, b]) * w)
    return Image.frombytes("RGB", (w, h), b"".join(rows))


def source_code(x: int, y: int) -> int:
    band = imaging.CARD_H // len(STRIPES)
    index = min(len(STRIPES) - 1, y // band)
    return STRIPES[index][3]


@pytest.fixture(scope="module")
def artifacts(tmp_path_factory: pytest.TempPathFactory):
    tmp = tmp_path_factory.mktemp("imaging")
    src = tmp / "stripes.png"
    stripe_image().save(src)
    return imaging.build(src, tmp / "image")


def test_build_writes_all_artifacts(artifacts: imaging.PayloadArtifacts) -> None:
    assert artifacts.bin.stat().st_size == imaging.T3_CONTROLLER_BYTES
    assert artifacts.controller.stat().st_size == imaging.T3_CONTROLLER_BYTES
    assert artifacts.legacy.stat().st_size == 4 + imaging.T3_CONTROLLER_BYTES // 64 * 67
    assert artifacts.bytes == imaging.T3_CONTROLLER_BYTES
    assert artifacts.legacy_bytes == artifacts.legacy.stat().st_size


def test_panel_calibration_is_vertical_flip(artifacts: imaging.PayloadArtifacts) -> None:
    bitmap = artifacts.bin.read_bytes()
    assert len(bitmap) == imaging.CARD_W * imaging.CARD_H // 2
    for x, y in [(0, 0), (1, 0), (17, 5), (100, 300), (527, 791), (300, 792 - 1)]:
        assert imaging._get(bitmap, y * imaging.CARD_W + x) == source_code(
            x, imaging.CARD_H - 1 - y
        )


def test_controller_order(artifacts: imaging.PayloadArtifacts) -> None:
    controller = artifacts.controller.read_bytes()
    dw = imaging.CARD_H
    for dest_index in [0, 1, 791, 792, 1000, 792 * 527 + 791]:
        dx, dy = dest_index % dw, dest_index // dw
        sx, sy = imaging.CARD_W - 1 - dy, imaging.CARD_H - 1 - dx
        # The controller map is built from the calibrated (vertically flipped) bitmap.
        assert imaging._get(controller, dest_index) == source_code(
            sx, imaging.CARD_H - 1 - sy
        )


def test_legacy_payload_layout(artifacts: imaging.PayloadArtifacts) -> None:
    controller = artifacts.controller.read_bytes()
    legacy = artifacts.legacy.read_bytes()
    assert legacy[:4] == b"\x00\x00\x00\x00"
    for offset in range(0, len(controller), 64):
        start = 4 + (offset // 64) * 67
        assert legacy[start : start + 3] == b"\x74\x43\x40"
        assert legacy[start + 3 : start + 67] == controller[offset : offset + 64]


def test_rotation_and_safety_limits(tmp_path: Path) -> None:
    src = tmp_path / "small.png"
    img = Image.new("RGB", (20, 30), (255, 0, 0))
    for y in range(30):
        for x in range(10):
            img.putpixel((x, y), (0, 0, 255))
    img.save(src)
    normal = imaging.build(src, tmp_path / "normal")
    rotated = imaging.build(src, tmp_path / "rotated", rotate_right_90=True)
    assert normal.bin.stat().st_size == rotated.bin.stat().st_size
    assert normal.bin.read_bytes() != rotated.bin.read_bytes()

    missing = tmp_path / "missing.png"
    with pytest.raises(imaging.ImageError, match="not found"):
        imaging.build(missing, tmp_path / "x")
