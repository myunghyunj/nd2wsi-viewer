"""Zeiss CZI SubBlock pixel-format tables and codec decoding helpers."""

from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

# Zeiss ZISRAW PixelType codes -> (numpy dtype, samples per pixel, is_rgb)
_PIXEL_TYPES: dict[int, tuple[np.dtype, int, bool]] = {
    0: (np.dtype("uint8"), 1, False),    # Gray8
    1: (np.dtype("uint16"), 1, False),   # Gray16
    2: (np.dtype("float32"), 1, False),  # Gray32Float
    3: (np.dtype("uint8"), 3, True),     # Bgr24
    4: (np.dtype("uint16"), 3, True),    # Bgr48
    8: (np.dtype("float32"), 3, True),   # Bgr96Float
    9: (np.dtype("uint8"), 4, True),     # Bgra32
}

_PIXEL_TYPE_NAMES: dict[int, str] = {
    0: "Gray8",
    1: "Gray16",
    2: "Gray32Float",
    3: "Bgr24",
    4: "Bgr48",
    8: "Bgr96Float",
    9: "Bgra32",
}

_COMPRESSION_NAMES: dict[int, str] = {
    0: "Uncompressed",
    1: "JPEG",
    2: "LZW",
    4: "JPEG-XR",
    5: "Zstd0",
    6: "Zstd1",
    121: "ApoTomeRaw",
}

_MAX_SUBBLOCK_BYTES = 256 * 1024 * 1024
_ZSTD_FRAME_MAGIC = b"\x28\xb5\x2f\xfd"


@dataclass(frozen=True)
class _SubBlockEntry:
    pixel_type: int
    file_pos: int
    compression: int
    pyramid_type: int
    ndims: int
    x_start: int
    x_size: int
    x_stored: int
    y_start: int
    y_size: int
    y_stored: int
    c: int
    z: int
    t: int
    s: int
    m: int


def _unpack_subblock_buffer(
    buf: bytes,
    h: int,
    w: int,
    dtype: np.dtype,
    samples: int,
    *,
    copy_gray: bool = False,
) -> np.ndarray:
    """Reshape a decoded SubBlock buffer into (h, w) or RGB (h, w, 3)."""
    arr = np.frombuffer(buf, dtype=dtype).reshape(h, w, samples)
    if samples == 1:
        return arr[:, :, 0].copy() if copy_gray else arr[:, :, 0]
    # Uncompressed / Zstd / LZW Bgr24 / Bgr48 / Bgra32 -> RGB
    return np.ascontiguousarray(arr[:, :, :3][:, :, ::-1])


def _zstd1_payload_bytes(raw: bytes, itemsize: int, expected_bytes: int) -> bytes:
    """CZI Zstd1 payload -> pixel bytes using only ``imagecodecs.zstd_decode``.

    Layout (libCZI): byte 0 = header size, then chunks; chunk type 1 carries the
    hi/lo byte-packing flag. A bare Zstd frame (no Zstd1 header) is accepted too.
    """
    import imagecodecs

    if raw[:4] == _ZSTD_FRAME_MAGIC:
        hilo = False
        zbytes = raw
    else:
        hdr = raw[0]
        if hdr < 1 or hdr >= len(raw):
            raise ValueError(f"invalid CZI Zstd1 header size {hdr} for a {len(raw)} B payload")
        if hdr == 1:
            hilo = False
        elif hdr >= 3 and raw[1] == 1:
            hilo = bool(raw[2] & 1)
        else:
            raise ValueError(f"unsupported CZI Zstd1 chunk layout (header_size={hdr})")
        zbytes = raw[hdr:]

    if expected_bytes <= 0 or expected_bytes > _MAX_SUBBLOCK_BYTES:
        raise ValueError(f"CZI Zstd1 expected SubBlock size {expected_bytes} exceeds safety limit")
    zstd_err = getattr(imagecodecs, "ZstdError", ValueError)
    try:
        out_buf = bytearray(expected_bytes)
        data = bytes(imagecodecs.zstd_decode(zbytes, out=out_buf))
    except (zstd_err, RuntimeError, MemoryError, ValueError) as exc:
        if "too small" in str(exc).lower():
            raise ValueError(
                f"CZI Zstd1 SubBlock decoded payload is larger than expected ({expected_bytes} B)"
            ) from exc
        raise ValueError(f"invalid CZI Zstd1 SubBlock payload: {exc}") from exc

    if len(data) < expected_bytes:
        raise ValueError(
            f"CZI Zstd1 SubBlock decoded payload ({len(data)} B) "
            f"smaller than expected ({expected_bytes} B)"
        )
    if len(data) > expected_bytes:
        raise ValueError(
            f"CZI Zstd1 SubBlock decoded payload ({len(data)} B) "
            f"larger than expected ({expected_bytes} B)"
        )
    if hilo and itemsize > 1:
        planes = np.frombuffer(data, dtype=np.uint8).reshape(itemsize, -1)
        data = np.ascontiguousarray(planes.T).tobytes()
    return data


