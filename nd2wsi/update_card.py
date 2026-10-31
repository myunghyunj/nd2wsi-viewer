"""Bounded page transport and main-thread Sparkle card session.

The native adapter owns this object. It never evaluates JavaScript on AppKit's
main thread; every Sparkle block is consumed on that thread at most once.
"""
from __future__ import annotations

import json
import math
import re
import threading
import uuid
from collections import deque
from html.parser import HTMLParser
from urllib.parse import quote

PAGE_TIMEOUT = 2.0
SKIP, INSTALL, DISMISS = 0, 1, 2  # SPUUserUpdateChoice in Sparkle 2.9.6
RELEASE_NOTE_LIMIT = 8
RELEASE_NOTE_ITEM_LIMIT = 300
RELEASE_NOTE_SOURCE_LIMIT = 65536


def _text_source(value, *, group_list_items=False):
    class Text(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts = []
            self.suppressed = 0
            self.list_depth = 0

        def boundary(self):
            self.parts.append(" " if group_list_items and self.list_depth else "\n")

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style"):
                self.suppressed += 1
            if tag == "li":
                self.boundary()
                if not group_list_items or not self.list_depth:
                    self.parts.append("• ")
                self.list_depth += 1
            elif tag in ("p", "br", "div", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "pre"):
                self.boundary()

        def handle_endtag(self, tag):
            if tag in ("script", "style"):
                self.suppressed = max(0, self.suppressed - 1)
            if tag == "li":
                self.list_depth = max(0, self.list_depth - 1)
            if tag in ("p", "li", "div", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "pre"):
                self.boundary()

        def handle_data(self, data):
            if not self.suppressed:
                if group_list_items and self.list_depth:
                    data = re.sub(r"\s+", " ", data)
                self.parts.append(data)

    parser = Text()
    parser.feed(str(value or "")[:65536])
    return "".join(parser.parts)


def plain_text(value, limit=600):
    lines = [" ".join(line.split()) for line in _text_source(value).splitlines()]
    return "\n".join(line for line in lines if line)[:limit]


def release_note_items(value):
    """Bound HTML, Markdown or plain release notes to display-only text items.

    Prefer the release's own bullets, without the download/checksum appendix.
    A prose-only appcast description becomes one item per paragraph. Neither
    markup nor links are handed to the page for HTML rendering.
    """
    if not isinstance(value, str):
        return []
    source = value[:RELEASE_NOTE_SOURCE_LIMIT]
    # Retain heading boundaries through the HTML-to-text pass.
    source = re.sub(r"<h[1-6]\b[^>]*>", "\n# ", source, flags=re.I)
    # Loose HTML lists wrap each item's text in paragraphs. Keep those nested
    # blocks together while preserving a boundary between top-level items.
    text = _text_source(source, group_list_items=True)
    bullets, paragraphs = [], []
    current = paragraph = None
    fenced = None
    for raw in text.splitlines():
        line = raw.strip()
        fence = re.match(r"^(`{3,}|~{3,})", line)
        if fence:
            marker = fence[1][0]
            if fenced is None:
                fenced = marker
            elif fenced == marker:
                fenced = None
            continue
        if fenced or re.match(r"^\[[^]]+\]:", line):
            continue
        if not line:
            current = paragraph = None
            continue
        heading = re.match(r"^#{1,6}\s+(.+?)\s*#*$", line)
        if heading:
            current = paragraph = None
            if heading[1].strip().casefold() in {"downloads", "checksums", "assets", "installation"}:
                break
            continue
        if re.fullmatch(r"[-*_]{3,}", line):
            current = paragraph = None
            continue
        item = re.match(r"^(?:[-*+•‣]|\d+[.)])\s+(.+)$", line)
        if item:
            bullets.append(item[1])
            current = len(bullets) - 1
            paragraph = None
        elif current is not None:
            bullets[current] += " " + line
        elif paragraph is not None:
            paragraphs[paragraph] += " " + line
        else:
            paragraphs.append(line)
            paragraph = len(paragraphs) - 1

    def clean(item):
        # Keep a Markdown link's label; never expose its target or an image URL.
        destination = r"\((?:[^()\n]|\([^()\n]*\))*\)"
        item = re.sub(r"!\[[^]]*\]" + destination, "", item)
        item = re.sub(r"\[([^]]+)\]" + destination, r"\1", item)
        item = re.sub(r"\[([^]]+)\]\[[^]]*\]", r"\1", item)
        item = re.sub(r"^\[[ xX]\]\s*", "", item)
        item = re.sub(r"(\*\*|__|~~|\*|_)(.+?)\1", r"\2", item)
        item = item.replace("`", "")
        item = re.sub(r"\\([\\*_{}\[\]()#+.!>\-])", r"\1", item)
        item = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", item)
        return " ".join(item.split())[:RELEASE_NOTE_ITEM_LIMIT]

    result = []
    for item in bullets or paragraphs:
        item = clean(item)
        if item:
            result.append(item)
        if len(result) == RELEASE_NOTE_LIMIT:
            break
    return result


def _downloaded_note_items(download):
    """Decode bounded SPUDownloadData bytes; invalid notes keep embedded notes."""
    data = property_value(download, "data", download)
    if isinstance(data, str):
        return release_note_items(data)
    try:
        if len(data) > RELEASE_NOTE_SOURCE_LIMIT:
            return []
        raw = bytes(data)
    except (TypeError, ValueError):
        return []
    encoding = property_value(download, "textEncodingName", "utf-8") or "utf-8"
    try:
        text = raw.decode(encoding, errors="replace")
    except (LookupError, TypeError, UnicodeError):
        text = raw.decode("utf-8", errors="replace")
    return release_note_items(text)


def property_value(obj, name, default=None):
    value = getattr(obj, name, default)
    return value() if callable(value) else value


class PageChannel:
    """One evaluate_js at a time; stalls suspend calls until the page returns."""
    def __init__(self, window, main):
        self.window, self.main = window, main
        self._guard = threading.Lock()
        self._queue = deque()
        self._running = False
        self._hung = False

    def submit(self, script, complete, *, coalesce=False, defer=False):
        start = False
        with self._guard:
            rejected = (self._hung and not defer) or len(self._queue) >= 64
            if not rejected:
                if coalesce and self._queue and self._queue[-1][2]:
                    self._queue.pop()  # progress is replaceable, replies never are
                self._queue.append((script, complete, coalesce, defer))
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
                script, complete, _, _ = self._queue.popleft()
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
            if finished:
                value = result[0] if result else None
                self.main(lambda complete=complete, value=value: complete(value))
                continue
            with self._guard:
                self._hung = True
                queued, self._queue = self._queue, deque()
                for entry in queued:
                    if entry[3]:  # token-scoped cleanup must run after recovery
                        self._queue.append(entry)
            self.main(lambda complete=complete: complete(None))
            for _, callback, _, deferred in queued:
                if not deferred:
                    self.main(lambda callback=callback: callback(None))
            # This one worker waits for its one outstanding evaluation. No second
            # evaluation runs concurrently, and the late result is never accepted.
            done.wait()
            with self._guard:
                self._hung = False


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
    "showInstallingUpdateWithApplicationTerminated_retryTerminatingApplication_": (1, None, True),
}


