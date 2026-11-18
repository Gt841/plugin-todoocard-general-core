"""Machine-readable core manifest: version and compatibility contract.

The JSON file ships inside the package so plugins and callers can read it from
the installed core directory (or via ``todoocard --manifest``) without parsing
README text.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

MANIFEST_PATH = Path(__file__).with_name("manifest.json")


class ManifestError(RuntimeError):
    """The bundled manifest is missing or malformed."""


def load_manifest() -> Dict[str, Any]:
    try:
        text = MANIFEST_PATH.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read core manifest: {exc}") from exc
    if not isinstance(data, dict) or not data.get("version"):
        raise ManifestError("core manifest is missing a version")
    return data


def manifest_text() -> str:
    return json.dumps(load_manifest(), ensure_ascii=False, indent=2) + "\n"
