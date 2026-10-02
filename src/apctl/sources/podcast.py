"""Podcasts: search, subscribe, and play episodes from the show's own feed.

Search goes to the iTunes Search API, the public directory most podcast apps
use; it needs no key and answers with each show's RSS address. Episodes come
from that RSS feed directly, so a subscription keeps working whether or not
the directory still lists the show.

Feeds are untrusted XML. They are size-capped and parsed with the standard
library's expat, which refuses entity expansion bombs; nothing from a feed
reaches the panel without going through the text sanitiser.
"""

import email.utils
import hashlib
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET

from ..util import textutil
from .base import SourceError, _fetcher, art_for, cached, fetch, fetch_json, item

SEARCH = "https://itunes.apple.com/search"
TOP = "https://itunes.apple.com/%s/rss/toppodcasts/limit=%d/json"
MAX_FEED_BYTES = 16 << 20
FEED_TTL = 30 * 60

ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
MEDIA = "{http://search.yahoo.com/mrss/}"


def _key(value, length=12):
    return hashlib.sha1(str(value).encode("utf-8", "replace")).hexdigest()[:length]


def show_item(feed, title, author="", art="", genre="", count=0, ident=""):
    """A podcast (a show, not an episode). Opening one lists its episodes."""
    ident = ident or _key(feed)
    return item(
        "podcast", "show-%s" % ident, textutil.text(title, 300) or "Podcast",
        url=feed, kind="podcast", artist=textutil.text(author, 200),
        art=art_for(art, "pod-%s" % ident, _fetcher()),
        extra={"feed": textutil.text(feed, 2048), "genre": textutil.text(genre, 80),
               "episodes": int(count or 0), "show": ident},
    )


def search(query, limit=30):
    query = textutil.text(query, 200)
    if not query:
        raise SourceError("type something to search for", "empty")
    url = SEARCH + "?" + urllib.parse.urlencode({
        "media": "podcast", "entity": "podcast", "term": query,
        "limit": max(1, min(50, int(limit)))})
    data, _age = cached("podcast:search:%s:%d" % (query.lower(), limit),
                        lambda: fetch_json(url, limit=4 << 20, seconds=15),
                        ttl=3600)
    out = []
    for entry in (data or {}).get("results") or []:
        if not isinstance(entry, dict):
            continue
        feed = entry.get("feedUrl") or ""
        if not feed.startswith(("http://", "https://")):
            continue
        out.append(show_item(feed, entry.get("collectionName"), entry.get("artistName"),
                             entry.get("artworkUrl600") or entry.get("artworkUrl100"),
                             entry.get("primaryGenreName"), entry.get("trackCount"),
                             str(entry.get("collectionId") or "")))
    return out


def top(country="us", limit=30):
    """The directory's current top shows, for a podcast tab with no
    subscriptions yet. Feeds are looked up in one batch."""
    country = (textutil.text(country, 4) or "us").lower()
    data, _age = cached("podcast:top:%s" % country,
                        lambda: fetch_json(TOP % (country, limit), limit=4 << 20,
                                           seconds=15), ttl=6 * 3600)
    ids = []
    for entry in ((data or {}).get("feed") or {}).get("entry") or []:
        ident = ((entry.get("id") or {}).get("attributes") or {}).get("im:id")
        if ident:
            ids.append(str(ident))
    if not ids:
        return []
    url = "https://itunes.apple.com/lookup?" + urllib.parse.urlencode(
        {"id": ",".join(ids[:limit]), "entity": "podcast"})
    found, _age = cached("podcast:lookup:%s" % _key(url),
                         lambda: fetch_json(url, limit=4 << 20, seconds=15),
                         ttl=6 * 3600)
    by_id = {str(e.get("collectionId")): e for e in (found or {}).get("results") or []
             if isinstance(e, dict)}
    out = []
    for ident in ids:
        entry = by_id.get(ident)
        if not entry or not str(entry.get("feedUrl", "")).startswith("http"):
            continue
        out.append(show_item(entry["feedUrl"], entry.get("collectionName"),
                             entry.get("artistName"),
                             entry.get("artworkUrl600") or entry.get("artworkUrl100"),
                             entry.get("primaryGenreName"), entry.get("trackCount"),
                             ident))
    return out


def _seconds(value):
    """itunes:duration is "3725", "1:02:05" or "62:05"."""
    text = str(value or "").strip()
    if not text:
        return 0
    if text.isdigit():
        return int(text)
    parts = text.split(":")
    if all(p.isdigit() for p in parts) and len(parts) <= 3:
        total = 0
        for part in parts:
            total = total * 60 + int(part)
        return total
    return 0


