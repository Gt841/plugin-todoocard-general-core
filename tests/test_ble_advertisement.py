from __future__ import annotations

from types import SimpleNamespace

from todoocard_core import ble, protocol


def ad_bytes(firmware: int = 0xB9, flags: int = 0x11, screen: int = 0x134C) -> bytes:
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


def test_reconstructs_company_id_stripped_by_bleak() -> None:
    payload = ad_bytes()[2:]
    discovered = ble._to_discovered(
        SimpleNamespace(address="519D2FA1-CB32-3C08-C409-EB8EB66DAAE1", name="TodooCard", rssi=-61),
        SimpleNamespace(
            manufacturer_data={protocol.MANUFACTURER_ID: payload},
            service_uuids=["0000fef0-0000-1000-8000-00805f9b34fb"],
            rssi=-61,
            local_name="FFFF99803616",
        ),
    )
    assert discovered.manufacturer_data == ad_bytes()
    assert discovered.adv is not None
    assert discovered.adv.compatible
    assert discovered.adv.screen_type == protocol.SCREEN_T3
    assert "manufacturer=0x5053" in discovered.line()


def test_accepts_value_that_already_has_company_id() -> None:
    discovered = ble._to_discovered(
        SimpleNamespace(address="AA:BB:CC:DD:EE:FF", name="", rssi=-70),
        SimpleNamespace(manufacturer_data={protocol.MANUFACTURER_ID: ad_bytes()}, service_uuids=[]),
    )
    assert discovered.manufacturer_data == ad_bytes()
    assert discovered.adv is not None


def test_other_keys_require_compatible_layout() -> None:
    discovered = ble._to_discovered(
        SimpleNamespace(address="AA:BB:CC:DD:EE:FF", name="", rssi=-70),
        SimpleNamespace(manufacturer_data={0x1234: ad_bytes()[2:]}, service_uuids=[]),
    )
    assert discovered.manufacturer_data is None
    assert "manufacturer=unknown" in discovered.line()


def test_ignores_unrelated_manufacturer_data() -> None:
    discovered = ble._to_discovered(
        SimpleNamespace(address="AA:BB:CC:DD:EE:FF", name="", rssi=-70),
        SimpleNamespace(manufacturer_data={0x0901: b"\x01\x02"}, service_uuids=[]),
    )
    assert discovered.manufacturer_data is None
    assert "manufacturer=unknown" in discovered.line()
