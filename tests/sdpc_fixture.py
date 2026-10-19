"""Synthetic, non-patient SDPC bytes and independent decoded RGB references.

The fixture encodes each rectangular tile separately with libx265. Expected
pixels come from a separate PyAV decode, never from the pre-compression image:
HEVC and RGB/YUV conversion are allowed to change the original samples.
"""

import struct
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO

import numpy as np

HEADER = struct.Struct("<H16sIq9IBB4sfdIqqB48s")
LEVEL = struct.Struct("<H5I2qfd2IB63s")


@dataclass(frozen=True)
class SyntheticSdpc:
    data: bytes
    decoded: tuple[np.ndarray, ...]
    level_offsets: tuple[int, ...]


def _encode_and_decode(rgb):
    import av

    output = BytesIO()
    with av.open(output, "w", format="hevc") as container:
        stream = container.add_stream("libx265", rate=1)
        stream.width, stream.height = rgb.shape[1], rgb.shape[0]
        stream.pix_fmt = "yuv420p"
        stream.options = {
            "preset": "ultrafast", "tune": "zerolatency", "crf": "24",
            "x265-params": "log-level=none:pools=none:frame-threads=1:wpp=0",
        }
        frame = av.VideoFrame.from_ndarray(rgb, format="rgb24")
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    encoded = output.getvalue()
    with av.open(BytesIO(encoded), format="hevc") as container:
        frames = list(container.decode(video=0))
        assert len(frames) == 1
        # Explicit chroma reconstruction and rounding avoid FFmpeg defaults
        # changing by release: BILINEAR | SWS_ACCURATE_RND | SWS_BITEXACT.
        decoded = frames[0].to_ndarray(format="rgb24", interpolation=2 | 0x40000 | 0x80000,
                                       src_color_range=frames[0].color_range)
    return encoded, decoded


@lru_cache(maxsize=4)
def build_sdpc(width=1027, height=773, tile_width=384, tile_height=256, levels=2):
    """Return a reusable SQ/SDPC fixture; no files are written by this helper."""
    mpp = 0.37
    extra = bytearray(38)
    struct.pack_into("<2sI", extra, 0, b"EI", len(extra))
    struct.pack_into("<q", extra, 6, HEADER.size + len(extra))
    struct.pack_into("<f", extra, 34, 2.2)
    # Extra metadata links directly to the baseline: no macros or labels.
    prefix = bytes(extra)
    cursor = HEADER.size + len(prefix)
    records, expected, offsets = [], [], []
    for k in range(levels):
        w, h = max(1, width // 2**k), max(1, height // 2**k)
        nx, ny = -(-w // tile_width), -(-h // tile_height)
        y, x = np.indices((h, w), dtype=np.uint32)
        source = np.stack(((3*x + y + 37*k) % 256,
                           (x + 5*y + 61*k) % 256,
                           (7*x + 2*y + 97*k) % 256), axis=-1).astype(np.uint8)
        padded = np.empty((ny * tile_height, nx * tile_width, 3), np.uint8)
        padded[:] = (245, 13, 231)
        padded[:h, :w] = source
        decoded = np.empty_like(padded)
        tiles = []
        for ty in range(ny):
            for tx in range(nx):
                ys = slice(ty * tile_height, (ty + 1) * tile_height)
                xs = slice(tx * tile_width, (tx + 1) * tile_width)
                blob, pixels = _encode_and_decode(np.ascontiguousarray(padded[ys, xs]))
                tiles.append(blob)
                decoded[ys, xs] = pixels
        table = struct.pack(f"<{len(tiles)}I", *(len(tile) for tile in tiles))
        payload = b"".join(tiles)
        next_offset = cursor + LEVEL.size + len(table) + len(payload)
        record = LEVEL.pack(0x4649, LEVEL.size, k + 1, nx * ny, nx, ny,
                            len(payload), next_offset, 2.0**-k, mpp * 2**k,
                            nx * tile_width - w, ny * tile_height - h, 0, b"")
        offsets.append(cursor)
        records.append(record + table + payload)
        cropped = np.ascontiguousarray(decoded[:h, :w])
        cropped.flags.writeable = False
        expected.append(cropped)
        cursor = next_offset
    header = HEADER.pack(0x5153, b"synthetic-v1", HEADER.size, cursor, 0, 0, levels,
                         width, height, tile_width, tile_height, 0, 0, 24, 80,
                         b"RGB\x00", 0.5, mpp, 40, HEADER.size, offsets[0], 4, b"")
    return SyntheticSdpc(header + prefix + b"".join(records), tuple(expected),
                         tuple(offsets))


def add_associated_images(synthetic):
    """Insert linked MI and layer-zero records; their dummy JPEGs stay opaque."""
    jpeg = b"\xff\xd8\xff\xd9"  # no label or thumbnail pixels are ever decoded
    macro_offset = synthetic.level_offsets[0]
    thumbnail_offset = macro_offset + 123 + len(jpeg)
    baseline_offset = thumbnail_offset + LEVEL.size + len(jpeg)
    shift = baseline_offset - macro_offset
    macro = bytearray(123)
    macro[:2] = b"MI"
    struct.pack_into("<Q", macro, 34, len(jpeg))
    struct.pack_into("<Q", macro, 43, thumbnail_offset)
    thumbnail = LEVEL.pack(0x4649, LEVEL.size, 0, 1, 1, 1,
                           len(jpeg), baseline_offset, 1.0, 0.37, 0, 0, 0, b"")
    data = bytearray(synthetic.data[:macro_offset] + bytes(macro) + jpeg
                     + thumbnail + jpeg + synthetic.data[macro_offset:])
    header = list(HEADER.unpack_from(data))
    header[3] += shift  # file length
    header[4] = 1  # associated macro count
    header[20] += shift  # baseline index
    HEADER.pack_into(data, 0, *header)
    offsets = tuple(offset + shift for offset in synthetic.level_offsets)
    for offset in offsets:
        level = list(LEVEL.unpack_from(data, offset))
        level[7] += shift
        LEVEL.pack_into(data, offset, *level)
    return SyntheticSdpc(bytes(data), synthetic.decoded, offsets)
