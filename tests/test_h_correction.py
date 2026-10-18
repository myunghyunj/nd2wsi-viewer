"""Correction keeps original pixels and global geometry across display levels."""

import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from nd2wsi import h_correction as hc
from nd2wsi.cache import quick_fingerprint
from nd2wsi.direct import _Lifecycle


class Model:
    indices = [3, 0]
    enabled = [True, False]

    def correct(self, channel, raw, y, x):
        yy = y + np.arange(raw.shape[0])[:, None]
        xx = x + np.arange(raw.shape[1])[None, :]
        gain = (1.1 + 0.27 * np.sin(xx / 3.3) + 0.11 * np.cos(yy / 5.7)).astype(np.float32)
        return np.asarray(raw, np.float32) / gain


def means(a, factor):
    """Independent reference including true area of right/bottom bins."""
    return np.array(
        [
            [
                np.mean(a[y : y + factor, x : x + factor], dtype=np.float64)
                for x in range(0, a.shape[1], factor)
            ]
            for y in range(0, a.shape[0], factor)
        ],
        dtype=np.float32,
    )


@pytest.fixture
def state(tmp_path):
    native = np.random.default_rng(63).integers(0, 5000, (4, 259, 267), dtype=np.uint16)
    native[:, :5, :7] = 0
    root = {"0": native}
    levels = [{"path": "0", "height": 259, "width": 267, "downsample": 1}]
    for k in range(1, 6):
        previous = root[str(k - 1)]
        h, w = previous.shape[1] // 2, previous.shape[2] // 2
        block = previous[:, : 2 * h, : 2 * w]
        root[str(k)] = np.rint(block.reshape(4, h, 2, w, 2).mean(axis=(2, 4))).astype(np.uint16)
        levels.append({"path": str(k), "height": h, "width": w, "downsample": 2**k})
    source = tmp_path / "source.nd2"
    source.write_bytes(b"registered source fixture")
    return SimpleNamespace(
        root=root,
        attrs={
            "nd2wsi": {"levels": levels, "rgb": False, "selection": {"z": 0}},
            "omero": {"channels": [{"label": name} for name in ("CY3", "BF", "DAPI", "FITC")]},
        },
        source_path=source,
        generation="fixture-generation",
        busy=_Lifecycle(),
        plate=None,
        manifest={"source": quick_fingerprint(source)},
    )


def metadata(state):
    return {
        "sizes": {"C": 4, "Y": 259, "X": 267},
        "dtype": "uint16",
        "channel_names": ["CY3", "BF", "DAPI", "FITC"],
        "channel_mapping": {"CY3": 0, "BF": 1, "DAPI": 2, "FITC": 3},
        "channel_acquisition_settings": [{"sensor_pixels_xy": {"cx": 32, "cy": 32}}] * 4,
        "source_stamp": hc._source_stamp(state.source_path),
    }


def reader(state):
    def read(ci, y0, y1, x0, x1):
        return state.root["0"][ci, y0:y1, x0:x1].copy()

    return read


def done(controller):
    controller._thread.join(10)
    assert not controller._thread.is_alive()
    return controller.status()


def fake_fitter(raw8, raw32, meta, output, check, progress):
    check()
    progress(0.5)
    return Model(), {
        "selected": [
            {"channel": "FITC", "outcome": "fitted"},
            {"channel": "CY3", "outcome": "unchanged"},
        ]
    }


def corrected_root(state, tmp_path, coarse=False):
    temporary = tempfile.TemporaryDirectory(dir=tmp_path, prefix="correction-")
    model = Model()
    maps = (
        hc._prepare_coarse(
            state, model, [3], Path(temporary.name), reader(state), lambda: None, lambda _: None
        )
        if coarse
        else {}
    )
    return hc._CorrectedRoot(
        state, model, [3], maps, temporary, hc._source_stamp(state.source_path), state.generation
    )


