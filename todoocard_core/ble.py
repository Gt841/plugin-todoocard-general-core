"""Platform-aware BLE transport built on bleak.

macOS  : CoreBluetooth through bleak. Pairing is triggered by reading the
         encrypted Battery Level characteristic; macOS creates the system bond.
Windows: WinRT through bleak. ``BleakClient.pair()`` creates the system bond;
         encrypted characteristics require an existing bond.
Linux  : BlueZ through bleak. Pairing must be done with ``bluetoothctl`` or a
         BlueZ agent before image transfer.
"""
from __future__ import annotations

import asyncio
import platform
import sys
import time
from dataclasses import dataclass, field
from typing import Callable

from . import protocol

LogFn = Callable[[str], None]


class BleError(RuntimeError):
    def __init__(self, message: str, exit_code: int = 3):
        super().__init__(message)
        self.exit_code = exit_code


@dataclass
class Discovered:
    device: object
    address: str
    name: str
    rssi: int | None
    services: list[str] = field(default_factory=list)
    adv: protocol.Advertisement | None = None
    manufacturer_data: bytes | None = None

    def line(self) -> str:
        name = self.name or "(no name)"
        services = ",".join(self.services)
        rssi = "" if self.rssi is None else str(self.rssi)
        return (
            f"BLE {name} id={self.address} RSSI={rssi} services={services} "
            f"{protocol.describe(self.adv)}"
        )


def _platform() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        return "windows"
    return "linux"


def canonical_id(value: str) -> str:
    return "".join(ch for ch in (value or "").upper() if ch.isalnum())


def _matches(address: str, wanted: str) -> bool:
    wanted_parts = {canonical_id(p) for p in wanted.replace(";", ",").split(",") if p.strip()}
    return canonical_id(address) in wanted_parts


def _with_company_id(value: bytes) -> bytes:
    """bleak 的 manufacturer_data 已按公司 ID 建键，值里不含 2 字节公司 ID。

    协议解析需要完整 AD 结构，因此这里补回 little-endian 公司 ID；若值本身已经
    带公司 ID（部分平台或测试注入），则原样保留。
    """
    if len(value) >= 2 and (value[0] | (value[1] << 8)) == protocol.MANUFACTURER_ID:
        return value
    return protocol.MANUFACTURER_ID.to_bytes(2, "little") + value


def _manufacturer_bytes(adv) -> bytes | None:
    data = getattr(adv, "manufacturer_data", None) or {}
    if not data:
        return None
    raw = data.get(protocol.MANUFACTURER_ID)
    if raw is not None:
        return _with_company_id(bytes(raw))
    # 少数协议栈把数据放在别的键下；只在补回公司 ID 后能解析成 TodooCard 时采用。
    for key, candidate in data.items():
        try:
            full = int(key).to_bytes(2, "little") + bytes(candidate)
        except (TypeError, ValueError, OverflowError):
            continue
        parsed = protocol.parse_manufacturer_data(full)
        if parsed and parsed.compatible:
            return full
    return None


def _to_discovered(device, adv) -> Discovered:
    raw = _manufacturer_bytes(adv)
    services = list(getattr(adv, "service_uuids", None) or [])
    rssi = getattr(adv, "rssi", None)
    if rssi is None:
        rssi = getattr(device, "rssi", None)
    name = getattr(adv, "local_name", None) or getattr(device, "name", None) or ""
    return Discovered(
        device=device,
        address=str(getattr(device, "address", "") or ""),
        name=str(name or ""),
        rssi=int(rssi) if rssi is not None else None,
        services=services,
        adv=protocol.parse_manufacturer_data(raw),
        manufacturer_data=raw,
    )


async def scan_once(seconds: float) -> list[Discovered]:
    import bleak

    found = await bleak.BleakScanner.discover(timeout=max(1.0, seconds), return_adv=True)
    out: list[Discovered] = []
    for _address, pair in found.items():
        device, adv = pair
        out.append(_to_discovered(device, adv))
    out.sort(key=lambda d: (d.rssi is None, -(d.rssi or -999)))
    return out


async def wait_for_target(
    device_id: str,
    *,
    timeout: float,
    log: LogFn,
    pair_mode: bool = False,
    for_send: bool = False,
) -> Discovered:
    """Scan in rounds until the exact target advertises, applying safety gates."""
    import bleak

    deadline = time.monotonic() + max(4.0, timeout)
    hinted = False
    while time.monotonic() < deadline:
        round_seconds = min(5.0, max(2.0, deadline - time.monotonic()))
        try:
            devices = await scan_once(round_seconds)
        except Exception as exc:
            log(f"[ble] scan error: {exc}")
            devices = []
        for item in devices:
            if not _matches(item.address, device_id):
                continue
            if item.adv is None:
                raise BleError("TodooCard manufacturer data is missing", 20)
            if not item.adv.compatible:
                raise BleError(
                    f"TodooCard identity mismatch: {protocol.describe(item.adv)}", 20
                )
            if pair_mode:
                if not item.adv.requires_encrypted_gatt:
                    raise BleError(
                        "Target firmware does not use the latest secure pairing protocol", 21
                    )
                if not item.adv.pairing_window_open:
                    if not hinted:
                        hinted = True
                        log(
                            "Target found, but pairing=closed; hold the rear button for "
                            f"{item.adv.pairing_hold_seconds} seconds until the light fast-flashes."
                        )
                    continue
                log("Physical pairing window detected; starting encrypted system pairing.")
                return item
            if item.adv.pairing_window_open:
                raise BleError(
                    "Refusing probe/transfer while the new-device pairing window is open; "
                    "use --pair for a new computer or wait for the window to close.",
                    26,
                )
            if for_send and item.adv.ota_recovery_mode:
                raise BleError(
                    "Refusing image transfer: the device advertises OTA-only recovery mode. "
                    "Repair the approved firmware first.",
                    28,
                )
            return item
        left = int(deadline - time.monotonic())
        if left > 0:
            log(f"[ble] target not seen yet, scanning again ({left}s left)")
    if pair_mode:
        raise BleError("Timed out waiting for the physical pairing window", 14)
    raise BleError("Timed out scanning for the exact target device", 14)


