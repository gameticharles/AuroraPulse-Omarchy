"""The daemon protocol.

Line-delimited JSON in both directions. This module owns the framing, the
size cap, and the sanitising that every outbound string goes through, so no
other module has to remember to do it.
"""

import json
import sys
import threading

from ..util import textutil

PROTOCOL_VERSION = 1

MAX_LINE = 1 << 20  # 1 MiB

# Stable error reasons. The shell words the message; it never string-matches
# one, so these can be reworded freely without breaking the UI.
REASONS = (
    "network", "resolve", "expired", "unsupported", "empty",
    "rate-limited", "sandbox", "cancelled", "not-found", "denied",
)

SOURCES = ("radio", "tv", "youtube", "music", "local", "podcast")

CAPABILITIES = {
    "sources": list(SOURCES),
    "video": True,
    "pip": True,
    "queue": True,
    "lyrics": True,
    "epg": True,
    "equalizer": True,
    "crossfade": True,
    "downloads": True,
    "levels": True,
}


_EMIT_LOCK = threading.Lock()


def emit(payload):
    """Write one event. ensure_ascii makes the framing impossible to break.

    Events come from the command lanes, the player's reader thread, the
    artwork pool and the sync thread at once, so the write is serialised: two
    threads sharing stdout could otherwise splice one line into another, and
    the panel silently drops a line it cannot parse.
    """
    line = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    if len(line) > MAX_LINE:
        line = json.dumps({"type": "error", "message": "event too large to send"},
                          separators=(",", ":"))
    with _EMIT_LOCK:
        try:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        except (BrokenPipeError, ValueError):
            # The panel went away. Exit quietly rather than tracebacking.
            raise SystemExit(0)


class Writer:
    """Serialises writes so two threads cannot interleave a line."""

    def __init__(self):
        self._lock = threading.Lock()

    def __call__(self, payload):
        with self._lock:
            emit(payload)


def read_lines(stream=None):
    """Yield decoded request objects, dropping anything oversized or invalid.

    Handles both a binary and a text stdin: the shell hands us a pipe, but a
    developer running the daemon from a terminal gets a text stream, and the
    difference should never be a traceback.
    """
    stream = stream or sys.stdin
    binary = True
    while True:
        try:
            raw = stream.readline(MAX_LINE + 1)
        except (ValueError, OSError):
            return
        if not raw:
            return
        if isinstance(raw, str):
            binary = False
            raw = raw.encode("utf-8", "replace")
        if len(raw) > MAX_LINE:
            # Overlong: drain the rest of the line so the next read is aligned.
            while raw and not raw.endswith(b"\n"):
                more = stream.readline(MAX_LINE)
                raw = more.encode("utf-8", "replace") if isinstance(more, str) else more
            continue
        raw = raw.strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(payload, dict):
            yield payload


def text(value, limit=256):
    return textutil.text(value, limit)


def clean_item(raw, fallback_uid=""):
    """Normalise anything a source produced into a MediaItem.

    This is the only door into the shell, so it is the only place that has to
    be paranoid. A source that returns a 4 KB title gets a 512 character one.
    """
    if not isinstance(raw, dict):
        return None

    uid = textutil.text(raw.get("uid") or fallback_uid, 200)
    if not uid or ":" not in uid:
        return None

    source = textutil.text(raw.get("source"), 16)
    if source not in SOURCES:
        return None

    art = raw.get("art") if isinstance(raw.get("art"), dict) else {}
    art_key = textutil.text(raw.get("artKey") or art.get("key"), 200)
    extra = raw.get("extra") if isinstance(raw.get("extra"), dict) else {}

    clean_extra = {}
    for key, value in list(extra.items())[:32]:
        if not isinstance(key, str):
            continue
        scalar = _scalar(value)
        if scalar is not None:
            clean_extra[key[:64]] = scalar

    return {
        "uid": uid,
        "source": source,
        "kind": textutil.text(raw.get("kind") or "track", 16),
        "title": textutil.text(raw.get("title") or "Unknown", 512),
        "artist": textutil.text(raw.get("artist"), 256),
        "album": textutil.text(raw.get("album"), 256),
        "art": {
            "url": textutil.text(art.get("url"), 2048),
            "path": textutil.text(art.get("path"), 1024),
            "key": art_key,
        },
        "duration": textutil.int_in(raw.get("duration"), 0, 86400 * 7),
        "is_live": textutil.bool_of(raw.get("is_live")),
        "url": textutil.text(raw.get("url"), 2048),
        "codec": textutil.text(raw.get("codec"), 32),
        "bitrate": textutil.int_in(raw.get("bitrate"), 0, 400000),
        "extra": clean_extra,
    }


def _scalar(value):
    """Extras reach QML as a QVariant, so only JSON scalars are allowed.

    A nested dict or list is dropped rather than stringified: a source that
    puts a structure in `extra` gets it ignored, not rendered as a Python
    repr in the user's face.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, str):
        return textutil.text(value, 512)
    return None


def error(message, reason="network", ident=None):
    payload = {"type": "error", "message": textutil.text(message, 300),
               "reason": reason if reason in REASONS else "network"}
    if ident is not None:
        payload["id"] = ident
    return payload
