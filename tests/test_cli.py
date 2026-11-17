from __future__ import annotations

import re
from pathlib import Path

import pytest
from PIL import Image

from todoocard_core import ble, cli, imaging, protocol, quicklz
from todoocard_core.transfer import TransferError


def _striped_image() -> Image.Image:
    colors = [(0, 0, 0), (255, 255, 255), (255, 255, 0), (0, 0, 255), (255, 0, 0), (0, 255, 0)]
    w, h = imaging.CARD_W, imaging.CARD_H
    band = h // len(colors)
    rows = []
    for y in range(h):
        rows.append(bytes(colors[min(len(colors) - 1, y // band)]) * w)
    return Image.frombytes("RGB", (w, h), b"".join(rows))


def test_prepare_stdout_contract(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = tmp_path / "card.png"
    _striped_image().save(src)
    out = tmp_path / "out"
    assert cli.main(["--input", str(src), "--output", str(out)]) == 0
    text = capsys.readouterr().out
    lines = text.splitlines()

    assert any(line.startswith("Color mode:") for line in lines)

    def pick(pattern: str, *, exclude: str | None = None) -> re.Match[str]:
        for line in lines:
            if exclude and exclude in line:
                continue
            match = re.match(pattern, line)
            if match:
                return match
        raise AssertionError(f"missing contract line: {pattern}\n{text}")

    bin_match = pick(r"^Wrote (.+?[^.]\.bin) \((\d+) bytes\)$", exclude=".controller.bin")
    controller_match = pick(r"^Wrote (.+?\.controller\.bin) \((\d+) bytes\)$")
    legacy_match = pick(r"^Wrote (.+?\.protocol\.legacy\.qlz) \((\d+) bytes\)$")
    qlz_match = pick(r"^Prepared v157\+ payload: (.+?\.protocol\.qlz)$")

    bin_path = Path(bin_match.group(1))
    controller_path = Path(controller_match.group(1))
    legacy_path = Path(legacy_match.group(1))
    qlz_path = Path(qlz_match.group(1))
    assert bin_path.stat().st_size == imaging.T3_CONTROLLER_BYTES
    assert controller_path.stat().st_size == imaging.T3_CONTROLLER_BYTES
    assert legacy_path.stat().st_size == 4 + imaging.T3_CONTROLLER_BYTES // 64 * 67
    qlz = qlz_path.read_bytes()
    assert qlz[:4] == b"\x00\x00\x00\x00"
    assert quicklz.unwrap_payload(qlz) == controller_path.read_bytes()


def test_invalid_arguments_exit_with_usage() -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--pair"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        cli.main(["--probe", "--device-id", "nope"])
    assert exc.value.code == 2


def _item(firmware: int) -> ble.Discovered:
    adv = protocol.Advertisement(
        manufacturer_id=protocol.MANUFACTURER_ID,
        screen_type=protocol.SCREEN_T3,
        capability_flags=0x01,
        firmware_version=firmware,
    )
    return ble.Discovered(device=None, address="AA:BB:CC:DD:EE:FF", name="T", rssi=-50, adv=adv)


def test_profile_selection_prefers_smaller_quicklz() -> None:
    options, name = cli._select_profile(_item(0xA7), b"q" * 100, b"l" * 300, b"r" * 900)
    assert name == "compressed"
    assert options.compressed is True
    assert options.payload == b"q" * 100

    options, name = cli._select_profile(_item(0xA7), b"q" * 900, b"l" * 300, b"r" * 800)
    assert name == "standard"
    assert options.compressed is False
    assert options.payload == b"r" * 800


def test_profile_selection_uses_legacy_for_old_firmware() -> None:
    options, name = cli._select_profile(_item(0x90), b"q" * 100, b"l" * 300, b"r" * 900)
    assert name == "legacy"
    assert options.compressed is False
    assert options.payload == b"l" * 300
    with pytest.raises(TransferError, match="pre-v157"):
        cli._select_profile(_item(0x90), b"", b"", b"r" * 900)
