"""Exercise Sparkle decisions with a deterministic page and main queue."""

import json
import threading
import time
from collections import deque
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nd2wsi import update_card as card


class Channel:
    def __init__(self):
        self.calls = []
        self.probe = None

    def submit(self, script, complete, **options):
        self.calls.append(script)
        if "ready===true" in script:
            self.probe = complete
        else:
            complete(True)


@pytest.fixture
def h():
    main, timers, received = deque(), [], []
    standard, channel = Mock(), Channel()
    retry, notes = Mock(return_value=True), Mock()
    session = card.UpdateCardSession(
        standard,
        lambda: None,
        main.append,
        retry=retry,
        open_notes=notes,
        channel=channel,
        later=lambda seconds, fn: timers.append(fn),
    )

    def block(*args):
        assert threading.current_thread() is threading.main_thread()
        received.append(args)

    def drain():
        while main:
            main.popleft()()

    def found(stage=0, info=False):
        item = SimpleNamespace(
            displayVersionString=lambda: "2.5.6",
            versionString=lambda: "2.5.6",
            itemDescription=lambda: "<b>Fixes</b><script>bad()</script>",
            isInformationOnlyUpdate=lambda: info,
        )
        session.dispatch(
            "showUpdateFoundWithAppcastItem_state_reply_",
            item,
            SimpleNamespace(stage=lambda: stage),
            block,
        )
        if session.mode == "probing":
            channel.probe("page-1")

    def action(name, **overrides):
        return session.action(
            {"token": session.token, "prompt": session.model["prompt"], "action": name, **overrides}
        )

    return SimpleNamespace(**locals())


def test_first_callback_probes_once_and_queues_subsequent_selectors(h):
    h.session.dispatch("showUserInitiatedUpdateCheckWithCancellation_", h.block)
    h.found()  # settles the first probe, replays check then found
    assert h.session.mode == "card" and h.session.model["state"] == "available"
    assert h.session.model["message"] == "Fixes"
    assert h.received == []  # checking cancellation was retired, not called
    assert sum("ready===true" in c for c in h.channel.calls) == 1
    h.action("notes")
    h.drain()
    h.notes.assert_called_once_with(
        "https://github.com/myunghyunj/nd2wsi-viewer/releases/tag/v2.5.6"
    )
    h.action("download", token="old")
    h.drain()
    assert not h.received
    h.action("download")
    h.action("download")
    h.drain()
    assert h.received == [(card.INSTALL,)]


def test_ready_later_means_install_on_quit_and_late_prompt_cannot_answer_it(h):
    h.found()
    old = dict(h.session.model)
    h.action("download")
    h.drain()
    h.session.dispatch("showReadyToInstallAndRelaunch_", h.block)
    assert h.session.model["on_quit"] is True
    h.session.action({"token": old["token"], "prompt": old["prompt"], "action": "later"})
    h.drain()
    assert h.received == [(card.INSTALL,)]
    h.action("later")
    h.drain()
    h.session.dismiss()
    assert h.received == [(card.INSTALL,), (card.DISMISS,)]


def test_download_progress_extraction_ready_and_install_callbacks(h):
    h.found()
    h.action("download")
    h.drain()
    h.session.dispatch("showDownloadInitiatedWithCancellation_", h.block)
    h.session.dispatch("showDownloadDidReceiveExpectedContentLength_", 20_000_000)
    h.session.dispatch("showDownloadDidReceiveDataOfLength_", 5_000_000)
    h.session.dispatch("showDownloadDidReceiveDataOfLength_", 4_000_000)
    assert h.session.model["received"] == 9_000_000 and h.session.model["total"] == 20_000_000
    h.session.dispatch("showDownloadDidStartExtractingUpdate")
    h.session.dispatch("showExtractionReceivedProgress_", 0.3)
    assert h.session.model["state"] == "waiting" and h.session.model["progress"] == 0.3
    h.session.dispatch("showReadyToInstallAndRelaunch_", h.block)
    h.action("install")
    h.drain()
    h.session.dispatch(
        "showInstallingUpdateWithApplicationTerminated_retryTerminatingApplication_", False, h.block
    )
    h.session.dispatch("showUpdateInstalledAndRelaunched_acknowledgement_", True, h.block)
    h.session.dispatch("showUpdateInFocus")
    h.timers.pop()()
    h.session.dismiss()
    assert h.received == [(card.INSTALL,), (card.INSTALL,), ()]