def test_fit_inputs_have_true_partial_edge_means_and_no_rounding(state, tmp_path):
    original = state.root["0"].copy()
    raw8, raw32 = hc._prepare_inputs(state, tmp_path, reader(state), lambda: None, lambda _: None)
    try:
        assert raw8.shape == (4, 33, 34)
        assert raw32.shape == (4, 9, 9)
        for ci in range(4):
            np.testing.assert_array_equal(raw8[ci], means(original[ci], 8))
            np.testing.assert_array_equal(raw32[ci], means(original[ci], 32))
        assert np.any(raw8 != np.rint(raw8))
        np.testing.assert_array_equal(state.root["0"], original)
    finally:
        hc._close_maps([raw8, raw32])


def test_native_coordinates_and_correction_before_reduction(state, tmp_path):
    root = corrected_root(state, tmp_path)
    originals = {key: value.copy() for key, value in state.root.items()}
    full = root.model.correct(3, state.root["0"][3], 0, 0)
    try:
        for level in range(3):
            factor = 2**level
            got = root[str(level)][:, 7:21, 9:25]
            expected = full if factor == 1 else means(full, factor)
            np.testing.assert_array_equal(got[3], expected[7:21, 9:25])
            # Includes BF, DAPI, and CY3 whose fitted model was disabled.
            np.testing.assert_array_equal(got[:3], state.root[str(level)][:3, 7:21, 9:25])
        np.testing.assert_array_equal(root["0"][3, 19:27, 31:44], full[19:27, 31:44])
        selected = root["0"][[3, 1], 19:27, 31:44]
        np.testing.assert_array_equal(selected[0], full[19:27, 31:44])
        np.testing.assert_array_equal(selected[1], state.root["0"][1, 19:27, 31:44])
        # A post-downsample approximation would change this concrete result.
        wrong = root.model.correct(3, state.root["1"][3, 7:21, 9:25], 14, 18)
        assert np.max(np.abs(root["1"][3, 7:21, 9:25] - wrong)) > 1
        for key, old in originals.items():
            np.testing.assert_array_equal(state.root[key], old)
    finally:
        root.close()


def test_coarse_levels_crop_fit_edges_and_keep_raw_other_channels(state, tmp_path):
    root = corrected_root(state, tmp_path, coarse=True)
    temporary = Path(root.temporary.name)
    full = root.model.correct(3, state.root["0"][3], 0, 0)
    try:
        for level in range(3, 6):
            got = root[str(level)][:]
            h, w = got.shape[1:]
            expected = means(full, 2**level)[:h, :w]
            # Coarser levels average float32 8x means; only normal floating
            # summation roundoff differs from one direct float64 reduction.
            np.testing.assert_allclose(got[3], expected, rtol=2e-7, atol=5e-4)
            np.testing.assert_array_equal(got[:3], state.root[str(level)][:3])
            assert got.dtype == np.float32
        assert root["3"].shape == (4, 32, 33)
    finally:
        root.close()
    assert not temporary.exists()
    with pytest.raises(ValueError, match="closed"):
        root["0"][:, :1, :1]


def test_controller_revision_is_private_and_rejects_stale_ids(state, monkeypatch):
    monkeypatch.setattr(hc, "_metadata_for_state", metadata)
    monkeypatch.setattr(hc, "_fit_model", fake_fitter)
    controller = hc.HCorrectionController(state)
    raw_root = state.root
    assert controller.status()["state"] == "idle"
    assert controller.status()["eta_seconds"] is None
    assert controller.start()["generation"] == state.generation
    status = done(controller)
    assert status["state"] == "ready", status
    assert status["channels"] == ["FITC"]
    assert status["pct"] == 100
    assert status["elapsed_seconds"] >= 0
    root = controller.root_for(status["correction_id"])
    assert state.root is raw_root
    assert controller.start()["correction_id"] == status["correction_id"]
    assert controller.cancel()["state"] == "ready"  # RAW/H toggle belongs to the pane.
    with pytest.raises(ValueError, match="unavailable"):
        controller.root_for("another-slide-or-old-revision")
    assert not (Path(root.temporary.name) / "raw8.npy").exists()
    assert state.busy.active == 0
    state.source_path.unlink()
    with pytest.raises(ValueError, match="source is unavailable"):
        controller.root_for(status["correction_id"])
    controller.close()


