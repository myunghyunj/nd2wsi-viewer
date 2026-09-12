"""HTTP-level window isolation, exact snapshots, and optimistic save conflicts."""
import json
import multiprocessing
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from nd2wsi.annotation_workspace import AnnotationConflict, AnnotationWorkspace
from nd2wsi.server import ViewerState, annotations_sidecar, create_server, plate_annotations_sidecar
from nd2wsi.window_sessions import create_window_session


def _attrs(source, *, plate=False):
    meta = {"source": source.name, "levels": [{"path": "0", "width": 2048, "height": 2044}],
            "selection": {"t": 0, "p": 0, "z": "mid"}, "pixel_size_um": [0.4, 0.4]}
    if plate:
        meta["plate"] = {"sites": [{"name": "A01"}, {"name": "A02"}]}
    return {"nd2wsi": meta}


@contextmanager
def _server(source, session=None, *, plate=False):
    httpd = create_server([], port=0, window_session=session)
    attrs = _attrs(source, plate=plate)
    path = httpd.registry.annotation_path(source, attrs, site=0 if plate else None)
    sid = httpd.registry.sid_for(source)
    st = ViewerState({}, attrs, annotations_path=path, source_path=source,
                     plate=SimpleNamespace(P=2) if plate else None)
    httpd.registry.slides[sid] = st
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}/{httpd.token}"
    try:
        yield httpd, base, f"{base}/s/{sid}/api/annotations"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