@pytest.mark.parametrize("first", ["check", "found"])
def test_missing_shell_chooses_standard_until_dismissal(h, first):
    if first == "check":
        h.session.dispatch("showUserInitiatedUpdateCheckWithCancellation_", h.block)
    else:
        h.session.dispatch(
            "showUpdateFoundWithAppcastItem_state_reply_", object(), object(), h.block
        )
    h.channel.probe(None)
    assert h.session.mode == "standard"
    h.session.dispatch("showDownloadInitiatedWithCancellation_", h.block)
    h.standard.showDownloadInitiatedWithCancellation_.assert_called_once()
    callback = h.standard.showDownloadInitiatedWithCancellation_.call_args.args[0]
    callback()
    callback()
    assert h.received == [()]
    h.session.dismiss()
    h.standard.dismissUpdateInstallation.assert_called_once()
    h.found()
    assert h.session.mode == "card"


@pytest.mark.parametrize("state", ["probing", "available", "downloading", "ready", "error"])
def test_reload_close_or_failed_page_ends_outstanding_block_once(h, state):
    if state == "probing":
        h.session.dispatch("showReadyToInstallAndRelaunch_", h.block)
    else:
        h.found()
        if state != "available":
            h.action("download")
            h.drain()
            h.received.clear()
            selector = {
                "downloading": "showDownloadInitiatedWithCancellation_",
                "ready": "showReadyToInstallAndRelaunch_",
                "error": "showUpdaterError_acknowledgement_",
            }[state]
            h.session.dispatch(
                selector, *([RuntimeError("offline")] if state == "error" else []), h.block
            )
    h.session.page_closed("old-token")
    assert not h.received
    h.session.page_closed(h.session.token)
    h.session.page_closed(h.session.token)
    h.session.dismiss()
    assert (
        h.received == [(card.DISMISS,)]
        if state in ("probing", "available", "ready")
        else h.received == [()]
    )


def test_error_retry_acknowledges_before_new_check_and_notes_html_is_ignored(h):
    h.found()
    h.action("download")
    h.drain()
    h.received.clear()
    h.session.dispatch("showUpdaterError_acknowledgement_", RuntimeError("offline"), h.block)
    h.session.dispatch("showUpdateReleaseNotesWithDownloadData_", b"<script>bad()</script>")
    h.session.dispatch("showUpdateReleaseNotesFailedToDownloadWithError_", RuntimeError())
    h.action("retry")
    h.drain()
    assert h.received == [()] and not h.retry.called
    h.session.dismiss()
    h.retry.assert_not_called()
    h.drain()
    h.retry.assert_called_once()


def test_information_only_never_offers_install_and_permissions_stay_native(h):
    h.session.dispatch("showUpdatePermissionRequest_reply_", object(), h.block)
    h.standard.showUpdatePermissionRequest_reply_.assert_called_once()
    assert h.channel.probe is None
    h.found(info=True)
    assert h.session.model["actions"] == ["later"]
    h.action("download")
    h.drain()
    assert h.received == []


def test_manual_no_update_acknowledges_once_after_visible_interval(h):
    h.session.dispatch(
        "showUpdateNotFoundWithError_acknowledgement_", RuntimeError("No newer version"), h.block
    )
    h.channel.probe("page")
    assert h.session.model["state"] == "up_to_date" and not h.received
    h.timers.pop()()
    h.session.dismiss()
    assert h.received == [()]


