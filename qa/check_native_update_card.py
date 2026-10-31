"""Exercise every Sparkle 2.9.6 user-driver selector in one private Agent window.

Run through run_native_check.py with a staged app. No Sparkle framework, feed,
download or installer is loaded; reply blocks only record main-thread calls.
"""
from __future__ import annotations

import json
import os
import threading
import traceback
from pathlib import Path
from types import SimpleNamespace

os.environ["ND2WSI_DISABLE_UPDATES"] = "1"
os.environ["ND2WSI_APP_NAME"] = "nd2wsi-viewer Update Card QA"

import objc  # noqa: E402
import webview  # noqa: E402
from Foundation import NSBundle, NSOperationQueue, NSThread  # noqa: E402

from nd2wsi import app, updater  # noqa: E402
from nd2wsi.translucent_smoke import bridge_result, save_cocoa_snapshot, wait_for  # noqa: E402
from nd2wsi.update_card import UpdateCardSession  # noqa: E402
from nd2wsi.window_sessions import create_window_session  # noqa: E402

qa = Path(os.environ["ASTROMAP_QA_ROOT"]).resolve()
session = create_window_session("agent", qa / "sessions")
api, window = app.create_app_window(None, window_session=session)
report = {"ok": False, "session": session.as_dict(), "selectors": [], "replies": {}, "screenshots": {}}
report_path = qa / os.environ["ASTROMAP_QA_REPORT"]
static = Path(app.__file__).parent / "static"


def on_main(callback):
    done, box = threading.Event(), {}

    def invoke():
        try:
            assert NSThread.isMainThread()
            box["result"] = callback()
        except BaseException as exc:
            box["error"] = exc
        finally:
            done.set()

    NSOperationQueue.mainQueue().addOperationWithBlock_(invoke)
    if not done.wait(10):
        raise TimeoutError("AppKit main queue did not answer")
    if "error" in box:
        raise box["error"]
    return box.get("result")


def record(name):
    def reply(*args):
        assert NSThread.isMainThread(), "Sparkle reply ran off main"
        report["replies"].setdefault(name, []).append(list(args))
        assert len(report["replies"][name]) == 1, "A reply ran twice"
    return reply


class Standard:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def receive(*args):
            assert NSThread.isMainThread()
            self.calls.append(name)
            if name == "showUpdatePermissionRequest_reply_":
                args[-1](None)
        return receive


def capture(name):
    expected_title = {
        "available": "Update available", "downloading": "Downloading 2.5.6",
        "ready": "Ready to install 2.5.6", "waiting": "Waiting to install",
        "error": "Could not update", "up-to-date": "No update available",
    }[name]
    wait_for(lambda: window.evaluate_js(
        "!document.getElementById('update-notice').hidden && "
        "document.getElementById('update-notice-title').textContent===" + json.dumps(expected_title)),
        description=name)
    if name == "downloading":
        wait_for(lambda: window.evaluate_js(
            "document.getElementById('update-notice-detail').textContent==='55.4 MB of 132.1 MB'"),
            description="download progress")
    for theme in ("dark", "light"):
        window.evaluate_js("document.documentElement.className=" + json.dumps(theme) + "; true")
        path = session.exports_root / f"update-card-{name}-{theme}.png"
        report["screenshots"][name + "-" + theme] = str(save_cocoa_snapshot(window, path))


