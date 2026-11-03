"""Exercise every Sparkle 2.9.6 user-driver selector in one private Agent window.

Run through run_native_check.py with a staged app. No Sparkle framework, feed,
download or installer is loaded; reply blocks only record main-thread calls.
"""
from __future__ import annotations

import json
import os
import re
import threading
import traceback
from html import escape
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
report = {"ok": False, "session": session.as_dict(), "selectors": [], "replies": {}, "screenshots": {}, "ui": {}}
report_path = qa / os.environ["ASTROMAP_QA_REPORT"]
static = Path(app.__file__).parent / "static"


def release_notes_fixture():
    """Appcast-style HTML with <strong> leads, and the items expected independently of the app."""
    notes = Path(__file__).resolve().parents[1] / "docs/release-notes-2.5.6.md"
    feature_text = notes.read_text(encoding="utf-8").split("\n### Downloads", 1)[0]
    html, items = [], []

    def plain(text):
        return " ".join(re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", text).replace("**", "").replace("`", "").split())

    for line in feature_text.splitlines():
        if line.startswith("- "):
            lead = re.match(r"\*\*(.+?)\*\*\s*(.*)", line[2:])
            assert lead, "Each 2.5.6 feature bullet leads with a bold title"
            title, detail = plain(lead[1]), plain(lead[2])
            html.append("<li><strong>" + escape(title) + "</strong> " + escape(detail) + "</li>")
            items.append({"title": title.removesuffix("."), "detail": detail})
    assert len(items) >= 5, "Release-note fixture must contain the actual feature bullets"
    assert all(len(item["title"]) <= 80 and len(item["detail"]) <= 300 for item in items)
    return "<ul>" + "".join(html) + "</ul>", items[:8]


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


def inspect_card(expected_notes=None, ready=False, notes_link=None):
    # Visibility is the computed display, not the hidden attribute: a later
    # display rule once showed parts the page had hidden.
    state = window.evaluate_js("""(() => {
      const byId=id=>document.getElementById(id), shown=element=>!!element && getComputedStyle(element).display!=='none';
      const list=byId('update-notice-changes'), full=byId('update-notice-entries');
      const expand=byId('update-notice-expand'), installed=byId('update-notice-installed');
      const box=element=>{const rect=element.getBoundingClientRect();return {top:rect.top,bottom:rect.bottom,height:rect.height};};
      return {listTag:list?.tagName,
        shown:{summary:shown(list), entries:shown(full), details:shown(expand), notes:shown(byId('update-notice-notes')),
          links:shown(byId('update-notice-links')), installed:shown(installed)},
        bullets:Array.from(list?.children||[],li=>({tag:li.tagName,text:li.textContent})),
        entries:Array.from(full?.children||[],li=>Array.from(li.children,node=>[node.tagName,node.textContent])),
        toggle:{tag:expand?.tagName,text:expand?.textContent,expanded:expand?.getAttribute('aria-expanded'),
          controls:expand?.getAttribute('aria-controls')},
        installed:shown(installed) ? installed.textContent : null,
        card:box(byId('update-notice')), viewport:innerHeight,
        entriesBox:shown(full) ? {...box(full), scrollHeight:full.scrollHeight, clientHeight:full.clientHeight} : null,
        actions:Array.from(document.querySelectorAll('#update-notice-actions button'),button=>{
          const rect=button.getBoundingClientRect();
          return {text:button.textContent,primary:button.classList.contains('update-primary'),
            disabled:button.disabled,left:rect.left,right:rect.right};
        })};
    })()""")
    if expected_notes is not None:
        assert state["listTag"] == "UL" and state["shown"]["summary"], state
        assert state["bullets"] == [{"tag": "LI", "text": item["title"]} for item in expected_notes[:4]], state
        # Details exists only for more than four notes or notes with detail text.
        details = len(expected_notes) > 4 or any(item["detail"] for item in expected_notes)
        assert state["shown"]["details"] == details, state
        # Every new state arrives collapsed, even after Details was opened.
        assert state["toggle"] == {"tag": "BUTTON", "text": "Details", "expanded": "false",
                                   "controls": "update-notice-entries"}, state
        assert not state["shown"]["entries"], state
        assert state["card"]["top"] >= 0 and state["card"]["bottom"] <= state["viewport"], state
    elif notes_link is not None:
        # No notes: no summary, entries or Details.
        assert not any(state["shown"][part] for part in ("summary", "entries", "details")), state
    if notes_link is not None:
        assert state["shown"]["notes"] == notes_link, state
        assert state["shown"]["links"] == (notes_link or state["shown"]["details"]), state
    if ready:
        assert [button["text"] for button in state["actions"]] == ["Install and relaunch", "Cancel"], state
        install, cancel = state["actions"]
        assert install["primary"] and not cancel["primary"], state
        assert not install["disabled"] and not cancel["disabled"], state
        assert install["right"] <= cancel["left"], state
    return state


def expand_details(name, expected_notes):
    """Open Details by its button, check every entry, screenshot, and close it again."""
    assert window.evaluate_js("document.getElementById('update-notice-expand').click(); true")
    wait_for(lambda: window.evaluate_js(
        "document.getElementById('update-notice-expand').getAttribute('aria-expanded')==='true'"),
        description=name + " details")
    state = inspect_card()
    assert state["toggle"]["expanded"] == "true" and state["shown"]["entries"] and not state["shown"]["summary"], state
    assert state["shown"]["details"], state
    assert state["entries"] == [
        [["STRONG", item["title"]]] + ([["SPAN", item["detail"]]] if item["detail"] else []) for item in expected_notes
    ], state
    assert state["entriesBox"]["clientHeight"] <= 240, state  # scrolls inside the card instead of growing past it
    assert state["card"]["top"] >= 0 and state["card"]["bottom"] <= state["viewport"], state
    report["ui"][name + "-details"] = state
    snapshot(name + "-details")
    assert window.evaluate_js("document.getElementById('update-notice-expand').click(); true")
    wait_for(lambda: window.evaluate_js(
        "document.getElementById('update-notice-expand').getAttribute('aria-expanded')==='false' && "
        "getComputedStyle(document.getElementById('update-notice-entries')).display==='none'"),
        description=name + " details closed")


def snapshot(name):
    for theme in ("dark", "light"):
        window.evaluate_js("document.documentElement.className=" + json.dumps(theme) + "; true")
        path = session.exports_root / f"update-card-{name}-{theme}.png"
        report["screenshots"][name + "-" + theme] = str(save_cocoa_snapshot(window, path))


def capture(name, expected_notes=None):
    expected_title = {
        "available": "nd2wsi-viewer 2.5.6 is available", "downloading": "Downloading 2.5.6",
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
    # Only the found offer carries the Release notes link; downloading and
    # ready keep the notes but must hide it.
    notes_link = None if name == "waiting" else name == "available"
    report["ui"][name] = inspect_card(expected_notes, ready=name == "ready", notes_link=notes_link)
    if name in ("available", "downloading", "ready"):
        # The frozen app must find its own package version for this line.
        assert report["ui"][name]["installed"] == "Installed: " + report["installed_version"], report["ui"][name]
    snapshot(name)
    if expected_notes is not None and name in ("available", "ready"):
        expand_details(name, expected_notes)


def run():
    card = None
    try:
        context = bridge_result(window, "pywebview.api.window_context()")
        assert context["role"] == "agent" and context["id"] == session.id
        assert api._updates_disabled and api._updater is None
        assert not any("Sparkle.framework" in str(bundle.bundlePath()) for bundle in NSBundle.allFrameworks())
        html = '<!doctype html><html data-update-card-shell="true"><head><style>'
        html += "\n".join((static / name).read_text(encoding="utf-8") for name in
                          ("materials-v1.css", "native-shell-v1.css", "update-card-v1.css"))
        html += 'body{background:linear-gradient(125deg,#31586c,#664a70,#2b6854);min-height:100vh}'
        html += '#qa-preview{position:fixed;top:16px;left:16px;color:var(--label);font:12px system-ui}</style></head><body>'
        html += '<p id="qa-preview">2.5.6 release notes · Preview</p>'
        html += (static / "update-card.html").read_text(encoding="utf-8") + '<script>'
        html += (static / "update-card-v1.js").read_text(encoding="utf-8") + '</script></body></html>'
        window.load_html(html)
        wait_for(lambda: window.evaluate_js("window.nd2wsiUpdateCard?.ready===true"), description="Agent update card")
        standard = Standard()
        card = UpdateCardSession(standard, lambda: window,
                                 lambda fn: NSOperationQueue.mainQueue().addOperationWithBlock_(fn))
        report["installed_version"] = card.current_version
        assert re.match(r"\d+\.\d+", card.current_version), "The packaged app must know its installed version"
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

        def click_ready(label, reply_name):
            assert window.evaluate_js("""(() => {
              const button=Array.from(document.querySelectorAll('#update-notice-actions button'))
                .find(button=>button.textContent===""" + json.dumps(label) + """);
              if(!button||button.disabled)return false;
              button.click();return true;
            })()""")
            wait_for(lambda: report["replies"].get(reply_name), description=reply_name + " native reply")

        call("showUpdatePermissionRequest_reply_", None, record("permission"))
        call("showUserInitiatedUpdateCheckWithCancellation_", record("check_cancel"))
        wait_for(lambda: card.mode == "card", description="card choice")
        click("cancel")
        call("dismissUpdateInstallation")
        notes_html, expected_notes = release_notes_fixture()
        report["release_notes"] = {"fixture": "docs/release-notes-2.5.6.md", "preview": True,
                                   "bullets": expected_notes}
        item = SimpleNamespace(displayVersionString=lambda: "2.5.6", versionString=lambda: "2.5.6",
                               itemDescription=lambda: notes_html, isInformationOnlyUpdate=lambda: False)
        call("showUpdateFoundWithAppcastItem_state_reply_", item, SimpleNamespace(stage=lambda: 0), record("found"))
        wait_for(lambda: card.model.get("state") == "available", description="available")
        assert card.model.get("release_notes") == expected_notes
        capture("available", expected_notes)
        call("showUpdateReleaseNotesWithDownloadData_", None)
        call("showUpdateReleaseNotesFailedToDownloadWithError_", None)
        call("showUpdateInFocus")
        click("download")
        call("showDownloadInitiatedWithCancellation_", record("download_cancel"))
        call("showDownloadDidReceiveExpectedContentLength_", 132100000)
        call("showDownloadDidReceiveDataOfLength_", 55400000)
        assert card.model.get("release_notes") == expected_notes
        capture("downloading", expected_notes)
        call("showDownloadDidStartExtractingUpdate")
        call("showExtractionReceivedProgress_", .5)
        call("showReadyToInstallAndRelaunch_", record("ready"))
        assert card.model.get("release_notes") == expected_notes
        capture("ready", expected_notes)
        click_ready("Install and relaunch", "ready")
        call("showInstallingUpdateWithApplicationTerminated_retryTerminatingApplication_", False, record("retry_termination"))
        window.evaluate_js("window.nd2wsiUpdateCard.notice('Waiting for 1 export to finish…'); true")
        capture("waiting")
        call("showUpdateInstalledAndRelaunched_acknowledgement_", True, record("installed"))
        call("dismissUpdateInstallation")
        # Cancel a second ready update through the visible button. Choice 0
        # cancels this ready installation; choice 2 would install on quit.
        call("showUpdateFoundWithAppcastItem_state_reply_", item, SimpleNamespace(stage=lambda: 0), record("cancel_found"))
        wait_for(lambda: card.model.get("state") == "available", description="cancel scenario available")
        click("download")
        call("showReadyToInstallAndRelaunch_", record("ready_cancel"))
        wait_for(lambda: card.model.get("state") == "ready", description="cancel scenario ready")
        wait_for(lambda: window.evaluate_js("document.getElementById('update-notice-title').textContent==='Ready to install 2.5.6'"),
                 description="cancel scenario ready card")
        assert card.model.get("release_notes") == expected_notes
        report["ui"]["ready-cancel"] = inspect_card(expected_notes, ready=True, notes_link=False)
        click_ready("Cancel", "ready_cancel")
        assert report["replies"]["ready_cancel"] == [[0]], report["replies"]
        call("dismissUpdateInstallation")
        # Two title-only notes fit the summary: Details must not be displayed.
        short_notes = [{"title": "First change", "detail": ""}, {"title": "Second change", "detail": ""}]
        short_item = SimpleNamespace(displayVersionString=lambda: "2.5.6", versionString=lambda: "2.5.6",
                                     itemDescription=lambda: "<ul><li>First change</li><li>Second change</li></ul>",
                                     isInformationOnlyUpdate=lambda: False)
        call("showUpdateFoundWithAppcastItem_state_reply_", short_item, SimpleNamespace(stage=lambda: 0), record("short_found"))
        wait_for(lambda: card.model.get("release_notes") == short_notes, description="short notes available")
        wait_for(lambda: window.evaluate_js(
            "!document.getElementById('update-notice').hidden && "
            "document.querySelector('#update-notice-changes li')?.textContent==='First change'"),
            description="short notes card")
        report["ui"]["available-short"] = inspect_card(short_notes, notes_link=True)
        snapshot("available-short")
        click("later")
        assert report["replies"]["short_found"] == [[2]], report["replies"]
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
        (session.exports_root / "update-card-report.json").write_text(payload, encoding="utf-8")
        report_path.write_text(payload, encoding="utf-8")
        window.destroy()


try:
    webview.start(run, private_mode=True)
finally:
    api._close_translucent_windows()
    session.mark_closed()
