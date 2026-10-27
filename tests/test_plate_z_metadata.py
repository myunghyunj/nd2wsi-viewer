"""Acquisition direction remains explicit metadata, never an invented default."""

from types import SimpleNamespace

import numpy as np
import pytest

from nd2wsi import plate
from nd2wsi.reader import ChannelInfo


@pytest.mark.parametrize("direction", [True, False, None, "missing", 0, "false"])
def test_direction_metadata_and_frame_indices_survive_plate_initialization(
    tmp_path, monkeypatch, direction,
):
    parameters = SimpleNamespace(homeIndex=2, stepUm=2.0)
    if direction != "missing":
        parameters.bottomToTop = direction
    f = SimpleNamespace(
        sizes={"T": 2, "P": 2, "Z": 6, "Y": 8, "X": 8},
        read_frame=lambda _: np.zeros((8, 8), dtype=np.uint16),
        experiment=[SimpleNamespace(type="ZStackLoop", parameters=parameters)],
        frame_metadata=lambda seq: SimpleNamespace(channels=[SimpleNamespace(
            time=SimpleNamespace(relativeTimeMs=1000.0 * (seq // 12)),
        )]),
    )
    monkeypatch.setattr(plate, "_channel_infos", lambda *_: [ChannelInfo("Test", (255, 255, 255))])
    monkeypatch.setattr(plate, "_nd2_pixel_size", lambda _: (None, None))
    monkeypatch.setattr(plate, "objective_magnification", lambda _: None)
    source = plate.PlateSource.__new__(plate.PlateSource)
    source.tile = 256
    source.path = tmp_path / "metadata-only-fixture.nd2"
    source.path.write_bytes(b"isolated metadata test")
    # Exercise real public metadata construction without cache/image sampling.
    source._windows = lambda _: [{"start": 0, "end": 65535, "min": 0, "max": 65535}]
    with source.path.open("rb") as stream:
        source._fd = stream.fileno()
        source._init_from_file(f)
    meta = source.attrs["nd2wsi"]["plate"]
    assert meta["bottomToTop"] is (direction if isinstance(direction, bool) else None)
    assert meta["zHome"] == 2
    assert meta["zStepUm"] == 2.0
    assert meta["timesMs"] == [0.0, 1000.0]
    for t in range(2):
        for p in range(2):
            for z in range(6):
                assert source.seq(t, p, z) == t * 12 + p * 6 + z
