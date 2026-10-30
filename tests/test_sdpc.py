"""Direct SDPC reading using only generated, non-patient HEVC tiles."""

import os
import struct
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import Event

import numpy as np
import pytest

av = pytest.importorskip("av")

from sdpc_fixture import HEADER, LEVEL, add_associated_images, build_sdpc  # noqa: E402

from nd2wsi import sdpc  # noqa: E402


@pytest.fixture(scope="module")
def synthetic():
    return build_sdpc()


@pytest.fixture
def slide(tmp_path, synthetic):
    path = tmp_path / "synthetic.sdpc"
    path.write_bytes(synthetic.data)
    return path


def test_metadata_calibration_and_rectangular_padding(slide):
    info = sdpc.sdpc_info(slide)
    assert info["format"] == "SDPC"
    assert info["container_version"] == "synthetic-v1"
    assert (info["width"], info["height"]) == (1027, 773)
    assert (info["tile_width"], info["tile_height"]) == (384, 256)
    assert info["pixel_size_um"] == [0.37, 0.37]
    assert info["objective_magnification"] == 40
    assert info["vendor_gamma_metadata"] == pytest.approx(2.2)
    assert info["color_policy"] == "decoded RGB; vendor colour correction not applied"
    assert info["levels"] == [
        {"level": 0, "width": 1027, "height": 773, "downsample": 1, "tiles": 12},
        {"level": 1, "width": 513, "height": 386, "downsample": 2, "tiles": 4},
    ]
    assert sdpc.is_sdpc("SCAN.SDPC") and not sdpc.is_sdpc("scan.sdpc.tif")


def test_native_embedded_and_generated_levels_match_decoded_reference(slide, synthetic):
    root, attrs = sdpc.open_direct(slide)
    try:
        assert list(root) == ["0", "1", "2"]
        assert root["0"].dtype == np.uint8
        assert root["0"].shape == (3, 773, 1027)
        for k, reference in enumerate(synthetic.decoded):
            np.testing.assert_array_equal(root[str(k)][:], np.moveaxis(reference, -1, 0))
        # The embedded 2x image has its own pattern; a generated 4x overview
        # must use that image, not recompute from the baseline.
        embedded = synthetic.decoded[1][:386, :512]
        reference = np.rint(embedded.reshape(193, 2, 256, 2, 3)
                            .mean(axis=(1, 3), dtype=np.float32)).astype(np.uint8)
        np.testing.assert_array_equal(root["2"][:], np.moveaxis(reference, -1, 0))
        meta = attrs["nd2wsi"]
        assert meta["source_format"] == "sdpc" and meta["direct"] is True
        assert meta["calibration"] == {"status": "calibrated", "source": "sdpc-ruler"}
        assert attrs["multiscales"][0]["datasets"][2]["coordinateTransformations"] == [
            {"type": "scale", "scale": [1.0, 1.48, 1.48]},
        ]
        assert [channel["label"] for channel in attrs["omero"]["channels"]] == [
            "Red", "Green", "Blue",
        ]
    finally:
        root.close(delay=0)


def test_seam_crossing_roi_channel_selection_and_empty_reads(slide, synthetic):
    root, _ = sdpc.open_direct(slide)
    reference = np.moveaxis(synthetic.decoded[0], -1, 0)
    try:
        level = root["0"]
        np.testing.assert_array_equal(level[[2, 0], 250:520, 370:790],
                                      reference[[2, 0], 250:520, 370:790])
        np.testing.assert_array_equal(level[1, -10:, -12:], reference[1, -10:, -12:])
        np.testing.assert_array_equal(level[:, -1, -1], reference[:, -1:, -1:])
        assert level[:, 257:257, 370:790].shape == (3, 0, 420)
        assert level[:, 250:520, 385:385].shape == (3, 270, 0)
        assert level[[], 250:520, 370:790].shape == (0, 270, 420)
        with pytest.raises(IndexError, match="strided"):
            level[:, ::2, :]
        with pytest.raises(IndexError, match="out of range"):
            level[:, 773, :]
    finally:
        root.close(delay=0)


