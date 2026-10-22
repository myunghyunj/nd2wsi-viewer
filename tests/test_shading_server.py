"""Committed pyramid routes, generation guards and original-source probes."""
from __future__ import annotations

import io
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from nd2wsi import render
from nd2wsi.convert import build_group_attrs
from nd2wsi.reader import ChannelInfo, PlaneSource
from nd2wsi.server import SlideRegistry, ViewerState, make_handler


@pytest.fixture
def setup():
    raw = np.full((1, 64, 64), 100, dtype=np.uint16)
    src = PlaneSource(data=None, dtype=raw.dtype, shape=raw.shape, rgb=False,
                      channels=[ChannelInfo("FITC", (255, 255, 255))],
                      pixel_size_um=(0.5, 0.5), source_name="test.nd2",
                      selection={"t": 0, "p": 0, "z": 0})
    attrs = build_group_attrs(src, [(64, 64)], 64,
                              [dict(start=0, end=255, min=0, max=255)])

    class PersistentRoot(dict):
        raw_level0 = raw

    committed = PersistentRoot({"0": raw.astype(np.float32) * 2})
    st = ViewerState(committed, attrs, generation="corrected-generation",
                     manifest={"h_correction": {"applied": True}})
    calls = []

    def status():
        applied = st.manifest["h_correction"]["applied"]
        return {"state": "ready" if applied else "idle", "supported": True,
                "applied": applied, "operation": None, "pct": 100 if applied else 0,
                "generation": st.generation, "can_cancel": False,
                "channels": ["FITC"] if applied else [], "reason": "",
                "estimated_seconds": 30, "eta_seconds": None}

    st._shading = SimpleNamespace(
        status=status,
        start=lambda: calls.append("start") or status(),
        revoke=lambda: calls.append("revoke") or status(),
        cancel=lambda: calls.append("cancel") or status(),
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
    return st, raw, registry, handler, response, calls


@pytest.mark.parametrize("query,cache", [("?g=corrected-generation", "private, max-age=31536000, immutable"),
                                         ("", "no-store")])
def test_tiles_default_to_committed_pyramid_pixels(setup, query, cache):
    st, raw, _, handler, response, _ = setup
    handler.path = "/s/12345678/api/tile/0/0/0.png" + query
    handler.do_GET()
    assert response["status"] == 200 and response["cache"] == cache
    shown = np.asarray(Image.open(io.BytesIO(response["body"])))
    original = np.asarray(Image.open(io.BytesIO(render.render_tile(
        {"0": raw}, st.attrs, 0, 0, 0, [0], "png", None))))
    assert shown.mean() > original.mean()
    assert np.all(st.root["0"] == 200) and np.all(st.root.raw_level0 == 100)


@pytest.mark.parametrize("query", ["g=raw-generation", "g=corrected-generation&correction=fit-a",
                                    "correction=old-fit"])
def test_stale_generations_and_legacy_revision_selectors_cannot_serve_pixels(setup, query):
    _, _, _, handler, response, calls = setup
    handler.path = "/s/12345678/api/tile/0/0/0.png?" + query
    handler.do_GET()
    assert response["status"] == 409
    assert "body" not in response and calls == []


def test_status_exposes_committed_applied_state_for_this_slide(setup):
    st, _, _, handler, response, calls = setup
    handler.path = "/s/12345678/api/shading/status"
    handler.do_GET()
    assert response["status"] == 200
    assert response["json"] == st.shading.status()
    assert response["json"]["applied"] is True
    assert response["json"]["generation"] == "corrected-generation"
    assert calls == []
    handler.path = "/s/87654321/api/shading/status"
    handler.do_GET()
    assert response["status"] == 404 and calls == []


@pytest.mark.parametrize("action", ["start", "revoke", "cancel"])
def test_actions_accept_only_the_current_slide_generation(setup, action):
    st, _, _, handler, response, calls = setup
    handler.path = f"/s/12345678/api/shading/{action}"
    handler._body_json = lambda **kw: {"generation": st.generation}
    handler.do_POST()
    assert response["status"] == 200 and calls == [action]
    assert response["json"]["generation"] == st.generation


@pytest.mark.parametrize("action", ["start", "revoke", "cancel"])
def test_stale_generation_rejects_action_without_mutation(setup, action):
    st, _, _, handler, response, calls = setup
    root, manifest = st.root, dict(st.manifest)
    handler.path = f"/s/12345678/api/shading/{action}"
    handler._body_json = lambda **kw: {"generation": "raw-generation"}
    handler.do_POST()
    assert response["status"] == 409 and "changed" in response["error"]
    assert calls == [] and st.root is root and st.manifest == manifest


@pytest.mark.parametrize("body", [None, [], {}, {"generation": 12},
    {"source": "/untrusted.nd2"}, {"generation": "corrected-generation", "source": "/untrusted.nd2"},
    {"generation": "corrected-generation", "operation": "revoke"}])
def test_missing_generation_or_extra_options_are_rejected_without_mutation(setup, body):
    _, _, _, handler, response, calls = setup
    handler.path = "/s/12345678/api/shading/start"
    handler._body_json = lambda **kw: body
    handler.do_POST()
    assert response["status"] == 400 and calls == []


@pytest.mark.parametrize("action", ["start", "revoke", "cancel"])
def test_agent_cannot_mutate_a_pyramid_outside_private_exports_scope(setup, action):
    st, _, _, handler, response, calls = setup
    st.correction_write_allowed = False
    handler.path = f"/s/12345678/api/shading/{action}"
    handler._body_json = lambda **kw: {"generation": st.generation}
    handler.do_POST()
    assert response["status"] == 403 and "private exports" in response["error"]
    assert calls == [] and st.manifest["h_correction"]["applied"] is True


def test_closed_slide_start_is_a_conflict_not_a_server_failure(setup):
    st, _, _, handler, response, _ = setup

    def closed():
        raise ValueError("This slide is closed")

    st._shading.start = closed
    handler.path = "/s/12345678/api/shading/start"
    handler._body_json = lambda **kw: {"generation": st.generation}
    handler.do_POST()
    assert response["status"] == 409 and "closed" in response["error"]


def test_corrected_display_pixel_probe_reads_original_raw_level0(setup):
    st, _, _, handler, response, _ = setup
    handler.path = "/s/12345678/api/pixel?x=3&y=5"
    handler.do_GET()
    assert response["status"] == 200
    assert response["json"]["values"] == [{"name": "FITC", "value": 100}]
    assert response["json"]["generation"] == st.generation
    assert response["json"]["frame"] is None
    assert np.all(st.root["0"] == 200)


@pytest.mark.parametrize("extra", [
    {"method": "bffree", "experimental_confirmed": "true"}, {"method": "unknown"},
])
def test_invalid_method_and_confirmation_type_are_rejected(setup, extra):
    st, _, _, handler, response, calls = setup
    handler.path = "/s/12345678/api/shading/start"
    handler._body_json = lambda **kw: {"generation": st.generation, **extra}
    handler.do_POST()
    assert response["status"] == 400 and calls == []


@pytest.mark.parametrize("extra", [{"method": "bffree"}, {"method": "bffree", "experimental_confirmed": False}])
def test_rank1_does_not_require_experimental_opt_in(setup, extra):
    st, _, _, handler, response, calls = setup
    st._shading.start = lambda method, **kwargs: calls.append((method, kwargs)) or st.shading.status()
    handler.path = "/s/12345678/api/shading/start"
    handler._body_json = lambda **kw: {"generation": st.generation, **extra}
    handler.do_POST()
    assert response["status"] == 200
    assert calls == [("bffree", {"experimental_confirmed": False})]


def test_experimental_opt_in_is_forwarded_only_for_current_private_slide(setup):
    st, _, _, handler, response, calls = setup
    st._shading.start = lambda method, **kwargs: calls.append((method, kwargs)) or st.shading.status()
    handler.path = "/s/12345678/api/shading/start"
    handler._body_json = lambda **kw: {"generation": st.generation, "method": "bffree",
                                     "experimental_confirmed": True}
    handler.do_POST()
    assert response["status"] == 200
    assert calls == [("bffree", {"experimental_confirmed": True})]
    calls.clear()
    handler._body_json = lambda **kw: {"generation": "stale", "method": "bffree",
                                     "experimental_confirmed": True}
    handler.do_POST()
    assert response["status"] == 409 and calls == []


def test_slide_close_cancels_and_closes_correction_controller(setup):
    st, _, registry, _, _, calls = setup
    assert registry.remove("12345678")
    assert calls == ["cancel", "close"] and st._closed


def test_busy_export_preserves_committed_pyramid_when_tab_close_is_refused(setup):
    st, _, registry, _, _, calls = setup
    root = st.root
    registry.window_session = SimpleNamespace(role="user")
    with st.busy:
        with pytest.raises(ValueError, match="export"):
            registry.remove("12345678")
        assert not st.busy.closed
    assert calls == ["cancel"]
    assert registry.get("12345678") is st and st.root is root
    assert st.shading.status()["applied"] is True and not st._closed