def _decode_zstd0_payload(raw: bytes, expected_bytes: int) -> bytes:
    import imagecodecs

    if expected_bytes <= 0 or expected_bytes > _MAX_SUBBLOCK_BYTES:
        raise ValueError(f"CZI Zstd0 SubBlock size {expected_bytes} exceeds safety limit")
    zstd_err = getattr(imagecodecs, "ZstdError", ValueError)
    try:
        out_buf = bytearray(expected_bytes)
        decomp = bytes(imagecodecs.zstd_decode(raw, out=out_buf))
    except (zstd_err, RuntimeError, MemoryError, ValueError) as exc:
        if "too small" in str(exc).lower():
            raise ValueError(
                f"CZI Zstd0 SubBlock decoded payload is larger than expected ({expected_bytes} B)"
            ) from exc
        raise ValueError(f"invalid CZI Zstd0 SubBlock payload: {exc}") from exc
    if len(decomp) != expected_bytes:
        raise ValueError(
            f"CZI Zstd0 SubBlock decoded payload ({len(decomp)} B) "
            f"does not match expected ({expected_bytes} B)"
        )
    return decomp


def _validate_jpegxr_header_bounds(raw: bytes, samples: int, itemsize: int) -> None:
    if len(raw) < 8 or raw[:4] != b"II\xbc\x01":
        return
    ifd_off = struct.unpack_from("<I", raw, 4)[0]
    if ifd_off + 2 > len(raw):
        return
    n_tags = struct.unpack_from("<H", raw, ifd_off)[0]
    jxr_w = jxr_h = None
    for i in range(min(n_tags, 64)):
        off = ifd_off + 2 + i * 12
        if off + 12 > len(raw):
            break
        tag, _typ, _cnt, val = struct.unpack_from("<HHII", raw, off)
        if tag == 0xBC80:
            jxr_w = val
        elif tag == 0xBC81:
            jxr_h = val
    if jxr_w is not None and jxr_h is not None:
        if (
            jxr_w <= 0
            or jxr_h <= 0
            or jxr_w * jxr_h * max(samples, 1) * max(itemsize, 1) > _MAX_SUBBLOCK_BYTES
        ):
            raise ValueError(
                f"CZI JPEG-XR header dimensions ({jxr_w}x{jxr_h}) exceed safety limit"
            )


def _validate_jpeg_header_bounds(raw: bytes, itemsize: int) -> None:
    # Scan SOF marker dimensions before decoding to block JPEG decompression bombs.
    pos = 2 if raw[:2] == b"\xff\xd8" else len(raw)
    while pos + 4 <= len(raw):
        while pos < len(raw) and raw[pos] != 0xFF:
            pos += 1
        while pos < len(raw) and raw[pos] == 0xFF:
            pos += 1
        if pos >= len(raw):
            break
        marker = raw[pos]
        pos += 1
        if marker in (0xD8, 0xD9, 0x01) or (0xD0 <= marker <= 0xD7):
            continue
        if marker == 0xDA or pos + 2 > len(raw):
            break
        seg_len = struct.unpack_from(">H", raw, pos)[0]
        if seg_len < 2 or pos + seg_len > len(raw):
            break
        if (0xC0 <= marker <= 0xCF) and marker not in (0xC4, 0xC8, 0xCC) and seg_len >= 8:
            jpg_h, jpg_w, jpg_c = struct.unpack_from(">HHB", raw, pos + 3)
            if (
                jpg_h <= 0
                or jpg_w <= 0
                or jpg_h * jpg_w * max(jpg_c, 1) * max(itemsize, 1) > _MAX_SUBBLOCK_BYTES
            ):
                raise ValueError(
                    f"CZI JPEG header dimensions ({jpg_w}x{jpg_h}x{jpg_c}) exceed safety limit"
                )
            break
        pos += seg_len


