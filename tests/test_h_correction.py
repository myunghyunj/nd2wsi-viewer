"""Persistent correction controller preserves committed state across jobs."""

import copy
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from nd2wsi import h_correction as hc
from nd2wsi import h_persistence as hp
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
        manifest={"source": quick_fingerprint(source), "generation": "fixture-generation"},
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


def mark_applied(state):
    state.generation = "committed-corrected-generation"
    state.manifest.update(generation=state.generation, h_correction={
        "applied": True, "correction_id": "persisted-revision", "channel_indices": [3],
    })


def fake_persistence(state, monkeypatch):
    """Exercise transaction ownership without rebuilding image arrays again."""
    prepared = []

    class Replacement:
        def __init__(self, operation, event):
            self.operation, self.event = operation, event
            self.closed = self.published = False
            self.manifest = copy.deepcopy(state.manifest)
            self.manifest["generation"] = operation + "-committed-generation"
            if operation == "apply":
                self.manifest["h_correction"] = {
                    "applied": True, "correction_id": "new-persisted-revision", "channel_indices": [3],
                }
            else:
                self.manifest.pop("h_correction", None)

        def publish(self):
            assert not self.event.is_set()
            self.published = True
            return self.manifest

        def close(self):
            self.closed = True

    def prepare(operation, event, progress):
        if event.is_set():
            raise hp.PreparationCancelled()
        progress(0.5)
        result = Replacement(operation, event)
        prepared.append(result)
        return result

    def replace(replacement):
        assert state.busy.active == 0
        state.manifest = copy.deepcopy(replacement.publish())
        state.generation = state.manifest["generation"]

    state.replace_pyramid = replace
    monkeypatch.setattr(hc, "_metadata_for_state", metadata)
    monkeypatch.setattr(hc, "_fit_model", fake_fitter)
    monkeypatch.setattr(hp, "prepare_apply", lambda _state, model, result, event, progress: prepare("apply", event, progress))
    monkeypatch.setattr(hp, "prepare_revoke", lambda _state, event, progress: prepare("revoke", event, progress))
    return prepared


def test_controller_applies_then_rebuilds_raw_with_committed_generations(state, monkeypatch):
    prepared = fake_persistence(state, monkeypatch)
    controller = hc.ShadingCorrectionController(state)
    assert controller.status()["state"] == "idle"
    assert controller.status()["applied"] is False
    controller.start("h")
    applied = done(controller)
    assert applied["state"] == "ready"
    assert applied["applied"] is True
    assert applied["generation"] == "apply-committed-generation"
    assert applied["correction_id"] == "new-persisted-revision"
    assert applied["channels"] == ["FITC"]
    assert applied["pct"] == 100
    assert applied["operation"] is None
    assert applied["can_cancel"] is False
    assert prepared[0].published and prepared[0].closed
    with pytest.raises(ValueError, match="Revoke"):
        controller.start("h")
    controller.revoke()
    reverted = done(controller)
    assert reverted["state"] == "idle"
    assert reverted["applied"] is False
    assert reverted["correction_id"] is None
    assert reverted["generation"] == "revoke-committed-generation"
    assert reverted["channels"] == []
    assert [item.operation for item in prepared] == ["apply", "revoke"]
    assert all(item.published and item.closed for item in prepared)
    with pytest.raises(ValueError, match="no applied"):
        controller.revoke()
    controller.close()


def test_reopened_controller_restores_applied_status_from_manifest(state):
    mark_applied(state)
    controller = hc.ShadingCorrectionController(state)
    status = controller.status()
    assert status["state"] == "ready"
    assert status["applied"] is True
    assert status["correction_id"] == "persisted-revision"
    assert status["generation"] == "committed-corrected-generation"
    assert status["channels"] == ["FITC"]
    assert status["pct"] == 100
    assert status["can_cancel"] is False
    assert status["estimated_seconds"] > 0
    assert controller._thread is None


def test_reopened_controller_reports_all_three_corrected_channels(state):
    mark_applied(state)
    state.manifest["h_correction"]["channel_indices"] = [3, 0, 1]
    status = hc.ShadingCorrectionController(state).status()
    assert status["channels"] == ["FITC", "CY3", "BF"]
    assert status["applied"] is True


def test_bf_remains_a_target_without_fluorescence_channels(state):
    state.attrs["omero"]["channels"][0]["label"] = "CY5"
    state.attrs["omero"]["channels"][3]["label"] = "GFP"
    controller = hc.ShadingCorrectionController(state)
    status = controller.status()
    assert status["supported"] is True
    assert status["state"] == "idle"
    assert hc._mapping(["Bright Field", "DAPI"]) == {"BF": 0, "DAPI": 1}
    assert controller._thread is None


