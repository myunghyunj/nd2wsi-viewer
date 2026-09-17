"""Site arrangement from stage positions, and the plate detection boundary."""

import pytest

from nd2wsi.plate import PLATE_MAX_FRAME_PX, is_plate_file, site_layout

# the demo acquisition, XYPosLoop points in file order (x, y in um)
DEMO_POINTS = [
    ("10(5)_PC", 23157.5, -23264.0),
    ("10(5)_MOI", 16507.7, -23264.0),
    ("10(6)_MOI", 16507.7, -1022.0),
    ("10(6)_PC", 24047.2, -1022.0),
    ("10(7)_PC", 24047.2, 20137.9),
    ("10(7)_MOI", 16483.8, 20137.9),
]


def _grid(points):
    layout = site_layout([(x, y) for _, x, y in points])
    rows = 1 + max(r for r, _ in layout)
    cols = 1 + max(c for _, c in layout)
    return layout, rows, cols


def test_demo_positions_form_three_rows_and_two_columns():
    layout, rows, cols = _grid(DEMO_POINTS)
    assert (rows, cols) == (3, 2)
    by_name = {name: rc for (name, _, _), rc in zip(DEMO_POINTS, layout)}
    # rows follow the dilution from the lowest stage y upward
    assert by_name["10(5)_PC"][0] == by_name["10(5)_MOI"][0] == 0
    assert by_name["10(6)_PC"][0] == by_name["10(6)_MOI"][0] == 1
    assert by_name["10(7)_PC"][0] == by_name["10(7)_MOI"][0] == 2
    # columns follow ascending stage x, so MOI (x 16.5 mm) sits before PC (x 23 mm)
    for dilution in ("10(5)", "10(6)", "10(7)"):
        assert by_name[f"{dilution}_MOI"][1] == 0
        assert by_name[f"{dilution}_PC"][1] == 1
    assert len(set(layout)) == 6  # every site has its own cell


def test_full_run_positions_put_pc_left_of_moi():
    # the 24 h acquisition, XYPosLoop points in file order. The columns
    # sit up to 11.6 mm apart with a 3.2 mm spread inside the MOI column,
    # which the gap rule must not split.
    points = [
        ("10(5)_MOI", 27741.2, -23714.5),
        ("10(5)_PC", 16507.7, -23264.0),
        ("10(6)_PC", 16159.4, -2383.0),
        ("10(6)_MOI", 24528.1, -1490.7),
        ("10(7)_MOI", 23583.7, 20851.1),
        ("10(7)_PC", 16230.8, 20808.2),
    ]
    layout, rows, cols = _grid(points)
    assert (rows, cols) == (3, 2)
    assert layout == [(0, 1), (0, 0), (1, 0), (1, 1), (2, 1), (2, 0)]
    by_name = {name: rc for (name, _, _), rc in zip(points, layout)}
    for dilution in ("10(5)", "10(6)", "10(7)"):
        assert by_name[f"{dilution}_PC"][1] == 0
        assert by_name[f"{dilution}_MOI"][1] == 1


def test_single_site_and_missing_positions():
    assert site_layout([(100.0, 200.0)]) == [(0, 0)]
    assert site_layout([]) == []
    assert site_layout([None, None, None]) == [(0, 0), (0, 1), (0, 2)]
    assert site_layout([(0.0, 0.0), None]) == [(0, 0), (0, 1)]


def test_small_jitter_never_splits_a_column():
    # a 24 um wobble between revisits of the same well is not a new column
    points = [(16507.7, 0.0), (16483.8, 0.0), (23157.5, 0.0), (24047.2, 0.0)]
    assert site_layout(points) == [(0, 0), (0, 0), (0, 1), (0, 1)]


def test_detection_rejects_what_is_not_an_nd2(tmp_path):
    assert not is_plate_file(tmp_path / "missing.nd2")
    other = tmp_path / "scan.svs"
    other.write_bytes(b"not a slide")
    assert not is_plate_file(other)
    junk = tmp_path / "junk.nd2"
    junk.write_bytes(b"junk")
    assert not is_plate_file(junk)
    assert PLATE_MAX_FRAME_PX == 4_500_000


def test_five_evenly_spaced_rows_stay_five_rows():
    """A threshold taken from the whole extent left no step wide enough to
    count once a plate had five or more evenly spaced tracks, so every site
    landed in one cell. Five dilutions by two conditions is an ordinary run."""
    points = [
        (f"10({5 + i})_{cond}", x, -23000.0 + i * 22000.0)
        for i in range(5)
        for cond, x in (("PC", 16400.0), ("MOI", 24000.0))
    ]
    layout, rows, cols = _grid(points)
    assert (rows, cols) == (5, 2)
    assert len(set(layout)) == 10  # every site in its own cell


def test_a_ninety_six_well_scan_keeps_its_eight_by_twelve_grid():
    points = [
        (f"{chr(65 + r)}{c + 1}", 10000.0 + c * 9000.0, 5000.0 + r * 9000.0)
        for r in range(8)
        for c in range(12)
    ]
    layout, rows, cols = _grid(points)
    assert (rows, cols) == (8, 12)
    assert len(set(layout)) == 96
    assert site_layout([(x, y) for _, x, y in points], names=[n for n, _, _ in points]) == layout


