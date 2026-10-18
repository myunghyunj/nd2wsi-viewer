"""Correction revisions select pixels explicitly; native exports remain RAW."""
from __future__ import annotations

import io
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from nd2wsi.convert import build_group_attrs
from nd2wsi.reader import ChannelInfo, PlaneSource
from nd2wsi.region_export import ExportRequestError, prepare_export
from nd2wsi.server import SlideRegistry, ViewerState, _frame_args, make_handler


@pytest.fixture
def setup():
    data = np.full((1, 64, 64), 100, dtype=np.uint16)
    src = PlaneSource(data=None, dtype=data.dtype, shape=data.shape, rgb=False,
                      channels=[ChannelInfo("FITC", (255, 255, 255))],
                      pixel_size_um=(0.5, 0.5), source_name="test.nd2",
                      selection={"t": 0, "p": 0, "z": 0})
    attrs = build_group_attrs(src, [(64, 64)], 64,
                              [dict(start=0, end=255, min=0, max=255)])
    st = ViewerState({"0": data}, attrs, generation="raw-generation")
    corrected = {"0": data.astype(np.float32) * 2}
    calls = []

    def root_for(revision):
        if revision != "fit-a":
            raise ValueError("Shading correction revision is unavailable")
        return corrected

    status = {"state": "ready", "supported": True, "pct": 100,
              "correction_id": "fit-a", "generation": st.generation,
              "channels": ["FITC"], "reason": ""}
    st._shading = SimpleNamespace(
        root_for=root_for, status=lambda: status,
        start=lambda: calls.append("start") or status,
        cancel=lambda: calls.append("cancel") or status,
        close=lambda: calls.append("close"),
    )
    registry = SlideRegistry()
    registry.slides["12345678"] = st
    cls = make_handler(registry)
    handler = cls.__new__(cls)
    response = {}
    handler.headers = {"Content-Type": "application/json"}
    handler._gate = lambda: handler.path.split("?")[0]
    handler._json = lambda body, **kw: response.update(status=kw.get("code", 200), json=body)
    handler._error = lambda code, message: response.update(status=code, error=message)
    handler._send = lambda code, body, ctype, **kw: response.update(
        status=code, body=body, ctype=ctype, **kw)
    return st, corrected, registry, handler, response, calls


def test_tile_revision_cannot_switch_raw_or_revive_stale_pixels(setup):
    st, _, _, handler, response, _ = setup
    averages = []
    for suffix in ("", "&correction=fit-a", ""):
        handler.path = "/s/12345678/api/tile/0/0/0.png?g=raw-generation" + suffix
        handler.do_GET()
        assert response["status"] == 200
        averages.append(np.asarray(Image.open(io.BytesIO(response["body"]))).mean())
        assert response["cache"].endswith("immutable")
    assert averages[0] == averages[2] < averages[1]
    assert np.all(st.root["0"] == 100)
    handler.path += "&correction=stale-fit"
    handler.do_GET()
    assert response["status"] == 409


def test_status_and_actions_are_slide_scoped_and_reject_options(setup):
    _, _, _, handler, response, calls = setup
    handler.path = "/s/12345678/api/shading/status"
    handler.do_GET()
    assert response["json"]["generation"] == "raw-generation"
    for action in ("start", "cancel"):
        handler.path = f"/s/12345678/api/shading/{action}"
        handler._body_json = lambda **kw: {}
        handler.do_POST()
        assert response["status"] == 200
    assert calls == ["start", "cancel"]
    handler._body_json = lambda **kw: {"source": "/untrusted.nd2"}
    handler.do_POST()
    assert response["status"] == 400
    assert calls == ["start", "cancel"]


def test_closed_slide_start_is_a_conflict_not_a_server_failure(setup):
    st, _, _, handler, response, _ = setup

    def closed():
        raise ValueError("This slide is closed")

    st._shading.start = closed
    handler.path = "/s/12345678/api/shading/start"
    handler._body_json = lambda **kw: {}
    handler.do_POST()
    assert response["status"] == 409
    assert "closed" in response["error"]


def test_rendered_export_uses_visible_revision_and_names_correction(setup):
    st, corrected, _, _, _, _ = setup
    q = {k: [str(v)] for k, v in dict(x=3, y=5, w=40, h=45, format="png").items()}
    raw = prepare_export(st, q, resolve_frame=_frame_args)
    shown = prepare_export(st, q | {"correction": ["fit-a"]}, resolve_frame=_frame_args)
    assert raw.root is st.root and shown.root is corrected
    assert "_shading-corrected_" in shown.rendered().filename
    assert "shading-corrected" not in raw.rendered().filename
    raw_pixels = np.asarray(Image.open(io.BytesIO(raw.rendered().body)))
    shown_pixels = np.asarray(Image.open(io.BytesIO(shown.rendered().body)))
    assert shown_pixels.mean() > raw_pixels.mean()
    for fmt in ("tiff", "nd2"):
        native = prepare_export(st, q | {"format": [fmt]}, resolve_frame=_frame_args)
        assert native.root is st.root
        with pytest.raises(ExportRequestError, match="original values"):
            prepare_export(st, q | {"format": [fmt], "correction": ["fit-a"]},
                           resolve_frame=_frame_args)
    with pytest.raises(ExportRequestError, match="revision"):
        prepare_export(st, q | {"correction": ["old"]}, resolve_frame=_frame_args)


def test_slide_close_cancels_and_closes_private_correction(setup):
    _, _, registry, _, _, calls = setup
    assert registry.remove("12345678")
    assert calls == ["cancel", "close"]


def test_busy_export_preserves_correction_if_tab_close_is_refused(setup):
    st, _, registry, _, _, calls = setup
    registry.window_session = SimpleNamespace(role="user")
    with st.busy:
        with pytest.raises(ValueError, match="export"):
            registry.remove("12345678")
        assert not st.busy.closed
    assert calls == ["cancel"]
    assert st.shading.root_for("fit-a") is not None
