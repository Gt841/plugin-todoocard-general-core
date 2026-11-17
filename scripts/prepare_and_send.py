#!/usr/bin/env python3
"""Compatibility shim: the old macOS skill exposed prepare_and_send.sh."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from todoocard_core.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