@pytest.mark.parametrize("mpp", [0.0, float("nan")])
def test_missing_calibration_does_not_invent_micrometers(slide, synthetic, mpp):
    header = list(HEADER.unpack_from(synthetic.data))
    header[17] = mpp
    slide.write_bytes(HEADER.pack(*header) + synthetic.data[HEADER.size:])
    root, attrs = sdpc.open_direct(slide)
    try:
        assert attrs["nd2wsi"]["pixel_size_um"] is None
        assert attrs["nd2wsi"]["calibration"]["status"] == "unknown"
        assert sdpc.sdpc_info(slide)["pixel_size_um"] is None
        assert all("unit" not in axis for axis in attrs["multiscales"][0]["axes"])
    finally:
        root.close(delay=0)


def test_generated_overview_cannot_average_collapsed_axis_padding(tmp_path):
    synthetic = build_sdpc(width=1027, height=1, levels=1)
    path = tmp_path / "collapsed.sdpc"
    path.write_bytes(synthetic.data)
    with pytest.raises(NotImplementedError, match="collapsed"):
        sdpc.open_direct(path)


@pytest.mark.parametrize("bad_link", [False, True])
def test_associated_macro_and_thumbnail_chain(slide, synthetic, bad_link):
    linked = add_associated_images(synthetic)
    data = bytearray(linked.data)
    if bad_link:
        macro_offset = synthetic.level_offsets[0]
        next_offset = struct.unpack_from("<Q", data, macro_offset + 43)[0]
        struct.pack_into("<Q", data, macro_offset + 43, next_offset + 1)
    slide.write_bytes(data)
    if bad_link:
        with pytest.raises(ValueError, match="macro-image"):
            sdpc.open_direct(slide)
        return
    root, attrs = sdpc.open_direct(slide)
    try:
        assert attrs["nd2wsi"]["sdpc"]["vendor_gamma_metadata"] == pytest.approx(2.2)
        np.testing.assert_array_equal(root["0"][:, 250:260, 380:390],
                                      np.moveaxis(linked.decoded[0][250:260, 380:390], -1, 0))
    finally:
        root.close(delay=0)


@pytest.mark.parametrize("corruption,error", [
    ("signature", ValueError),
    ("codec", NotImplementedError),
    ("level_count", NotImplementedError),
    ("tile_count", ValueError),
    ("next_offset", ValueError),
    ("calibration", ValueError),
    ("zero_tile", ValueError),
    ("oversized_payload", ValueError),
    ("truncated", ValueError),
])
def test_malformed_indices_rejected_before_pixel_decode(slide, synthetic, monkeypatch,
                                                       corruption, error):
    data = bytearray(synthetic.data)
    header = list(HEADER.unpack_from(data))
    offset = synthetic.level_offsets[0]
    level = list(LEVEL.unpack_from(data, offset))
    if corruption == "signature":
        header[0] = 0
    elif corruption == "codec":
        header[21] = 3
    elif corruption == "level_count":
        header[6] = 17
    elif corruption == "tile_count":
        level[3] += 1
    elif corruption == "next_offset":
        level[7] += 1
    elif corruption == "calibration":
        level[9] = 1.0
    elif corruption == "zero_tile":
        struct.pack_into("<I", data, offset + LEVEL.size, 0)
    elif corruption == "oversized_payload":
        struct.pack_into("<I", data, offset + LEVEL.size, len(data) + 1)
    HEADER.pack_into(data, 0, *header)
    LEVEL.pack_into(data, offset, *level)
    if corruption == "truncated":
        del data[-1:]
    slide.write_bytes(data)

    def unexpected_decode(*args):
        pytest.fail("malformed SDPC metadata must fail before any HEVC decode")

    monkeypatch.setattr(sdpc, "_decode_tile", unexpected_decode)
    with pytest.raises(error):
        sdpc.open_direct(slide)


