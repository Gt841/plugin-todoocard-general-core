from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from todoocard_core import __version__, cli, imaging, protocol, quicklz
from todoocard_core.manifest import MANIFEST_PATH, load_manifest, manifest_text

ROOT = Path(__file__).resolve().parent.parent


def test_manifest_versions_and_contract() -> None:
    manifest = load_manifest()
    assert manifest["schemaVersion"] == 1
    assert manifest["name"] == "todoocard-general-core"
    assert manifest["version"] == __version__
    assert manifest["license"] == "MIT"
    assert manifest["entry"] == "todoocard_core.cli:main"
    assert "darwin" in manifest["platforms"]
    assert "win32" in manifest["platforms"]

    card = manifest["card"]
    assert (card["width"], card["height"]) == (imaging.CARD_W, imaging.CARD_H)
    assert card["bitmapBytes"] == imaging.CARD_W * imaging.CARD_H // 2

    device = manifest["device"]
    assert device["manufacturerId"] == f"0x{protocol.MANUFACTURER_ID:04X}"
    assert device["screenType"] == f"0x{protocol.SCREEN_T3:04X}"
    assert device["controlUuids"] == list(protocol.CONTROL_UUIDS)
    assert device["dataUuids"] == list(protocol.DATA_UUIDS)

    firmware = manifest["firmware"]
    assert firmware["verifiedMin"] == f"0x{protocol.VERIFIED_TRANSFER_MIN_VERSION:02X}"
    assert firmware["diagnosticsMin"] == f"0x{protocol.DIAGNOSTICS_MIN_VERSION:02X}"

    transfer = manifest["transfer"]
    assert transfer["blockSizeBytes"] == quicklz.QUICKLZ_BLOCK_SIZE
    assert transfer["verifiedWindowBlocks"] == protocol.VERIFIED_WINDOW_BLOCKS
    assert transfer["checkpointTimeoutSeconds"] == protocol.VERIFIED_CHECKPOINT_TIMEOUT
    assert transfer["finalAckTimeoutSeconds"] == protocol.FINAL_REFRESH_ACK_TIMEOUT
    assert transfer["legacyFinalAckTimeoutSeconds"] == protocol.LEGACY_FINAL_ACK_TIMEOUT


def test_pyproject_version_matches_manifest() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"', pyproject, re.M)
    assert match is not None
    assert match.group(1) == load_manifest()["version"]


def test_manifest_file_is_bundled_in_package() -> None:
    assert MANIFEST_PATH.is_file()
    assert MANIFEST_PATH.parent.name == "todoocard_core"
    assert json.loads(manifest_text())["version"] == __version__


def test_cli_manifest_is_machine_readable(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--manifest"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["version"] == __version__
    assert data["device"]["manufacturerId"] == "0x5053"