def test_consensus_is_default_without_bf_but_loading_does_not_start_work(state, monkeypatch):
    state.attrs["omero"]["channels"][1]["label"] = "unknown"
    controller = hc.ShadingCorrectionController(state)
    status = controller.status()
    assert status["supported"] and not status["applied"]
    assert status["method"] == "bffree"
    assert not status["methods"]["h"]["supported"]
    assert status["methods"]["bffree"]["supported"]
    assert controller._thread is None
    calls = []
    monkeypatch.setattr(controller, "_start", lambda *args: calls.append(args))
    controller.start()
    controller.start("h")
    assert calls == [("apply", "bffree"), ("apply", "h")]


def test_consensus_requires_a_correctable_channel_but_legacy_dapi_mapping_loads():
    with pytest.raises(hc.UnsupportedCorrection, match="FITC or CY3"):
        hc._mapping(["BF", "DAPI"], "bffree")
    assert hc._mapping(["BF", "DAPI"], "bffree", require_correctable=False) == {"DAPI": 1}


def test_bffree_input_preparation_never_reads_bf_and_preserves_edge_means(state, tmp_path):
    mapping = hc._mapping([ch["label"] for ch in state.attrs["omero"]["channels"]], "bffree")
    assert mapping == {"CY3": 0, "DAPI": 2, "FITC": 3}
    read_indices = set()

    def read(ci, y0, y1, x0, x1):
        assert ci != 1
        read_indices.add(ci)
        return state.root["0"][ci, y0:y1, x0:x1]

    raw32 = hc._prepare_bffree_inputs(state, tmp_path, mapping, read, lambda: None, lambda _: None)
    try:
        assert read_indices == {0, 2, 3}
        for target, ci in enumerate(sorted(mapping.values())):
            np.testing.assert_array_equal(raw32[target], means(state.root["0"][ci], 32))
    finally:
        hc._close_maps([raw32])


def test_cancel_revoke_keeps_the_committed_corrected_state(state, monkeypatch):
    mark_applied(state)
    fake_persistence(state, monkeypatch)
    before = copy.deepcopy(state.manifest)
    entered, release = threading.Event(), threading.Event()

    def prepare(_state, event, progress):
        progress(0.25)
        entered.set()
        release.wait(3)
        assert event.is_set()
        raise hp.PreparationCancelled()

    monkeypatch.setattr(hp, "prepare_revoke", prepare)
    controller = hc.ShadingCorrectionController(state)
    controller.revoke()
    assert entered.wait(3)
    running = controller.status()
    assert running["state"] == "rebuilding"
    assert running["applied"] is True
    assert running["operation"] == "revoke"
    assert running["can_cancel"] is True
    cancelled = controller.cancel()
    assert cancelled["state"] == "cancelled"
    assert cancelled["applied"] is True
    release.set()
    status = done(controller)
    assert status["state"] == "cancelled"
    assert status["applied"] is True
    assert status["correction_id"] == "persisted-revision"
    assert state.manifest == before
    assert status["generation"] == before["generation"]


@pytest.mark.parametrize("operation", ["apply", "revoke"])
@pytest.mark.parametrize("phase", ["prepare", "publish"])
def test_failure_preserves_committed_correction_and_generation(state, monkeypatch, operation, phase):
    if operation == "revoke":
        mark_applied(state)
    prepared = fake_persistence(state, monkeypatch)
    before = copy.deepcopy(state.manifest)

    def fail(*_):
        raise OSError("injected replacement failure")

    if phase == "prepare":
        monkeypatch.setattr(hp, "prepare_" + operation, fail)
    else:
        state.replace_pyramid = fail
    controller = hc.ShadingCorrectionController(state)
    (lambda: controller.start("h") if operation == "apply" else controller.revoke())()
    status = done(controller)
    assert status["state"] == "failed"
    assert "injected replacement failure" in status["reason"]
    assert status["applied"] is (operation == "revoke")
    assert status["generation"] == before["generation"]
    assert state.manifest == before
    assert not any(item.published for item in prepared)
    assert all(item.closed for item in prepared)


def test_publish_disables_cancellation_until_atomic_activation_completes(state, monkeypatch):
    prepared = fake_persistence(state, monkeypatch)
    original_replace = state.replace_pyramid
    entered, release = threading.Event(), threading.Event()

    def replace(replacement):
        entered.set()
        release.wait(3)
        original_replace(replacement)

    state.replace_pyramid = replace
    controller = hc.ShadingCorrectionController(state)
    controller.start("h")
    assert entered.wait(3)
    assert controller.status()["state"] == "publishing"
    assert controller.status()["can_cancel"] is False
    assert controller.cancel()["state"] == "publishing"
    assert not controller._cancel.is_set()
    release.set()
    status = done(controller)
    assert status["state"] == "ready" and status["applied"] is True
    assert prepared[0].published and prepared[0].closed


