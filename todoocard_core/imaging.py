"""Image -> TodooCard T3 528x792 six-color payload.

Pure-Python replacement for image_to_payload.swift. The pipeline is:

1. EXIF transpose, optional 90-degree clockwise rotation
2. cover-fit to 528x792 on a white canvas
3. Floyd-Steinberg dithering against the six native panel colors
4. pack two 4-bit color codes per byte (high nibble first)
5. T3 panel calibration: rotate 180 then flip horizontally
6. controller order: rotate clockwise + mirror
7. legacy pre-v157 payload: 3-byte header per 64-byte chunk

Palette-exact images (text/templates) are byte-identical to the Swift
converter; photo output is visually equivalent but dither phase differs
because CoreGraphics and Pillow use different resampling kernels.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

CARD_W, CARD_H = 528, 792
T3_CONTROLLER_BYTES = CARD_W * CARD_H // 2
MAX_INPUT_BYTES = 100_000_000
MAX_PIXELS = 50_000_000

# (R, G, B, code); order defines the tie-break priority, matching Swift.
PALETTE = [
    (0, 0, 0, 0),
    (255, 255, 255, 1),
    (255, 255, 0, 2),
    (255, 0, 0, 3),
    (0, 0, 255, 5),
    (0, 255, 0, 6),
]

_HEIF_REGISTERED = False


class ImageError(RuntimeError):
    pass


def _register_heif() -> None:
    global _HEIF_REGISTERED
    if _HEIF_REGISTERED:
        return
    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except Exception:
        # pillow-heif is optional; non-HEIC images still work.
        pass
    _HEIF_REGISTERED = True


def _check_input(path: Path) -> None:
    if not path.is_file():
        raise ImageError(f"Input image not found: {path}")
    size = path.stat().st_size
    if size > MAX_INPUT_BYTES:
        raise ImageError(
            f"Input image is {size} bytes; the 100 MB safety limit was exceeded"
        )


def _cover(img: Image.Image, w: int = CARD_W, h: int = CARD_H) -> Image.Image:
    src_w, src_h = img.size
    if src_w <= 0 or src_h <= 0:
        raise ImageError("Input image has invalid dimensions")
    scale = max(w / src_w, h / src_h)
    draw_w, draw_h = src_w * scale, src_h * scale
    canvas = Image.new("RGB", (w, h), (255, 255, 255))
    resized = img.resize((max(1, round(draw_w)), max(1, round(draw_h))), Image.LANCZOS)
    canvas.paste(resized, (int((w - resized.width) / 2), int((h - resized.height) / 2)))
    return canvas


def _quantize(img: Image.Image) -> bytes:
    """Floyd-Steinberg dithering, returns a 4-bit-per-pixel bitmap."""
    w, h = img.size
    pixels = list(img.getdata())
    work = [[float(p[0]), float(p[1]), float(p[2])] for p in pixels]
    codes = bytearray(w * h)

    def add(x: int, y: int, dr: float, dg: float, db: float, k: float) -> None:
        if 0 <= x < w and 0 <= y < h:
            o = y * w + x
            work[o][0] += dr * k
            work[o][1] += dg * k
            work[o][2] += db * k

    for y in range(h):
        for x in range(w):
            i = y * w + x
            r, g, b = (min(255.0, max(0.0, v)) for v in work[i])
            best, best_d = PALETTE[0], float("inf")
            for pr, pg, pb, code in PALETTE:
                dr, dg, db = r - pr, g - pg, b - pb
                d = dr * dr + dg * dg + db * db
                if d < best_d:
                    best_d, best = d, (pr, pg, pb, code)
            codes[i] = best[3]
            er, eg, eb = r - best[0], g - best[1], b - best[2]
            add(x + 1, y, er, eg, eb, 7 / 16)
            add(x - 1, y + 1, er, eg, eb, 3 / 16)
            add(x, y + 1, er, eg, eb, 5 / 16)
            add(x + 1, y + 1, er, eg, eb, 1 / 16)

    out = bytearray((w * h + 1) // 2)
    for i, code in enumerate(codes):
        if i % 2 == 0:
            out[i // 2] = (code & 0x0F) << 4
        else:
            out[i // 2] |= code & 0x0F
    return bytes(out)


def _get(buf: bytes | bytearray, i: int) -> int:
    byte = buf[i // 2]
    return byte >> 4 if i % 2 == 0 else byte & 0x0F


def _set(buf: bytearray, i: int, code: int) -> None:
    if i % 2 == 0:
        buf[i // 2] = (buf[i // 2] & 0x0F) | ((code & 0x0F) << 4)
    else:
        buf[i // 2] = (buf[i // 2] & 0xF0) | (code & 0x0F)


def _flip_h(buf: bytes, w: int = CARD_W, h: int = CARD_H) -> bytes:
    out = bytearray(len(buf))
    for y in range(h):
        for x in range(w):
            _set(out, y * w + (w - 1 - x), _get(buf, y * w + x))
    return bytes(out)


def _rot180(buf: bytes, n: int = CARD_W * CARD_H) -> bytes:
    out = bytearray(len(buf))
    for i in range(n):
        _set(out, n - 1 - i, _get(buf, i))
    return bytes(out)


def _rotate_cw_mirrored(buf: bytes, w: int = CARD_W, h: int = CARD_H) -> bytes:
    """T3 controller order: destination width is the panel height (792)."""
    out = bytearray(len(buf))
    for di in range(w * h):
        dx, dy = di % h, di // h
        sx, sy = w - 1 - dy, h - 1 - dx
        _set(out, di, _get(buf, sy * w + sx))
    return bytes(out)


def _legacy_qlz(raw: bytes, block: int = 64) -> bytes:
    out = bytearray(b"\x00\x00\x00\x00")
    for off in range(0, len(raw), block):
        out += b"\x74\x43\x40" + raw[off : off + block]
    return bytes(out)


@dataclass(frozen=True)
class PayloadArtifacts:
    bin: Path
    controller: Path
    legacy: Path
    width: int = CARD_W
    height: int = CARD_H

    @property
    def bytes(self) -> int:
        return self.bin.stat().st_size

    @property
    def legacy_bytes(self) -> int:
        return self.legacy.stat().st_size


def build(
    image_path: Path,
    out_prefix: Path,
    *,
    rotate_right_90: bool = False,
) -> PayloadArtifacts:
    _register_heif()
    _check_input(image_path)
    try:
        img = Image.open(image_path)
        img.load()
    except Exception as exc:  # Pillow raises different classes per codec
        raise ImageError(f"Cannot decode image: {image_path} ({exc})") from exc
    if img.width * img.height > MAX_PIXELS:
        raise ImageError(
            f"Input image is {img.width}x{img.height}; the 50-megapixel safety limit was exceeded"
        )
    img = ImageOps.exif_transpose(img).convert("RGB")
    if rotate_right_90:
        img = img.transpose(Image.ROTATE_270)  # clockwise 90

    bitmap = _quantize(_cover(img))
    calibrated = _flip_h(_rot180(bitmap))  # todoocard-correct panel calibration
    controller = _rotate_cw_mirrored(calibrated)
    legacy = _legacy_qlz(controller)

    prefix = Path(out_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    art = PayloadArtifacts(
        bin=Path(f"{prefix}.bin"),
        controller=Path(f"{prefix}.controller.bin"),
        legacy=Path(f"{prefix}.protocol.legacy.qlz"),
    )
    art.bin.write_bytes(calibrated)
    art.controller.write_bytes(controller)
    art.legacy.write_bytes(legacy)
    return art