def _request(url, payload=None):
    request = urllib.request.Request(url, data=None if payload is None else json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "research" / "sample.nd2"
    path.parent.mkdir()
    path.write_bytes(b"not opened by annotation-only tests")
    return path


def test_two_agent_snapshots_preserve_full_json_and_never_modify_user(source, tmp_path):
    canonical = annotations_sidecar(source, _attrs(source))
    original = b'{ "items": [{"id":"manual-1","type":"polygon","points":[[1.25,2],[8,9]],"text":"Exact ROI"}], "custom_metadata":{"owner":"user"} }\n'
    canonical.write_bytes(original)
    a = create_window_session("agent", tmp_path / "sessions")
    b = create_window_session("agent", tmp_path / "sessions")
    with _server(source, a) as (_, _, au), _server(source, b) as (_, _, bu):
        _, first = _request(au)
        _, second = _request(bu)
        assert first["path"] != second["path"] != str(canonical)
        assert Path(first["path"]).read_bytes() == original
        assert Path(second["path"]).read_bytes() == original
        items = first["items"] + [{"id": "agent-only", "type": "pin", "x": 3, "y": 4}]
        status, saved = _request(au, {"items": items, "expected_revision": first["revision"]})
        assert status == 200 and saved["revision"] != first["revision"]
        assert json.loads(Path(first["path"]).read_bytes())["custom_metadata"] == {"owner": "user"}
        assert _request(bu)[1]["items"] == second["items"]
        assert canonical.read_bytes() == original
        # Re-resolving does not overwrite the first agent's private edits.
        assert _request(au)[1]["items"] == items
    assert canonical.read_bytes() == original


def test_agent_reads_matching_legacy_without_migration_or_canonical_directory(source, tmp_path):
    legacy = source.parent / "annotations_sample.json"
    original = json.dumps({"source": {"name": source.name}, "items": [{"id": "legacy-roi"}],
                           "extension": ["keep", 1]}).encode()
    legacy.write_bytes(original)
    names = set(source.parent.iterdir())
    session = create_window_session("agent", tmp_path / "sessions")
    with _server(source, session) as (_, _, url):
        status, result = _request(url)
        assert status == 200 and result["items"] == [{"id": "legacy-roi"}]
        assert Path(result["path"]).read_bytes() == original
        assert _request(url, {"items": [], "expected_revision": result["revision"]})[0] == 200
    assert set(source.parent.iterdir()) == names
    assert legacy.read_bytes() == original


def test_user_stale_save_conflicts_and_durable_draft_keeps_attempt(source, tmp_path):
    a = create_window_session("user", tmp_path / "sessions")
    b = create_window_session("user", tmp_path / "sessions")
    with _server(source, a) as (_, _, au), _server(source, b) as (_, _, bu):
        first, second = _request(au)[1], _request(bu)[1]
        assert first["revision"] == second["revision"] == "missing"
        assert _request(au, {"items": [{"id": "user-a"}], "expected_revision": first["revision"]})[0] == 200
        canonical = Path(first["path"])
        saved_bytes = canonical.read_bytes()
        attempted = [{"id": "user-b-unsaved", "points": [[1.1, 2.2], [3.3, 4.4]]}]
        status, conflict = _request(bu, {"items": attempted, "expected_revision": second["revision"]})
        assert status == 409 and conflict["conflict"] is True
        draft = Path(conflict["draft_path"])
        record = json.loads(draft.read_bytes())
        assert record["payload"]["items"] == attempted
        assert record["expected_revision"] == "missing"
        assert record["current_revision"] == conflict["current_revision"]
        assert record["canonical_path"] == str(canonical)
        assert record["source_path"] == str(source)
        assert record["window"]["id"] == b.id and record["auto_merge"] is False
        assert canonical.read_bytes() == saved_bytes
        # Omitting the revision must not become an overwrite escape hatch.
        assert _request(bu, {"items": attempted})[0] == 409
        assert canonical.read_bytes() == saved_bytes
    assert draft.is_file() and canonical.read_bytes() == saved_bytes


def test_plate_agent_site_scope_shared_across_time_and_z_only(source, tmp_path):
    attrs = _attrs(source, plate=True)
    for p in range(2):
        plate_annotations_sidecar(source, attrs, p).write_text(json.dumps({"items": [{"id": f"site-{p}"}]}))
    session = create_window_session("agent", tmp_path / "sessions")
    with _server(source, session, plate=True) as (_, _, url):
        assert _request(url)[0] == 400
        site0 = _request(url + "?p=0&t=0&z=0")[1]
        site1 = _request(url + "?p=1&t=48&z=12")[1]
        same1 = _request(url + "?p=1&t=0&z=0")[1]
        assert site1["path"] == same1["path"] and site0["path"] != site1["path"]
        assert site1["items"] == [{"id": "site-1"}]
        assert _request(url + "?p=1&t=48&z=12", {"items": [{"id": "agent-site-1"}], "expected_revision": site1["revision"]})[0] == 200
        doc = json.loads(Path(site1["path"]).read_bytes())
        assert doc["selection"] == {"p": 1} and doc["site"] == "A02"
        assert _request(url + "?p=0")[1]["items"] == [{"id": "site-0"}]
        assert json.loads(plate_annotations_sidecar(source, attrs, 1).read_bytes())["items"] == [{"id": "site-1"}]


@pytest.mark.parametrize("role", ["agent", "user"])
def test_agent_trash_blocked_and_window_close_rejects_active_export(source, tmp_path, role):
    session = create_window_session(role, tmp_path / "sessions")
    with _server(source, session) as (httpd, base, _):
        sid = httpd.registry.sid_for(source)
        assert _request(base + "/api/trash", {"sid": sid})[0] == 403
        with pytest.raises(PermissionError):
            httpd.registry.trash_cache(sid)
        st = httpd.registry.get(sid)
        with st.busy:
            assert _request(base + "/api/close", {"sid": sid})[0] == 409
            assert httpd.registry.get(sid) is st and not st.busy.closed
        assert _request(base + "/api/close", {"sid": sid})[0] == 200
        assert st.busy.closed


def test_default_server_keeps_legacy_api_without_revision(source):
    with _server(source) as (_, _, url):
        assert "revision" not in _request(url)[1]
        assert _request(url, {"items": [{"id": "legacy-write"}]})[0] == 200
        assert _request(url)[1]["items"] == [{"id": "legacy-write"}]


def test_conflict_draft_failure_still_never_overwrites_canonical(source, tmp_path, monkeypatch):
    import nd2wsi.annotation_workspace as module
    session = create_window_session("user", tmp_path / "sessions")
    with _server(source, session) as (_, _, url):
        first = _request(url)[1]
        assert _request(url, {"items": [{"id": "preserved"}], "expected_revision": first["revision"]})[0] == 200
        canonical = Path(first["path"])
        original = canonical.read_bytes()

        def full_disk(*args):
            raise OSError("synthetic disk full while saving draft")

        monkeypatch.setattr(module, "_atomic_write", full_disk)
        code, body = _request(url, {"items": [{"id": "stale"}], "expected_revision": first["revision"]})
        assert code == 409 and body["draft_path"] is None
        assert "synthetic disk full" in body["draft_error"]
        assert canonical.read_bytes() == original


def test_agent_uncached_source_never_enters_shared_builder(source, tmp_path, monkeypatch):
    import nd2wsi.convert as convert
    import nd2wsi.plate as plate
    from nd2wsi.server import SlideRegistry
    session = create_window_session("agent", tmp_path / "sessions")
    registry = SlideRegistry(window_session=session)
    monkeypatch.setattr(plate, "is_plate_file", lambda _: False)
    monkeypatch.setattr(convert, "existing_cache_store", lambda _: None)

    def forbidden(*args, **kwargs):
        pytest.fail("agent entered shared cache build/repair")

    monkeypatch.setattr(convert, "ensure_cache", forbidden)
    with pytest.raises(PermissionError, match="cannot build or repair"):
        registry.open_path(source)


def test_agent_svs_conversion_fallback_is_blocked(source, tmp_path, monkeypatch):
    import nd2wsi.convert as convert
    import nd2wsi.svs as svs
    from nd2wsi.server import SlideRegistry
    session = create_window_session("agent", tmp_path / "sessions")
    registry = SlideRegistry(window_session=session)
    path = source.with_suffix(".svs")
    path.write_bytes(b"not opened by mocked conversion test")
    monkeypatch.setattr(svs, "is_svs", lambda _: True)

    def unsupported(*args):
        raise NotImplementedError("needs cache")

    def forbidden(*args, **kwargs):
        pytest.fail("agent entered SVS conversion")

    monkeypatch.setattr(registry, "add_direct", unsupported)
    monkeypatch.setattr(convert, "convert", forbidden)
    with pytest.raises(PermissionError, match="cannot build a shared cache"):
        registry.add_store(path)


def _process_save(path, sessions, barrier, queue, value):
    session = create_window_session("user", sessions)
    workspace = AnnotationWorkspace(session)
    _, expected = workspace.read(Path(path))
    barrier.wait(timeout=10)
    try:
        workspace.write(Path(path), {"items": [{"id": value}]}, expected)
        queue.put("saved")
    except AnnotationConflict:
        queue.put("conflict")


def test_compare_and_swap_lock_serializes_separate_processes(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    path = tmp_path / "shared" / "annotations.json"
    barrier, queue = ctx.Barrier(2), ctx.Queue()
    children = [ctx.Process(target=_process_save, args=(str(path), str(tmp_path / "sessions"), barrier, queue, str(n))) for n in range(2)]
    try:
        for child in children:
            child.start()
        assert sorted(queue.get(timeout=20) for _ in children) == ["conflict", "saved"]
        for child in children:
            child.join(timeout=10)
            assert child.exitcode == 0
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
            child.join(timeout=5)
        queue.close()


def test_frontend_queued_edits_advance_only_own_committed_revision():
    from test_annotation_site_transitions_js import NODE, _run
    if NODE is None:
        pytest.skip("node is not installed")
    result = _run(r'''
(async () => {
  state.plate.focus = 0;
  const posted = [];
  context.fetch = async (url, options = {}) => {
    if (!options.method) return {ok: true, json: async () => ({items: [], revision: "r0", path: "a.json"})};
    const payload = JSON.parse(options.body);
    posted.push(payload);
    return {ok: true, json: async () => ({revision: "r" + posted.length, path: "a.json"})};
  };
  await api.loadAnnotations(0);
  state.annotations = [{id: "edit-one"}]; state.annRevision = 1; state.annDirty = true;
  const first = api.saveAnnotations();
  state.annotations = [{id: "edit-two"}]; state.annRevision = 2;
  const second = api.saveAnnotations();
  await Promise.all([first, second]);
  emit({posted, revision: state.annServerRevision, dirty: state.annDirty});
})().catch(e => { console.error(e); process.exit(1); });
''')
    assert [item["expected_revision"] for item in result["posted"]] == ["r0", "r1"]
    assert result["posted"][1]["items"] == [{"id": "edit-two"}]
    assert result["revision"] == "r2" and result["dirty"] is False


def test_frontend_conflict_keeps_dirty_site_draft_and_never_adopts_other_revision():
    from test_annotation_site_transitions_js import NODE, _run
    if NODE is None:
        pytest.skip("node is not installed")
    result = _run(r'''
(async () => {
  state.plate.focus = 0;
  const posted = [], gets = [];
  context.fetch = async (url, options = {}) => {
    if (!options.method) {
      gets.push(url);
      return {ok: true, json: async () => ({items: [{id: "disk"}], revision: url.endsWith("p=0") ? "r0" : "b0", path: "a.json"})};
    }
    posted.push(JSON.parse(options.body));
    return {ok: false, status: 409, json: async () => ({current_revision: "OTHER-WINDOW-r99", draft_path: "/session/drafts/preserved.json"})};
  };
  await api.loadAnnotations(0);
  state.annotations = [{id: "my-unsaved-roi"}]; state.annRevision = 1; state.annDirty = true;
  await api.saveAnnotations();
  const dirtyAfterConflict = state.annDirty;
  state.plate.focus = 1; state.annContext += 1; state.annDirty = false;
  await api.loadAnnotations(1);
  state.plate.focus = 0; state.annContext += 1;
  await api.loadAnnotations(0);
  await api.saveAnnotations();
  emit({posted, gets, dirtyAfterConflict, dirty: state.annDirty, revision: state.annServerRevision,
        items: state.annotations, failed: state.annFailedSaves.size, statuses});
})().catch(e => { console.error(e); process.exit(1); });
''')
    assert result["dirtyAfterConflict"] and result["dirty"]
    assert result["revision"] == "r0" and result["items"] == [{"id": "my-unsaved-roi"}]
    assert result["gets"].count("api/annotations?p=0") == 1
    assert [item["expected_revision"] for item in result["posted"]] == ["r0", "r0"]
    assert result["failed"] == 1 and any("Recovery draft:" in text for text in result["statuses"])
