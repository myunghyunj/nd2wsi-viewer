"""Bounded page transport and main-thread Sparkle card session.

The native adapter owns this object. It never evaluates JavaScript on AppKit's
main thread; every Sparkle block is consumed on that thread at most once.
"""
from __future__ import annotations

import json
import math
import threading
import uuid
from collections import deque
from html.parser import HTMLParser
from urllib.parse import quote

PAGE_TIMEOUT = 2.0
DISMISS, INSTALL = 2, 1  # SPUUserUpdateChoice in Sparkle 2.9.6


def plain_text(value, limit=600):
    class Text(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts = []
            self.suppressed = 0

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style"):
                self.suppressed += 1
            if tag in ("p", "br", "li", "div"):
                self.parts.append(" ")

        def handle_endtag(self, tag):
            if tag in ("script", "style"):
                self.suppressed = max(0, self.suppressed - 1)
            self.parts.append(" ")

        def handle_data(self, data):
            if not self.suppressed:
                self.parts.append(data)

    parser = Text()
    parser.feed(str(value or "")[:65536])
    return " ".join("".join(parser.parts).split())[:limit]


def property_value(obj, name, default=None):
    value = getattr(obj, name, default)
    return value() if callable(value) else value


class PageChannel:
    """One evaluate_js at a time; a hung call permanently retires this channel."""
    def __init__(self, window, main):
        self.window, self.main = window, main
        self._guard = threading.Lock()
        self._queue = deque()
        self._running = False
        self._hung = False

    def submit(self, script, complete, *, coalesce=False):
        start = False
        with self._guard:
            rejected = self._hung or len(self._queue) >= 64
            if not rejected:
                if coalesce and self._queue and self._queue[-1][2]:
                    self._queue.pop()  # progress is replaceable, replies never are
                self._queue.append((script, complete, coalesce))
                if not self._running:
                    self._running = start = True
        if rejected:
            self.main(lambda: complete(None))
        elif start:
            threading.Thread(target=self._run, name="update-card-page", daemon=True).start()

    def _run(self):
        while True:
            with self._guard:
                if not self._queue:
                    self._running = False
                    return
                script, complete, _ = self._queue.popleft()
            done, result = threading.Event(), []

            def evaluate(script=script, done=done, result=result):
                try:
                    window = self.window()
                    result.append(window.evaluate_js(script) if window is not None else None)
                except Exception:  # noqa: BLE001 - a closing page is unavailable
                    result.append(None)
                finally:
                    done.set()

            threading.Thread(target=evaluate, name="update-card-evaluate", daemon=True).start()
            finished = done.wait(PAGE_TIMEOUT)
            value = result[0] if finished and result else None
            self.main(lambda complete=complete, value=value: complete(value))
            if not finished:
                with self._guard:
                    self._hung = True
                    queued, self._queue = self._queue, deque()
                    self._running = False
                for _, callback, _ in queued:
                    self.main(lambda callback=callback: callback(None))
                return


class Reply:
    """Invoked or retired only by the main-thread session."""
    def __init__(self, block, default=(), *, optional=False):
        self.block, self.default, self.optional = block, default, optional

    def __call__(self, *args):
        block, self.block = self.block, None
        if block is not None:
            block(*args)

    def retire(self):
        self.block = None


# Incoming selectors with blocks: argument index, safe teardown reply, optional.
BLOCKS = {
    "showUserInitiatedUpdateCheckWithCancellation_": (0, (), True),
    "showUpdateFoundWithAppcastItem_state_reply_": (2, (DISMISS,), False),
    "showUpdateNotFoundWithError_acknowledgement_": (1, (), False),
    "showUpdaterError_acknowledgement_": (1, (), False),
    "showDownloadInitiatedWithCancellation_": (0, (), True),
    "showReadyToInstallAndRelaunch_": (0, (DISMISS,), False),
    "showUpdateInstalledAndRelaunched_acknowledgement_": (1, (), False),
}


class UpdateCardSession:
    """All state mutations and block invocations run on the injected main queue."""
    def __init__(self, standard, window, main, *, retry=lambda: False, open_notes=lambda _: None,
                 channel=None, later=None):
        self.standard, self.main = standard, main
        self.channel = channel or PageChannel(window, main)
        self.retry, self.open_notes = retry, open_notes
        self.later = later or self._later
        self.mode = None
        self.token = None
        self.page_id = None
        self.model = {}
        self.events = []
        self.replies = []
        self.pending = None
        self.actions = {}
        self.closed = False
        self.retry_requested = False
        self.received = self.expected = 0
        self.release_url = None

    def _later(self, seconds, callback):
        timer = threading.Timer(seconds, lambda: self.main(callback))
        timer.daemon = True
        timer.start()
        return timer

    def dispatch(self, selector, *args):
        # Permission remains entirely native; automatic checks make it uncommon.
        if selector == "showUpdatePermissionRequest_reply_":
            reply = Reply(args[1])
            getattr(self.standard, selector)(args[0], reply)
            return
        if selector == "dismissUpdateInstallation":
            self.dismiss()
            return
        if self.token is None:
            self.token = uuid.uuid4().hex
            self.closed = False
            self.mode = "probing"
            token = self.token
            # Bound the whole decision, including time behind an older page call.
            # A late probe or an earlier session's timer cannot change the choice.
            self.later(PAGE_TIMEOUT, lambda: self._choose(token, None))
            self.channel.submit(
                "window.nd2wsiUpdateCard?.ready===true ? window.nd2wsiUpdateCard.pageId : null",
                lambda answer: self._choose(token, answer))
        wrapped = list(args)
        if selector in BLOCKS:
            index, default, optional = BLOCKS[selector]
            wrapped[index] = Reply(args[index], default, optional=optional)
            self.replies.append(wrapped[index])
        if self.closed:
            if selector in BLOCKS:
                reply = wrapped[BLOCKS[selector][0]]
                reply(*reply.default)
            return
        if self.mode == "probing":
            self.events.append((selector, wrapped))
            if len(self.events) >= 64:
                self._choose(self.token, None)
        else:
            self._route(selector, wrapped)

    def _choose(self, token, answer):
        if token != self.token or self.mode != "probing":
            return
        self.mode = "card" if isinstance(answer, str) and 0 < len(answer) <= 128 else "standard"
        self.page_id = answer if self.mode == "card" else None
        events, self.events = self.events, []
        for selector, args in events:
            self._route(selector, args)

    def _route(self, selector, args):
        if self.closed:
            return
        if self.mode == "standard":
            # Sparkle still calls and receives blocks on main. Wrappers protect
            # against a second call during standard-driver dismissal.
            getattr(self.standard, selector)(*args)
            return
        self._card(selector, args)

    def _set(self, state, title, message="", *, reply=None, actions=None, **extra):
        if self.pending is not None and self.pending.block is not None:
            if self.pending.optional:
                self.pending.retire()  # normal progress supersedes cancellation
            else:
                self.pending(*self.pending.default)
        self.pending = reply
        self.actions = actions or {}
        self.model = {"state": state, "title": title, "message": message, "token": self.token,
                      "prompt": uuid.uuid4().hex, "actions": list(self.actions),
                      "version": self.model.get("version", ""), **extra}
        self._show()

    def _show(self):
        token = self.token
        payload = json.dumps(self.model, ensure_ascii=True, allow_nan=False)
        page = json.dumps(self.page_id)
        script = f"window.nd2wsiUpdateCard?.pageId==={page} && window.nd2wsiUpdateCard.show({payload})===true"
        self.channel.submit(script, lambda ok: self.page_closed(token) if ok is not True else None, coalesce=True)

    def _card(self, selector, args):
        if selector == "showUserInitiatedUpdateCheckWithCancellation_":
            self._set("checking", "Checking for updates…", reply=args[0], actions={"cancel": ()})
        elif selector == "showUpdateFoundWithAppcastItem_state_reply_":
            item, state, reply = args
            version = plain_text(property_value(item, "displayVersionString", ""), 80)
            version_tag = str(property_value(item, "versionString", version))[:100]
            self.release_url = "https://github.com/myunghyunj/nd2wsi-viewer/releases/tag/" + quote("v" + version_tag.removeprefix("v"), safe="")
            installing = property_value(state, "stage", 0) == 2
            info_only = bool(property_value(item, "informationOnlyUpdate", False))
            self._set("ready" if installing else "available", "Ready to install " + version if installing else "Update available",
                      "The update may install when you quit." if installing else plain_text(property_value(item, "itemDescription", "")),
                      reply=reply, actions={"later": (DISMISS,), **({} if info_only else {"install" if installing else "download": (INSTALL,)})},
                      version=version, notes=True, on_quit=installing)
        elif selector == "showUpdateReleaseNotesWithDownloadData_":
            pass  # embedded notes are sufficient; never render downloaded HTML
        elif selector == "showUpdateReleaseNotesFailedToDownloadWithError_":
            pass
        elif selector in ("showUpdateNotFoundWithError_acknowledgement_", "showUpdaterError_acknowledgement_"):
            error, reply = args
            failed = selector == "showUpdaterError_acknowledgement_"
            self._set("error" if failed else "up_to_date", "Could not update" if failed else "No update available",
                      plain_text(property_value(error, "localizedDescription", str(error))), reply=reply,
                      actions={"close": (), "retry": ()} if failed else {})
            if not failed:
                self.later(5, lambda: reply())  # leave the manual result visible briefly
        elif selector == "showDownloadInitiatedWithCancellation_":
            self.received = self.expected = 0
            self._set("downloading", "Downloading " + self.model.get("version", ""),
                      "You can keep working while the update downloads.", reply=args[0], actions={"cancel": ()}, received=0, total=0)
        elif selector == "showDownloadDidReceiveExpectedContentLength_":
            self.expected = max(0, int(args[0]))
            self.model.update(total=self.expected)
            self._show()
        elif selector == "showDownloadDidReceiveDataOfLength_":
            self.received += max(0, int(args[0]))
            self.model.update(received=self.received)
            self._show()
        elif selector == "showDownloadDidStartExtractingUpdate":
            self._set("waiting", "Preparing the update", "Extracting the download…")
        elif selector == "showExtractionReceivedProgress_":
            value = float(args[0])
            self.model["progress"] = min(1, max(0, value)) if math.isfinite(value) else 0
            self._show()
        elif selector == "showReadyToInstallAndRelaunch_":
            self._set("ready", "Ready to install " + self.model.get("version", ""),
                      "Install now to save your work and relaunch. Install on quit keeps the update for when you close the app.",
                      reply=args[0], actions={"later": (DISMISS,), "install": (INSTALL,)}, on_quit=True)
        elif selector == "showInstallingUpdateWithApplicationTerminated_retryTerminatingApplication_":
            self._set("waiting", "Waiting to install", "Saving your work and preparing to relaunch…")
        elif selector == "showUpdateInstalledAndRelaunched_acknowledgement_":
            self._set("up_to_date", "Update installed", reply=args[1])
            self.later(5, lambda: args[1]())
        elif selector == "showUpdateInFocus":
            if self.model:
                self._show()

    def action(self, payload):
        if (not isinstance(payload, dict) or set(payload) != {"token", "prompt", "action"}
                or any(not isinstance(v, str) or len(v) > 128 for v in payload.values())):
            return False
        # Admission is cheap; validate again on main after any intervening callback.
        self.main(lambda: self._action(payload))
        return True

    def _action(self, payload):
        if (self.closed or self.mode != "card" or payload["token"] != self.token
                or payload["prompt"] != self.model.get("prompt")):
            return
        action = payload["action"]
        if action == "notes" and self.release_url:
            self.open_notes(self.release_url)
            return
        if action not in self.actions or self.pending is None or self.pending.block is None:
            return
        reply, values = self.pending, self.actions[action]
        self.actions = {}
        if action == "retry":
            self.retry_requested = True
        reply(*values)

    def page_closed(self, token=None):
        """Close/reload/transport failure ends card ownership without changing modes."""
        if token is not None and token != self.token:
            return
        if self.mode not in ("card", "probing"):
            return
        self.closed = True
        self.events = []
        for reply in self.replies:
            reply(*reply.default)
        self.actions = {}

    def dismiss(self):
        mode, token = self.mode, self.token
        self.closed = True
        # Normal Sparkle completion retires optional cancellation handlers; they
        # are not acknowledgments and calling them here could abort a newer phase.
        replies, self.replies = self.replies, []
        for reply in replies:
            if reply.optional:
                reply.retire()
            else:
                reply(*reply.default)
        if mode == "standard":
            self.standard.dismissUpdateInstallation()
        elif mode == "card":
            self.channel.submit("window.nd2wsiUpdateCard?.dismiss(" + json.dumps(token) + ")", lambda _: None)
        retry = self.retry_requested
        self.token = self.mode = self.page_id = None
        self.events, self.actions, self.model = [], {}, {}
        self.pending, self.retry_requested, self.release_url = None, False, None
        if retry:
            self._retry(20)

    def _retry(self, remaining):
        if self.token is not None:
            return
        if not self.retry() and remaining:
            self.later(.1, lambda: self._retry(remaining - 1))
