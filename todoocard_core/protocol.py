"""TodooCard BLE protocol constants, advertisement parsing and diagnostics."""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum

MANUFACTURER_ID = 0x5053
SCREEN_T3 = 0x134C

SERVICE_UUIDS = (
    "0000fef0-0000-1000-8000-00805f9b34fb",
    "0000fdf0-0000-1000-8000-00805f9b34fb",
)
CONTROL_UUIDS = (
    "0000fef1-0000-1000-8000-00805f9b34fb",
    "0000fdf1-0000-1000-8000-00805f9b34fb",
)
DATA_UUIDS = (
    "0000fef2-0000-1000-8000-00805f9b34fb",
    "0000fdf2-0000-1000-8000-00805f9b34fb",
)
BATTERY_SERVICE_UUID = "0000180f-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL_UUID = "00002a19-0000-1000-8000-00805f9b34fb"
DIAGNOSTICS_SERVICE_UUID = "d8b282f0-0f9b-4a7a-b5e8-8e6e5a3c1001"
DIAGNOSTICS_CHAR_UUID = "d8b282f0-0f9b-4a7a-b5e8-8e6e5a3c1003"

VERIFIED_TRANSFER_MIN_VERSION = 0x9D  # v157
DIAGNOSTICS_MIN_VERSION = 0xA5  # v165
VERIFIED_WINDOW_BLOCKS = 32
VERIFIED_CHECKPOINT_TIMEOUT = 5.0
FINAL_REFRESH_ACK_TIMEOUT = 10.0
MAX_REPAIR_REQUESTS = 16
LEGACY_FINAL_ACK_TIMEOUT = 90.0

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)
_MAC_RE = re.compile(r"^[0-9a-f]{2}([:-][0-9a-f]{2}){5}$", re.I)
_LEGACY_RE = re.compile(r"^[0-9a-f]{12,16}$", re.I)


def is_device_id(value: str) -> bool:
    """Accept macOS CBUUID, Windows MAC address, or a bare hex device id."""
    v = (value or "").strip()
    return bool(_UUID_RE.match(v) or _MAC_RE.match(v) or _LEGACY_RE.match(v))


class Command(IntEnum):
    REQUEST_BLOCK_SIZE = 0x01
    REQUEST_SCREEN_SIZE = 0x02
    REQUEST_START_TRANSFER = 0x03
    DATA_ACK = 0x05


class Status(IntEnum):
    SUCCESS = 0x00
    TRANSFER_END = 0x08


@dataclass(frozen=True)
class Advertisement:
    manufacturer_id: int
    screen_type: int
    capability_flags: int
    firmware_version: int

    @property
    def compatible(self) -> bool:
        return self.manufacturer_id == MANUFACTURER_ID and self.screen_type == SCREEN_T3

    @property
    def requires_encrypted_gatt(self) -> bool:
        return (
            self.screen_type == SCREEN_T3
            and self.firmware_version >= 0x8C
            and bool(self.capability_flags & 0x01)
        )

    @property
    def pairing_window_open(self) -> bool:
        return self.requires_encrypted_gatt and bool(self.capability_flags & 0x02)

    @property
    def ota_recovery_mode(self) -> bool:
        return bool(self.capability_flags & 0x08)

    @property
    def turbo_link(self) -> bool:
        return bool(self.capability_flags & 0x10)

    @property
    def verified_transfer(self) -> bool:
        return self.firmware_version >= VERIFIED_TRANSFER_MIN_VERSION

    @property
    def pairing_hold_seconds(self) -> int:
        return 5 if self.firmware_version >= 0x98 else 10

    @property
    def diagnostics_available(self) -> bool:
        return self.firmware_version >= DIAGNOSTICS_MIN_VERSION


def parse_manufacturer_data(data: bytes | bytearray | None) -> Advertisement | None:
    if data is None or len(data) < 7:
        return None
    return Advertisement(
        manufacturer_id=data[0] | (data[1] << 8),
        screen_type=data[2] | (data[6] << 8),
        capability_flags=data[3],
        firmware_version=data[4],
    )


def describe(ad: Advertisement | None) -> str:
    if ad is None:
        return "manufacturer=unknown"
    if ad.manufacturer_id != MANUFACTURER_ID:
        return f"manufacturer=0x{ad.manufacturer_id:04X}"
    yes = lambda b: "yes" if b else "no"  # noqa: E731
    return (
        f"manufacturer=0x{ad.manufacturer_id:04X} screen=0x{ad.screen_type:04X} "
        f"firmware=v{ad.firmware_version}(raw=0x{ad.firmware_version:02X}) "
        f"flags=0x{ad.capability_flags:02X} secure={yes(ad.requires_encrypted_gatt)} "
        f"pairing={'open' if ad.pairing_window_open else 'closed'} "
        f"recovery={yes(ad.ota_recovery_mode)} turbo={yes(ad.turbo_link)}"
    )


@dataclass(frozen=True)
class ImageAckDiagnostics:
    session: int
    flags: int
    generated_count: int
    notify_attempt_count: int
    notify_accepted_count: int
    notify_rejected_count: int
    requested_block: int
    previous_block_status: int
    last_notify_result: int
    last_received_block: int

    @classmethod
    def parse(cls, data: bytes | bytearray) -> "ImageAckDiagnostics | None":
        if len(data) < 20 or data[0] != 0x27 or data[1] != 0x01:
            return None

        def u16(offset: int) -> int:
            return data[offset] | (data[offset + 1] << 8)

        requested = (
            data[12] | (data[13] << 8) | (data[14] << 16) | (data[15] << 24)
        )
        return cls(
            session=data[2],
            flags=data[3],
            generated_count=u16(4),
            notify_attempt_count=u16(6),
            notify_accepted_count=u16(8),
            notify_rejected_count=u16(10),
            requested_block=requested,
            previous_block_status=data[16],
            last_notify_result=data[17],
            last_received_block=u16(18),
        )

    def describe(self) -> str:
        flags = self.flags
        yes = lambda bit: "yes" if flags & bit else "no"  # noqa: E731
        return (
            f"session={self.session} active={yes(0x01)} pending={yes(0x02)} "
            f"blockReceived={yes(0x04)} lastAccepted={yes(0x08)} "
            f"generated={self.generated_count} attempts={self.notify_attempt_count} "
            f"accepted={self.notify_accepted_count} rejected={self.notify_rejected_count} "
            f"requestedBlock={self.requested_block} "
            f"previousStatus=0x{self.previous_block_status:02X} "
            f"lastNotifyResult=0x{self.last_notify_result:02X} "
            f"lastReceivedBlock={self.last_received_block}"
        )


def transfer_error_message(status: int) -> str:
    if status == 0x01:
        return "Device rejected a malformed or out-of-order image block (status 0x01)"
    if status == 0x09:
        return "External Flash write failed on the device (status 0x09)"
    if status == 0x0A:
        return "External Flash readback verification failed on the device (status 0x0A)"
    return f"Device interrupted image transfer with status 0x{status:02X}"