def test_invalid_hevc_payload_fails_when_the_tile_is_read(slide, synthetic):
    data = bytearray(synthetic.data)
    offset = synthetic.level_offsets[0]
    level = LEVEL.unpack_from(data, offset)
    tile_size = struct.unpack_from("<I", data, offset + LEVEL.size)[0]
    start = offset + LEVEL.size + level[3] * 4
    data[start:start + tile_size] = b"\x00" * tile_size
    slide.write_bytes(data)
    root, _ = sdpc.open_direct(slide)
    try:
        with pytest.raises(ValueError, match="cannot decode SDPC HEVC tile"):
            root["0"][:, :10, :10]
    finally:
        root.close(delay=0)


def _hevc(rgb, *, frames=1, pix_fmt="yuv420p", params=""):
    """One raw HEVC stream of ``frames`` pictures; the encoder is test-only."""
    output = BytesIO()
    with av.open(output, "w", format="hevc") as container:
        stream = container.add_stream("libx265", rate=1)
        stream.width, stream.height = rgb.shape[1], rgb.shape[0]
        stream.pix_fmt = pix_fmt
        stream.options = {
            "preset": "ultrafast", "tune": "zerolatency", "crf": "24",
            "x265-params": "log-level=none:pools=none:frame-threads=1:wpp=0" + params,
        }
        for index in range(frames):
            frame = av.VideoFrame.from_ndarray(np.roll(rgb, index, axis=1), format="rgb24")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return output.getvalue()


def _demuxer_decode(data):
    """Independent reference: the raw-HEVC demuxer path with the same swscale flags."""
    with av.open(BytesIO(data), format="hevc") as container:
        frames = list(container.decode(video=0))
    assert len(frames) == 1
    return frames[0].to_ndarray(format="rgb24", interpolation=2 | 0x40000 | 0x80000,
                                src_color_range=frames[0].color_range)


@pytest.mark.parametrize("pix_fmt, params", [
    ("yuv420p", ""),
    ("yuv420p", ":range=full:colorprim=bt709:transfer=bt709:colormatrix=bt709"),
    ("yuv444p", ":colormatrix=bt470bg"),
    ("yuv420p10le", ""),
])
def test_tile_decode_matches_the_demuxer_decode_bit_for_bit(pix_fmt, params):
    rgb = np.random.default_rng(5).normal(180, 30, (256, 384, 3)).clip(0, 255).astype(np.uint8)
    data = _hevc(rgb, pix_fmt=pix_fmt, params=params)
    decoded = sdpc._decode_tile(data, 384, 256)
    assert decoded.dtype == np.uint8 and decoded.flags.c_contiguous
    np.testing.assert_array_equal(decoded, _demuxer_decode(data))


def test_tile_decode_rejects_extra_frames_and_undeclared_dimensions():
    rgb = np.random.default_rng(6).integers(0, 256, (128, 128, 3), dtype=np.uint8)
    with pytest.raises(ValueError, match="cannot decode SDPC HEVC tile: SDPC tile contains "
                                         "multiple frames"):
        sdpc._decode_tile(_hevc(rgb, frames=2), 128, 128)
    data = _hevc(rgb)
    with pytest.raises(ValueError, match="did not decode to its declared dimensions"):
        sdpc._decode_tile(data, 256, 256)
    # max_pixels refuses the oversized picture before any frame exists, so the
    # later dimension check is never reached.
    with pytest.raises(ValueError, match="cannot decode SDPC HEVC tile") as refused:
        sdpc._decode_tile(data, 64, 64)
    assert "declared dimensions" not in str(refused.value)


