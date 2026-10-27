"""Per-root member lookups for single-file stores, and one read per ND2 frame."""
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from nd2wsi import reader, render
from nd2wsi.convert import _ClosableGroup, open_store
from nd2wsi.storage.single_file import open_zarr_group


class _CountingGroup:
    def __init__(self, members):
        self.members = members
        self.lookups = []
        self.store = SimpleNamespace(closed=0)
        self.store.close = lambda: setattr(self.store, "closed", self.store.closed + 1)
        self.attrs = {"a": 1}

    def __getitem__(self, key):
        self.lookups.append(key)
        return self.members[key]()


def test_each_member_is_looked_up_once_per_root():
    group = _CountingGroup({"0": object, "1": object})
    root = _ClosableGroup(group)
    first = root["0"]
    assert root["0"] is first
    assert root["1"] is not first
    assert root["1"] is root["1"]
    assert group.lookups == ["0", "1"]
    assert root.attrs == {"a": 1}  # everything else still goes to the group


def test_missing_member_is_not_remembered():
    group = _CountingGroup({"0": object})
    root = _ClosableGroup(group)
    for _ in range(2):
        with pytest.raises(KeyError):
            root["9"]
    assert group.lookups == ["9", "9"]
    group.members["9"] = object
    assert root["9"] is root["9"]


def test_close_drops_members_with_the_connection():
    group = _CountingGroup({"0": object})
    root = _ClosableGroup(group)
    before = root["0"]
    root.close(delay=0)
    assert group.store.closed == 1
    # after close a lookup reaches the (closed) group again instead of
    # handing out an array bound to a dead connection
    assert root["0"] is not before
    assert group.lookups == ["0", "0"]


def test_concurrent_first_lookups_agree_on_one_member():
    gate = threading.Barrier(8)
    group = _CountingGroup({"0": object})
    root = _ClosableGroup(group)
    seen = []

    def lookup():
        gate.wait()
        seen.append(root["0"])

    threads = [threading.Thread(target=lookup) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(seen) == 8
    assert all(member is seen[0] for member in seen)
    assert root["0"] is seen[0]


@pytest.fixture()
def store_file(tmp_path):
    path = tmp_path / "pyramid.nd2svs"
    data = np.arange(2 * 16 * 16, dtype=np.uint16).reshape(2, 16, 16)
    group = open_zarr_group(path, prefix="store.ome.zarr", mode="a")
    try:
        group.create_array("0", data=data, chunks=(1, 8, 8))
        group.create_array("1", data=data[:, ::2, ::2], chunks=(1, 8, 8))
        group.attrs.update({
            "multiscales": [{"datasets": [{"path": "0"}, {"path": "1"}]}],
            "nd2wsi": {
                "levels": [
                    {"path": "0", "width": 16, "height": 16, "downsample": 1},
                    {"path": "1", "width": 8, "height": 8, "downsample": 2},
                ],
            },
        })
    finally:
        group.store.close()
    return path, data


def test_single_file_root_reuses_level_arrays_and_reads_the_same_pixels(store_file):
    path, data = store_file
    root, _ = open_store(path)
    try:
        assert isinstance(root, _ClosableGroup)
        level = root["0"]
        assert root["0"] is level
        assert root["1"] is not level
        np.testing.assert_array_equal(np.asarray(root["0"][:]), data)
        np.testing.assert_array_equal(np.asarray(root["1"][:]), data[:, ::2, ::2])
        np.testing.assert_array_equal(
            render._read_region(root, "0", 4, 2, 8, 8), data[:, 2:10, 4:12]
        )
        np.testing.assert_array_equal(
            render._read_channel_region(root, "1", 1, 0, 0, 8, 8), data[1, ::2, ::2]
        )
    finally:
        root.close()
    assert root.store._closed
    with pytest.raises(ValueError, match="closed"):
        root["0"]  # the closed connection is not hidden behind a stale array


def _fake_nd2(monkeypatch, sizes, frame):
    reads = []

    def read_frame(seq):
        reads.append(seq)
        return frame

    monkeypatch.setattr(reader, "_nd2_pixel_size", lambda _f: (None, None))
    monkeypatch.setattr(reader, "objective_magnification", lambda _f: None)
    f = SimpleNamespace(
        sizes=sizes,
        attributes=SimpleNamespace(compressionType="none"),
        read_frame=read_frame,
        dtype=frame.dtype,
        path="fake.nd2",
        metadata=SimpleNamespace(channels=[]),
    )
    return f, reads


@pytest.mark.parametrize(
    "sizes, frame, rgb, z, channels",
    [
        ({"Z": 3, "C": 2, "Y": 8, "X": 8}, np.ones((2, 8, 8), np.uint16), False, 1, 2),
        ({"Z": 3, "Y": 8, "X": 8}, np.ones((8, 8), np.uint16), False, 1, 1),
        ({"Y": 8, "X": 8, "S": 3}, np.ones((8, 8, 3), np.uint8), True, 0, 3),
    ],
)
def test_open_plane_reads_the_chosen_frame_once(monkeypatch, sizes, frame, rgb, z, channels):
    f, reads = _fake_nd2(monkeypatch, sizes, frame)
    source = reader.open_plane(f, reader.PlaneSelection(z="mid"))
    assert reads == [z]
    assert source.rgb is rgb
    assert source.selection["z"] == z
    assert source.shape == (channels, 8, 8)
    assert len(source.channels) == channels


def test_max_projection_reads_each_plane_once(monkeypatch):
    f, reads = _fake_nd2(monkeypatch, {"Z": 4, "Y": 8, "X": 8}, np.ones((8, 8), np.uint16))
    source = reader.open_plane(f, reader.PlaneSelection(z="max"))
    assert reads == [0, 1, 2, 3]
    assert source.rgb is False
    assert source.selection["z"] == "max"
