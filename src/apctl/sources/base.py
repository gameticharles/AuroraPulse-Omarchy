"""Shared plumbing for the content sources.

Every source is synchronous and pure-ish: it takes plain arguments and
returns plain dictionaries. The daemon owns all threading, caching policy and
protocol events, so a source never has to know it is running next to a UI.
"""

import json
import os
import re
import threading
import time

from ..util import netguard, textutil

# One shared lock so a burst of requests cannot stampede a rate-limited API.
_ART_FETCHER = [None]


def set_art_fetcher(fetcher):
    _ART_FETCHER[0] = fetcher


def _fetcher():
    return _ART_FETCHER[0]


NET_LOCK = threading.Lock()
NET_MIN_INTERVAL = 0.25
_last_net = 0.0

CACHE_TTL = 15 * 60

_bcache = {}
_bcache_lock = threading.Lock()

USER_AGENT = "AuroraPulse/0.1 (Omarchy)"

_SAFE_SCHEMES = ("https://", "http://")


class SourceError(Exception):
    """Carries a protocol reason so the shell can word it."""

    def __init__(self, message, reason="network"):
        super().__init__(message)
        self.reason = reason


def throttle():
    global _last_net
    with NET_LOCK:
        wait = NET_MIN_INTERVAL - (time.monotonic() - _last_net)
        if wait > 0:
            time.sleep(wait)
        _last_net = time.monotonic()


def fetch(url, limit=4 << 20, seconds=20, allowed=None):
    """A guarded GET. Raises SourceError on anything the guard refuses."""
    if not isinstance(url, str) or not url.startswith(_SAFE_SCHEMES):
        raise SourceError("only http and https URLs are fetched", "denied")
    throttle()
    try:
        return netguard.fetch(url, limit=limit, seconds=seconds, allowed=allowed)
    except netguard.Blocked as exc:
        raise SourceError(str(exc), "denied") from exc
    except TimeoutError as exc:
        raise SourceError("the station did not answer in time", "expired") from exc
    except OSError as exc:
        raise SourceError("network error: %s" % exc, "network") from exc


def fetch_text(url, limit=4 << 20, seconds=20, allowed=None, encoding="utf-8"):
    raw = fetch(url, limit=limit, seconds=seconds, allowed=allowed)
    return raw.decode(encoding, "replace")


def fetch_json(url, limit=4 << 20, seconds=20, allowed=None):
    body = fetch_text(url, limit=limit, seconds=seconds, allowed=allowed)
    try:
        return json.loads(body)
    except ValueError as exc:
        raise SourceError("the server sent something that is not JSON", "network") from exc


def cached(key, producer, ttl=CACHE_TTL):
    """A tiny in-process TTL cache. Returns (value, age_seconds)."""
    now = time.monotonic()
    with _bcache_lock:
        entry = _bcache.get(key)
        if entry and now - entry[0] < ttl:
            return entry[1], now - entry[0]
    value = producer()
    with _bcache_lock:
        _bcache[key] = (now, value)
    return value, 0.0


def cache_age(key):
    with _bcache_lock:
        entry = _bcache.get(key)
    return None if not entry else time.monotonic() - entry[0]


# -- URLs -----------------------------------------------------------------

_TRACKING = re.compile(r"^(utm_|fbclid|gclid|_ga|si$)", re.IGNORECASE)


def clean_url(url, limit=2048):
    """Strip tracking noise. Never changes the host, path or query semantics
    that a stream actually needs."""
    if not isinstance(url, str) or len(url) > limit * 2:
        return ""
    if not url.startswith(_SAFE_SCHEMES):
        return ""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if not parts.hostname:
        return ""
    keep = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not _TRACKING.match(k)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(keep), ""))[:limit]


def bitrate_kbps(url_or_text):
    """Pull a bitrate out of a codec string or a stream URL's path."""
    text = url_or_text or ""
    match = re.search(r"(\d{2,4})\s*k", text, re.IGNORECASE)
    if match:
        try:
            value = int(match.group(1))
            if 8 <= value <= 320:
                return value
        except ValueError:
            pass
    return 0