def _when(value):
    try:
        return int(email.utils.parsedate_to_datetime(str(value)).timestamp())
    except (TypeError, ValueError, IndexError, OverflowError):
        return 0


def _strip_html(value, limit=400):
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return textutil.text(re.sub(r"\s+", " ", text).strip(), limit)


def parse_feed(body):
    """RSS bytes -> plain data: {"show": {...}, "episodes": [{...}]}.

    Pure and side-effect free, so it can run in the artwork sandbox: the
    daemon hands it the bytes on stdin and reads JSON back. A hostile feed
    then meets an XML parser with no network, no files and a memory cap.
    """
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        return {"error": "that feed is not valid RSS: %s" % exc}
    channel = root.find("channel")
    if channel is None:
        return {"error": "that feed has no channel"}
    image = channel.find(ITUNES + "image")
    show_art = image.get("href") if image is not None else ""
    if not show_art:
        show_art = channel.findtext("image/url") or ""
    show = {
        "title": textutil.text(channel.findtext("title"), 300),
        "author": textutil.text(channel.findtext(ITUNES + "author")
                                or channel.findtext("managingEditor"), 200),
        "description": _strip_html(channel.findtext("description"), 600),
        "art": textutil.text(show_art, 2048),
    }
    episodes = []
    for node in channel.findall("item")[:3000]:
        enclosure = node.find("enclosure")
        media_url = enclosure.get("url") if enclosure is not None else ""
        if not media_url:
            content = node.find(MEDIA + "content")
            media_url = content.get("url") if content is not None else ""
        if not str(media_url).startswith(("http://", "https://")):
            continue
        art = node.find(ITUNES + "image")
        episodes.append({
            "title": textutil.text(node.findtext("title"), 300),
            "url": str(media_url)[:2048],
            "mime": textutil.text((enclosure.get("type") if enclosure is not None else "") or "", 40),
            "guid": textutil.text(node.findtext("guid") or media_url, 400),
            "art": textutil.text((art.get("href") if art is not None else "") or show_art, 2048),
            "duration": _seconds(node.findtext(ITUNES + "duration")),
            "published": _when(node.findtext("pubDate")),
            "description": _strip_html(node.findtext("description")
                                       or node.findtext(ITUNES + "summary")),
        })
    return {"show": show, "episodes": episodes}


def _parse_sandboxed(body):
    import json
    import os
    import subprocess
    from ..util import sandbox
    # A feed is a document from the internet: it is parsed sandboxed or not at
    # all. Parsing it here instead ran the XML parser inside the daemon.
    try:
        cmd, fds = sandbox.sandbox_command("artwork", with_script=True)
    except sandbox.SandboxUnavailable as exc:
        raise SourceError(str(exc), "sandbox") from exc
    try:
        proc = subprocess.run(cmd + ["--", sandbox.SANDBOX_SCRIPT, "parse-feed"],
                              input=body, capture_output=True, timeout=30, pass_fds=fds)
    finally:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass
    try:
        return json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    except ValueError:
        return {"error": "the feed could not be read"}


def feed(url):
    """(show dict, [episode items]) for an RSS feed, newest first."""
    if not str(url).startswith(("http://", "https://")):
        raise SourceError("that is not a feed address", "unsupported")

    def load():
        return _parse_sandboxed(fetch(url, limit=MAX_FEED_BYTES, seconds=30))

    data, _age = cached("podcast:feed:%s" % _key(url, 20), load, ttl=FEED_TTL)
    if data.get("error"):
        raise SourceError(data["error"], "unsupported")
    raw = data.get("show") or {}
    show = dict(raw, feed=url, show=_key(url))
    episodes = []
    for entry in data.get("episodes") or []:
        episode_key = _key(entry.get("guid") or entry.get("url"), 16)
        episodes.append(item(
            "podcast", "ep-%s" % episode_key, entry.get("title") or "Episode",
            url=entry.get("url", ""), kind="episode",
            artist=show.get("author") or show.get("title", ""), album=show.get("title", ""),
            duration=int(entry.get("duration") or 0),
            art=art_for(entry.get("art", ""), "pod-ep-%s" % episode_key, _fetcher()),
            extra={"published": int(entry.get("published") or 0),
                   "description": entry.get("description", ""),
                   "mime": entry.get("mime", ""), "feed": url[:2048],
                   "show": show["show"]},
        ))
    episodes.sort(key=lambda e: e["extra"]["published"], reverse=True)
    return show, episodes