def _crop_decoded_image(
    arr: np.ndarray,
    h: int,
    w: int,
    samples: int,
    codec_label: str,
) -> np.ndarray:
    if samples == 1:
        if arr.ndim == 3 and arr.shape[2] == 1:
            arr = arr[:, :, 0]
        if arr.ndim != 2 or arr.shape[0] < h or arr.shape[1] < w:
            raise ValueError(f"CZI {codec_label} decoded shape {arr.shape} does not cover ({h}, {w})")
        return np.ascontiguousarray(arr[:h, :w])
    if arr.ndim != 3 or arr.shape[0] < h or arr.shape[1] < w or arr.shape[2] < 3:
        raise ValueError(f"CZI {codec_label} decoded shape {arr.shape} does not cover ({h}, {w}, 3)")
    return np.ascontiguousarray(arr[:h, :w, :3])


def _decode_apotome_phases(
    raw: bytes,
    h: int,
    w: int,
    dtype: np.dtype,
    samples: int,
) -> np.ndarray:
    plane_bytes = h * w * samples * dtype.itemsize
    n_phases = len(raw) // plane_bytes
    phases = np.frombuffer(raw, dtype=dtype).reshape(n_phases, h, w, samples)
    if n_phases == 1:
        arr = phases[0]
    elif n_phases == 3 and samples == 1:
        # Zeiss ApoTome 3-phase (0, 120, 240 deg) optical sectioning demodulation
        f0 = phases[0, :, :, 0].astype(np.float32)
        f1 = phases[1, :, :, 0].astype(np.float32)
        f2 = phases[2, :, :, 0].astype(np.float32)
        sec = np.float32(0.47140452) * np.sqrt((f0 - f1) ** 2 + (f1 - f2) ** 2 + (f2 - f0) ** 2)
        if np.issubdtype(dtype, np.integer):
            info_i = np.iinfo(dtype)
            return np.clip(np.rint(sec), info_i.min, info_i.max).astype(dtype)
        return sec.astype(dtype)
    else:
        mean = phases.astype(np.float32).mean(axis=0)
        arr = mean.astype(dtype)
    if samples == 1:
        return arr[:, :, 0]
    return np.ascontiguousarray(arr[:, :, :3][:, :, ::-1])