def test_page_transport_never_evaluates_on_main_and_recovers_after_stall(monkeypatch):
    monkeypatch.setattr(card, "PAGE_TIMEOUT", 0.03)
    main, replies, called = deque(), [], []
    release = threading.Event()

    def evaluate(script):
        assert threading.current_thread() is not threading.main_thread()
        called.append(script)
        if script == "probe":
            release.wait(2)
        return "healthy"

    channel = card.PageChannel(lambda: SimpleNamespace(evaluate_js=evaluate), main.append)
    try:
        channel.submit("probe", replies.append)
        channel.submit("next", replies.append)
        deadline = time.monotonic() + 1
        while not channel._hung and time.monotonic() < deadline:
            time.sleep(0.005)
        while main:
            main.popleft()()
        channel.submit("never", replies.append)
        while main:
            main.popleft()()
        assert called == ["probe"] and replies == [None, None, None]
        channel.submit("cleanup", replies.append, defer=True)
        assert called == ["probe"]  # cleanup cannot overlap the hung call
        release.set()
        deadline = time.monotonic() + 1
        while (channel._hung or channel._running) and time.monotonic() < deadline:
            time.sleep(.005)
        while main:
            main.popleft()()
        assert called == ["probe", "cleanup"] and replies[-1] == "healthy"
        channel.submit("next-session", replies.append)
        deadline = time.monotonic() + 1
        while channel._running and time.monotonic() < deadline:
            time.sleep(.005)
        while main:
            main.popleft()()
        assert called[-1] == "next-session" and replies[-1] == "healthy"
    finally:
        release.set()


def test_release_notes_are_bounded_plain_text():
    assert card.plain_text("<p>Hello &amp; <b>safe</b></p><style>bad</style>") == "Hello & safe"
    assert len(card.plain_text("a" * 900)) == 600
    assert json.dumps(card.plain_text('<img src=x onerror="bad()">Text')) == '"Text"'


def test_native_forwarded_blocks_have_complete_pyobjc_signatures():
    from nd2wsi.updater import _register_driver_metadata

    calls = []
    _register_driver_metadata(SimpleNamespace(registerMetaDataForSelector=lambda *args: calls.append(args)))
    assert len(calls) == 18
    for cls, selector, metadata in calls:
        assert cls in (b"SPUStandardUserDriver", b"Nd2wsiUserDriver")
        [(index, argument)] = metadata["arguments"].items()
        assert index >= 2 and argument["callable"]["arguments"][0]["type"] == b"^v"
        assert argument["callable"]["retval"]["type"] == b"v"
        if b"state:reply:" in selector or b"ReadyToInstall" in selector:
            assert argument["callable"]["arguments"][1]["type"] == b"q"


def test_manual_download_uses_only_cached_compatible_asset_and_token(monkeypatch):
    import platform
    import sys
    import webbrowser

    from nd2wsi.app import Api

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(platform, "machine", lambda: "AMD64")
    monkeypatch.delenv("ND2WSI_DISABLE_UPDATES", raising=False)
    url = "https://github.com/myunghyunj/nd2wsi-viewer/releases/download/v2.5.6/nd2wsi-viewer-2.5.6-windows-x64.zip"
    selected = {"ok": True, "available": True, "mode": "manual-download", "platform": "win32", "architecture": "x86_64",
                "download_url": url, "release_url": "https://github.com/myunghyunj/nd2wsi-viewer/releases/tag/v2.5.6"}
    check = Mock(return_value=selected)
    opened = Mock(return_value=True)
    monkeypatch.setattr("nd2wsi.release_selection.check_for_updates", check)
    monkeypatch.setattr(webbrowser, "open", opened)
    api = Api(None)
    assert not check.called  # no launch-time network check
    result = api.check_for_updates(False)
    check.assert_called_once_with(open_download=False, channel="stable")
    assert not opened.called
    assert not api.open_update_download("wrong")["ok"]
    assert api.open_update_download(result["token"])["ok"]
    assert not api.open_update_download(result["token"])["ok"]
    opened.assert_called_once_with(url)
    result = api.check_for_updates(False)
    monkeypatch.setattr(platform, "machine", lambda: "unknown")
    assert not api.open_update_download(result["token"])["ok"]


