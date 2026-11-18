"""Command line interface mirroring the macOS-only prepare_and_send.sh contract."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import re
import sys
import tempfile
from pathlib import Path

from . import __version__, ble, imaging, protocol, quicklz
from .manifest import manifest_text
from .transfer import HandshakeError, Transfer, TransferError, TransferOptions


def _abs(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _prefix_for(image: Path, output: Path) -> Path:
    try:
        stat = image.stat()
        key = hashlib.sha256(
            f"{image}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")
        ).hexdigest()[:10]
    except OSError:
        key = hashlib.sha256(str(image).encode("utf-8")).hexdigest()[:10]
    stem = re.sub(r"[^\w.-]+", "_", image.stem).strip("._")[:40] or "image"
    return output / f"{stem}-{key}"


def prepare(
    image: Path,
    *,
    output: Path,
    rotate_right_90: bool = False,
) -> tuple[Path, imaging.PayloadArtifacts]:
    """Run the hex/QuickLZ pipeline and print the upstream-compatible contract."""
    image = _abs(image)
    output = _abs(output)
    output.mkdir(parents=True, exist_ok=True)
    print(f"Input image: {image}", flush=True)
    print("Color mode: six-color (T3 native palette)", flush=True)
    print("Panel calibration: rotate 180 + horizontal flip", flush=True)

    prefix = _prefix_for(image, output)
    art = imaging.build(image, prefix, rotate_right_90=rotate_right_90)
    print(f"Wrote {art.bin} ({art.bytes} bytes)", flush=True)
    print(
        f"Wrote {art.controller} ({art.controller.stat().st_size} bytes)",
        flush=True,
    )
    print(f"Wrote {art.legacy} ({art.legacy_bytes} bytes)", flush=True)

    controller = art.controller.read_bytes()
    qlz = quicklz.build_level3_payload(controller)
    qlz_path = Path(f"{prefix}.protocol.qlz")
    qlz_path.write_bytes(qlz)
    print(f"Prepared v157+ payload: {qlz_path}", flush=True)
    print(f"Prepared legacy payload: {art.legacy}", flush=True)
    print(
        f"Payload verification: controller={len(controller)} bytes, "
        f"quicklz={len(qlz)} bytes, legacy={art.legacy_bytes} bytes",
        flush=True,
    )
    return qlz_path, art


async def _scan(seconds: float) -> int:
    print(f"Platform: {ble.platform_summary()}", flush=True)
    print(f"Scanning nearby BLE devices for {int(seconds)}s", flush=True)
    try:
        devices = await ble.scan_once(seconds)
    except Exception as exc:
        message = _bluetooth_message(exc)
        print(f"Scan failed: {message}", file=sys.stderr, flush=True)
        return 3
    for item in devices:
        print(item.line(), flush=True)
    print(f"Scan complete: {len(devices)} device(s)", flush=True)
    return 0


def _bluetooth_message(exc: BaseException) -> str:
    text = str(exc)
    if "adapter" in text.lower() and ("off" in text.lower() or "not" in text.lower()):
        return "蓝牙不可用：请打开系统蓝牙并确认思源/终端有蓝牙权限"
    return text or repr(exc)


async def _pair(device_id: str, timeout: float) -> int:
    try:
        item = await ble.wait_for_target(
            device_id, timeout=timeout, log=_log, pair_mode=True
        )
        client = await ble.connect(item, log=_log)
        try:
            await ble.pair(client, item, log=_log)
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
        return 0
    except ble.BleError as exc:
        print(f"Pairing did not complete: {exc}", file=sys.stderr, flush=True)
        return exc.exit_code
    except Exception as exc:
        print(f"Pairing did not complete: {exc}", file=sys.stderr, flush=True)
        return 18


async def _probe(device_id: str, timeout: float) -> int:
    try:
        item = await ble.wait_for_target(device_id, timeout=timeout, log=_log)
        client = await ble.connect(item, log=_log)
        try:
            await ble.verify_bond(client, item, log=_log)
            control, data, _diagnostics = await ble.find_transfer_chars(client)
            if control is None or data is None:
                raise ble.BleError(
                    "Image-transfer service FEF1/FEF2 was not found on the device", 19
                )
            print(
                "Probe completed: exact target, manufacturer=0x5053, screen=0x134C, "
                "and secure FEF1/FEF2 access verified",
                flush=True,
            )
            return 0
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
    except ble.BleError as exc:
        print(f"Probe ended: {exc}", file=sys.stderr, flush=True)
        return exc.exit_code
    except Exception as exc:
        print(f"Probe ended: {exc}", file=sys.stderr, flush=True)
        return 18


def _log(message: str) -> None:
    print(message, flush=True)


def _load_payloads(
    qlz_path: Path | None,
    legacy_path: Path | None,
    raw_path: Path | None,
) -> tuple[bytes, bytes, bytes]:
    qlz = qlz_path.read_bytes() if qlz_path else b""
    legacy = legacy_path.read_bytes() if legacy_path else b""
    raw = raw_path.read_bytes() if raw_path else b""
    if not qlz and not raw:
        raise TransferError("No payload was provided", 2)
    return qlz, legacy, raw


def _select_profile(item: ble.Discovered, qlz: bytes, legacy: bytes, raw: bytes):
    """Choose compressed/standard/legacy exactly like the upstream sender."""
    if item.adv is not None and item.adv.verified_transfer and qlz and raw:
        # The verified protocol accepts either profile. Use QuickLZ only when it
        # actually reduces the block count; otherwise send the raw controller map.
        if len(qlz) < len(raw):
            return TransferOptions(compressed=True, payload=qlz, legacy_payload=legacy,
                                   raw_payload=raw), "compressed"
        return TransferOptions(compressed=False, payload=raw, legacy_payload=legacy,
                               raw_payload=raw), "standard"
    if not legacy:
        raise TransferError(
            "This pre-v157 device needs the legacy QuickLZ payload "
            "(--legacy-payload or --input)", 2
        )
    return TransferOptions(compressed=False, payload=legacy, legacy_payload=legacy,
                           raw_payload=raw), "legacy"


async def _send(
    device_id: str,
    qlz: bytes,
    legacy: bytes,
    raw: bytes,
    *,
    timeout: float,
    allow_device_button: bool,
) -> int:
    attempts = 3
    for attempt in range(1, attempts + 1):
        item = await ble.wait_for_target(
            device_id, timeout=timeout, log=_log, for_send=True
        )
        print(f"Connecting to {item.name or item.address} ({item.address}) ...", flush=True)
        client = await ble.connect(item, log=_log)
        diagnostics = None
        try:
            await ble.verify_bond(client, item, log=_log)
            control, data, diagnostics = await ble.find_transfer_chars(client)
            if control is None or data is None:
                raise TransferError(
                    "Image-transfer service FEF1/FEF2 was not found on the device", 19
                )
            options, profile = _select_profile(item, qlz, legacy, raw)
            options.allow_device_button = allow_device_button
            print(f"Protocol selected: {profile} payload ({len(options.payload)} bytes)", flush=True)
            if item.adv is not None and item.adv.verified_transfer:
                print(
                    "Verified transfer enabled: durable checkpoints every "
                    f"{protocol.VERIFIED_WINDOW_BLOCKS} blocks",
                    flush=True,
                )
            transfer = Transfer(
                client, item, control, data, diagnostics, options, log=_log
            )
            await transfer.run()
            return 0
        except HandshakeError as exc:
            print(f"Handshake attempt {attempt}/{attempts} failed: {exc}", flush=True)
            if attempt == attempts:
                print(f"Transfer failed: {exc}", file=sys.stderr, flush=True)
                return exc.exit_code
        except TransferError as exc:
            await _report_failure(client, item, diagnostics, exc)
            return exc.exit_code
        except Exception as exc:
            await _report_failure(
                client, item, diagnostics, TransferError(f"{type(exc).__name__}: {exc}", 29)
            )
            return 29
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
    return 16


async def _report_failure(client, item, diagnostics, exc: TransferError) -> None:
    print(f"Transfer failed: {exc}", file=sys.stderr, flush=True)
    if (
        diagnostics is not None
        and item.adv is not None
        and item.adv.diagnostics_available
    ):
        print("Reading retained v165+ image-ACK diagnostics before exiting ...", flush=True)
        await ble.read_diagnostics(client, diagnostics, log=_log)
    print(f"Failure summary: {exc}", file=sys.stderr, flush=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="todoocard",
        description="TodooCard/T3 six-color image payload + BLE transfer (macOS/Windows/Linux)",
    )
    parser.add_argument("--version", action="version", version=f"todoocard-general-core {__version__}")
    parser.add_argument("--manifest", action="store_true",
                        help="print the core manifest (version and compatibility contract) and exit")
    parser.add_argument("--list", action="store_true", help="scan for nearby devices and exit")
    parser.add_argument("--pair", action="store_true", help="create the system BLE bond")
    parser.add_argument("--probe", action="store_true", help="verify a bonded, encrypted link")
    parser.add_argument("--input", help="source image; prepares the payload")
    parser.add_argument("--send", action="store_true", help="send the prepared payload")
    parser.add_argument("--device-id", "--id", dest="device_id", help="target UUID / MAC address")
    parser.add_argument("--rotate-right-90", action="store_true", help="rotate the source image clockwise")
    parser.add_argument("--allow-device-button", action="store_true",
                        help="let the device start the transfer with its own button")
    parser.add_argument("--output", help="artifact directory (default: a private temp dir)")
    parser.add_argument("--payload", help="prebuilt .protocol.qlz (with --send)")
    parser.add_argument("--legacy-payload", help="prebuilt .protocol.legacy.qlz")
    parser.add_argument("--raw-payload", help="prebuilt .controller.bin")
    parser.add_argument("--scan-seconds", type=float, default=30.0)
    parser.add_argument("--wait-seconds", type=float, default=180.0,
                        help="how long to wait for the device to advertise")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.manifest:
        print(manifest_text(), end="")
        return 0
    modes = [bool(args.list), bool(args.pair), bool(args.probe), bool(args.input), bool(args.send)]
    if sum(modes) != 1:
        if not (args.input and args.send):
            parser.error(
                "choose exactly one of --list, --pair, --probe, --input, or --input ... --send"
            )
    if (args.pair or args.probe or args.send) and not args.device_id:
        parser.error("--device-id is required for --pair, --probe, and --send")
    if args.device_id and not protocol.is_device_id(args.device_id):
        parser.error(f"invalid device id: {args.device_id}")

    output = _abs(args.output) if args.output else _default_output_dir()

    try:
        if args.list:
            return asyncio.run(_scan(max(1.0, args.scan_seconds)))
        if args.pair:
            return asyncio.run(_pair(args.device_id, args.wait_seconds))
        if args.probe:
            return asyncio.run(_probe(args.device_id, args.wait_seconds))
        if args.input:
            qlz_path, art = prepare(
                _abs(args.input),
                output=output,
                rotate_right_90=args.rotate_right_90,
            )
            if not args.send:
                return 0
            qlz, legacy, raw = (
                qlz_path.read_bytes(),
                art.legacy.read_bytes(),
                art.controller.read_bytes(),
            )
        else:
            qlz, legacy, raw = _load_payloads(
                _abs(args.payload) if args.payload else None,
                _abs(args.legacy_payload) if args.legacy_payload else None,
                _abs(args.raw_payload) if args.raw_payload else None,
            )
        return asyncio.run(
            _send(
                args.device_id,
                qlz,
                legacy,
                raw,
                timeout=max(10.0, args.wait_seconds),
                allow_device_button=args.allow_device_button,
            )
        )
    except imaging.ImageError as exc:
        print(f"Failed: {exc}", file=sys.stderr, flush=True)
        return 2
    except ble.BleError as exc:
        print(f"Transfer failed: {exc}", file=sys.stderr, flush=True)
        return exc.exit_code
    except TransferError as exc:
        print(f"Transfer failed: {exc}", file=sys.stderr, flush=True)
        return exc.exit_code
    except KeyboardInterrupt:
        print("Canceled.", file=sys.stderr, flush=True)
        return 130
    except Exception as exc:  # keep the CLI contract even for unexpected faults
        print(f"Transfer failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 29


def _default_output_dir() -> Path:
    return Path(tempfile.gettempdir()) / "todoocard-general"


if __name__ == "__main__":
    raise SystemExit(main())
