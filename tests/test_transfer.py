from __future__ import annotations

import asyncio

import pytest

from todoocard_core import protocol
from todoocard_core.transfer import Transfer, TransferError, TransferOptions


class FakeChar:
    def __init__(self, uuid: str, properties: list[str]):
        self.uuid = uuid
        self.properties = properties


class FakeDevice:
    """Minimal device model: handshake, checkpoints and the final 0x05/0x08."""

    def __init__(
        self,
        *,
        total_blocks: int,
        block_size: int = 12,
        ack_every: int = 32,
        reject_block_size: bool = False,
        send_final_ack: bool = True,
    ):
        self.total_blocks = total_blocks
        self.block_size = block_size
        self.ack_every = ack_every
        self.reject_block_size = reject_block_size
        self.send_final_ack = send_final_ack
        self.control = FakeChar(protocol.CONTROL_UUIDS[0], ["write", "notify"])
        self.data = FakeChar(protocol.DATA_UUIDS[0], ["write-without-response", "write"])
        self.callback = None
        self.received: list[int] = []
        self.payload = b""
        self.flags = 0
        self.disconnected_cb = None

    def set_disconnected_callback(self, cb) -> None:
        self.disconnected_cb = cb

    async def start_notify(self, char, cb) -> None:
        self.callback = cb

    async def stop_notify(self, char) -> None:
        self.callback = None

    async def disconnect(self) -> None:
        pass

    def _notify(self, data: bytes) -> None:
        assert self.callback is not None
        self.callback(self.control, bytearray(data))

    async def write_gatt_char(self, char, data, response: bool = False) -> None:
        data = bytes(data)
        if char is self.control:
            command = data[0]
            if command == protocol.Command.REQUEST_BLOCK_SIZE:
                if self.reject_block_size:
                    self._notify(bytes([1, self.block_size, 1]))
                else:
                    self._notify(bytes([1, self.block_size, 0]))
            elif command == protocol.Command.REQUEST_SCREEN_SIZE:
                self.payload_len = int.from_bytes(data[1:5], "little")
                self.flags = data[5]
                self._notify(bytes([2, 0]))
            elif command == protocol.Command.REQUEST_START_TRANSFER:
                self._notify(b"\x05\x00" + (0).to_bytes(4, "little"))
            return

        index = int.from_bytes(data[:4], "little")
        self.received.append(index)
        done = index + 1
        should_ack = (
            self.ack_every == 1
            or done % self.ack_every == 0
            or done == self.total_blocks
        )
        if not should_ack:
            return
        if done == self.total_blocks and self.send_final_ack:
            self._notify(b"\x05\x08")
        else:
            self._notify(b"\x05\x00" + done.to_bytes(4, "little"))


class FakeItem:
    def __init__(self, firmware: int):
        self.address = "AA:BB:CC:DD:EE:FF"
        self.name = "TodooCard"
        self.adv = protocol.Advertisement(
            manufacturer_id=protocol.MANUFACTURER_ID,
            screen_type=protocol.SCREEN_T3,
            capability_flags=0x01,
            firmware_version=firmware,
        )


@pytest.fixture(autouse=True)
def fast_timeouts(monkeypatch):
    monkeypatch.setattr(protocol, "VERIFIED_CHECKPOINT_TIMEOUT", 0.3)
    monkeypatch.setattr(protocol, "FINAL_REFRESH_ACK_TIMEOUT", 0.3)
    monkeypatch.setattr(protocol, "LEGACY_FINAL_ACK_TIMEOUT", 0.3)


def run(device: FakeDevice, item: FakeItem, *, compressed: bool):
    logs: list[str] = []
    per_block = device.block_size - 4
    payload = bytes(i % 256 for i in range(per_block * device.total_blocks))
    options = TransferOptions(compressed=compressed, payload=payload)
    transfer = Transfer(
        device,
        item,
        device.control,
        device.data,
        None,
        options,
        log=logs.append,
    )
    asyncio.run(transfer.run())
    return logs, device


def test_verified_transfer_with_window_checkpoints() -> None:
    device = FakeDevice(total_blocks=64, ack_every=32)
    logs, device = run(device, FakeItem(0xA7), compressed=True)
    assert device.received == list(range(64))
    assert device.payload_len == 512
    assert device.flags == 0x01
    assert "Transfer completed and refresh acknowledged by 0x05/0x08" in logs
    assert any(line.startswith("Verified window: [0, 32)") for line in logs)
    assert any(line.startswith("Verified window: [32, 64)") for line in logs)


def test_verified_final_window_accepts_early_end_ack() -> None:
    device = FakeDevice(total_blocks=40, ack_every=32)
    logs, _ = run(device, FakeItem(0xA7), compressed=True)
    assert "Transfer completed and refresh acknowledged by 0x05/0x08" in logs


def test_legacy_transfer_per_block_ack() -> None:
    device = FakeDevice(total_blocks=3, ack_every=1)
    logs, device = run(device, FakeItem(0x90), compressed=False)
    assert device.received == [0, 1, 2]
    assert device.flags == 0x03
    assert "Transfer completed and refresh acknowledged by 0x05/0x08" in logs


def test_rejected_block_size_fails_cleanly() -> None:
    device = FakeDevice(total_blocks=2, reject_block_size=True)
    with pytest.raises(TransferError, match="rejected"):
        run(device, FakeItem(0xA7), compressed=True)


def test_missing_refresh_ack_is_a_failure() -> None:
    device = FakeDevice(
        total_blocks=2, block_size=8, ack_every=1, send_final_ack=False
    )
    with pytest.raises(TransferError, match="refresh acknowledgement"):
        run(device, FakeItem(0xA7), compressed=True)
