from __future__ import annotations

from todoocard_core import protocol


def ad_bytes(firmware: int = 0xA7, flags: int = 0x03, screen: int = 0x134C) -> bytes:
    return bytes(
        [
            0x53,
            0x50,
            screen & 0xFF,
            flags,
            firmware,
            0x00,
            (screen >> 8) & 0xFF,
        ]
    )


def test_parses_manufacturer_data_like_swift() -> None:
    adv = protocol.parse_manufacturer_data(ad_bytes())
    assert adv is not None
    assert adv.manufacturer_id == 0x5053
    assert adv.screen_type == 0x134C
    assert adv.firmware_version == 0xA7
    assert adv.capability_flags == 0x03
    assert adv.compatible
    assert adv.requires_encrypted_gatt
    assert adv.pairing_window_open
    assert adv.verified_transfer
    assert adv.diagnostics_available
    assert not adv.ota_recovery_mode


def test_description_contract() -> None:
    adv = protocol.parse_manufacturer_data(ad_bytes())
    assert protocol.describe(adv) == (
        "manufacturer=0x5053 screen=0x134C firmware=v167(raw=0xA7) flags=0x03 "
        "secure=yes pairing=open recovery=no turbo=no"
    )


def test_old_firmware_dimensions() -> None:
    adv = protocol.parse_manufacturer_data(ad_bytes(firmware=0x90, flags=0x00))
    assert adv is not None
    assert not adv.requires_encrypted_gatt
    assert not adv.verified_transfer
    assert not adv.pairing_window_open
    assert adv.pairing_hold_seconds == 10


def test_ota_and_turbo_flags() -> None:
    adv = protocol.parse_manufacturer_data(ad_bytes(flags=0x1B))
    assert adv is not None
    assert adv.ota_recovery_mode
    assert adv.turbo_link
    assert adv.pairing_window_open


def test_rejects_short_or_foreign_data() -> None:
    assert protocol.parse_manufacturer_data(None) is None
    assert protocol.parse_manufacturer_data(b"\x53\x50") is None
    adv = protocol.parse_manufacturer_data(ad_bytes().replace(b"\x53\x50", b"\x34\x12", 1))
    assert adv is not None
    assert not adv.compatible


def test_device_ids() -> None:
    assert protocol.is_device_id("6f1c9a4e-3d8b-4f2a-9e11-0a1b2c3d4e5f")
    assert protocol.is_device_id("AA:BB:CC:DD:EE:FF")
    assert protocol.is_device_id("aa-bb-cc-dd-ee-ff")
    assert protocol.is_device_id("A4C138FFEE01")
    assert not protocol.is_device_id("not-a-device")
    assert not protocol.is_device_id("AA:BB:CC")


def test_diagnostics_round_trip() -> None:
    data = bytes(
        [
            0x27,
            0x01,
            7,
            0x07,
            10,
            0,
            11,
            0,
            9,
            0,
            2,
            0,
            0x40,
            0x01,
            0,
            0,
            0x02,
            0x00,
            0x3F,
            0x01,
        ]
    )
    text = protocol.ImageAckDiagnostics.parse(data)
    assert text is not None
    assert text.generated_count == 10
    assert text.notify_attempt_count == 11
    assert text.notify_rejected_count == 2
    assert text.requested_block == 320
    assert text.last_received_block == 319
    assert "requestedBlock=320" in text.describe()
    assert protocol.ImageAckDiagnostics.parse(b"\x00" * 20) is None


def test_error_messages() -> None:
    assert "out-of-order" in protocol.transfer_error_message(0x01)
    assert "Flash write" in protocol.transfer_error_message(0x09)
    assert "readback" in protocol.transfer_error_message(0x0A)
    assert "0x7F" in protocol.transfer_error_message(0x7F)
