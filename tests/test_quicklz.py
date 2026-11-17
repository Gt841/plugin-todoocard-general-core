from __future__ import annotations

import random

from todoocard_core import quicklz


def test_round_trip_controller_payload() -> None:
    rng = random.Random(7)
    raw = bytes(rng.getrandbits(8) for _ in range(quicklz.T3_CONTROLLER_BYTES))
    payload = quicklz.build_level3_payload(raw)
    assert payload[:4] == b"\x00\x00\x00\x00"
    info = quicklz.inspect_payload(payload)
    assert info.decoded_bytes == len(raw)
    assert info.levels == (3,)
    assert quicklz.unwrap_payload(payload) == raw


def test_compressible_payload_shrinks() -> None:
    raw = (b"\xff" * 64 + b"\x11" * 64) * 1633 + b"\xff" * 64
    assert len(raw) == quicklz.T3_CONTROLLER_BYTES
    payload = quicklz.build_level3_payload(raw)
    info = quicklz.inspect_payload(payload)
    assert len(payload) < len(raw)
    assert info.compressed_blocks > 0
    assert quicklz.unwrap_payload(payload) == raw


def test_level3_encoder_matches_stored_choice_for_tiny_block() -> None:
    block = bytes([0xAB]) * 3
    encoded = quicklz.encode_level3_block(block)
    # A 3-byte block cannot beat the stored header; the encoder must store it.
    assert not encoded[0] & 1
    assert encoded[-3:] == block


def test_payload_rejects_wrong_size() -> None:
    try:
        quicklz.build_level3_payload(b"\x00" * 100)
    except ValueError as exc:
        assert "controller payload" in str(exc)
    else:
        raise AssertionError("wrong payload size must be rejected")


def test_inspect_rejects_missing_prefix() -> None:
    try:
        quicklz.inspect_payload(b"\x01\x02\x03\x04")
    except ValueError as exc:
        assert "zero prefix" in str(exc)
    else:
        raise AssertionError("missing prefix must be rejected")
