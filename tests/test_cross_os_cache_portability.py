"""End-to-end cross-OS portability tests (exFAT mtime_ns skew + Unicode NFC/NFD)."""

from __future__ import annotations

import copy
import json
import os
import shutil
import threading
import unicodedata
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from nd2_fixture import write_synthetic_nd2
from test_h_persistence import _original_state, _prepare
from test_plate import H, P, T, W, Z, value
from test_plate import plate_nd2 as _plate_nd2_fixture

from nd2wsi import cli
from nd2wsi import h_correction as hc
from nd2wsi import h_persistence as hp
from nd2wsi.annotation_sidecars import (
    _legacy_sidecar_matches,
    _manifest_source_path,
    annotations_sidecar,
    plate_annotations_sidecar,
)
from nd2wsi.cache import (
    ANNOTATIONS_DIR,
    CACHE_SUFFIX,
    MANAGED_DIR,
    quick_fingerprint,
    read_manifest,
)
from nd2wsi.convert import ensure_cache
from nd2wsi.direct import open_nd2_backed
from nd2wsi.h_model import Model
from nd2wsi.metal_viewport.source import RawTileSource
from nd2wsi.plate import THUMB_K, WINDOWS_KEY, PlateSource, PlateStore, plate_container
from nd2wsi.server import SlideRegistry
from nd2wsi.server_series import _associated_source_path
from nd2wsi.storage.single_file import write_entry
from nd2wsi.window_sessions import create_window_session

plate_nd2 = _plate_nd2_fixture

HOUR_NS = 3600 * 1_000_000_000
EXFAT_10MS_NS = 10_000_000