def test_each_tile_gets_a_fresh_single_threaded_decoder(monkeypatch):
    rgb = np.random.default_rng(7).normal(120, 40, (128, 128, 3)).clip(0, 255).astype(np.uint8)
    good = _hevc(rgb)
    reference = _demuxer_decode(good)
    real, contexts = av.CodecContext, []

    class Recording:
        @staticmethod
        def create(*args, **kwargs):
            context = real.create(*args, **kwargs)
            contexts.append(context)
            return context

    monkeypatch.setattr(av, "CodecContext", Recording)
    np.testing.assert_array_equal(sdpc._decode_tile(good, 128, 128), reference)
    for damaged in (b"\x00" * 4096, good[:200]):
        with pytest.raises(ValueError, match="cannot decode SDPC HEVC tile"):
            sdpc._decode_tile(damaged, 128, 128)
    # A damaged tile leaves no decoder state behind for the next one.
    np.testing.assert_array_equal(sdpc._decode_tile(good, 128, 128), reference)
    assert len({id(context) for context in contexts}) == len(contexts) == 4
    assert all(context.is_decoder and context.name == "hevc" and context.thread_count == 1
               for context in contexts)


@pytest.mark.parametrize("mutation", ["replacement", "mtime"])
def test_source_replacement_is_detected_even_for_a_cached_tile(slide, synthetic, monkeypatch,
                                                             mutation):
    root, _ = sdpc.open_direct(slide)
    try:
        reference = root["0"][:, :10, :10]
        assert reference.shape == (3, 10, 10)

        def unexpected_decode(*args):
            pytest.fail("a source identity check must run before returning cached pixels")

        monkeypatch.setattr(sdpc, "_decode_tile", unexpected_decode)
        stat = slide.stat()
        if mutation == "replacement":
            replacement = slide.with_suffix(".replacement")
            replacement.write_bytes(synthetic.data)
            os.utime(replacement, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            # Same bytes, length and mtime still have a different filesystem identity.
            try:
                replacement.replace(slide)
            except PermissionError as exc:
                if os.name != "nt" or getattr(exc, "winerror", None) not in (5, 32):
                    raise
                # Windows can deny replacing an open file. Confirm that this
                # preserved the original and its cached pixels, then exercise
                # a real metadata mutation instead of skipping stale checks.
                assert replacement.is_file()
                assert sdpc._identity(slide.stat()) == sdpc._identity(stat)
                np.testing.assert_array_equal(root["0"][:, :10, :10], reference)
                mutation = "mtime"
        if mutation == "mtime":
            os.utime(slide, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
            changed = slide.stat()
            assert (changed.st_dev, changed.st_ino, changed.st_size) == (
                stat.st_dev, stat.st_ino, stat.st_size)
            assert changed.st_mtime_ns != stat.st_mtime_ns
        with pytest.raises(ValueError, match="source changed"):
            root["0"][:, :10, :10]
    finally:
        root.close(delay=0)


def test_close_waits_for_active_read_then_rejects_further_reads(slide, synthetic, monkeypatch):
    opened = []
    actual_slide = sdpc._Slide

    def track_open(path):
        obj = actual_slide(path)
        opened.append(obj)
        return obj

    monkeypatch.setattr(sdpc, "_Slide", track_open)
    root, _ = sdpc.open_direct(slide)
    started, release, close_started = Event(), Event(), Event()
    decode = sdpc._decode_tile
    closer = root._closer

    def blocked_decode(*args):
        started.set()
        assert release.wait(5), "test did not release the decoder"
        return decode(*args)

    def tracked_close():
        close_started.set()
        closer()

    monkeypatch.setattr(sdpc, "_decode_tile", blocked_decode)
    root._closer = tracked_close
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            reading = pool.submit(lambda: root["0"][:, :10, :10])
            try:
                assert started.wait(5)
                closing = pool.submit(root.close, 0)
                assert close_started.wait(5)
                assert not closing.done()
                os.fstat(opened[0].fd)  # file remains live throughout the decode
            finally:
                release.set()
            np.testing.assert_array_equal(reading.result(timeout=5),
                                          np.moveaxis(synthetic.decoded[0][:10, :10], -1, 0))
            closing.result(timeout=5)
        root.close(delay=0)  # idempotent
        with pytest.raises(ValueError, match="closed"):
            root["0"][:, :10, :10]
        with pytest.raises(OSError):
            os.fstat(opened[0].fd)
        key = ((str(slide.resolve()), opened[0].ident), 0, 0, 0)
        assert sdpc._TILE_CACHE.get(key) is None
    finally:
        release.set()
        root.close(delay=0)