def _decode_subblock_bytes(
    raw: bytes,
    sb: _SubBlockEntry,
    dtype: np.dtype,
    samples: int,
) -> np.ndarray:
    """Decode one SubBlock payload into (y_stored, x_stored) or (y_stored, x_stored, 3)."""
    h, w = sb.y_stored, sb.x_stored
    comp = sb.compression
    if comp == 0:
        return _unpack_subblock_buffer(raw, h, w, dtype, samples)

    if comp == 5:  # Zstd0
        expected_bytes = h * w * samples * dtype.itemsize
        decomp = _decode_zstd0_payload(raw, expected_bytes)
        return _unpack_subblock_buffer(decomp, h, w, dtype, samples)

    if comp == 6:  # Zstd1
        if not raw:
            raise ValueError("empty CZI Zstd1 SubBlock payload")
        expected_bytes = h * w * samples * dtype.itemsize
        decomp = _zstd1_payload_bytes(raw, dtype.itemsize, expected_bytes)
        return _unpack_subblock_buffer(decomp, h, w, dtype, samples, copy_gray=True)

    if comp == 4:  # JPEG-XR (jxrlib already outputs RGB channel order)
        import imagecodecs

        expected_bytes = h * w * samples * dtype.itemsize
        if expected_bytes <= 0 or expected_bytes > _MAX_SUBBLOCK_BYTES:
            raise ValueError(f"CZI JPEG-XR SubBlock size {expected_bytes} exceeds safety limit")
        _validate_jpegxr_header_bounds(raw, samples, dtype.itemsize)
        arr = np.asarray(imagecodecs.jpegxr_decode(raw))
        return _crop_decoded_image(arr, h, w, samples, "JPEG-XR")

    if comp == 1:  # JPEG
        import imagecodecs

        expected_bytes = h * w * samples * dtype.itemsize
        if expected_bytes <= 0 or expected_bytes > _MAX_SUBBLOCK_BYTES:
            raise ValueError(f"CZI JPEG SubBlock size {expected_bytes} exceeds safety limit")
        _validate_jpeg_header_bounds(raw, dtype.itemsize)
        arr = np.asarray(imagecodecs.jpeg_decode(raw))
        return _crop_decoded_image(arr, h, w, samples, "JPEG")

    if comp == 2:  # LZW
        import imagecodecs

        expected_bytes = h * w * samples * dtype.itemsize
        if expected_bytes <= 0 or expected_bytes > _MAX_SUBBLOCK_BYTES:
            raise ValueError(f"CZI LZW SubBlock size {expected_bytes} exceeds safety limit")
        out_buf = bytearray(expected_bytes)
        decomp = bytes(imagecodecs.lzw_decode(raw, out=out_buf))
        if len(decomp) != expected_bytes:
            raise ValueError(
                f"CZI LZW SubBlock decoded payload ({len(decomp)} B) "
                f"does not match expected ({expected_bytes} B)"
            )
        return _unpack_subblock_buffer(decomp, h, w, dtype, samples)

    plane_bytes = h * w * samples * dtype.itemsize
    if comp >= 100 and plane_bytes > 0 and len(raw) % plane_bytes == 0:
        return _decode_apotome_phases(raw, h, w, dtype, samples)

    raise NotImplementedError(
        f"unsupported CZI compression code {comp} ({_COMPRESSION_NAMES.get(comp, 'unknown')})"
    )


def _resolve_czi_entries_spec(
    entries: list[_SubBlockEntry],
) -> tuple[np.dtype, bool, list[str]]:
    """Validate SubBlock pixel types and return (promoted_dtype, rgb, channel_dtypes)."""
    if not entries:
        raise ValueError("CZI selection contains no SubBlocks")
    rgb_flags: set[bool] = set()
    by_c: dict[int, set[np.dtype]] = {}
    all_dtypes: list[np.dtype] = []
    for e in entries:
        if e.pixel_type not in _PIXEL_TYPES:
            raise NotImplementedError(f"unsupported CZI PixelType code {e.pixel_type}")
        dt, _samples, is_rgb = _PIXEL_TYPES[e.pixel_type]
        rgb_flags.add(is_rgb)
        by_c.setdefault(e.c, set()).add(dt)
        all_dtypes.append(dt)
    if len(rgb_flags) > 1:
        raise ValueError("unsupported CZI layout: mixing RGB and grayscale SubBlocks")
    rgb = next(iter(rgb_flags))
    promoted = all_dtypes[0]
    for dt in all_dtypes[1:]:
        promoted = np.promote_types(promoted, dt)
    if rgb:
        raw_ptypes = sorted({_PIXEL_TYPE_NAMES[e.pixel_type] for e in entries})
        if len(raw_ptypes) > 1 or any(e.pixel_type in (8, 9) for e in entries):
            sig = "+".join(raw_ptypes)
        else:
            sig = str(promoted)
        channel_dtypes = [sig] * 3
    else:
        channel_dtypes = []
        for c_val in sorted(by_c):
            c_dts = sorted(by_c[c_val], key=lambda d: str(np.dtype(d)))
            if len(c_dts) == 1:
                ch_sig = str(np.dtype(c_dts[0]))
            else:
                ch_sig = "+".join(str(np.dtype(d)) for d in c_dts)
            channel_dtypes.append(ch_sig)
    return np.dtype(promoted), rgb, channel_dtypes
