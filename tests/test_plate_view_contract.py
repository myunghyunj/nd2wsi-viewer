"""Static wiring checks for plate-only view controls.

The geometry and well-name inference have Node tests of their own. These
checks keep the HTML, CSS, and application wiring from silently drifting apart.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "nd2wsi" / "static" / "app.js"
CSS = ROOT / "nd2wsi" / "static" / "style.css"
INDEX = ROOT / "nd2wsi" / "static" / "index.html"
README = ROOT / "README.md"


def test_well_headers_and_view_menu_are_loaded_and_wired():
    app = APP.read_text()
    index = INDEX.read_text()

    assert index.index("plate-ui-v1.js") < index.index("app.js")
    assert 'id="tb-plate-view"' in index
    assert 'id="plate-view-menu" role="menu"' in index
    assert 'role="menuitemcheckbox"' in index
    for key in ("siteLabels", "timeline", "zAxis"):
        assert f'data-plate-view="{key}"' in index

    assert 'plateUI.wellHeaders(pl.placed, pl.rows, pl.cols)' in app
    assert 'pl.headerKind = pl.wellHeaders ? "well"' in app
    assert 'pl.view.siteLabels = pl.headerKind !== "well"' in app
    assert 'wrap.classList.toggle("plate-labels-hidden", !pl.view.siteLabels)' in app
    assert 'wrap.classList.toggle("plate-time-hidden", !pl.view.timeline)' in app
    assert 'wrap.classList.toggle("plate-z-hidden", !pl.view.zAxis)' in app
    assert '$("time-line").hidden = !pl.view.timeline' in app
    assert '$("z-slider").hidden = !pl.view.zAxis' in app
    assert 'ev.key === "ArrowDown"' in app
    assert 'ev.key === "ArrowUp"' in app
    assert 'ev.key === "Home"' in app
    assert 'ev.key === "End"' in app
    assert 'menu.addEventListener("focusout"' in app
    assert 'ev.relatedTarget !== btn' in app
    assert app.count('ev.target.closest?.("#tb-plate-view, #plate-view-menu")') == 4


def test_singleton_axes_default_hidden_and_cannot_start_useless_work():
    app = APP.read_text()

    assert "timeline: Number(info.plate.T) > 1" in app
    assert "zAxis: Number(info.plate.Z) > 1" in app
    assert 'pl.playing = state.info.plate.T > 1 && !!on' in app
    assert '$("t-play").disabled = info.T <= 1' in app
    assert "state.info.plate.Z <= 1 || measured === 0" in app
    assert "if (info.Z > 1) loadPlateFocus();" in app


def test_hidden_controls_reclaim_space_without_hiding_focus_strip_labels():
    css = CSS.read_text()

    assert "#stage-wrap.plate-grid.plate-time-hidden #plate { bottom: 14px; }" in css
    assert "#stage-wrap.plate-grid.plate-z-hidden #plate-block" in css
    assert "#stage-wrap.plate-focus.plate-z-hidden #plate-block { display: none; }" in css
    assert "#stage-wrap.plate-focus.plate-time-hidden #plate-strip { bottom: 14px; }" in css
    assert "#stage-wrap.plate-labels-hidden #plate-grid .site .pill { display: none; }" in css
    assert "#stage-wrap.plate-labels-hidden #plate-strip" not in css


def test_readme_explains_coordinate_headers_and_view_options():
    readme = README.read_text()

    assert "Coordinate names such as `A01` become lettered and numbered" in readme
    assert "one z plane or one time point hides the corresponding control" in readme
    assert "**View** menu can show or hide Site Labels, Timeline, and Z Axis" in readme
