"""Site arrangement from stage positions, and the plate detection boundary."""

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
