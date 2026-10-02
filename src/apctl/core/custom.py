"""Sources the user added: their own stations, channels and TV playlists.

The mirror is replaced wholesale on every full sync, so nothing the user adds
can live only in it - it would vanish the next time the directory is
re-downloaded. This file is the record of what the user added; the syncer
merges it back into the mirror after every replace, and every change here is
also written straight into the mirror so it shows up without waiting.

Ids are prefixed `custom-`, which is how the "My stations" filter finds them
and how a re-merge replaces the old copies instead of duplicating them.
"""

import csv
import hashlib
import io
import json
import os
import re
import threading
import time

from ..util import netguard, taxonomy, textutil
from .state import read_json, write_json

MAX_ITEMS = 20000
MAX_PLAYLISTS = 64
MAX_IMPORT_BYTES = 32 << 20


def _hash(value, length=10):
    return hashlib.sha1(value.encode("utf-8", "replace")).hexdigest()[:length]


def _http(url):
    return isinstance(url, str) and url.startswith(("http://", "https://"))


class Custom:
    """The user's own additions, persisted as one JSON file."""

    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "custom.json")
        self.playlist_dir = os.path.join(data_dir, "playlists")
        self._lock = threading.RLock()
        self._data = None

    # -- persistence -------------------------------------------------------

    def data(self):
        with self._lock:
            if self._data is None:
                raw = read_json(self.path, {}) or {}
                if not isinstance(raw, dict):
                    raw = {}
                self._data = {
                    "radio": [s for s in raw.get("radio") or []
                              if isinstance(s, dict) and s.get("url")],
                    "tv": [c for c in raw.get("tv") or []
                           if isinstance(c, dict) and c.get("url")],
                    "playlists": [p for p in raw.get("playlists") or []
                                  if isinstance(p, dict) and p.get("id")],
                    "podcasts": [p for p in raw.get("podcasts") or []
                                 if isinstance(p, dict) and p.get("feed")],
                }
            return self._data

    def save(self):
        with self._lock:
            try:
                write_json(self.path, self.data())
            except OSError:
                pass

    def summary(self):
        """What the settings page lists. Rows are trimmed to what it shows."""
        data = self.data()
        return {
            "radio": [{"id": s["id"], "name": s.get("name", ""),
                       "url": s.get("url", ""), "country": s.get("country", ""),
                       "tags": s.get("tags", "")} for s in data["radio"][:500]],
            "radioCount": len(data["radio"]),
            "tv": [{"id": c["id"], "name": c.get("name", ""),
                    "url": c.get("url", ""), "country": c.get("country", ""),
                    "category": c.get("category", "")} for c in data["tv"][:500]],
            "tvCount": len(data["tv"]),
            "playlists": [dict(p) for p in data["playlists"]],
            "podcasts": [dict(p) for p in data["podcasts"]],
        }

    # -- radio -------------------------------------------------------------

    def add_station(self, name, url, country="", tags="", favicon="",
                    homepage="", bitrate=0, codec="", save=True):
        url = (url or "").strip()
        if not _http(url):
            raise ValueError("a stream address has to start with http:// or https://")
        name = textutil.text(name, 200) or netguard.url_host(url) or "My station"
        station = {
            "id": "custom-%s" % _hash(url),
            "name": name,
            "url": textutil.text(url, 2048),
            "country": textutil.text(country, 80),
            "tags": textutil.text(tags, 300),
            "favicon": textutil.text(favicon, 2048) if _http(favicon) else "",
            "homepage": textutil.text(homepage, 500) if _http(homepage) else "",
            "bitrate": textutil.int_in(bitrate, 0, 2000, 0),
            "codec": textutil.text(codec, 16).upper(),
            "added": int(time.time()),
        }
        with self._lock:
            radio = self.data()["radio"]
            radio[:] = [s for s in radio if s.get("id") != station["id"]]
            radio.append(station)
            del radio[:-MAX_ITEMS]
            if save:
                self.save()
        return station

    def remove_station(self, ident):
        with self._lock:
            radio = self.data()["radio"]
            before = len(radio)
            radio[:] = [s for s in radio if s.get("id") != ident]
            self.save()
            return before != len(radio)

    def clear_stations(self):
        with self._lock:
            self.data()["radio"] = []
            self.save()

    def radio_rows(self, now=None):
        """Stations in the mirror's row shape, plus their tag rows."""
        from .sync import _radio_row, _tag_rows
        now = now or time.time()
        rows = []
        for s in self.data()["radio"]:
            country = s.get("country", "")
            code = taxonomy.country_code(country)
            entry = {
                "stationuuid": s["id"], "name": s.get("name"),
                "url_resolved": s.get("url"), "url": s.get("url"),
                "homepage": s.get("homepage", ""), "favicon": s.get("favicon", ""),
                "codec": s.get("codec", ""), "bitrate": s.get("bitrate", 0),
                "country": taxonomy.country_label(code) if len(code) == 2 else country,
                "countrycode": code if len(code) == 2 else "",
                "tags": s.get("tags", ""), "votes": 0, "clickcount": 0,
                # A station the user added is one they expect to see; the
                # directory's health flag does not apply to it.
                "lastcheckok": 1,
            }
            rows.append(_radio_row(entry, now))
        return rows, _tag_rows(rows)

    # -- podcasts ----------------------------------------------------------

    def add_podcast(self, feed, title="", author="", art=""):
        feed = (feed or "").strip()
        if not _http(feed):
            raise ValueError("a podcast feed address starts with http:// or https://")
        with self._lock:
            podcasts = self.data()["podcasts"]
            podcasts[:] = [p for p in podcasts if p.get("feed") != feed]
            podcasts.insert(0, {"feed": textutil.text(feed, 2048),
                                "title": textutil.text(title, 300) or netguard.url_host(feed),
                                "author": textutil.text(author, 200),
                                "art": textutil.text(art, 2048) if _http(art) else "",
                                "added": int(time.time())})
            self.save()

    def remove_podcast(self, feed):
        with self._lock:
            podcasts = self.data()["podcasts"]
            before = len(podcasts)
            podcasts[:] = [p for p in podcasts if p.get("feed") != feed]
            self.save()
            return before != len(podcasts)

    def is_subscribed(self, feed):
        return any(p.get("feed") == feed for p in self.data()["podcasts"])

    # -- tv ----------------------------------------------------------------

    def add_channel(self, name, url, country="", category="", logo="",
                    save=True, playlist=None):
        url = (url or "").strip()
        if not _http(url):
            raise ValueError("a stream address has to start with http:// or https://")
        channel = {
            "id": "custom-%s" % _hash(url),
            "name": textutil.text(name, 200) or netguard.url_host(url) or "My channel",
            "url": textutil.text(url, 2048),
            "country": textutil.text(country, 80),
            "category": textutil.text(category, 80),
            "logo": textutil.text(logo, 2048) if _http(logo) else "",
            "added": int(time.time()),
        }
        with self._lock:
            tv = self.data()["tv"]
            tv[:] = [c for c in tv if c.get("id") != channel["id"]]
            tv.append(channel)
            del tv[:-MAX_ITEMS]
            if save:
                self.save()
        return channel

    def remove_channel(self, ident):
        with self._lock:
            tv = self.data()["tv"]
            before = len(tv)
            tv[:] = [c for c in tv if c.get("id") != ident]
            self.save()
            return before != len(tv)

    def channel_rows(self, now=None):
        """Channels added by hand and from enabled playlists, as mirror rows."""
        now = int(now or time.time())
        rows, tags = [], []
        seen = set()

        def add(ident, entry):
            if ident in seen:
                return
            seen.add(ident)
            code = taxonomy.country_code(entry.get("country", ""))
            category = entry.get("category", "") or "Custom"
            rows.append((ident[:64], textutil.text(entry.get("name"), 300) or "Channel",
                         entry.get("url", "")[:2048], entry.get("logo", "")[:2048],
                         category[:120], code[:8], "", 0, 0, now))
            tags.append((ident[:64], category))

        for channel in self.data()["tv"]:
            add(channel["id"], channel)
        for playlist in self.data()["playlists"]:
            if not playlist.get("enabled", True):
                continue
            for entry in self.playlist_entries(playlist["id"]):
                add("custom-pl%s-%s" % (playlist["id"], _hash(entry.get("url", ""), 8)),
                    entry)
        return rows, tags

    # -- playlists ---------------------------------------------------------

    def add_playlist(self, url="", text="", name=""):
        """A TV playlist the user subscribes to: a URL, or pasted M3U text.

        Pasted text is saved as a file of its own, so it survives restarts and
        can be refreshed like any other playlist.
        """
        url = (url or "").strip()
        if text and not url:
            os.makedirs(self.playlist_dir, mode=0o700, exist_ok=True)
            ident = _hash(text, 8)
            path = os.path.join(self.playlist_dir, "pasted-%s.m3u" % ident)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text[:MAX_IMPORT_BYTES])
            url = path
        if not url:
            raise ValueError("give a playlist address or paste its contents")
        if not _http(url) and not os.path.isfile(os.path.expanduser(url)):
            raise ValueError("that is neither a web address nor a file")
        ident = _hash(url, 8)
        with self._lock:
            playlists = self.data()["playlists"]
            if len(playlists) >= MAX_PLAYLISTS:
                raise ValueError("that is as many playlists as can be kept")
            if any(p["id"] == ident for p in playlists):
                raise ValueError("that playlist is already in the list")
            playlists.append({
                "id": ident, "url": url,
                "name": textutil.text(name, 120) or _playlist_name(url),
                "enabled": True, "count": 0, "error": "", "updated": 0,
            })
            self.save()
        return ident

    def remove_playlist(self, ident):
        with self._lock:
            playlists = self.data()["playlists"]
            gone = [p for p in playlists if p["id"] == ident]
            playlists[:] = [p for p in playlists if p["id"] != ident]
            self.save()
        for p in gone:
            for path in (self._cache_path(ident),
                         p.get("url") if str(p.get("url", "")).startswith(
                             self.playlist_dir) else None):
                if path:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
        return bool(gone)

    def set_playlist_enabled(self, ident, enabled):
        with self._lock:
            for p in self.data()["playlists"]:
                if p["id"] == ident:
                    p["enabled"] = bool(enabled)
            self.save()

    def _cache_path(self, ident):
        return os.path.join(self.playlist_dir, "%s.json" % ident)

    def playlist_entries(self, ident):
        data = read_json(self._cache_path(ident), [])
        return data if isinstance(data, list) else []

    def refresh_playlist(self, ident):
        """Download (or re-read) one playlist and cache its channels."""
        with self._lock:
            playlist = next((p for p in self.data()["playlists"]
                             if p["id"] == ident), None)
        if playlist is None:
            return 0
        try:
            body = read_source(playlist["url"])
            entries = parse_m3u_entries(body)
            if not entries:
                raise ValueError("no channels in it")
            os.makedirs(self.playlist_dir, mode=0o700, exist_ok=True)
            write_json(self._cache_path(ident), entries[:MAX_ITEMS])
            error = ""
        except Exception as exc:
            entries = self.playlist_entries(ident)
            error = textutil.text(str(exc), 160)
        with self._lock:
            for p in self.data()["playlists"]:
                if p["id"] == ident:
                    p["count"] = len(entries)
                    p["error"] = error
                    p["updated"] = int(time.time())
            self.save()
        if error and not entries:
            raise ValueError(error)
        return len(entries)

    def refresh_all(self):
        for playlist in list(self.data()["playlists"]):
            if playlist.get("enabled", True):
                try:
                    self.refresh_playlist(playlist["id"])
                except Exception:
                    pass