def run():
    card = None
    try:
        context = bridge_result(window, "pywebview.api.window_context()")
        assert context["role"] == "agent" and context["id"] == session.id
        assert api._updates_disabled and api._updater is None
        assert not any("Sparkle.framework" in str(bundle.bundlePath()) for bundle in NSBundle.allFrameworks())
        html = '<!doctype html><html data-update-card-shell="true"><head><style>'
        html += (static / "native-shell-v1.css").read_text() + (static / "update-card-v1.css").read_text()
        html += 'body{background:linear-gradient(125deg,#31586c,#664a70,#2b6854);min-height:100vh}</style></head><body>'
        html += (static / "update-card.html").read_text() + '<script>' + (static / "update-card-v1.js").read_text() + '</script></body></html>'
        window.load_html(html)
        wait_for(lambda: window.evaluate_js("window.nd2wsiUpdateCard?.ready===true"), description="Agent update card")
        standard = Standard()
        card = UpdateCardSession(standard, lambda: window,
                                 lambda fn: NSOperationQueue.mainQueue().addOperationWithBlock_(fn))
        signatures = {
            "showUpdatePermissionRequest:reply:": b"v@:@@?",
            "showUserInitiatedUpdateCheckWithCancellation:": b"v@:@?",
            "showUpdateFoundWithAppcastItem:state:reply:": b"v@:@@@?",
            "showUpdateReleaseNotesWithDownloadData:": b"v@:@",
            "showUpdateReleaseNotesFailedToDownloadWithError:": b"v@:@",
            "showUpdateNotFoundWithError:acknowledgement:": b"v@:@@?",
            "showUpdaterError:acknowledgement:": b"v@:@@?",
            "showDownloadInitiatedWithCancellation:": b"v@:@?",
            "showDownloadDidReceiveExpectedContentLength:": b"v@:Q",
            "showDownloadDidReceiveDataOfLength:": b"v@:Q",
            "showDownloadDidStartExtractingUpdate": b"v@:",
            "showExtractionReceivedProgress:": b"v@:d",
            "showReadyToInstallAndRelaunch:": b"v@:@?",
            "showInstallingUpdateWithApplicationTerminated:retryTerminatingApplication:": b"v@:Z@?",
            "showUpdateInstalledAndRelaunched:acknowledgement:": b"v@:Z@?",
            "dismissUpdateInstallation": b"v@:", "showUpdateInFocus": b"v@:",
        }
        protocol = objc.formal_protocol("Nd2wsiQAUserDriverStandIn", None, [
            objc.selector(None, selector=name.encode(), signature=signature) for name, signature in signatures.items()])
        driver = on_main(lambda: updater._driver_class(protocol).alloc().initWithCard_(card))
        api.attach_updater(SimpleNamespace(card=card))

        def call(name, *args):
            report["selectors"].append(name)
            return on_main(lambda: getattr(driver, name)(*args))

        def click(action):
            value = on_main(lambda: {"token": card.token, "prompt": card.model["prompt"], "action": action})
            assert api.update_card_action(value)["ok"]
            on_main(lambda: None)  # drain the action before advancing the protocol

        call("showUpdatePermissionRequest_reply_", None, record("permission"))
        call("showUserInitiatedUpdateCheckWithCancellation_", record("check_cancel"))
        wait_for(lambda: card.mode == "card", description="card choice")
        click("cancel")
        call("dismissUpdateInstallation")
        item = SimpleNamespace(displayVersionString=lambda: "2.5.6", versionString=lambda: "2.5.6",
                               itemDescription=lambda: "<p>Viewer improvements and fixes.</p>", isInformationOnlyUpdate=lambda: False)
        call("showUpdateFoundWithAppcastItem_state_reply_", item, SimpleNamespace(stage=lambda: 0), record("found"))
        wait_for(lambda: card.model.get("state") == "available", description="available")
        capture("available")
        call("showUpdateReleaseNotesWithDownloadData_", None)
        call("showUpdateReleaseNotesFailedToDownloadWithError_", None)
        call("showUpdateInFocus")
        click("download")
        call("showDownloadInitiatedWithCancellation_", record("download_cancel"))
        call("showDownloadDidReceiveExpectedContentLength_", 132100000)
        call("showDownloadDidReceiveDataOfLength_", 55400000)
        capture("downloading")
        call("showDownloadDidStartExtractingUpdate")
        call("showExtractionReceivedProgress_", .5)
        call("showReadyToInstallAndRelaunch_", record("ready"))
        capture("ready")
        click("install")
        call("showInstallingUpdateWithApplicationTerminated_retryTerminatingApplication_", False, record("retry_termination"))
        window.evaluate_js("window.nd2wsiUpdateCard.notice('Waiting for 1 export to finish…'); true")
        capture("waiting")
        call("showUpdateInstalledAndRelaunched_acknowledgement_", True, record("installed"))
        call("dismissUpdateInstallation")
        call("showUpdaterError_acknowledgement_", SimpleNamespace(localizedDescription=lambda: "Could not download. Try again."), record("error"))
        wait_for(lambda: card.model.get("state") == "error", description="error")
        capture("error")
        click("close")
        call("dismissUpdateInstallation")
        call("showUpdateNotFoundWithError_acknowledgement_", SimpleNamespace(localizedDescription=lambda: "You're up to date."), record("no_update"))
        wait_for(lambda: card.model.get("state") == "up_to_date", description="up to date")
        capture("up-to-date")
        call("dismissUpdateInstallation")
        # Reload ends an unanswered choice once, even if a stale button arrives.
        call("showReadyToInstallAndRelaunch_", record("reload"))
        wait_for(lambda: card.model.get("state") == "ready", description="reload choice")
        token = card.token
        on_main(lambda: card.page_closed(token))
        on_main(lambda: card.page_closed(token))
        call("dismissUpdateInstallation")
        # The same production driver must forward a whole session when no page answers.
        fallback = UpdateCardSession(standard, lambda: None,
                                     lambda fn: NSOperationQueue.mainQueue().addOperationWithBlock_(fn))
        on_main(lambda: setattr(driver, "card", fallback))
        call("showUserInitiatedUpdateCheckWithCancellation_", record("fallback_cancel"))
        wait_for(lambda: fallback.mode == "standard", description="standard fallback")
        call("showDownloadDidReceiveExpectedContentLength_", 100)
        call("dismissUpdateInstallation")
        assert "showDownloadDidReceiveExpectedContentLength_" in standard.calls
        assert report["replies"]["found"] == [[1]] and report["replies"]["ready"] == [[1]]
        assert report["replies"]["reload"] == [[2]]
        assert "download_cancel" not in report["replies"] and "retry_termination" not in report["replies"]
        assert not any("Sparkle.framework" in str(bundle.bundlePath()) for bundle in NSBundle.allFrameworks())
        report["ok"] = True
    except BaseException:
        report["error"] = traceback.format_exc()
    finally:
        if card is not None:
            on_main(card.page_closed)
        payload = json.dumps(report, ensure_ascii=False, indent=2)
        (session.exports_root / "update-card-report.json").write_text(payload)
        report_path.write_text(payload)
        window.destroy()


try:
    webview.start(run, private_mode=True)
finally:
    api._close_translucent_windows()
    session.mark_closed()