def test_sixteen_by_sixteen_serpentine_scan_keeps_well_coordinates():
    """The supplied spheroid acquisition names a full A01..P16 plate but
    records alternate rows in opposite orders. Stage geometry, not file
    order, must place A16 at the visual left and A01 at the visual right."""
    points = []
    for row in range(16):
        numbers = range(1, 17) if row % 2 == 0 else range(16, 0, -1)
        for number in numbers:
            points.append(
                (
                    f"{chr(65 + row)}{number:02d}",
                    (16 - number) * 1500.0,
                    row * 1500.0,
                )
            )
    layout, rows, cols = _grid(points)
    by_name = {name: rc for (name, _, _), rc in zip(points, layout)}
    assert (rows, cols) == (16, 16)
    assert len(set(layout)) == 256
    assert by_name["A16"] == (0, 0)
    assert by_name["A01"] == (0, 15)
    assert by_name["P16"] == (15, 0)
    assert by_name["P01"] == (15, 15)
    assert site_layout([(x, y) for _, x, y in points], names=[n for n, _, _ in points]) == layout


@pytest.mark.parametrize("positions", [
    [0, 9000, 18000, 45000],  # A01 A02 A03 A06
    [0, 9000, 27000],  # A01 A02 A04: the previous strict gap test also merged these
])
@pytest.mark.parametrize("vertical", [False, True])
def test_missing_tracks_do_not_merge_unnamed_stage_sites(positions, vertical):
    points = [(0, x) if vertical else (x, 0) for x in positions]
    expected = [(i, 0) if vertical else (0, i) for i in range(len(points))]
    assert site_layout(points) == expected
    assert site_layout(list(reversed(points))) == list(reversed(expected))


def test_missing_row_refinement_preserves_within_column_spread():
    # The row gap must not collapse three rows, nor should repairing those
    # collisions turn the 3.2 mm drift within the right column into new columns.
    points = [
        (16000, 0), (24000, 0),
        (16000, 9000), (27200, 9000),
        (16000, 18000), (24500, 18000),
        (16000, 45000), (25000, 45000),
    ]
    assert site_layout(points) == [(r, c) for r in range(4) for c in range(2)]


def test_ambiguous_unnamed_tracks_stop_at_the_existing_jitter_tolerance():
    # Intermediate positions in another row can bridge a wide gap between
    # two sites. Without well identities it is ambiguous, but must terminate.
    points = [(0, 0), (9000, 0)] + [(x, 9000) for x in range(900, 9000, 900)]
    assert site_layout(points) == [(0, 0), (0, 0)] + [(1, 0)] * 9


@pytest.mark.parametrize("transpose", [False, True])
@pytest.mark.parametrize("reverse_x", [False, True])
@pytest.mark.parametrize("reverse_y", [False, True])
def test_named_missing_wells_preserve_stage_orientation(transpose, reverse_x, reverse_y):
    wells = [("A", 1), ("A", 2), ("A", 4), ("C", 1), ("C", 2), ("C", 4)]
    names = [f"{row}{col:02}" for row, col in wells]
    points, expected = [], []
    for row, col in wells:
        r, c = {"A": 0, "C": 1}[row], {1: 0, 2: 1, 4: 2}[col]
        x, y = (col - 1) * 9000, (ord(row) - ord("A")) * 9000
        if transpose:
            x, y = y, x
            r, c = c, r
        if reverse_x:
            x = -x
            c = (1 if transpose else 2) - c
        if reverse_y:
            y = -y
            r = (2 if transpose else 1) - r
        points.append((x, y))
        expected.append((r, c))
    assert site_layout(points, names=names) == expected


def test_well_identity_resolves_small_spacing_and_large_stage_jitter():
    # The geometric jitter tolerance is deliberately not a minimum well pitch.
    points = [(0, 0), (500, 120), (1500, -80), (0, 500), (650, 600), (1400, 450)]
    names = ["A01", "A02", "A04", "B01", "B02", "B04"]
    assert site_layout(points, names=names) == [(r, c) for r in range(2) for c in range(3)]


def test_invalid_or_repeated_well_names_leave_stage_layout_authoritative():
    points = [(0, 0), (400, 0), (9000, 0)]
    expected = [(0, 0), (0, 0), (0, 1)]
    for names in (["A01", "A001", "A02"], ["A01", "Treatment B", "A02"], ["A01"]):
        assert site_layout(points, names=names) == expected


def test_plate_source_uses_well_names_for_ambiguous_stage_geometry(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import numpy as np

    from nd2wsi import plate

    positions = [("A01", 0), ("A02", 400), ("A04", 1200)]
    points = [SimpleNamespace(name=name, stagePositionUm=SimpleNamespace(x=x, y=0, z=0))
              for name, x in positions]
    f = SimpleNamespace(
        sizes={"P": 3, "Y": 8, "X": 8},
        read_frame=lambda _: np.zeros((8, 8), dtype=np.uint16),
        experiment=[SimpleNamespace(type="XYPosLoop", parameters=SimpleNamespace(points=points))],
    )
    monkeypatch.setattr(plate, "_channel_infos", lambda *_: [])
    monkeypatch.setattr(plate, "_nd2_pixel_size", lambda _: (None, None))
    monkeypatch.setattr(plate, "objective_magnification", lambda _: None)
    source = plate.PlateSource.__new__(plate.PlateSource)
    source.tile = 256
    source._build_attrs = lambda _: {}
    path = tmp_path / "metadata-fixture.nd2"
    path.write_bytes(b"metadata test")
    with path.open("rb") as stream:
        source._fd = stream.fileno()
        source._init_from_file(f)
    assert [(s["row"], s["col"]) for s in source.sites] == [(0, 0), (0, 1), (0, 2)]
    assert (source.rows, source.cols) == (1, 3)