async def connect(item: Discovered, *, log: LogFn, timeout: float = 15.0, attempts: int = 3):
    from bleak import BleakClient

    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        client = None
        try:
            client = BleakClient(item.device, timeout=timeout)
            await client.connect()
            # Touch the service cache immediately: a link that is already gone
            # raises here instead of later during notify/write.
            _ = list(client.services)
            log(f"Connected (attempt {attempt}/{attempts})")
            return client
        except Exception as exc:
            last = exc
            log(f"Connect failed (attempt {attempt}/{attempts}): {exc}")
            try:
                if client is not None:
                    await client.disconnect()
            except Exception:
                pass
            if attempt < attempts:
                await asyncio.sleep(0.75 * attempt)
    raise BleError(f"Connect failed: {last}", 3)


async def _battery_char(client):
    for service in client.services:
        if service.uuid.lower() == protocol.BATTERY_SERVICE_UUID:
            for char in service.characteristics:
                if char.uuid.lower() == protocol.BATTERY_LEVEL_UUID:
                    return char
    return None


async def read_battery(client, *, timeout: float = 6.0) -> int | None:
    char = await _battery_char(client)
    if char is None:
        return None
    value = await asyncio.wait_for(client.read_gatt_char(char), timeout=timeout)
    if not value:
        return None
    return int(value[0])


async def pair(client, item: Discovered, *, log: LogFn) -> None:
    """Create the system bond. Must only be called during an open pairing window."""
    plat = _platform()
    if plat == "windows":
        pair_method = getattr(client, "pair", None)
        if pair_method is None:
            raise BleError("This bleak version has no Windows pairing API", 18)
        log("Requesting Windows system pairing ...")
        await pair_method()
        log("Windows pairing completed.")
    try:
        battery = await read_battery(client)
    except Exception as exc:
        raise BleError(f"Encrypted pairing verification failed: {exc}", 18) from exc
    if battery is None:
        raise BleError("Encrypted Battery Level is missing; cannot verify the secure bond", 18)
    log(f"Encrypted link verified; battery={battery}%")
    log("Pairing completed. The system bond is ready for a separate --probe.")


async def verify_bond(client, item: Discovered, *, log: LogFn) -> None:
    if item.adv is None or not item.adv.requires_encrypted_gatt:
        return
    try:
        battery = await read_battery(client)
    except Exception as exc:
        raise BleError(
            "The secure bond is unavailable. Run --pair during a new physical pairing window, "
            f"then probe again. ({exc})",
            18,
        ) from exc
    if battery is None:
        raise BleError(
            "Encrypted Battery Level is missing; cannot verify the secure bond", 18
        )
    log(f"Encrypted link verified; battery={battery}%")


async def find_transfer_chars(client) -> tuple[object | None, object | None, object | None]:
    control = None
    data = None
    diagnostics = None
    for service in client.services:
        su = service.uuid.lower()
        for char in service.characteristics:
            cu = char.uuid.lower()
            if cu in protocol.CONTROL_UUIDS:
                control = char
            elif cu in protocol.DATA_UUIDS:
                data = char
            elif cu == protocol.DIAGNOSTICS_CHAR_UUID:
                diagnostics = char
        if su == protocol.DIAGNOSTICS_SERVICE_UUID:
            for char in service.characteristics:
                if char.uuid.lower() == protocol.DIAGNOSTICS_CHAR_UUID:
                    diagnostics = char
    return control, data, diagnostics


async def read_diagnostics(client, char, *, log: LogFn) -> None:
    if char is None:
        return
    try:
        value = await asyncio.wait_for(client.read_gatt_char(char), timeout=4.0)
    except Exception as exc:
        log(f"Image-ACK diagnostics read failed: {exc}")
        return
    parsed = protocol.ImageAckDiagnostics.parse(value)
    if parsed:
        log(f"Image-ACK diagnostics: {parsed.describe()}")
    else:
        hexed = " ".join(f"{b:02x}" for b in value) if value else "empty"
        log(f"Image-ACK diagnostics were malformed: {hexed}")


def is_encryption_error(exc: BaseException) -> bool:
    text = repr(exc).lower()
    markers = (
        "encryption",
        "authentication",
        "insufficient",
        "not paired",
        "peer removed pairing",
        "0x8007",
        "cbattlasterrorcode=15",
        "cbattlasterrorcode 15",
    )
    return any(marker in text for marker in markers)


def platform_summary() -> str:
    return f"{platform.system()} {platform.release()} / Python {platform.python_version()}"