def test_cancel_fit_is_nonblocking_and_never_publishes(state, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    workspaces = []

    def fit(raw8, raw32, meta, output, check, progress):
        workspaces.append(output.parent)
        assert state.busy.active == 0  # Optimizing temp data does not hold the source.
        entered.set()
        release.wait(3)
        check()
        return fake_fitter(raw8, raw32, meta, output, check, progress)

    monkeypatch.setattr(hc, "_metadata_for_state", metadata)
    monkeypatch.setattr(hc, "_fit_model", fit)
    controller = hc.HCorrectionController(state)
    controller.start()
    assert entered.wait(3)
    first_thread = controller._thread
    assert controller.start()["state"] == "fitting"
    assert controller._thread is first_thread
    begin = time.monotonic()
    controller.close()
    assert time.monotonic() - begin < 0.5
    assert controller.status()["state"] == "cancelled"
    release.set()
    assert done(controller)["correction_id"] is None
    assert not workspaces[0].exists()
    assert state.busy.active == 0


def test_no_enabled_targets_is_unchanged_not_ready(state, monkeypatch):
    def fitter(*args):
        model = Model()
        model.enabled = [False, False]
        return model, {"selected": [{"channel": "FITC", "outcome": "unchanged"}]}

    monkeypatch.setattr(hc, "_metadata_for_state", metadata)
    monkeypatch.setattr(hc, "_fit_model", fitter)
    controller = hc.HCorrectionController(state)
    controller.start()
    status = done(controller)
    assert status["state"] == "unchanged"
    assert status["channels"] == []
    assert status["correction_id"] is None
    controller.close()


@pytest.mark.parametrize(
    "change,reason",
    [
        (lambda s: setattr(s, "plate", object()), "single-frame"),
        (lambda s: s.attrs["nd2wsi"].update(rgb=True), "scalar"),
        (lambda s: s.attrs["nd2wsi"]["selection"].update(z="max"), "projections"),
        (lambda s: s.attrs["omero"]["channels"][1].update(label="unknown"), "BF"),
        (lambda s: s.attrs["omero"]["channels"][0].update(label="FITC"), "duplicate"),
        (lambda s: s.attrs["nd2wsi"]["levels"].pop(0), "level 0"),
    ],
)
def test_unsupported_state_never_starts_worker(state, change, reason):
    change(state)
    controller = hc.HCorrectionController(state)
    status = controller.start()
    assert status["state"] == "unsupported"
    assert not status["supported"]
    assert reason in status["reason"]
    assert controller._thread is None


def test_original_metadata_binding_and_no_direct_pixel_reads(state, monkeypatch):
    import nd2

    class Reader:
        sizes = {"C": 4, "Y": 259, "X": 267}
        dtype = np.dtype("uint16")
        is_rgb = False
        attributes = SimpleNamespace(sequenceCount=1)
        metadata = SimpleNamespace(
            channels=[
                SimpleNamespace(channel=SimpleNamespace(name=name))
                for name in ("CY3", "BF", "DAPI", "FITC")
            ]
        )

        def __init__(self, path):
            assert path == str(state.source_path)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def unstructured_metadata(self, strip_prefix):
            return {
                "ImageMetadataSeqLV|0": {
                    "SLxPictureMetadata": {
                        "sPicturePlanes": {
                            "sSampleSetting": {
                                "a1": {
                                    "pCameraSetting": {
                                        "FormatQuality": {
                                            "fmtDesc": {"sizeSensorPixels": {"cx": 32, "cy": 32}}
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }

        def read_frame(self, *args):
            raise AssertionError("pixels must come from the registered root")

    monkeypatch.setattr(nd2, "ND2File", Reader)
    result = hc._metadata_for_state(state)
    assert result["channel_mapping"] == {"CY3": 0, "BF": 1, "DAPI": 2, "FITC": 3}
    Reader.sizes = {**Reader.sizes, "T": 2}
    with pytest.raises(hc.UnsupportedCorrection, match="single stitched frame"):
        hc._metadata_for_state(state)
    Reader.sizes.pop("T")
    state.attrs["omero"]["channels"][0]["label"] = "renamed"
    with pytest.raises(hc.UnsupportedCorrection, match="channel order"):
        hc._metadata_for_state(state)
    state.source_path.write_bytes(b"replacement source")
    with pytest.raises(hc.UnsupportedCorrection, match="source changed"):
        hc._metadata_for_state(state)


def test_closed_source_guard_blocks_worker_reads(state, monkeypatch):
    monkeypatch.setattr(hc, "_metadata_for_state", metadata)
    monkeypatch.setattr(hc, "_fit_model", fake_fitter)
    state.busy.close()
    controller = hc.HCorrectionController(state)
    controller.start()
    status = done(controller)
    assert status["state"] == "failed"
    assert "closed" in status["reason"]
    assert status["correction_id"] is None
    controller.close()


def test_eta_is_estimated_only_after_progress(state, monkeypatch):
    controller = hc.HCorrectionController(state)
    monkeypatch.setattr(hc.time, "monotonic", lambda: 110)
    controller._started_at = 100
    controller._status.update(state="preparing", pct=0)
    assert controller.status()["eta_seconds"] is None
    controller._status.update(pct=25)
    status = controller.status()
    assert status["elapsed_seconds"] == 10
    assert status["eta_seconds"] == 30
    controller.cancel()
    assert controller.status()["eta_seconds"] is None


def test_native_chunk_alignment_remains_bounded(state):
    state.root["0"] = SimpleNamespace(chunks=(1, 1024, 1024))
    assert hc._block_geometry(state) == (1024, 2048)
    state.root["0"].chunks = (1, 16384, 16384)
    assert hc._block_geometry(state) == (512, 2048)


def test_source_change_during_fit_cannot_publish(state, monkeypatch):
    def fitter(*args):
        state.source_path.write_bytes(b"changed during fitting")
        return Model(), {"selected": []}

    monkeypatch.setattr(hc, "_metadata_for_state", metadata)
    monkeypatch.setattr(hc, "_fit_model", fitter)
    controller = hc.HCorrectionController(state)
    controller.start()
    status = done(controller)
    assert status["state"] == "unsupported"
    assert "source changed" in status["reason"]
    assert status["correction_id"] is None
    controller.close()


def test_preflight_estimate_scales_with_pixel_and_channel_work_without_reads(state, monkeypatch):
    class MetadataOnlyArray:
        dtype = np.dtype("uint16")

        def __init__(self, shape):
            self.shape = shape

        def __getitem__(self, _key):
            raise AssertionError("Preflight estimation must not read image pixels")

    def no_source_metadata(_state):
        raise AssertionError("Preflight estimation must not reopen the original source")

    monkeypatch.setattr(hc, "_metadata_for_state", no_source_metadata)

    def estimate(height, width, names):
        state.root = {"0": MetadataOnlyArray((len(names), height, width))}
        state.attrs["nd2wsi"]["levels"] = [
            {"path": "0", "height": height, "width": width, "downsample": 1}
        ]
        state.attrs["omero"]["channels"] = [{"label": name} for name in names]
        controller = hc.HCorrectionController(state)
        status = controller.status()
        assert status["state"] == "idle"
        assert controller._thread is None
        assert status["eta_seconds"] is None
        return status["estimated_seconds"]

    channels = ["CY3", "BF", "DAPI", "FITC"]
    reference = estimate(22895, 36599, channels)
    assert np.isfinite(reference) and reference >= 173.3
    assert estimate(22895, 36599 * 2, channels) > reference
    assert estimate(22895, 36599, channels + ["CY5"]) > reference
    assert estimate(22895, 36599, ["BF", "DAPI", "FITC"]) < reference


def test_preflight_estimate_is_null_for_unsupported_slide(state):
    state.attrs["nd2wsi"]["rgb"] = True
    controller = hc.HCorrectionController(state)
    assert controller.status()["estimated_seconds"] is None
    assert controller.start()["estimated_seconds"] is None
