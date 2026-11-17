"""Firmware-aware image transfer state machine.

This is a Python port of the verified transfer contract implemented by the
Swift ``mac_image_transfer`` tool:

* v157+ : independent 2 KB QuickLZ blocks, 32-block application windows,
          durable Flash checkpoints, resume/repair from the reported block.
* pre-v157 : legacy 64-byte stored QuickLZ stream with per-block ACKs.
* v165+ : retained image-ACK diagnostics on failure.

Transport differences are confined to :mod:`todoocard_core.ble`.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Callable

from . import protocol

LogFn = Callable[[str], None]


class TransferError(RuntimeError):
    def __init__(self, message: str, exit_code: int = 29):
        super().__init__(message)
        self.exit_code = exit_code


class HandshakeError(TransferError):
    """Control write/notification never settled; a fresh connection may help."""


@dataclass
class TransferOptions:
    compressed: bool = True
    allow_device_button: bool = False
    payload: bytes = b""
    legacy_payload: bytes = b""
    raw_payload: bytes = b""


def _props(char) -> set[str]:
    raw = getattr(char, "properties", None) or []
    return {str(p).lower().replace("_", "-") for p in raw}


class Transfer:
    def __init__(
        self,
        client,
        item,
        control,
        data,
        diagnostics,
        options: TransferOptions,
        *,
        log: LogFn,
    ):
        self.client = client
        self.item = item
        self.adv = item.adv
        self.control = control
        self.data = data
        self.diagnostics = diagnostics
        self.options = options
        self.log = log

        self.payload = options.payload
        self.block_size = 0
        self.total_blocks = 0
        self.ack_queue: asyncio.Queue[int] = asyncio.Queue()
        self.ended = asyncio.Event()
        self.disconnected = asyncio.Event()
        self.error: str | None = None
        self.screen_acked = False
        self.last_progress = -1
        self.writes_without_response = False
        self._last_ack_at = time.monotonic()

    # ── control notifications ───────────────────────────────────────────

    def _on_control(self, _char, data: bytearray) -> None:
        raw = bytes(data)
        self.log("Control <- " + " ".join(f"{b:02x}" for b in raw))
        if not raw:
            return
        cmd = raw[0]
        if cmd == protocol.Command.REQUEST_BLOCK_SIZE:
            if len(raw) >= 3 and raw[2] == protocol.Status.SUCCESS:
                self.block_size = max(0, int(raw[1]) - 4)
                if self.block_size <= 0:
                    self.error = f"Device returned an invalid block size: {raw[1]}"
            else:
                self.error = "Block size request was rejected"
        elif cmd == protocol.Command.REQUEST_SCREEN_SIZE:
            if len(raw) >= 2 and raw[1] != protocol.Status.SUCCESS:
                self.error = "Payload length was rejected"
            else:
                self.screen_acked = True
        elif cmd == protocol.Command.DATA_ACK:
            if len(raw) >= 2 and raw[1] == protocol.Status.TRANSFER_END:
                self.ended.set()
                return
            if len(raw) >= 2 and raw[1] != protocol.Status.SUCCESS:
                self.error = protocol.transfer_error_message(raw[1])
                return
            if len(raw) >= 6:
                index = int.from_bytes(raw[2:6], "little")
                self.ack_queue.put_nowait(index)
                self._last_ack_at = time.monotonic()
        else:
            pass

    def _on_disconnect(self, _client) -> None:
        self.disconnected.set()

    # ── helpers ─────────────────────────────────────────────────────────

    def _emit(self, percent: int, index: int) -> None:
        if percent <= self.last_progress:
            return
        if percent >= 100 or percent % 5 == 0:
            self.last_progress = percent
            self.log(f"Progress: {percent}% block={index}")

    def _write_timeout(self) -> float:
        return 8.0

    async def _write(self, char, payload: bytes, *, response: bool) -> None:
        if self.disconnected.is_set() or not self._is_connected():
            raise TransferError("Device disconnected during transfer", 29)
        try:
            await asyncio.wait_for(
                self.client.write_gatt_char(char, payload, response=response),
                timeout=self._write_timeout(),
            )
        except asyncio.TimeoutError as exc:
            raise TransferError(
                f"Write timed out for {char.uuid} (response={response})", 17
            ) from exc

    async def _write_control(self, payload: bytes) -> None:
        props = _props(self.control)
        supports_response = "write" in props
        supports_no_response = "write-without-response" in props
        response = supports_response
        self.log("Control -> " + " ".join(f"{b:02x}" for b in payload))
        try:
            await asyncio.wait_for(
                self.client.write_gatt_char(self.control, payload, response=response),
                timeout=1.2,
            )
            return
        except asyncio.TimeoutError:
            if not (response and supports_no_response):
                raise HandshakeError("Control response timed out", 16)
            self.log("Control response timed out; retrying withoutResponse")
        except Exception as exc:
            text = str(exc).lower()
            if not (response and supports_no_response) or not any(
                k in text for k in ("not support", "not permitted", "rejected", "invalid")
            ):
                raise HandshakeError(f"Control write failed: {exc}", 16)
            self.log(f"Control write rejected; retrying withoutResponse ({exc})")
        await asyncio.wait_for(
            self.client.write_gatt_char(self.control, payload, response=False),
            timeout=2.5,
        )

    def _is_connected(self) -> bool:
        value = getattr(self.client, "is_connected", True)
        try:
            return bool(value)
        except Exception:
            return True

    async def _wait_for(self, pred, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.error:
                raise TransferError(self.error, 11)
            if self.disconnected.is_set() or not self._is_connected():
                raise TransferError("Device disconnected during transfer", 29)
            if pred():
                return True
            await asyncio.sleep(0.05)
        return False

    async def _next_ack(self, timeout: float) -> int | None:
        deadline = time.monotonic() + timeout
        while True:
            if self.error:
                raise TransferError(self.error, 11)
            if self.disconnected.is_set() or not self._is_connected():
                raise TransferError("Device disconnected during transfer", 29)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                return await asyncio.wait_for(self.ack_queue.get(), remaining)
            except asyncio.TimeoutError:
                return None

    def _drain_acks(self) -> None:
        while not self.ack_queue.empty():
            try:
                self.ack_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    # ── session ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        # bleak 0.22 exposes set_disconnected_callback; bleak 1.x removed it and
        # only accepts a constructor callback. Fall back to is_connected polling.
        register = getattr(self.client, "set_disconnected_callback", None)
        if callable(register):
            try:
                register(self._on_disconnect)
            except Exception as exc:
                self.log(f"Disconnect callback unavailable: {exc}")

        await self.client.start_notify(self.control, self._on_control)
        self.log("Control notification enabled; settling for 400ms")
        await asyncio.sleep(0.4)

        self.log(f"Characteristics ready: control={self.control.uuid}, data={self.data.uuid}")
        self.log(f"Control props: {', '.join(sorted(_props(self.control))) or 'none'}")
        self.log(f"Data props: {', '.join(sorted(_props(self.data))) or 'none'}")

        if not await self._negotiate_block_size():
            raise HandshakeError("Could not negotiate a payload block size", 16)

        if self.options.compressed:
            remainder = len(self.payload) % self.block_size
            if remainder:
                pad = self.block_size - remainder
                self.payload += b"\x00" * pad
                self.log(
                    f"Compressed payload padded by {pad} bytes to {len(self.payload)} "
                    f"for {self.block_size}-byte blocks"
                )
        self.total_blocks = (
            len(self.payload) + self.block_size - 1
        ) // self.block_size if self.block_size else 0
        self.log(f"Block payload size: {self.block_size}")
        self.log(f"Total blocks: {self.total_blocks}")

        if not await self._announce_payload():
            raise TransferError("Payload length was rejected", 10)

        await self._write_control(bytes([protocol.Command.REQUEST_START_TRANSFER]))
        self.log("Transfer started; waiting for the first checkpoint")
        initial = await self._next_ack(10.0)
        if initial is None:
            raise TransferError(
                "Device did not request an initial block within 10s after "
                "REQUEST_START_TRANSFER",
                11,
            )
        self._drain_acks()

        if self.adv and self.adv.verified_transfer:
            await self._verified_transfer(initial)
        else:
            await self._legacy_transfer(initial)

    async def _negotiate_block_size(self) -> bool:
        for round_index in range(2):
            self.block_size = 0
            self.error = None
            try:
                await self._write_control(bytes([protocol.Command.REQUEST_BLOCK_SIZE]))
            except HandshakeError as exc:
                self.log(str(exc))
            if await self._wait_for(lambda: self.block_size > 0, 2.5):
                return True
            self.log(
                "Block size response timed out; retrying control command "
                f"({round_index + 1}/2)"
            )
        return False

    async def _announce_payload(self) -> bool:
        length = len(self.payload)
        flags = 0x01 if self.options.compressed else 0x03
        if self.options.allow_device_button:
            flags |= 0x10
        msg = bytes(
            [
                protocol.Command.REQUEST_SCREEN_SIZE,
                length & 0xFF,
                (length >> 8) & 0xFF,
                (length >> 16) & 0xFF,
                (length >> 24) & 0xFF,
                flags,
            ]
        )
        await self._write_control(msg)
        return await self._wait_for(lambda: self.screen_acked, 5.0)

    def _packet(self, index: int) -> tuple[bytes, int, bool]:
        offset = index * self.block_size
        chunk = self.payload[offset : offset + self.block_size]
        packet = index.to_bytes(4, "little") + chunk
        written = offset + len(chunk)
        percent = int(written * 100 / max(1, len(self.payload)))
        return packet, percent, written >= len(self.payload)

    async def _write_window(self, start: int, end: int) -> None:
        props = _props(self.data)
        if not props:
            props = {"write"}
        supports_response = "write" in props
        supports_no_response = "write-without-response" in props or "write-no-response" in props
        if not supports_response and not supports_no_response:
            raise TransferError("Data characteristic supports neither write mode", 12)

        for index in range(start, end):
            if self.error:
                raise TransferError(self.error, 11)
            packet, percent, _ = self._packet(index)
            barrier = index + 1 >= end
            if supports_response and (barrier or not supports_no_response):
                try:
                    await self._write(self.data, packet, response=True)
                except TransferError as exc:
                    if "timed out" not in str(exc).lower() or not supports_no_response:
                        raise
                    self.log("Barrier write timed out; falling back to withoutResponse")
                    await self._write(self.data, packet, response=False)
            else:
                await self._write(self.data, packet, response=False)
                await asyncio.sleep(0.003)
            self._emit(percent, index)

    async def _await_checkpoint(self, end: int, timeout: float) -> int | None:
        """Collect progress ACKs until the device reaches ``end`` or goes quiet."""
        latest: int | None = None
        deadline = time.monotonic() + timeout
        while True:
            if self.ended.is_set():
                return max(end, latest or 0)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return latest
            ack = await self._next_ack(remaining)
            if ack is None:
                return latest
            latest = ack if latest is None else max(latest, ack)
            if latest >= end:
                return latest

    async def _verified_transfer(self, index: int) -> None:
        repairs = 0
        while True:
            if index > self.total_blocks:
                raise TransferError(
                    f"Device returned invalid resume block {index}; "
                    f"total blocks={self.total_blocks}",
                    11,
                )
            if index == self.total_blocks:
                self.log(
                    f"Device checkpoint covers all {self.total_blocks} blocks; "
                    "waiting for refresh acknowledgement"
                )
                await self._await_refresh(protocol.FINAL_REFRESH_ACK_TIMEOUT)
                return

            self._drain_acks()
            start = index
            end = min(((index // protocol.VERIFIED_WINDOW_BLOCKS) + 1) * protocol.VERIFIED_WINDOW_BLOCKS,
                      self.total_blocks)
            self.log(f"Verified window: [{start}, {end}) of {self.total_blocks} blocks")
            await self._write_window(start, end)

            ack = await self._await_checkpoint(end, protocol.VERIFIED_CHECKPOINT_TIMEOUT)
            if ack is None:
                repairs += 1
                if repairs > protocol.MAX_REPAIR_REQUESTS:
                    raise TransferError(
                        f"Timed out waiting for the durable checkpoint after "
                        f"{protocol.MAX_REPAIR_REQUESTS} probes",
                        30,
                    )
                probe_index = max(end - 1, start)
                self.log(
                    f"Checkpoint timeout; re-sending window-end block {probe_index} "
                    f"to request device progress ({repairs}/{protocol.MAX_REPAIR_REQUESTS})"
                )
                packet, percent, _ = self._packet(probe_index)
                props = _props(self.data)
                await self._write(
                    self.data, packet, response="write" in props or not props
                )
                ack = await self._next_ack(protocol.VERIFIED_CHECKPOINT_TIMEOUT)
                if ack is None:
                    continue

            if ack > end:
                raise TransferError(
                    f"Device checkpoint skipped from window end {end} to block {ack}", 11
                )
            if ack < end:
                repairs += 1
                if repairs > protocol.MAX_REPAIR_REQUESTS:
                    raise TransferError(
                        f"Device requested too many block repairs ({repairs}); "
                        "move the computer closer and retry",
                        30,
                    )
                self.log(
                    f"Device verified only through block {ack}; repairing the "
                    f"unfinished window ({repairs}/{protocol.MAX_REPAIR_REQUESTS})"
                )
            else:
                repairs = 0
                self.log(f"Durable Flash checkpoint accepted through block {ack}")
            index = ack

    async def _legacy_transfer(self, index: int) -> None:
        index = 0 if index < 0 else index
        while True:
            if index >= self.total_blocks:
                await self._await_refresh(protocol.LEGACY_FINAL_ACK_TIMEOUT)
                return
            packet, percent, last = self._packet(index)
            props = _props(self.data)
            await self._write(
                self.data, packet, response="write-without-response" not in props
            )
            self._emit(percent, index)
            if last:
                await self._await_refresh(protocol.LEGACY_FINAL_ACK_TIMEOUT)
                return
            ack = await self._next_ack(10.0)
            if ack is None:
                raise TransferError(
                    f"Device stopped acknowledging after block {index}", 11
                )
            index = ack

    async def _await_refresh(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.ended.is_set():
                self.log("Transfer completed and refresh acknowledged by 0x05/0x08")
                return
            if self.error:
                raise TransferError(self.error, 11)
            if self.disconnected.is_set() or not self._is_connected():
                raise TransferError(
                    "Device disconnected after the payload was written but before the "
                    "required 0x05/0x08 refresh acknowledgement. Treating this run as "
                    "unconfirmed.",
                    29,
                )
            await asyncio.sleep(0.05)
        raise TransferError(
            "Timed out waiting for the required 0x05/0x08 refresh acknowledgement", 13
        )