def test_changed_generation_after_preparation_cannot_publish(state, monkeypatch):
    prepared = fake_persistence(state, monkeypatch)
    prepare = hp.prepare_apply

    def changed(*args):
        replacement = prepare(*args)
        state.generation = "another-window-committed-generation"
        state.manifest["generation"] = state.generation
        return replacement

    monkeypatch.setattr(hp, "prepare_apply", changed)
    controller = hc.ShadingCorrectionController(state)
    controller.start("h")
    status = done(controller)
    assert not any(item.published for item in prepared)
    assert all(item.closed for item in prepared)
    assert status["applied"] is False
    assert status["generation"] == "another-window-committed-generation"


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
    controller = hc.ShadingCorrectionController(state)
    controller.start("h")
    assert entered.wait(3)
    first_thread = controller._thread
    assert controller.start("h")["state"] == "fitting"
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
    controller = hc.ShadingCorrectionController(state)
    controller.start("h")
    status = done(controller)
    assert status["state"] == "unchanged"
    assert status["channels"] == []
    assert status["correction_id"] is None
    controller.close()


def test_model_validation_abstention_is_readable_and_never_replaces_pyramid(state, monkeypatch):
    from nd2wsi import h_model

    before = copy.deepcopy(state.manifest)
    original_root = state.root
    code = "insufficient_repeat_cells_heldout_rows"

    def abstain(*args, **kwargs):
        kwargs["progress"](0.12)
        raise h_model.Unsupported(code)

    def forbidden(*args, **kwargs):
        raise AssertionError("An unverified correction must not prepare or publish a pyramid")

    monkeypatch.setattr(hc, "_metadata_for_state", metadata)
    monkeypatch.setattr(h_model, "fit", abstain)
    monkeypatch.setattr(hp, "prepare_apply", forbidden)
    state.replace_pyramid = forbidden
    controller = hc.ShadingCorrectionController(state)
    controller.start("h")
    status = done(controller)
    assert status["state"] == "unsupported"
    assert status["supported"] is True  # The default correction remains supported.
    assert status["methods"]["h"]["supported"] is False
    assert status["methods"]["bffree"]["supported"] is True
    assert status["applied"] is False
    assert status["reason_code"] == code
    assert "Too few usable repeated fields" in status["reason"]
    assert "independent validation rows" in status["reason"]
    assert code not in status["reason"]
    assert status["pct"] == 42
    assert status["can_cancel"] is False
    assert state.manifest == before
    assert state.root is original_root
    assert status["generation"] == before["generation"]
    controller.close()


@pytest.mark.parametrize(
    "change,reason",
    [
        (lambda s: setattr(s, "plate", object()), "single-frame"),
        (lambda s: s.attrs["nd2wsi"].update(rgb=True), "scalar"),
        (lambda s: s.attrs["nd2wsi"]["selection"].update(z="max"), "projections"),
        (lambda s: s.attrs["omero"]["channels"][0].update(label="FITC"), "duplicate"),
        (lambda s: s.attrs["nd2wsi"]["levels"].pop(0), "level 0"),
    ],
)
def test_unsupported_state_never_starts_worker(state, change, reason):
    change(state)
    controller = hc.ShadingCorrectionController(state)
    status = controller.start("h")
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
    def forbidden_camera_metadata(*args, **kwargs):
        raise AssertionError("BF-free must never inspect camera or BF geometry")
    monkeypatch.setattr(Reader, "unstructured_metadata", forbidden_camera_metadata)
    bffree = hc._metadata_for_state(state, "bffree")
    assert bffree["channel_mapping"] == {"CY3": 0, "DAPI": 2, "FITC": 3}
    assert "channel_acquisition_settings" not in bffree
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
    controller = hc.ShadingCorrectionController(state)
    controller.start("h")
    status = done(controller)
    assert status["state"] == "failed"
    assert "closed" in status["reason"]
    assert status["correction_id"] is None
    controller.close()


def test_eta_is_estimated_only_after_progress(state, monkeypatch):
    controller = hc.ShadingCorrectionController(state)
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
    controller = hc.ShadingCorrectionController(state)
    controller.start("h")
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
        controller = hc.ShadingCorrectionController(state)
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
    assert estimate(22895, 36599, ["CY5", "BF", "DAPI", "FITC"]) < reference
    assert estimate(22895, 36599, ["BF"]) > 0


def test_preflight_estimate_is_null_for_unsupported_slide(state):
    state.attrs["nd2wsi"]["rgb"] = True
    controller = hc.ShadingCorrectionController(state)
    assert controller.status()["estimated_seconds"] is None
    assert controller.start("h")["estimated_seconds"] is None
