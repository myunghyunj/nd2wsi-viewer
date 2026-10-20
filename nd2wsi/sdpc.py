"""Read the SQ/SDPC HEVC RGB pyramid directly, without a conversion cache.

Supported layout: packed SQ header, 122-byte IF records, uint32 tile-size
tables and independent HEVC tiles in row-major order. Only the validated
power-of-two RGB variant is accepted. Explicit MI photographs and the layer-zero
thumbnail are available separately; structured patient records are not exposed.
Encoded RGB is preserved; undocumented vendor colour correction is not applied.
"""

from __future__ import annotations

import math
import os
import struct
import threading
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

import numpy as np

from .direct_common import _TILE_CACHE, MAX_EXTRA, _Lifecycle, _Root, _SvsLevel
from .platform_io import BINARY, read_at
from .reader import ChannelInfo, PlaneSource, level_shapes

_HEADER = struct.Struct("<H16sIq9IBB4sfdIqqB48s")
_LEVEL = struct.Struct("<H5I2qfd2IB63s")
_FIRST_LEVEL = b"IF\x7a\x00\x00\x00\x01\x00\x00\x00"
_MAX_PREFIX = 16 * 1024 * 1024
_MAX_TILES = 2_000_000
_MAX_TILE_BYTES = 64 * 1024 * 1024
_MAX_ASSOCIATED_BYTES = 16 * 1024 * 1024
_MAX_ASSOCIATED_PIXELS = 16_000_000
_MAX_ASSOCIATED_SIDE = 16_384
_MAX_PREVIEW_SIDE = 2048
DECODER_HINT = (
    'reading SDPC needs PyAV with HEVC decoding: pip install "nd2wsi-viewer[sdpc]"'
)


def is_sdpc(path: str | Path) -> bool:
    return Path(path).suffix.lower() == ".sdpc"


def _require_av():
    try:
        import av
    except ImportError as exc:
        raise RuntimeError(DECODER_HINT) from exc
    return av


def _identity(st) -> tuple[int, int, int, int]:
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns


@dataclass(frozen=True)
class _Level:
    width: int
    height: int
    nx: int
    ny: int
    offset: int
    offsets: np.ndarray
    sizes: np.ndarray


@dataclass(frozen=True)
class _AssociatedImage:
    name: str
    width: int
    height: int
    offset: int
    size: int
    encoding: str


def _associated_dimensions(width: int, height: int) -> None:
    if (not 1 <= width <= _MAX_ASSOCIATED_SIDE
            or not 1 <= height <= _MAX_ASSOCIATED_SIDE
            or width * height > _MAX_ASSOCIATED_PIXELS):
        raise ValueError("invalid or excessive SDPC associated-image dimensions")


