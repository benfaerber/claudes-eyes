"""The memory layer: observations, live activity, and the audit trail.
All in-memory by design; a restart clears everything (privacy posture)."""

import json
import threading
import time


class ObservationLog:
    """What Claude said it saw, newest first, capped."""

    MAX_ENTRIES = 200
    MAX_TEXT_BYTES = 2000

    def __init__(self) -> None:
        self.entries: list[dict] = []
        self.lock = threading.Lock()

    def add(self, text: str) -> None:
        entry = {"text": text[: self.MAX_TEXT_BYTES], "at": time.time()}
        with self.lock:
            self.entries.insert(0, entry)
            del self.entries[self.MAX_ENTRIES:]

    def as_dict(self) -> dict:
        with self.lock:
            return {"observations": list(self.entries)}


class ClaudeActivity:
    """What Claude is doing with the Eyes right now, shown live on the dash.
    Set automatically when a non-browser client downloads a frame, refined by
    POST /activity, cleared when the observation lands."""

    def __init__(self) -> None:
        self.text: str | None = None
        self.at: float = 0.0
        self.lock = threading.Lock()

    def set(self, text: str) -> None:
        with self.lock:
            self.text = text
            self.at = time.time()

    def clear(self) -> None:
        with self.lock:
            self.text = None

    def snapshot(self) -> dict | None:
        with self.lock:
            if self.text is None:
                return None
            return {"text": self.text, "at": self.at}


class EventLog:
    """Unified audit trail of everything that happens to the Eyes: looks,
    gimbal moves, observations, pauses. Newest first, in memory only, same
    privacy posture as the observation log (a restart clears it). Repeated
    frame fetches coalesce into one counted entry so a burst of looks
    doesn't drown the log."""

    MAX_ENTRIES = 500
    MAX_TEXT_BYTES = 2000
    COALESCE_SECONDS = 45

    def __init__(self) -> None:
        self.entries: list[dict] = []
        self.lock = threading.Lock()

    def add(self, kind: str, text: str, coalesce: bool = False) -> None:
        now = time.time()
        with self.lock:
            if coalesce and self.entries:
                newest = self.entries[0]
                if newest["kind"] == kind and now - newest["at"] < self.COALESCE_SECONDS:
                    newest["count"] += 1
                    newest["at"] = now
                    return
            self.entries.insert(0, {"kind": kind, "text": text[: self.MAX_TEXT_BYTES], "at": now, "count": 1})
            del self.entries[self.MAX_ENTRIES:]

    def as_dict(self) -> dict:
        with self.lock:
            return {"events": [dict(entry) for entry in self.entries]}