PHASES = set(BLOCKS) | {"showDownloadDidStartExtractingUpdate",
                       "showInstallingUpdateWithApplicationTerminated_retryTerminatingApplication_"}


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
        self.release_notes = []
        self.phase = None
        self.was_card = False

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
                if reply.default is None:
                    reply.retire()
                else:
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
        self.was_card = self.mode == "card"
        events, self.events = self.events, []
        for selector, args in events:
            self._route(selector, args)

    def _route(self, selector, args):
        if self.closed:
            return
        if self.mode == "standard":
            # Sparkle still calls and receives blocks on main. Wrappers protect
            # against a second call during standard-driver dismissal.
            if selector in PHASES:
                if self.pending is not None:
                    self.pending.retire()
                self.pending = args[BLOCKS[selector][0]] if selector in BLOCKS else None
            getattr(self.standard, selector)(*args)
            return
        self._card(selector, args)

    def _set(self, state, title, message="", *, reply=None, actions=None, **extra):
        if self.pending is not None:
            self.pending.retire()  # a new phase supersedes the previous UI reply
        self.pending = reply
        self.actions = actions or {}
        self.model = {"state": state, "title": title, "message": message, "token": self.token,
                      "prompt": uuid.uuid4().hex, "actions": list(self.actions),
                      "version": self.model.get("version", ""),
                      "release_notes": list(self.release_notes), **extra}
        self._show()

    def _show(self):
        token = self.token
        payload = json.dumps(self.model, ensure_ascii=True, allow_nan=False)
        page = json.dumps(self.page_id)
        script = f"window.nd2wsiUpdateCard?.pageId==={page} && window.nd2wsiUpdateCard.show({payload})===true"
        self.channel.submit(script, lambda ok: self._fallback(token) if ok is not True else None, coalesce=True)

    def _clear_card(self, token):
        self.channel.submit("window.nd2wsiUpdateCard?.dismiss(" + json.dumps(token) + ")",
                            lambda _: None, defer=True)

    def _fallback(self, token):
        """Transfer the current phase and live reply; a slow page is not Cancel."""
        if token != self.token or self.mode != "card" or self.closed:
            return
        self.mode = "standard"
        self.actions = {}
        self._clear_card(token)
        if self.phase is None:
            return
        selector, args = self.phase
        if selector in BLOCKS and args[BLOCKS[selector][0]].block is None:
            return  # a consumed prompt is awaiting Sparkle's next phase
        getattr(self.standard, selector)(*args)
        if selector == "showDownloadInitiatedWithCancellation_":
            self.standard.showDownloadDidReceiveExpectedContentLength_(self.expected)
            self.standard.showDownloadDidReceiveDataOfLength_(self.received)
        elif selector == "showDownloadDidStartExtractingUpdate":
            self.standard.showExtractionReceivedProgress_(self.model.get("progress", 0))

    def _ack_visible(self, token, reply):
        if token == self.token and self.mode == "card" and not self.closed and reply is self.pending:
            reply()

    def _card(self, selector, args):
        if selector in PHASES:
            self.phase = selector, args
        if selector == "showUserInitiatedUpdateCheckWithCancellation_":
            self._set("checking", "Checking for updates…", reply=args[0], actions={"cancel": ()})
        elif selector == "showUpdateFoundWithAppcastItem_state_reply_":
            item, state, reply = args
            version = plain_text(property_value(item, "displayVersionString", ""), 80)
            version_tag = str(property_value(item, "versionString", version))[:100]
            self.release_url = "https://github.com/myunghyunj/nd2wsi-viewer/releases/tag/" + quote("v" + version_tag.removeprefix("v"), safe="")
            info_only = bool(property_value(item, "isInformationOnlyUpdate", False))
            installing = property_value(state, "stage", 0) == 2 and not info_only
            self.release_notes = release_note_items(property_value(item, "itemDescription", ""))
            actions = {"later": (DISMISS,)} if info_only else (
                {"install": (INSTALL,), "cancel": (SKIP,)} if installing
                else {"later": (DISMISS,), "download": (INSTALL,)})
            self._set("ready" if installing else "available", "Ready to install " + version if installing else "Update available",
                      ("Cancel stops this installation and hides automatic reminders for this version. "
                       "You can still use Check for Updates.") if installing else "",
                      reply=reply, actions=actions, version=version, notes=True)
        elif selector == "showUpdateReleaseNotesWithDownloadData_":
            items = _downloaded_note_items(args[0])
            if items and self.release_url is not None and self.model:
                self.release_notes = items
                self.model["release_notes"] = list(items)
                self._show()
        elif selector == "showUpdateReleaseNotesFailedToDownloadWithError_":
            pass
        elif selector in ("showUpdateNotFoundWithError_acknowledgement_", "showUpdaterError_acknowledgement_"):
            error, reply = args
            failed = selector == "showUpdaterError_acknowledgement_"
            self._set("error" if failed else "up_to_date", "Could not update" if failed else "No update available",
                      plain_text(property_value(error, "localizedDescription", str(error))), reply=reply,
                      actions={"close": (), "retry": ()} if failed else {})
            if not failed:
                self.later(5, lambda token=self.token: self._ack_visible(token, reply))  # leave the manual result visible briefly
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
                      reply=args[0], actions={"install": (INSTALL,), "cancel": (SKIP,)})
        elif selector == "showInstallingUpdateWithApplicationTerminated_retryTerminatingApplication_":
            terminated, retry_termination = args
            self._set("waiting", "Waiting to install", "Saving your work and preparing to relaunch…",
                      reply=retry_termination, actions={} if terminated else {"install": ()})
        elif selector == "showUpdateInstalledAndRelaunched_acknowledgement_":
            self._set("up_to_date", "Update installed", reply=args[1])
            self.later(5, lambda token=self.token: self._ack_visible(token, args[1]))
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
        self.model["actions"] = []
        if action == "retry":
            self.retry_requested = True
        if action == "cancel" and self.model.get("state") == "ready":
            # Ready's Skip cancels installation; Dismiss may install on quit.
            # Close now so a focus event cannot revive an answered prompt while
            # Sparkle asynchronously tears down its installation driver.
            self.closed = True
            self._clear_card(self.token)
        reply(*values)

    def page_closed(self, token=None):
        """Explicit close/reload cancels a card-owned download, even after fallback."""
        if token is not None and token != self.token:
            return
        if self.mode not in ("card", "probing") and not self.was_card:
            return
        self.closed = True
        self.events = []
        for reply in self.replies:
            if reply.default is None:
                reply.retire()  # closing a window must never retry a refused quit
            else:
                reply(*reply.default)
        self.actions = {}

    def dismiss(self):
        mode, token = self.mode, self.token
        self.closed = True
        # Sparkle owns teardown. Dropping callbacks matches its standard driver;
        # invoking a stale reply here can abort a later update session.
        replies, self.replies = self.replies, []
        for reply in replies:
            reply.retire()
        if mode == "standard":
            self.standard.dismissUpdateInstallation()
        if mode == "card" or self.was_card:
            self._clear_card(token)
        retry = self.retry_requested
        self.token = self.mode = self.page_id = None
        self.events, self.actions, self.model = [], {}, {}
        self.pending, self.retry_requested, self.release_url = None, False, None
        self.release_notes = []
        self.phase, self.was_card = None, False
        if retry:
            # Sparkle tears down its core driver after this callback returns.
            self.main(lambda: self._retry(20))

    def _retry(self, remaining):
        if self.token is not None:
            return
        if not self.retry() and remaining:
            self.later(.1, lambda: self._retry(remaining - 1))