def item(source, uid, title, url="", **rest):
    """Build a MediaItem. Sources use this so the shape is always identical."""
    out = {
        "uid": "%s:%s" % (source, textutil.slug(uid, 200)),
        "source": source,
        "kind": rest.pop("kind", "track"),
        "title": title,
        "url": url,
    }
    for key in ("artist", "album", "codec", "bitrate", "duration",
                "is_live", "extra", "art", "artKey"):
        if key in rest:
            out[key] = rest[key]
    return out


def art(path_or_url, cache_dir=None, key=None):
    """A reference to artwork. The shell only ever loads a local path, so a
    remote image is downloaded and sanitised by the caller, never handed to
    the compositor as a URL."""
    path = path_or_url or ""
    if not path:
        return {"url": "", "path": ""}
    if path.startswith("/"):
        try:
            if os.path.getsize(path) > 8 << 20:
                return {"url": "", "path": ""}
        except OSError:
            return {"url": "", "path": ""}
        return {"url": "", "path": path}
    if cache_dir and key:
        target = os.path.join(cache_dir, "%s.img" % textutil.slug(key, 120))
        return {"url": "", "path": target if os.path.exists(target) else ""}
    return {"url": path[:2048], "path": ""}


def art_for(raw, key, fetcher=None):
    """Artwork for an item, without ever blocking the list.

    Returns whatever is already on disk and hands the download to the
    background fetcher otherwise. The `key` is what identifies the image
    everywhere else, so the shell can be told later when it lands.

    Doing this inline cost 32 seconds for fifty stations and produced no
    pictures, because one dead image host cost the whole page.
    """
    if not key or not raw:
        return {"url": "", "path": ""}
    if not (raw.startswith("http://") or raw.startswith("https://")):
        return {"url": "", "path": ""}
    slug = textutil.slug(key, 120)
    if fetcher is None:
        return {"url": "", "path": ""}
    path = fetcher.submit(key, raw, slug)
    return {"url": "", "path": path or "", "key": key}


def parse_m3u(body, source, id_prefix="", group_by="", limit=5000):
    """Parse a plain or extended M3U into MediaItems.

    Extended entries are the point: a #EXTINF carries the title, the group
    and often the logo, which is most of what a directory can offer.
    """
    items = []
    current = {}
    seen = set()
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            current = {"attrs": line[8:]}
            payload = line[8:].rsplit(",", 1)
            current["title"] = payload[-1].strip() if len(payload) > 1 else ""
            current["attrs_text"] = payload[0]
            # group-title lives in the attributes, not in an #EXTGRP line.
            # Extended playlists almost never emit #EXTGRP, so reading only
            # that left the group empty for every channel in iptv-org's
            # catalogue - which is why group_title had to come from the API
            # instead, and why the channels that did not match an API row had
            # no category at all.
            group = re.search(r'group-title="([^"]*)"', current["attrs"])
            if group:
                current["group"] = group.group(1)
        elif line.startswith("#EXTGRP:"):
            if current:
                current["group"] = line[8:].strip()
        elif line.startswith("#"):
            continue
        else:
            url = clean_url(line)
            if not url or url in seen or len(items) >= limit:
                continue
            seen.add(url)
            title = textutil.text(current.get("title") or netguard.url_host(url),
                                  300)
            if group_by and current.get("group"):
                title = "%s — %s" % (current["group"], title)
            items.append(item(
                source,
                "%s%s%d" % (id_prefix, textutil.slug(title, 60) or "s", len(items)),
                title,
                url=url,
                kind="channel",
                is_live=True,
                extra={"group": textutil.text(current.get("group"), 120),
                       "attrs": textutil.text(current.get("attrs_text"), 200)},
                art=art(_extv(current), None, None),
            ))
            current = {}
    return items


def _extv(current):
    """Pull an attribute out of an #EXTINF line.

    The attributes are space-separated key="value" pairs, not the
    comma-separated list the trailing title is. Getting this wrong silently
    loses every logo in every playlist, so the value is extracted with a
    regular expression rather than by splitting.
    """
    for key in ("tvg-logo", "logo", "tvg-logo-small"):
        match = re.search(r'%s="([^"]*)"' % re.escape(key),
                          current.get("attrs") or "")
        if match and match.group(1).startswith(_SAFE_SCHEMES):
            return clean_url(match.group(1))
    return ""