def _playlist_name(url):
    base = os.path.basename(url.split("?", 1)[0]) or netguard.url_host(url) or "Playlist"
    return textutil.text(re.sub(r"\.m3u8?$", "", base, flags=re.I), 120)


def read_source(source):
    """Text from a URL or a local file path, size-capped."""
    source = (source or "").strip()
    if _http(source):
        return netguard.fetch(source, limit=MAX_IMPORT_BYTES, seconds=60,
                              allowed=netguard.WEB_PORTS).decode("utf-8", "replace")
    path = os.path.expanduser(source)
    if not os.path.isfile(path):
        raise ValueError("there is no file at %s" % source)
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read(MAX_IMPORT_BYTES)


_ATTR = re.compile(r'([\w-]+)="([^"]*)"')


def parse_m3u_entries(body):
    """An M3U or M3U8 playlist as plain dicts: name, url, logo, category,
    country. Unlike base.parse_m3u this keeps the attributes a settings import
    cares about and makes no MediaItems."""
    out = []
    current = {}
    seen = set()
    for raw in (body or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.upper().startswith("#EXTINF"):
            attrs = dict(_ATTR.findall(line))
            title = line.rsplit(",", 1)[1].strip() if "," in line else ""
            current = {
                "name": title or attrs.get("tvg-name", ""),
                "logo": attrs.get("tvg-logo", ""),
                "category": (attrs.get("group-title") or "").split(";")[0],
                "country": attrs.get("tvg-country", "").split(";")[0],
            }
        elif line.startswith("#EXTGRP:"):
            current["category"] = line[8:].strip()
        elif line.startswith("#"):
            continue
        elif _http(line):
            if line not in seen:
                seen.add(line)
                entry = dict(current)
                entry["url"] = line
                entry.setdefault("name", "")
                out.append(entry)
            current = {}
    return out


def parse_import(text):
    """Stations from pasted or downloaded text in any of the formats the
    original app exported (JSON, CSV) or that stations are shared in (M3U, PLS).

    Returns a list of dicts with name, url, country, tags, favicon.
    """
    text = (text or "").strip()
    if not text:
        return []
    if text[:1] in "[{":
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("stations") or data.get("items") or [data]
        out = []
        for entry in data if isinstance(data, list) else []:
            if not isinstance(entry, dict):
                continue
            urls = entry.get("streamUrls") or []
            url = (entry.get("url_resolved") or entry.get("url")
                   or (urls[0] if isinstance(urls, list) and urls else ""))
            out.append({
                "name": entry.get("name") or entry.get("title") or "",
                "url": url,
                "country": entry.get("country") or entry.get("countrycode") or "",
                "tags": entry.get("tags") or entry.get("genre") or "",
                "favicon": entry.get("favicon") or entry.get("imageUrl")
                or entry.get("logo") or "",
                "homepage": entry.get("homepage") or entry.get("websiteUrl") or "",
                "bitrate": entry.get("bitrate") or 0,
            })
        return out
    if "#EXTINF" in text.upper() or text.upper().startswith("#EXTM3U"):
        return [{"name": e.get("name", ""), "url": e["url"],
                 "country": e.get("country", ""), "tags": e.get("category", ""),
                 "favicon": e.get("logo", "")} for e in parse_m3u_entries(text)]
    if text.lower().startswith("[playlist]"):
        files, titles = {}, {}
        for line in text.splitlines():
            key, _, value = line.partition("=")
            key = key.strip().lower()
            if key.startswith("file"):
                files[key[4:]] = value.strip()
            elif key.startswith("title"):
                titles[key[5:]] = value.strip()
        return [{"name": titles.get(k, ""), "url": v} for k, v in files.items()]
    first = text.splitlines()[0].lower()
    if "url" in first and "," in first:
        reader = csv.DictReader(io.StringIO(text))
        return [{"name": row.get("name", ""), "url": row.get("url", ""),
                 "country": row.get("country", ""),
                 "tags": row.get("tags", "") or row.get("genre", ""),
                 "favicon": row.get("favicon", ""),
                 "homepage": row.get("homepage", ""),
                 "bitrate": row.get("bitrate", 0)} for row in reader]
    # A bare list of stream addresses, one per line.
    return [{"name": "", "url": line.strip()} for line in text.splitlines()
            if _http(line.strip())]


def export_rows(rows, fmt):
    """Mirror rows (sqlite3.Row from the radio table) as JSON, M3U or CSV."""
    if fmt == "m3u":
        out = ["#EXTM3U"]
        for r in rows:
            logo = (' tvg-logo="%s"' % r["favicon"]) if r["favicon"] else ""
            group = (r["tags"] or "").split(",")[0].strip()
            out.append('#EXTINF:-1 tvg-country="%s" group-title="%s"%s,%s'
                       % (r["countrycode"] or "", group.replace('"', ""), logo,
                          (r["name"] or "").replace("\n", " ")))
            out.append(r["url_resolved"] or r["url"])
        return "\n".join(out) + "\n"
    if fmt == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["id", "name", "url", "country", "tags", "favicon",
                         "homepage", "votes", "bitrate", "is_custom"])
        for r in rows:
            writer.writerow([r["uuid"], r["name"], r["url_resolved"] or r["url"],
                             r["country"], r["tags"], r["favicon"], r["homepage"],
                             r["votes"], r["bitrate"],
                             str(r["uuid"]).startswith("custom-")])
        return buffer.getvalue()
    return json.dumps([{
        "id": r["uuid"], "name": r["name"], "url": r["url_resolved"] or r["url"],
        "country": r["country"], "countrycode": r["countrycode"],
        "tags": r["tags"], "favicon": r["favicon"], "homepage": r["homepage"],
        "bitrate": r["bitrate"], "codec": r["codec"], "votes": r["votes"],
    } for r in rows], ensure_ascii=False, indent=1)


def downloads_dir():
    """Where an export is written: the XDG download directory, or ~."""
    try:
        import subprocess
        out = subprocess.run(["xdg-user-dir", "DOWNLOAD"], capture_output=True,
                             text=True, timeout=3).stdout.strip()
        if out and os.path.isdir(out):
            return out
    except (OSError, ValueError):
        pass
    fallback = os.path.expanduser("~/Downloads")
    return fallback if os.path.isdir(fallback) else os.path.expanduser("~")
