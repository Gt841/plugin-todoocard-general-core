"""TodooCard universal core: image payloads and BLE transfer for macOS/Windows/Linux."""
from __future__ import annotations

__version__ = "0.1.0"

from .imaging import CARD_H, CARD_W, ImageError, PayloadArtifacts, build
from .protocol import Advertisement, is_device_id, parse_manufacturer_data
from .quicklz import build_level3_payload, unwrap_payload

__all__ = [
    "__version__",
    "CARD_W",
    "CARD_H",
    "ImageError",
    "PayloadArtifacts",
    "build",
    "Advertisement",
    "is_device_id",
    "parse_manufacturer_data",
    "build_level3_payload",
    "unwrap_payload",
]
