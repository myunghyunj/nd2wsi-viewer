"""Ordered, bounded delivery of observed laser presses to the owning pane.

The Raw Input callback only offers records to a queue. The pane owns annotations,
numbering and shot counts; this worker never reads or writes an annotation file.
"""
from __future__ import annotations

import copy
import json
import math
import queue
import re
import threading
import time
import uuid
from collections import deque

from . import session_log

QUEUE_SIZE = 64
VIEW_WAIT = 1.0
DELIVERY_WAIT = 5.0


def metadata(value):
    """Bounded optional pane metadata, saved through the usual revision-checked sidecar."""
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) != {"isolation_highwater", "presses"}:
        raise ValueError("Invalid laser annotation metadata")
    highwater, presses = value["isolation_highwater"], value["presses"]
    if type(highwater) is not int or not 0 <= highwater < 2**53:
        raise ValueError("Invalid isolation number")
    if not isinstance(presses, dict) or len(presses) > 256:
        raise ValueError("Invalid laser press history")
    for receiver, press in presses.items():
        if (not isinstance(receiver, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", receiver)
                or type(press) is not int or not 0 < press < 2**53):
            raise ValueError("Invalid laser press history")
    return {"slacs": {"isolation_highwater": highwater, "presses": dict(presses)}}


class LaserDelivery:
    def __init__(self, *, prepare, deliver, confirmed=None):
        self._prepare, self._deliver, self._confirmed = prepare, deliver, confirmed
        self._queue = queue.Queue(maxsize=QUEUE_SIZE)
        self._wake = threading.Event()
        self._closed = threading.Event()
        self._disabled = threading.Event()
        self._pending = None
        self._guard = threading.Lock()
        self._reports = deque()
        self._thread = threading.Thread(target=self._run, name="nd2wsi-laser-delivery", daemon=True)
        self._thread.start()

    @staticmethod
    def skipped(record, reason):
        session_log.event("slacs_laser_box", press=record.get("press"), decision="skipped", reason=reason, status=reason)

    def enqueue(self, record):
        if self._closed.is_set():
            return
        try:
            self._queue.put_nowait(dict(record))
        except queue.Full:
            self.skipped(record, "queue_full")
        self._wake.set()

    def view_changed(self):
        self._wake.set()

    def close(self):
        self._closed.set()
        self._wake.set()
        try:
            self._queue.put_nowait(None)  # wake an idle, blocking queue reader
        except queue.Full:
            pass
        # Never join: a web view may be hung, and a caller may hold a lifecycle lock.

    @property
    def status(self):
        return "delivery_disabled" if self._disabled.is_set() else "ready"

    def _timeout(self, finished):
        # Keep only one browser call alive. Observe/log subsequent presses while
        # it is stalled, then rearm after it returns; never replay skipped shots.
        self._disabled.set()
        with self._guard:
            self._pending = None
        while True:
            while True:
                try:
                    record = self._queue.get_nowait()
                    if record is not None:
                        self.skipped(record, "delivery_disabled")
                except queue.Empty:
                    break
            if self._closed.is_set() or finished.is_set():
                break
            self._wake.clear()
            self._wake.wait(.1)
        self._disabled.clear()

    def accept(self, payload):
        """Only the current delivery can confirm an annotation; bounded untrusted input."""
        if (not isinstance(payload, dict) or set(payload) - {
                "sid", "token", "press", "decision", "reason", "id", "target_px", "box", "distance_px", "item"}):
            return {"ok": False}
        # This bridge accepts at most one acknowledgment per pending press, and
        # bounds even rejected calls before walking a potentially large payload.
        with self._guard:
            now = time.monotonic()
            while self._reports and self._reports[0] <= now - 1:
                self._reports.popleft()
            if len(self._reports) >= 128:
                return {"ok": False}
            self._reports.append(now)
        def numeric(v):
            return type(v) in (int, float) and math.isfinite(v) and abs(v) <= 1e9

        point = payload.get("target_px")
        box = payload.get("box")
        distance = payload.get("distance_px")
        identifier = payload.get("id")
        if (point is not None and (not isinstance(point, list) or len(point) != 2 or not all(map(numeric, point)))
                or box is not None and (not isinstance(box, dict) or set(box) != {"x", "y", "w", "h"}
                                        or not all(map(numeric, box.values())))
                or distance is not None and (not numeric(distance) or distance < 0)
                or identifier is not None and (not isinstance(identifier, str) or len(identifier) > 240)):
            return {"ok": False}
        try:
            raw = json.dumps(payload, allow_nan=False)
            if len(raw) > 65536:
                return {"ok": False}
        except (ValueError, TypeError, RecursionError):
            return {"ok": False}
        with self._guard:
            pending = self._pending
            if (self._closed.is_set() or pending is None or pending.get("reply") is not None
                    or payload.get("token") != pending["token"] or payload.get("press") != pending["press"]
                    or payload.get("decision") not in ("added", "updated", "skipped")):
                return {"ok": False}
            reason = payload.get("reason", "")
            if not isinstance(reason, str) or len(reason) > 80:
                return {"ok": False}
            item = payload.get("item")
            if payload["decision"] in ("added", "updated"):
                if (not isinstance(item, dict) or not isinstance(item.get("slacs"), dict)
                        or item["slacs"].get("kind") != "laser_shot"
                        or item.get("id") != identifier or item.get("type") not in ("box", "geojson")):
                    return {"ok": False}
            if item is not None and payload["decision"] == "skipped":
                return {"ok": False}
            pending["reply"] = copy.deepcopy(payload)
        self._wake.set()
        return {"ok": True}

    def _run(self):
        while not self._closed.is_set():
            record = self._queue.get()
            if record is None or self._closed.is_set():
                return
            deadline = time.monotonic() + VIEW_WAIT
            payload, reason = None, "stale_view"
            while not self._closed.is_set():
                self._wake.clear()
                try:
                    payload, reason = self._prepare(record)
                except Exception:  # noqa: BLE001 - fail closed if the owner has gone away
                    payload, reason = None, "pane_unavailable"
                if payload is not None or reason != "stale_view" or time.monotonic() >= deadline:
                    break
                self._wake.wait(max(0, deadline - time.monotonic()))
            if self._closed.is_set():
                return
            if payload is None:
                self.skipped(record, reason)
                continue
            token = uuid.uuid4().hex
            payload = {**payload, "token": token}
            with self._guard:
                self._pending = {"token": token, "press": record["press"], "reply": None}
            finished, result = threading.Event(), []

            def send(value=payload):
                try:
                    result.append(self._deliver(value))
                except Exception:  # noqa: BLE001 - an unavailable page must not stop the app
                    result.append(False)
                finally:
                    finished.set()
                    self._wake.set()

            threading.Thread(target=send, name="nd2wsi-laser-page", daemon=True).start()
            deadline = time.monotonic() + DELIVERY_WAIT
            reply = None
            while not self._closed.is_set():
                self._wake.clear()
                with self._guard:
                    reply = self._pending["reply"]
                if finished.is_set() and result and result[0] is not True:
                    self.skipped(record, "pane_unavailable")
                    break
                if reply is not None and finished.is_set():
                    break
                if time.monotonic() >= deadline:
                    self.skipped(record, "delivery_timeout")
                    self._timeout(finished)
                    reply = None  # late acknowledgments belong to the retired token
                    break
                self._wake.wait(min(.1, max(0, deadline - time.monotonic())))
            with self._guard:
                self._pending = None
            if reply is not None and not self._closed.is_set():
                allowed = ("press", "decision", "reason", "id", "target_px", "box", "distance_px")
                session_log.event("slacs_laser_box", **{k: reply.get(k) for k in allowed})
                if reply.get("item") is not None and self._confirmed is not None:
                    # The same worker relays only acknowledged items, and waits for it
                    # before the next press. Stalls suspend delivery until return.
                    done = threading.Event()

                    def confirm(value=reply):
                        try:
                            self._confirmed(value)
                        except Exception:  # noqa: BLE001 - the overlay may have closed
                            pass
                        finally:
                            done.set()
                            self._wake.set()

                    threading.Thread(target=confirm, name="nd2wsi-laser-confirm", daemon=True).start()
                    if not done.wait(DELIVERY_WAIT):
                        self._timeout(done)