def _decode_associated(data: bytes, record: _AssociatedImage) -> np.ndarray:
    """Native JPEG/BMP RGB without rotation, ICC/gamma edits, or EXIF exposure."""
    from PIL import Image

    _associated_dimensions(record.width, record.height)
    try:
        if record.encoding == "JPEG" and not (data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9")):
            raise ValueError("truncated or invalid SDPC JPEG markers")
        if record.encoding == "BMP" and (len(data) < 26 or struct.unpack_from("<I", data, 2)[0] != len(data)):
            raise ValueError("truncated or invalid SDPC BMP length")
        with Image.open(BytesIO(data), formats=[record.encoding]) as image:
            if image.format != record.encoding or image.size != (record.width, record.height):
                raise ValueError("SDPC associated image disagrees with its declared dimensions/format")
            _associated_dimensions(*image.size)
            if image.mode not in ("RGB", "L"):
                raise ValueError("unsupported SDPC associated-image color mode")
            image.load()
            return np.array(image.convert("RGB"), dtype=np.uint8, copy=True, order="C")
    except Exception as exc:
        raise ValueError(f"cannot decode SDPC associated image: {exc}") from exc


class _Slide:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self._associated_life = _Lifecycle()
        self._close_lock = threading.Lock()
        self._closed = False
        self.fd = os.open(str(self.path), os.O_RDONLY | BINARY)
        try:
            self.ident = _identity(os.fstat(self.fd))
            self.size = self.ident[2]
            self._parse()
            self.check_source()
        except BaseException:
            os.close(self.fd)
            raise

    def check_source(self) -> None:
        try:
            same = _identity(self.path.stat()) == self.ident
            same = same and _identity(os.fstat(self.fd)) == self.ident
        except OSError as exc:
            raise ValueError("SDPC source is unreachable; close and reopen the slide") from exc
        if not same:
            raise ValueError("SDPC source changed; close and reopen the slide")

    def read(self, offset: int, size: int) -> bytes:
        if offset < 0 or size < 0 or offset + size > self.size:
            raise ValueError("SDPC record extends beyond the file")
        data = read_at(self.fd, size, offset)
        if len(data) != size:
            raise ValueError("truncated SDPC record")
        return data

    def _parse(self) -> None:
        values = _HEADER.unpack(self.read(0, _HEADER.size))
        (flag, version, head_size, file_size, macros, _people, count,
         self.width, self.height, self.tw, self.th, _thumbw, _thumbh,
         bpp, quality, color_space, scale, self.mpp, self.objective,
         extra_offset, _tile_offset, codec, _reserved) = values
        self.associated: list[_AssociatedImage] = []
        if flag != 0x5153 or not (_HEADER.size <= head_size <= self.size):
            raise ValueError("not a supported SQ/SDPC header")
        if file_size != self.size:
            raise ValueError("SDPC file size does not match its header (possibly incomplete)")
        if (bpp != 24 or codec != 4 or not math.isclose(scale, 0.5)
                or not 1 <= count <= 16):
            raise NotImplementedError("SDPC support requires a 24-bit HEVC halving pyramid")
        if (not 1 <= self.tw <= 4096 or not 1 <= self.th <= 4096
                or not 1 <= self.width <= 2**31 - 1
                or not 1 <= self.height <= 2**31 - 1):
            raise ValueError("invalid or excessive SDPC image/tile dimensions")
        self.gamma = None
        baseline_offset = None
        if extra_offset:
            extra = self.read(extra_offset, 38)
            if extra[:2] != b"EI":
                raise ValueError("invalid SDPC extra-info record")
            extra_size = struct.unpack_from("<I", extra, 2)[0]
            if extra_size < 38 or extra_offset + extra_size > self.size:
                raise ValueError("invalid SDPC extra-info size")
            gamma = struct.unpack_from("<f", extra, 34)[0]
            self.gamma = gamma if math.isfinite(gamma) else None
            offset = struct.unpack_from("<q", extra, 6)[0]
            if offset < extra_offset + extra_size or not 0 <= macros <= 16:
                raise ValueError("invalid SDPC associated-image chain")
            # MI records are 123-byte headers followed by a JPEG. No published
            # discriminator establishes label versus macro: keep neutral names
            # in file order rather than guessing from aspect ratio or contents.
            for index in range(macros):
                macro = self.read(offset, 123)
                length = struct.unpack_from("<Q", macro, 34)[0]
                next_offset = struct.unpack_from("<Q", macro, 43)[0]
                if (macro[:2] != b"MI" or not 0 < length <= _MAX_ASSOCIATED_BYTES
                        or next_offset != offset + 123 + length
                        or next_offset > self.size):
                    raise ValueError("invalid SDPC macro-image record")
                width, height = struct.unpack_from("<2I", macro, 10)
                _associated_dimensions(width, height)
                self.associated.append(_AssociatedImage(
                    f"associated-{index + 1}", width, height, offset + 123, length, "JPEG",
                ))
                offset = next_offset
            thumbnail = _LEVEL.unpack(self.read(offset, _LEVEL.size))
            if thumbnail[:3] == (0x4649, _LEVEL.size, 0):
                length, next_offset = thumbnail[6:8]
                # The layer-zero thumbnail has no tile-size table.
                if (not 0 < length <= _MAX_ASSOCIATED_BYTES
                        or next_offset != offset + _LEVEL.size + length
                        or next_offset > self.size):
                    raise ValueError("invalid SDPC thumbnail record")
                # The supplied layer-zero images are BMP, not HEVC. Only these
                # bounded still-image formats are accepted; never infer pixels
                # by passing an arbitrary record to a general media decoder.
                signature = self.read(offset + _LEVEL.size, min(2, length))
                encoding = {b"BM": "BMP", b"\xff\xd8": "JPEG"}.get(signature)
                if encoding is not None:
                    _associated_dimensions(_thumbw, _thumbh)
                    self.associated.insert(0, _AssociatedImage(
                        "thumbnail", _thumbw, _thumbh, offset + _LEVEL.size, length, encoding,
                    ))
                offset = next_offset
            baseline_offset = offset
        self.version = version.rstrip(b"\x00").decode("ascii", errors="replace")
        self.quality = quality
        self.color_space = color_space.hex()
        if baseline_offset is not None:
            self.levels = self._parse_levels(baseline_offset, count)
            return
        # Associated images have variant-dependent record layouts. Search only
        # a bounded prefix for a baseline IF marker, then validate the ENTIRE
        # linked pyramid and every table before accepting the candidate.
        end = min(self.size, _MAX_PREFIX)
        prefix = self.read(head_size, end - head_size)
        pos = -1
        error: Exception | None = None
        for _ in range(32):
            pos = prefix.find(_FIRST_LEVEL, pos + 1)
            if pos < 0:
                break
            try:
                self.levels = self._parse_levels(head_size + pos, count)
                return
            except (ValueError, NotImplementedError) as exc:
                error = exc
        if error is not None:
            raise ValueError(f"invalid SDPC pyramid: {error}") from error
        raise NotImplementedError("no supported SDPC baseline index in the first 16 MiB")

    def _parse_levels(self, offset: int, count: int) -> list[_Level]:
        levels = []
        total_tiles = 0
        for k in range(count):
            (flag, header_size, number, n, nx, ny, payload_size, next_offset,
             scale, mpp, pad_x, pad_y, fmt, _reserved) = _LEVEL.unpack(
                self.read(offset, _LEVEL.size)
            )
            w, h = max(1, self.width // 2**k), max(1, self.height // 2**k)
            total_tiles += n
            if (flag != 0x4649 or header_size != _LEVEL.size or number != k + 1
                    or not n or n != nx * ny or total_tiles > _MAX_TILES
                    or nx != (w + self.tw - 1) // self.tw
                    or ny != (h + self.th - 1) // self.th
                    or pad_x != nx * self.tw - w or pad_y != ny * self.th - h
                    or not math.isclose(scale, 2.0**-k, rel_tol=1e-6)
                    or fmt != 0):
                raise ValueError(f"unsupported dimensions/index at SDPC level {k}")
            if (math.isfinite(self.mpp) and self.mpp > 0
                    and not math.isclose(mpp, self.mpp * 2**k, rel_tol=1e-5)):
                raise ValueError(f"inconsistent SDPC calibration at level {k}")
            sizes = np.frombuffer(self.read(offset + header_size, n * 4), dtype="<u4")
            if np.any(sizes == 0) or np.any(sizes > _MAX_TILE_BYTES):
                raise ValueError("invalid or excessive SDPC compressed tile size")
            cumulative = np.empty(n + 1, dtype=np.uint64)
            cumulative[0] = 0
            np.cumsum(sizes, dtype=np.uint64, out=cumulative[1:])
            start = offset + header_size + n * 4
            end = start + int(cumulative[-1])
            if payload_size != int(cumulative[-1]) or end > self.size:
                raise ValueError("SDPC tile-size table exceeds its payload")
            if next_offset != end:
                raise ValueError("inconsistent SDPC next-level offset")
            levels.append(_Level(w, h, nx, ny, offset, cumulative[:-1] + start, sizes))
            offset = next_offset
        return levels

    def info(self) -> dict[str, Any]:
        calibrated = math.isfinite(self.mpp) and self.mpp > 0
        return {
            "format": "SDPC", "container_version": self.version,
            "width": self.width, "height": self.height,
            "tile_width": self.tw, "tile_height": self.th,
            "compression": "HEVC", "rgb": True, "dtype": "uint8",
            "pixel_size_um": [self.mpp, self.mpp] if calibrated else None,
            "objective_magnification": self.objective or None,
            "color_space_code": self.color_space, "quality_field": self.quality,
            "vendor_gamma_metadata": self.gamma,
            "color_policy": "decoded RGB; vendor colour correction not applied",
            "associated_images": self.associated_image_metadata(),
            "levels": [{"level": k, "width": lv.width, "height": lv.height,
                        "downsample": 2**k, "tiles": len(lv.sizes)}
                       for k, lv in enumerate(self.levels)],
        }

    def associated_image_metadata(self) -> list[dict[str, Any]]:
        """Only fixed names, dimensions and image encoding; no private fields."""
        with self._associated_life:
            self.check_source()
            return [{"name": record.name, "width": record.width, "height": record.height,
                     "encoding": record.encoding} for record in self.associated]

    def read_associated_image(self, name: str) -> np.ndarray:
        with self._associated_life:
            self.check_source()
            record = next((record for record in self.associated if record.name == name), None)
            if record is None:
                raise KeyError(name)
            pixels = _decode_associated(self.read(record.offset, record.size), record)
            self.check_source()
            return pixels

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            if not self._associated_life.close():
                raise TimeoutError("SDPC associated-image reads did not finish before close")
            os.close(self.fd)
            self._closed = True


def sdpc_info(path: str | Path) -> dict[str, Any]:
    slide = _Slide(path)
    try:
        return slide.info()
    finally:
        slide.close()


def associated_image_names(path: str | Path) -> list[str]:
    """Stable, bounded names: thumbnail, then associated-1 through associated-16."""
    slide = _Slide(path)
    try:
        return [item["name"] for item in slide.associated_image_metadata()]
    finally:
        slide.close()


def associated_image_rgb(path: str | Path, name: str) -> np.ndarray:
    """Read native associated-image RGB without downsampling or color edits."""
    slide = _Slide(path)
    try:
        return slide.read_associated_image(name)
    finally:
        slide.close()


def associated_image_jpeg(path: str | Path, name: str, *, max_side: int = 512) -> bytes:
    """A bounded UI preview; all source EXIF/ICC/text metadata is discarded."""
    from PIL import Image

    if type(max_side) is not int or not 1 <= max_side <= _MAX_PREVIEW_SIDE:
        raise ValueError(f"SDPC associated-image preview size must be 1..{_MAX_PREVIEW_SIDE}")
    pixels = associated_image_rgb(path, name)
    image = Image.fromarray(pixels)
    image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    output = BytesIO()
    image.save(output, format="JPEG", quality=90)
    return output.getvalue()


def _decode_tile(data: bytes, width: int, height: int) -> np.ndarray:
    av = _require_av()
    try:
        with av.open(BytesIO(data), format="hevc", options={
            "max_pixels": str(width * height), "threads": "1",
        }) as container:
            stream = container.streams.video[0]
            stream.codec_context.thread_count = 1
            stream.codec_context.options = {"max_pixels": str(width * height)}
            if (stream.codec_context.width, stream.codec_context.height) != (width, height):
                raise ValueError("SDPC tile dimensions disagree with the container")
            frames = container.decode(video=0)
            frame = next(frames, None)
            if frame is None or (frame.width, frame.height) != (width, height):
                raise ValueError("SDPC tile did not decode to its declared dimensions")
            # Explicit chroma reconstruction/rounding avoids version/CPU-specific
            # swscale fast paths. Preserve decoded colorspace and range metadata;
            # this is not the vendor's separate gamma/CCM colour correction.
            # PyAV 16 accepts integer swscale flags but exposes no enum names for
            # SWS_ACCURATE_RND (0x40000) or SWS_BITEXACT (0x80000).
            rgb = frame.to_ndarray(
                format="rgb24", interpolation=2 | 0x40000 | 0x80000,
                src_color_range=frame.color_range,
            )
            if next(frames, None) is not None:
                raise ValueError("SDPC tile contains multiple frames")
            return np.ascontiguousarray(rgb)
    except Exception as exc:
        raise ValueError(f"cannot decode SDPC HEVC tile: {exc}") from exc


def open_direct(path: str | Path) -> tuple[Any, dict[str, Any]]:
    """Virtual CYX uint8 store; reads and exports use decoded native RGB."""
    from .convert import build_group_attrs

    _require_av()
    slide = _Slide(path)
    life = _Lifecycle()
    owner = str(slide.path)
    tag = (owner, slide.ident)
    try:
        shapes = level_shapes(slide.height, slide.width, 512)
        if len(shapes) - len(slide.levels) > int(math.log2(MAX_EXTRA)):
            raise NotImplementedError("SDPC embedded pyramid is too sparse")

        def reader(k):
            lv = slide.levels[k]

            def read(y0, y1, x0, x1):
                slide.check_source()  # also check before returning cached pixels
                out = np.empty((y1 - y0, x1 - x0, 3), dtype=np.uint8)
                for ty in range(y0 // slide.th, (y1 + slide.th - 1) // slide.th):
                    for tx in range(x0 // slide.tw, (x1 + slide.tw - 1) // slide.tw):
                        key = (tag, k, ty, tx)
                        tile = _TILE_CACHE.get(key)
                        if tile is None:
                            index = ty * lv.nx + tx
                            blob = slide.read(int(lv.offsets[index]), int(lv.sizes[index]))
                            tile = _decode_tile(blob, slide.tw, slide.th)
                            _TILE_CACHE.put(key, tile)
                        ya, yb = max(y0, ty * slide.th), min(y1, (ty + 1) * slide.th)
                        xa, xb = max(x0, tx * slide.tw), min(x1, (tx + 1) * slide.tw)
                        out[ya-y0:yb-y0, xa-x0:xb-x0] = tile[
                            ya-ty*slide.th:yb-ty*slide.th, xa-tx*slide.tw:xb-tx*slide.tw
                        ]
                slide.check_source()
                return out

            return read

        readers = {k: reader(k) for k in range(len(slide.levels))}
        root = {}
        for k, (h, w) in enumerate(shapes):
            source = min(k, len(slide.levels) - 1)
            extra = 2**(k-source)
            native = slide.levels[source]
            if h * extra > native.height or w * extra > native.width:
                raise NotImplementedError("SDPC overview would read a collapsed axis beyond its edge")
            root[str(k)] = _SvsLevel(readers[source], h, w, extra, life)
        meta = slide.info()
        src = PlaneSource(
            data=None, dtype=np.dtype(np.uint8), shape=(3, slide.height, slide.width),
            rgb=True, channels=[ChannelInfo("Red", (255, 0, 0)),
                                ChannelInfo("Green", (0, 255, 0)),
                                ChannelInfo("Blue", (0, 0, 255))],
            pixel_size_um=tuple(meta["pixel_size_um"]) if meta["pixel_size_um"] else None,
            source_name=str(slide.path), selection={},
            notes=[f"served directly from SDPC ({len(slide.levels)} embedded levels)",
                   "Native values are decoded lossy HEVC RGB, not pre-compression samples.",
                   meta["color_policy"]],
            magnification=meta["objective_magnification"],
            calibration_source="sdpc-ruler" if meta["pixel_size_um"] else "unknown",
        )
        windows = [{"start": 0.0, "end": 255.0, "min": 0.0, "max": 255.0}] * 3
        attrs = build_group_attrs(src, shapes, 512, windows)
        attrs["nd2wsi"].update(direct=True, source_format="sdpc", sdpc=meta)
        attrs["multiscales"][0]["metadata"]["description"] = (
            "embedded SDPC pyramid; additional overview levels use local means"
        )
    except BaseException:
        slide.close()
        raise

    def teardown():
        if life.close():
            slide.close()
            _TILE_CACHE.purge(owner)

    return _Root(root, teardown), attrs