@pytest.mark.parametrize("start_error", [None, "Invalid feed URL"])
def test_composite_installs_direct_updater_and_validates_menu_without_standard_controller(monkeypatch, tmp_path, start_error):
    import sys

    from test_updater_delegate import NSObject

    from nd2wsi import updater

    standard = Mock()
    native = Mock()
    metadata = {}

    def start(_error):
        assert metadata[b"SPUUpdater", b"startUpdater:"]["arguments"][2]["type"] == b"o^@"
        return start_error is None, start_error

    native.startUpdater_.side_effect = start
    native.canCheckForUpdates.return_value = True
    host = object()
    standard_type = SimpleNamespace(alloc=lambda: SimpleNamespace(initWithHostBundle_delegate_=lambda *args: standard))
    updater_init = Mock(return_value=native)
    updater_type = SimpleNamespace(alloc=lambda: SimpleNamespace(initWithHostBundle_applicationBundle_userDriver_delegate_=updater_init))
    classes = {"SPUStandardUserDriver": standard_type, "SPUUpdater": updater_type}
    monkeypatch.setitem(sys.modules, "objc", SimpleNamespace(super=super, typedSelector=lambda sig: lambda fn: fn,
                        registerMetaDataForSelector=lambda name, selector, value: metadata.__setitem__((name, selector), value),
                        protocolNamed=lambda name: object(),
                        lookUpClass=lambda name: classes[name]))
    bundle = SimpleNamespace(load=lambda: True)
    monkeypatch.setitem(sys.modules, "Foundation", SimpleNamespace(NSObject=NSObject,
                        NSBundle=SimpleNamespace(mainBundle=lambda: host, bundleWithPath_=lambda p: bundle),
                        NSOperationQueue=SimpleNamespace(mainQueue=lambda: SimpleNamespace(addOperationWithBlock_=lambda fn: fn()))))
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(updater, "_DRIVER_TYPE", None)
    monkeypatch.setattr(updater, "_DELEGATE_TYPE", None)
    monkeypatch.setattr(updater, "_framework_path", lambda: tmp_path)
    menu = Mock()
    monkeypatch.setattr(updater, "_install_menu_item", menu)
    logs = []
    handle = updater.install_sparkle_updater(shutdown_coordinator=Mock(), logger=logs.append)
    if start_error:
        assert handle is None and any(start_error in message for message in logs)
        standard.showUpdaterError_acknowledgement_.assert_called_once()
        error, acknowledgement = standard.showUpdaterError_acknowledgement_.call_args.args
        assert error == start_error
        acknowledgement()
        return
    assert handle is not None
    updater_init.assert_called_once_with(host, host, handle.user_driver, handle.delegate)
    assert handle.controller.validateMenuItem_(None)
    handle.check_for_updates()
    native.checkForUpdates.assert_called_once()
    native.canCheckForUpdates.return_value = False
    assert not handle.controller.validateMenuItem_(None)
    handle.check_for_updates()
    native.checkForUpdates.assert_called_once()
    menu.assert_called_once_with(handle.controller)
    native.canCheckForUpdates.return_value = True
    native.sessionInProgress.return_value = True
    assert not handle.card.retry()
    native.checkForUpdates.assert_called_once()
    native.sessionInProgress.return_value = False
    assert handle.card.retry()
    assert native.checkForUpdates.call_count == 2


def test_probe_deadline_includes_queue_wait_and_late_answer_cannot_switch_ui(h):
    h.session.dispatch("showUserInitiatedUpdateCheckWithCancellation_", h.block)
    deadline, answer = h.timers[-1], h.channel.probe
    deadline()
    assert h.session.mode == "standard"
    h.standard.showUserInitiatedUpdateCheckWithCancellation_.assert_called_once()
    answer("late-page")
    assert h.session.mode == "standard"
    h.session.dismiss()
    h.found()
    deadline()
    assert h.session.mode == "card"


