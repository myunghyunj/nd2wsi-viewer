"""Publication/reader coordination without touching a shared research cache."""
import threading
from types import SimpleNamespace

import pytest

from nd2wsi.server import ViewerState


class Root(dict):
    def __init__(self, generation):
        super().__init__()
        self.generation = generation
        self.closed = False

    def close(self, delay=0):
        self.closed = True


@pytest.fixture
def activation(tmp_path, monkeypatch):
    cache = tmp_path / "private.nd2svs"
    cache.write_bytes(b"old")
    attrs = {"nd2wsi": {}, "omero": {"channels": []}}
    old = {"generation": "old", "kind": "full"}
    new = {"generation": "new", "kind": "overview", "h_correction": {"applied": True}}
    st = ViewerState(Root("old"), attrs, store_path=cache, manifest=old, generation="old")
    created = []

    def open_root(path, manifest):
        root = Root(manifest["generation"])
        created.append(root)
        return root, attrs

    monkeypatch.setattr(st, "_open_pyramid", open_root)
    replacement = SimpleNamespace(staging=tmp_path / "staging", final=cache,
                                  manifest=new, prior_manifest=old,
                                  publish=lambda: cache.write_bytes(b"new-pyramid"))
    return st, replacement, created, open_root


def test_commit_drains_reads_and_preserves_annotation_and_lut_state(activation):
    st, replacement, created, _ = activation
    annotations, lut = object(), object()
    st.annotations_path, st._lut_settings_store = annotations, lut
    old_root = st.root
    entered, release = threading.Event(), threading.Event()

    def reader():
        with st.busy:
            entered.set()
            release.wait(3)
            assert not old_root.closed

    worker = threading.Thread(target=reader)
    worker.start()
    assert entered.wait(1)
    commit = threading.Thread(target=st.replace_pyramid, args=(replacement,))
    commit.start()
    assert st.generation == "old"
    release.set()
    worker.join(3)
    commit.join(3)
    assert not commit.is_alive()
    assert old_root.closed and created[0].closed
    assert st.generation == "new" and not st.busy.closed
    assert st.annotations_path is annotations and st._lut_settings_store is lut


def test_staging_open_failure_does_not_close_live_root(activation, monkeypatch):
    st, replacement, _, _ = activation
    monkeypatch.setattr(st, "_open_pyramid", lambda *_: (_ for _ in ()).throw(ValueError("invalid staging")))
    with pytest.raises(ValueError, match="invalid staging"):
        st.replace_pyramid(replacement)
    assert not st.root.closed and not st.busy.closed
    assert replacement.final.read_bytes() == b"old"


def test_denied_publication_reopens_old_cache(activation):
    st, replacement, _, _ = activation
    replacement.publish = lambda: (_ for _ in ()).throw(PermissionError("other reader"))
    with pytest.raises(PermissionError, match="other reader"):
        st.replace_pyramid(replacement)
    assert st.generation == "old" and not st.root.closed and not st.busy.closed
    assert replacement.final.read_bytes() == b"old"


def test_saved_but_activation_failed_is_recoverable(activation, monkeypatch):
    st, replacement, _, open_root = activation

    def fail_final(path, manifest):
        if path == replacement.final:
            raise OSError("temporarily unavailable")
        return open_root(path, manifest)

    monkeypatch.setattr(st, "_open_pyramid", fail_final)
    with pytest.raises(RuntimeError, match="was saved but could not be opened"):
        st.replace_pyramid(replacement)
    assert st.manifest["h_correction"]["applied"] and st.generation == "new"
    assert st.busy.closed and replacement.final.read_bytes() == b"new-pyramid"
    monkeypatch.setattr(st, "_open_pyramid", open_root)
    monkeypatch.setattr("nd2wsi.cache.read_manifest", lambda *a, **k: replacement.manifest)
    st.refresh_pyramid()
    assert not st.busy.closed and not st.root.closed


def test_other_window_generation_reopens_once(activation, monkeypatch):
    st, replacement, created, _ = activation
    monkeypatch.setattr("nd2wsi.cache.read_manifest", lambda *a, **k: replacement.manifest)
    replacement.publish()
    st.refresh_pyramid()
    assert st.generation == "new" and not st.busy.closed
    st.refresh_pyramid()
    assert len(created) == 1