EXFAT_SKEWS = [
    pytest.param(lambda ns: ns + 9 * HOUR_NS, id="plus-9h-kst"),
    pytest.param(lambda ns: ns - 9 * HOUR_NS, id="minus-9h-kst"),
    pytest.param(
        lambda ns: (ns // EXFAT_10MS_NS) * EXFAT_10MS_NS + HOUR_NS,
        id="plus-1h-dst-10ms-trunc",
    ),
    pytest.param(
        lambda ns: (ns // EXFAT_10MS_NS) * EXFAT_10MS_NS - HOUR_NS,
        id="minus-1h-dst-10ms-trunc",
    ),
]


def _simulate_norm_sensitive_fs(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Enforce exact UTF-8 codepoint matching under ``root`` (Windows/Linux behavior)."""
    real_exists = Path.exists
    real_is_symlink = Path.is_symlink
    real_is_dir = Path.is_dir
    real_resolve = Path.resolve
    root_resolved = real_resolve(root)

    def _in_root(p: Path) -> bool:
        try:
            real_resolve(p.parent).relative_to(root_resolved)
            return True
        except Exception:
            return False

    def _missing_exact_entry(p: Path) -> bool:
        if not p.name or p.name.isascii() or not _in_root(p):
            return False
        try:
            return p.name not in os.listdir(p.parent)
        except OSError:
            return True

    def strict_exists(self: Path, *args: object, **kwargs: object) -> bool:
        if _missing_exact_entry(self):
            return False
        return real_exists(self, *args, **kwargs)

    def strict_is_symlink(self: Path) -> bool:
        if _missing_exact_entry(self):
            return False
        return real_is_symlink(self)

    def strict_is_dir(self: Path, *args: object, **kwargs: object) -> bool:
        if _missing_exact_entry(self):
            return False
        return real_is_dir(self, *args, **kwargs)

    def strict_resolve(self: Path, strict: bool = False) -> Path:
        if _missing_exact_entry(self):
            if strict:
                raise FileNotFoundError(str(self))
            # Collapse ".." via parent resolution while preserving exact non-ASCII leaf spelling.
            return real_resolve(self.parent) / self.name
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "exists", strict_exists)
    monkeypatch.setattr(Path, "is_symlink", strict_is_symlink)
    monkeypatch.setattr(Path, "is_dir", strict_is_dir)
    monkeypatch.setattr(Path, "resolve", strict_resolve)


def _create_small_nd2(path: Path) -> tuple[Path, np.ndarray]:
    rng = np.random.default_rng(19)
    img = rng.integers(10, 4000, size=(600, 640, 2), dtype=np.uint16)
    channels = [("CY5", "#FF0000"), ("DAPI", "#0000FF")]
    write_synthetic_nd2(path, img, channels, pixel_um=0.5)
    return path, np.moveaxis(img, -1, 0)


def _rename_exact(path: Path, new_name: str) -> Path:
    """Rename ``path`` to ``new_name`` even on normalization-insensitive macOS APFS."""
    target = path.with_name(new_name)
    if target.name in os.listdir(path.parent):
        return target
    staging = path.with_name(f".rename-tmp-{os.getpid()}{path.suffix}")
    path.rename(staging)
    staging.rename(target)
    return target


# ---- 1. Overview cache: SlideRegistry._add_overview ------------------------


@pytest.mark.parametrize("skew_fn", EXFAT_SKEWS)
def test_overview_stays_source_backed_with_annotations_across_exfat_skew_and_nfc_nfd(
    tmp_path, skew_fn
):
    pytest.importorskip("limnd2")
    pytest.importorskip("nd2")

    nfc_name = unicodedata.normalize("NFC", "샘플_테스트.nd2")
    nfd_name = unicodedata.normalize("NFD", "샘플_테스트.nd2")
    assert nfc_name != nfd_name

    slide, cyx = _create_small_nd2(tmp_path / nfc_name)
    store = ensure_cache(slide, tile=512)
    manifest = read_manifest(store)
    assert manifest is not None and manifest["kind"] == "overview"
    orig_gen = manifest["generation"]

    # Simulate cache created on macOS (NFD name/path + skewed exFAT mtime_ns).
    manifest["source"]["mtime_ns"] = skew_fn(int(manifest["source"]["mtime_ns"]))
    manifest["source"]["name"] = nfd_name
    manifest["source"]["relative_path"] = f"../../{nfd_name}"
    write_entry(store, "manifest.json", json.dumps(manifest).encode("utf-8"))

    reg = SlideRegistry()
    try:
        sid = reg.add_store(store)
        st = reg.get(sid)
        assert st is not None
        meta = st.attrs["nd2wsi"]
        assert meta["kind"] == "source-backed"
        assert not st.generation.endswith("-degraded")
        assert st.generation == orig_gen
        assert st.annotations_path is not None
        assert np.array_equal(st.root["0"][:, 10:20, 20:30], cyx[:, 10:20, 20:30])
        assert not list(store.parent.glob("*.corrupt-*"))
    finally:
        reg.close_all(immediate=True)


def test_sid_for_normalizes_nfc_and_nfd_paths_to_same_tab_id(tmp_path):
    nfc = tmp_path / unicodedata.normalize("NFC", "café_슬라이드.nd2svs")
    nfd = tmp_path / unicodedata.normalize("NFD", "café_슬라이드.nd2svs")
    assert str(nfc) != str(nfd)
    assert SlideRegistry.sid_for(nfc) == SlideRegistry.sid_for(nfd)


def test_add_store_recognizes_nfd_managed_full_container_for_rebuild(tmp_path, monkeypatch):
    pytest.importorskip("limnd2")
    pytest.importorskip("nd2")

    nfc_name = unicodedata.normalize("NFC", "샘플_리빌드.nd2")
    slide, _ = _create_small_nd2(tmp_path / nfc_name)
    store = ensure_cache(slide, tile=512, kind="full")
    nfd_store = _rename_exact(store, unicodedata.normalize("NFD", store.name))

    # Force semantic mismatch on the initial add_store check while source is readable;
    # add_store must recognize nfd_store as the managed cache for slide and rebuild
    # via ensure_cache instead of raising "cache ... is outdated".
    from nd2wsi import convert as convert_mod

    called = []
    real_ensure = ensure_cache
    real_semantics = convert_mod._store_matches_source_semantics
    semantics_calls = 0

    def _stale_once(*a, **kw):
        nonlocal semantics_calls
        semantics_calls += 1
        if semantics_calls == 1:
            return False
        return real_semantics(*a, **kw)

    def _recording_ensure(src, **kwargs):
        called.append((Path(src), kwargs))
        return real_ensure(src, **kwargs)

    monkeypatch.setattr("nd2wsi.convert._store_matches_source_semantics", _stale_once)
    monkeypatch.setattr("nd2wsi.convert.ensure_cache", _recording_ensure)

    reg = SlideRegistry()
    try:
        sid = reg.add_store(nfd_store, source_path=slide)
        assert reg.get(sid) is not None
        assert len(called) == 1
    finally:
        reg.close_all(immediate=True)


# ---- 2. Plate cache: SlideRegistry._add_plate_cache & PlateStore -----------


@pytest.mark.parametrize("skew_fn", EXFAT_SKEWS)
def test_registry_add_plate_cache_and_recorded_windows_survive_exfat_skew_and_nfc_nfd(
    tmp_path, plate_nd2, monkeypatch, skew_fn
):
    monkeypatch.setattr(PlateStore, "start", lambda self: None)

    nfc_name = unicodedata.normalize("NFC", "café_플레이트.nd2")
    nfd_name = unicodedata.normalize("NFD", "café_플레이트.nd2")
    slide = tmp_path / nfc_name
    shutil.copy2(plate_nd2, slide)

    first = PlateSource(slide)
    try:
        expected_windows = [ch["window"] for ch in first.attrs["omero"]["channels"]]
        assert np.all(first.reduced(0, 0, 0, THUMB_K) == value(0, 0, 0))
    finally:
        first.close()

    package = plate_container(slide)
    manifest = read_manifest(package)
    orig_gen = manifest["generation"]
    skewed_ns = skew_fn(int(manifest["source"]["mtime_ns"]))
    manifest["source"]["mtime_ns"] = skewed_ns
    manifest["source"]["name"] = nfd_name
    manifest["source"]["relative_path"] = f"../../{nfd_name}"
    manifest[WINDOWS_KEY]["source"]["mtime_ns"] = skewed_ns
    write_entry(package, "manifest.json", json.dumps(manifest).encode("utf-8"))

    # Verify recorded_windows matches without re-measuring.
    probe = PlateSource(slide, store=False)
    try:
        assert PlateStore.recorded_windows(probe, cache_path=package) == expected_windows
    finally:
        probe.close()

    def _fail_measure(*_args, **_kwargs):
        raise AssertionError("cross-OS plate cache should reuse recorded display windows")

    monkeypatch.setattr(PlateSource, "_measure_windows", _fail_measure)

    reg = SlideRegistry()
    try:
        sid = reg.open_path(package)
        st = reg.get(sid)
        assert st is not None and st.plate is not None and st.plate.store is not None
        assert st.plate.store.manifest["generation"] == orig_gen
        assert np.all(st.plate.store.get(0, 0, 0) == value(0, 0, 0))
        assert not list(package.parent.glob("*.corrupt-*"))
    finally:
        reg.close_all(immediate=True)


def test_plate_container_for_finds_legacy_nfd_directory_on_norm_sensitive_fs(
    tmp_path, plate_nd2, monkeypatch
):
    monkeypatch.setattr(PlateStore, "start", lambda self: None)

    nfc_name = unicodedata.normalize("NFC", "샘플_레거시.nd2")
    slide = tmp_path / nfc_name
    shutil.copy2(plate_nd2, slide)

    source = PlateSource(slide, store=False)
    try:
        nfc_legacy = plate_container(slide).with_suffix(CACHE_SUFFIX)
        nfd_legacy = nfc_legacy.with_name(unicodedata.normalize("NFD", nfc_legacy.name))
        shape = (T, P, Z, 1, H // THUMB_K, W // THUMB_K)
        PlateStore._create(source, nfd_legacy, quick_fingerprint(slide), shape)
        assert nfd_legacy.name in os.listdir(nfd_legacy.parent)

        _simulate_norm_sensitive_fs(monkeypatch, tmp_path)
        found = PlateStore._container_for(source, None)
        assert found == nfd_legacy
    finally:
        source.close()


# ---- 3. Shading-corrected nd2wsi-cache/5: open_nd2_backed & h_persistence --


def _fitted_h_model(tmp_path: Path) -> Model:
    path = tmp_path / "fitted-model.npz"
    yy, xx = np.indices((8, 8))
    pattern = 0.15 * np.sin(xx * np.pi / 4) + 0.07 * np.cos(yy * np.pi / 4)
    np.savez_compressed(
        path,
        basis=np.diag([0.6, 0.7]),
        oldbasis=np.diag([0.6, 0.7]),
        templates=np.stack([pattern, pattern]),
        oldtemplates=np.stack([pattern, pattern]),
        delta=np.zeros((4, 4)),
        local=np.array([False, False]),
        old=np.array([False, False]),
        channel_indices=np.array([1, 2]),
        enabled=np.array([True, False]),
    )
    return Model(path)


@pytest.mark.parametrize("skew_fn", EXFAT_SKEWS)
def test_open_nd2_backed_and_h_persistence_tolerate_exfat_skew(
    tmp_path, monkeypatch, skew_fn
):
    rng = np.random.default_rng(11)
    native = rng.integers(0, 8000, (4, 67, 73), dtype=np.uint16)
    original = _original_state(tmp_path, native)
    model = _fitted_h_model(tmp_path)
    try:
        prepared = _prepare(original, model)
        prepared.publish()
        prepared.close()

        manifest = read_manifest(original.container_path)
        assert manifest is not None and manifest["format"] == "nd2wsi-cache/5"

        # Mutate manifest source.mtime_ns by cross-OS exFAT skew while provenance keeps original mtime_ns.
        # Exercises BOTH open_nd2_backed (manifest vs live source) AND load_model (provenance vs manifest).
        manifest["source"]["mtime_ns"] = skew_fn(int(manifest["source"]["mtime_ns"]))
        write_entry(original.container_path, "manifest.json", json.dumps(manifest).encode("utf-8"))

        loaded = hp.load_model(original.container_path, manifest)
        loaded.data.close()

        reader = SimpleNamespace(close=lambda: None)
        monkeypatch.setattr("nd2.ND2File", lambda _: reader)
        monkeypatch.setattr("nd2wsi.reader.is_source_backable", lambda *_: (True, ""))
        monkeypatch.setattr("nd2wsi.reader.plane_view", lambda *_: (original.root["0"], None))
        reopened, attrs = open_nd2_backed(original.source_path, original.container_path)
        try:
            assert attrs["nd2wsi"]["kind"] == "source-backed"
            np.testing.assert_array_equal(
                reopened["0"][1, 3:17, 7:29],
                model.correct(1, original.root["0"][1, 3:17, 7:29], 3, 7),
            )
        finally:
            reopened.close(delay=0)

        # PreparedReplacement.__init__ also accepts cross-OS skewed prior_manifest.
        original.manifest = manifest
        repl = hp.PreparedReplacement(original, threading.Event())
        repl.close()
    finally:
        model.data.close()


def test_h_correction_metadata_for_state_tolerates_exfat_skew(tmp_path, monkeypatch):
    source = tmp_path / "source.nd2"
    source.write_bytes(b"synthetic nd2 payload for metadata check")
    fp = quick_fingerprint(source)
    skewed_fp = {**fp, "mtime_ns": int(fp["mtime_ns"]) + 9 * HOUR_NS}

    native = np.zeros((3, 64, 64), dtype=np.uint16)
    state = SimpleNamespace(
        source_path=source,
        manifest={"source": skewed_fp, "generation": "gen-1"},
        attrs={
            "omero": {"channels": [{"label": "BF"}, {"label": "FITC"}, {"label": "DAPI"}]},
        },
        root={"0": native},
    )

    class _FakeReader:
        is_legacy = False
        is_rgb = False
        dtype = np.dtype("uint16")
        sizes = {"C": 3, "Y": 64, "X": 64}
        attributes = SimpleNamespace(compressionType="none", sequenceCount=1)
        metadata = SimpleNamespace(
            channels=[
                SimpleNamespace(channel=SimpleNamespace(name=n))
                for n in ("BF", "FITC", "DAPI")
            ]
        )

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

    monkeypatch.setattr("nd2.ND2File", lambda _: _FakeReader())
    meta = hc._metadata_for_state(state, method="bffree")
    assert meta["channel_names"] == ["BF", "FITC", "DAPI"]


# ---- 4. CLI view & tidy ----------------------------------------------------


def test_cli_view_reuses_cache_across_exfat_skew_and_nfc_nfd(tmp_path, monkeypatch, capsys):
    pytest.importorskip("limnd2")
    pytest.importorskip("nd2")

    nfc_name = unicodedata.normalize("NFC", "샘플_뷰.nd2")
    nfd_name = unicodedata.normalize("NFD", "샘플_뷰.nd2")
    slide, _ = _create_small_nd2(tmp_path / nfc_name)
    store = ensure_cache(slide, tile=512, kind="full")

    manifest = read_manifest(store)
    manifest["source"]["mtime_ns"] = (
        (int(manifest["source"]["mtime_ns"]) // EXFAT_10MS_NS) * EXFAT_10MS_NS + 9 * HOUR_NS
    )
    manifest["source"]["name"] = nfd_name
    write_entry(store, "manifest.json", json.dumps(manifest).encode("utf-8"))

    served: list[Path] = []
    monkeypatch.setattr("nd2wsi.server.serve", lambda stores, **kw: served.extend(stores))

    def _forbid_convert(*_args, **_kwargs):
        raise AssertionError("cli view should reuse matching cross-OS cache without rebuilding")

    monkeypatch.setattr("nd2wsi.convert.convert", _forbid_convert)

    rc = cli.main(["view", str(slide), "--store", str(store), "--tile", "512"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "reusing existing pyramid" in out
    assert served == [store]


def test_cli_tidy_move_annotations_detects_nfd_twin_on_norm_sensitive_fs(
    tmp_path, monkeypatch, capsys
):
    nfc_ann = unicodedata.normalize("NFC", "annotations_샘플.nd2.json")
    nfd_ann = unicodedata.normalize("NFD", "annotations_샘플.nd2.json")
    assert nfc_ann != nfd_ann

    managed = tmp_path / MANAGED_DIR / ANNOTATIONS_DIR
    managed.mkdir(parents=True)
    existing = managed / nfd_ann
    existing.write_text('{"items":[{"id":"mac"}]}', encoding="utf-8")
    stray = tmp_path / nfc_ann
    stray.write_text('{"items":[{"id":"win"}]}', encoding="utf-8")

    _simulate_norm_sensitive_fs(monkeypatch, tmp_path)
    rc = cli.main(["tidy", str(tmp_path), "--move-annotations"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "already lives in the managed folder" in out
    assert stray.exists()
    assert json.loads(existing.read_text(encoding="utf-8"))["items"] == [{"id": "mac"}]


# ---- 5. annotation_sidecars: NFC/NFD + strict folder confinement -----------


def test_manifest_source_path_nfc_nfd_and_folder_confinement_security(tmp_path, monkeypatch):
    nfc_name = unicodedata.normalize("NFC", "샘플.nd2")
    nfd_name = unicodedata.normalize("NFD", "샘플.nd2")
    assert nfc_name != nfd_name

    slides = tmp_path / "slides"
    other = tmp_path / "other"
    slides.mkdir()
    other.mkdir()
    (other / nfc_name).write_bytes(b"outside")
    slide_nfc = slides / nfc_name
    slide_nfc.write_bytes(b"inside")

    container = slides / MANAGED_DIR / "caches" / f"{nfc_name}--t0-p0-zmid.nd2svs"
    container.parent.mkdir(parents=True)
    container.write_bytes(b"cache")

    # 1. Native FS: NFD manifest against NFC on-disk file succeeds.
    found = _manifest_source_path(
        container, {"source": {"name": nfd_name, "relative_path": f"../../{nfd_name}"}}
    )
    assert found is not None and found.parent == slides.resolve()

    # 2. Simulated Windows/Linux normalization-sensitive FS: NFD manifest finds NFC file.
    _simulate_norm_sensitive_fs(monkeypatch, tmp_path)
    found_strict = _manifest_source_path(
        container, {"source": {"name": nfd_name, "relative_path": f"../../{nfd_name}"}}
    )
    assert found_strict == slide_nfc.resolve()

    # 3. Security: traversal outside slide folder is STILL strictly rejected!
    for attack in (
        f"../../../other/{nfc_name}",
        f"../../../other/{nfd_name}",
        str(other / nfc_name),
        f"../../sub/{nfc_name}",
    ):
        assert (
            _manifest_source_path(
                container, {"source": {"name": nfc_name, "relative_path": attack}}
            )
            is None
        )

    # 4. Security: symlink inside slide folder pointing outside is rejected even via NFD fallback!
    slide_nfc.unlink()
    slide_nfc.symlink_to(other / nfc_name)
    assert (
        _manifest_source_path(
            container, {"source": {"name": nfd_name, "relative_path": f"../../{nfd_name}"}}
        )
        is None
    )

    # 5. Security: directory named like source is rejected even via NFD fallback!
    slide_nfc.unlink()
    slide_nfc.mkdir()
    assert (
        _manifest_source_path(
            container, {"source": {"name": nfd_name, "relative_path": f"../../{nfd_name}"}}
        )
        is None
    )

    # 6. Security: NFKC fullwidth slash U+FF0F is NOT converted to "/" separator.
    fullwidth_name = "a\uff0fb.nd2"
    fullwidth_file = slides / fullwidth_name
    fullwidth_file.write_bytes(b"fullwidth")
    fw_found = _manifest_source_path(
        container, {"source": {"name": fullwidth_name, "relative_path": f"../../{fullwidth_name}"}}
    )
    assert fw_found == fullwidth_file.resolve()
    assert fw_found.parent == slides.resolve()
    assert (
        _manifest_source_path(
            container, {"source": {"name": "a/b.nd2", "relative_path": f"../../{fullwidth_name}"}}
        )
        is None
    )


def test_annotations_and_plate_sidecars_preserve_existing_nfd_and_write_canonical_nfc(
    tmp_path, monkeypatch
):
    nfc_name = unicodedata.normalize("NFC", "샘플_주석.nd2")
    nfd_name = unicodedata.normalize("NFD", "샘플_주석.nd2")
    assert nfc_name != nfd_name

    slide = tmp_path / nfc_name
    slide.write_bytes(b"nd2")
    ann_dir = tmp_path / MANAGED_DIR / ANNOTATIONS_DIR
    ann_dir.mkdir(parents=True)

    # Pre-existing NFD sidecars created by macOS: Windows/Linux opening NFC must find them!
    nfd_sidecar = ann_dir / unicodedata.normalize("NFD", f"annotations_{nfc_name}--t0-p0-z0.json")
    nfd_sidecar.write_text('{"items":[{"id":"roi-1"}]}', encoding="utf-8")
    nfd_plate_sidecar = ann_dir / unicodedata.normalize("NFD", f"annotations_{nfc_name}--site2.json")
    nfd_plate_sidecar.write_text('{"items":[{"id":"site-2"}]}', encoding="utf-8")

    _simulate_norm_sensitive_fs(monkeypatch, tmp_path)

    attrs = {"nd2wsi": {"source": nfc_name, "selection": {"t": 0, "p": 0, "z": 0}}}
    found = annotations_sidecar(slide, attrs, migrate=False)
    assert found == nfd_sidecar
    assert json.loads(found.read_text(encoding="utf-8"))["items"] == [{"id": "roi-1"}]

    found_plate = plate_annotations_sidecar(slide, attrs, 2, create_directory=False)
    assert found_plate == nfd_plate_sidecar

    # Brand-new sidecars for an NFD source name are created with canonical NFC names.
    fresh_attrs = {"nd2wsi": {"source": nfd_name, "selection": {"t": 0, "p": 0, "z": 5}}}
    brand_new = annotations_sidecar(slide, fresh_attrs, migrate=False)
    assert unicodedata.is_normalized("NFC", brand_new.name)
    brand_new_plate = plate_annotations_sidecar(slide, fresh_attrs, 7, create_directory=False)
    assert unicodedata.is_normalized("NFC", brand_new_plate.name)


def test_legacy_sidecar_matching_handles_nfc_nfd_and_ambiguity(tmp_path):
    nfc_stem = unicodedata.normalize("NFC", "샘플")
    nfd_stem = unicodedata.normalize("NFD", "샘플")
    nfc_nd2 = f"{nfc_stem}.nd2"
    nfd_nd2 = f"{nfd_stem}.nd2"

    # Declared source name matches across NFC/NFD.
    legacy = tmp_path / f"annotations_{nfd_stem}.json"
    legacy.write_text(json.dumps({"source": {"name": nfd_nd2}, "items": []}), encoding="utf-8")
    assert _legacy_sidecar_matches(legacy, nfc_nd2, tmp_path) is True

    # Undeclared legacy sidecar detects ambiguity when NFD sibling .jpg exists beside NFC .nd2.
    undeclared = tmp_path / "annotations_undeclared.json"
    undeclared.write_text(json.dumps({"items": []}), encoding="utf-8")
    (tmp_path / nfc_nd2).write_bytes(b"nd2")
    (tmp_path / f"{nfd_stem}.jpg").write_bytes(b"jpg")
    assert _legacy_sidecar_matches(undeclared, nfc_nd2, tmp_path) is False


def test_associated_source_path_tolerates_exfat_skew(tmp_path):
    svs = tmp_path / "slide.svs"
    svs.write_bytes(b"synthetic svs payload")
    fp = quick_fingerprint(svs)
    st = SimpleNamespace(
        source_path=svs,
        manifest={"source": {**fp, "mtime_ns": int(fp["mtime_ns"]) - 9 * HOUR_NS}},
    )
    assert _associated_source_path(st) == svs


# ---- 6. Intra-session strictness (Group B allow_mtime_skew=False) ----------


@pytest.mark.parametrize(
    "delta_ns",
    [
        pytest.param(-1, id="minus-1ns"),
        pytest.param(9 * HOUR_NS, id="plus-9h-exfat-skew"),
    ],
)
def test_add_plate_retries_strictly_on_any_intra_session_mtime_change(
    plate_nd2, monkeypatch, delta_ns
):
    from nd2wsi import cache as cache_mod
    from nd2wsi import plate as plate_mod

    actual = cache_mod.quick_fingerprint(plate_nd2)
    before = {**actual, "mtime_ns": int(actual["mtime_ns"]) + delta_ns}
    fingerprints = iter((before, actual, actual))
    real_source = plate_mod.PlateSource
    opened = []

    monkeypatch.setattr(cache_mod, "quick_fingerprint", lambda _p: next(fingerprints))

    def recording_source(path):
        src = real_source(path)
        opened.append(src)
        return src

    monkeypatch.setattr(plate_mod, "PlateSource", recording_source)
    reg = SlideRegistry()
    try:
        sid = reg.add_plate(plate_nd2)
        assert len(opened) == 2
        assert reg.get(sid).plate is opened[1]
    finally:
        reg.close_all(immediate=True)


def test_validate_before_publish_rejects_even_exfat_skew_intra_session(tmp_path, monkeypatch):
    rng = np.random.default_rng(29)
    native = rng.integers(0, 8000, (4, 67, 73), dtype=np.uint16)
    original = _original_state(tmp_path, native)
    model = _fitted_h_model(tmp_path)
    try:
        prepared = _prepare(original, model)
        try:
            # Even if inode/identity check were bypassed, fingerprints_match(..., allow_mtime_skew=False)
            # MUST reject a +9h mtime_ns shift during the same publish transaction.
            skewed = {
                **prepared._fingerprint,
                "mtime_ns": int(prepared._fingerprint["mtime_ns"]) + 9 * HOUR_NS,
            }
            monkeypatch.setattr(hp, "quick_fingerprint", lambda _p: skewed)
            with pytest.raises(ValueError, match="original ND2 changed during preparation"):
                prepared._validate_before_publish()
        finally:
            prepared.close()
    finally:
        model.data.close()


def test_metal_viewport_preservation_report_is_strict_on_mtime_skew(tmp_path):
    from test_metal_viewport_source import _store

    store_path, _attrs, _arrays = _store(tmp_path)
    session = create_window_session("agent", tmp_path / "sessions")
    source = RawTileSource(store_path, session)
    fp = quick_fingerprint(tmp_path / "research" / "specimen.ome.zarr" / ".zattrs")
    source.fingerprints_before["/fake/source.nd2"] = copy.deepcopy(fp)
    source.close()
    source.fingerprints_after["/fake/source.nd2"] = {
        **fp,
        "mtime_ns": int(fp["mtime_ns"]) + 9 * HOUR_NS,
    }
    report = source.preservation_report()
    assert report["unchanged"] is False


def test_nfc_nfd_cache_locks_and_sweep_coexist_with_2610_locks_on_norm_sensitive_fs(
    tmp_path, monkeypatch
):
    from nd2wsi.cache import CacheLock, sweep_stale_builds

    caches = tmp_path / "caches"
    caches.mkdir()
    nfc_family = unicodedata.normalize("NFC", "샘플_락.nd2--t0-p0-zmid.nd2svs")
    nfd_family = unicodedata.normalize("NFD", "샘플_락.nd2--t0-p0-zmid.nd2svs")
    assert nfc_family != nfd_family

    # 1. Without existing locks, both NFC and NFD container names pick the same NFC lock path.
    lock_from_nfd = CacheLock(caches / nfd_family)
    lock_from_nfc = CacheLock(caches / nfc_family)
    assert lock_from_nfd.path == lock_from_nfc.path
    assert unicodedata.is_normalized("NFC", lock_from_nfd.path.name)

    # 2. Simulate a live 2.6.10 builder holding an NFD lock and staging directory on exFAT/Linux.
    nfd_lock_file = caches / (nfd_family + ".lock")
    nfd_staging = caches / f"{nfd_family}.building-live2610"
    nfd_staging.mkdir()
    (nfd_staging / "chunk").write_bytes(b"in-flight")
    nfd_lock_file.write_text(
        CacheLock._payload("legacy-2610-gen").decode("utf-8"),
        encoding="utf-8",
    )

    _simulate_norm_sensitive_fs(monkeypatch, tmp_path)

    # 2.6.11 CacheLock(NFC) must discover and contend against the existing NFD sibling lock.
    contender = CacheLock(caches / nfc_family)
    assert contender.path == nfd_lock_file
    with pytest.raises(TimeoutError):
        contender.acquire(timeout=0.02, poll=0.005)

    # sweep_stale_builds must preserve the NFD staging dir while either NFD or NFC lock is live.
    assert sweep_stale_builds(caches) == 0
    assert (nfd_staging / "chunk").read_bytes() == b"in-flight"

    # Also test NFC staging dir paired with NFD live lock (cross-spelling sweep check).
    nfc_staging = caches / f"{nfc_family}.building-cross"
    nfc_staging.mkdir()
    assert sweep_stale_builds(caches) == 0
    assert nfc_staging.is_dir()

    # Once released, sweep_stale_builds cleans both stranded staging dirs.
    nfd_lock_file.write_text(
        CacheLock._payload("legacy-2610-gen", released=True).decode("utf-8"),
        encoding="utf-8",
    )
    assert sweep_stale_builds(caches) == 2
    assert not nfd_staging.exists() and not nfc_staging.exists()


def test_resolve_existing_path_and_cache_lock_init_route_through_filesystem_path(
    tmp_path, monkeypatch
):
    from nd2wsi import cache as cache_mod
    from nd2wsi.cache import CacheLock, resolve_existing_path

    seen: list[str] = []
    real_fs_path = cache_mod.filesystem_path

    def spy_fs_path(p, **kw):
        res = real_fs_path(p, **kw)
        seen.append(res)
        return res

    monkeypatch.setattr(cache_mod, "filesystem_path", spy_fs_path)

    caches = tmp_path / "caches"
    caches.mkdir()
    nfc_name = unicodedata.normalize("NFC", "샘플_경로.nd2svs")
    nfd_name = unicodedata.normalize("NFD", "샘플_경로.nd2svs")
    assert nfc_name != nfd_name

    # 1. resolve_existing_path routes exists/is_symlink/is_dir/scandir through filesystem_path
    #    while returning a plain Path matching the existing NFD sibling.
    nfd_file = caches / nfd_name
    nfd_file.write_bytes(b"cache")
    _simulate_norm_sensitive_fs(monkeypatch, tmp_path)

    found = resolve_existing_path(caches / nfc_name, kind="file")
    assert found == nfd_file
    assert real_fs_path(caches / nfc_name) in seen
    assert real_fs_path(caches) in seen

    # 2. CacheLock.__init__ routes its canonical-vs-arbiter existence check through filesystem_path.
    seen.clear()
    nfd_arbiter = caches / (nfd_name + ".lock.arbiter")
    nfd_arbiter.write_bytes(b"")
    lock = CacheLock(caches / nfc_name)
    assert lock.path == caches / (nfd_name + ".lock")
    expected_canonical_lock = real_fs_path(caches / (nfc_name + ".lock"))
    assert seen.count(expected_canonical_lock) >= 2
    assert real_fs_path(caches / (nfc_name + ".lock.arbiter")) in seen
    assert real_fs_path(caches) in seen