def test_retry_waits_until_sparkle_has_returned_from_dismissal(h):
    h.found()
    h.action("download")
    h.drain()
    events = []
    native = SimpleNamespace(showing=True)
    def check():
        # Sparkle allows Check while showing an update, but only refocuses it.
        if native.showing:
            events.append("refocus")
            h.session.dispatch("showUpdateInFocus")
        else:
            events.append("new-check")
        return True
    h.session.retry = check
    def abort_after_ack():
        h.session.dismiss()
        assert not events
        native.showing = False  # coreDriver abort runs after the UI callback
    def acknowledge():
        h.main.append(abort_after_ack)
    h.session.dispatch("showUpdaterError_acknowledgement_", RuntimeError("broken enclosure"), acknowledge)
    h.action("retry")
    h.drain()
    assert events == ["new-check"] and h.session.mode is None and h.session.model == {}


def test_stalled_download_transfers_live_cancel_and_progress_without_cancelling(h):
    h.found()
    h.action("download")
    h.drain()
    h.received.clear()
    h.session.dispatch("showDownloadInitiatedWithCancellation_", h.block)
    h.session.dispatch("showDownloadDidReceiveExpectedContentLength_", 1000)
    h.session.dispatch("showDownloadDidReceiveDataOfLength_", 125)
    old_token = h.session.token
    pending = dict(h.session.model)
    h.session._fallback(old_token)
    assert not h.received and h.session.mode == "standard"
    h.standard.showDownloadInitiatedWithCancellation_.assert_called_once()
    h.standard.showDownloadDidReceiveExpectedContentLength_.assert_called_once_with(1000)
    h.standard.showDownloadDidReceiveDataOfLength_.assert_called_once_with(125)
    assert any("dismiss" in script and old_token in script for script in h.channel.calls)
    h.session.action({"token": old_token, "prompt": pending["prompt"], "action": "cancel"})
    h.drain()
    assert not h.received  # stale card cannot abort the standard driver's download
    h.session.dispatch("showDownloadDidReceiveDataOfLength_", 40)
    assert h.standard.showDownloadDidReceiveDataOfLength_.call_args.args == (40,)
    cancel = h.standard.showDownloadInitiatedWithCancellation_.call_args.args[0]
    cancel()
    cancel()
    assert h.received == [()]
    h.session.dismiss()
    h.found()
    assert h.session.mode == "card" and h.session.token != old_token


def test_failed_page_send_uses_standard_ui_but_explicit_reload_cancels(h):
    h.found()
    h.action("download")
    h.drain()
    h.received.clear()
    h.session.dispatch("showDownloadInitiatedWithCancellation_", h.block)
    original = h.channel.submit
    h.channel.submit = lambda script, complete, **opts: original(script, complete, **opts) if opts.get("defer") else complete(None)
    h.session.dispatch("showDownloadDidReceiveDataOfLength_", 30)
    assert h.session.mode == "standard" and not h.received
    h.session.page_closed(h.session.token)
    h.session.page_closed(h.session.token)
    assert h.received == [()]


@pytest.mark.parametrize("standard", [False, True])
def test_dismiss_drops_unanswered_callbacks_instead_of_answering_them(h, standard):
    h.session.dispatch("showReadyToInstallAndRelaunch_", h.block)
    h.channel.probe(None if standard else "page")
    reply = h.session.replies[0]
    h.session.dismiss()
    h.found()
    reply(card.INSTALL)  # a late native control cannot touch the next session
    assert h.received == [] and h.session.mode == "card"


def test_refused_quit_offers_install_retry_but_close_does_not_trigger_it(h):
    h.found()
    h.action("download")
    h.drain()
    h.received.clear()
    selector = "showInstallingUpdateWithApplicationTerminated_retryTerminatingApplication_"
    h.session.dispatch(selector, False, h.block)
    assert h.session.model["actions"] == ["install"]
    h.action("install")
    h.drain()
    assert h.received == [()]
    h.session.dispatch(selector, False, h.block)
    h.session.page_closed()
    assert h.received == [()]


def test_block_notes_keep_heading_bullets_and_inline_spacing():
    text = card.plain_text("<h2>Changes</h2><ul><li>Fixed <b>save</b> errors</li><li>Faster tiles</li></ul><p>Thanks</p>")
    assert text == "Changes\n• Fixed save errors\n• Faster tiles\nThanks"
