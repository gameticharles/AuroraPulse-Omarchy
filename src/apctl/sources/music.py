"""YouTube Music: search, the library, and the lyrics that come with it.

YouTube Music is YouTube with a different front end, so a music search
returns a browse page rather than a playlist of video ids. yt-dlp flattens
it into entries for us; the work here is filtering out the things that are
not songs (music videos, podcasts, uploaded lectures) and pulling the track
metadata Music actually publishes.
"""

import re
import time

from ..core import resolver
from ..util import netguard, textutil
from .base import SourceError, _fetcher, art_for, item
from .youtube import Budget, WATCH, to_item

MUSIC = "https://music.youtube.com"

# What Music calls a song, versus a video, in a browse result.
_NOISE = re.compile(
    r"\b(official video|official audio|official lyric|lyric video|"
    r"music video|visualizer|visualiser|mv|pt\.?\s*\d*|hd|hq|4k|remastered"
    r"\s*\d*|full album|full song|concert|performance|cover)\b",
    re.IGNORECASE)


def looks_like_song(entry):
    """Keep songs, drop the rest.

    Deliberately conservative about the title (a song called "Video" is
    common) and strict about the channel and duration, which is where the
    noise actually is.
    """
    duration = int(entry.get("duration") or 0)
    if duration and duration < 45:
        return False           # too short to be a track
    if duration and duration > 45 * 60:
        return False           # a set, not a song
    channel = (entry.get("channel") or entry.get("uploader") or "").lower()
    if re.search(r"vevo|\blive\b|\btv\b", channel):
        return False
    title = entry.get("title") or ""
    if "official video" in title.lower() or "visualizer" in title.lower():
        return False
    return True


def _parse_artists(raw):
    return [textutil.text(a, 120) for a in (raw or []) if a][:4]


def to_track(entry, cache_dir=None, index=0, prefix="ym"):
    """A YouTube Music entry into a MediaItem, with artist and album filled in."""
    built = to_item(entry, cache_dir, index, prefix, source="music")
    if built is None:
        return None
    extra = built.setdefault("extra", {})

    artist = built.get("artist") or ""
    album = built.get("album") or ""

    # Music entries carry structured fields; the title is "Artist - Title"
    # when they are missing.
    parts = str(extra.get("artists") or "").split(", ")
    if not artist and len(parts) > 1:
        artist = parts[0]
    title = built["title"]
    if not artist and " - " in title and not _NOISE.search(title):
        head, _, tail = title.partition(" - ")
        if len(head) <= 60 and len(tail) <= 120:
            artist, title = head, tail
    built["artist"] = textutil.text(artist, 200)
    built["title"] = textutil.text(title, 400)
    built["album"] = textutil.text(album, 200)

    if built.get("is_live"):
        return None            # a live stream is not a track
    return built


# YouTube Music's own search API, as its web player calls it. yt-dlp's
# YouTube Music search returns nothing but a title and a link per result, so
# the Music tab could not say who a song was by, which album it was from or
# how long it was. This answers all three, plus the cover art.
_INNERTUBE = "https://music.youtube.com/youtubei/v1/search?prettyPrint=false"
_CLIENT = {"clientName": "WEB_REMIX", "clientVersion": "1.20250101.01.00",
           "hl": "en", "gl": "US"}
# The "Songs" filter of the search page.
_SONGS = "EgWKAQIIAWoMEA4QChADEAQQCRAF"


def _runs(column):
    renderer = (column or {}).get("musicResponsiveListItemFlexColumnRenderer") or {}
    return [r.get("text", "") for r in (renderer.get("text") or {}).get("runs") or []]


def _seconds(text):
    parts = str(text or "").strip().split(":")
    if not parts or not all(p.isdigit() for p in parts) or len(parts) > 3:
        return 0
    total = 0
    for part in parts:
        total = total * 60 + int(part)
    return total


def _walk(node):
    if isinstance(node, dict):
        if "musicResponsiveListItemRenderer" in node:
            yield node["musicResponsiveListItemRenderer"]
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


# query -> {"items": [...], "token": next page token or "", "at": time}.
# YouTube Music pages its search 20 at a time with a continuation token; the
# pages already fetched are kept, so "load more" asks only for the next one.
_PAGES = {}
_PAGES_TTL = 1800


def _token(data):
    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "nextContinuationData" and isinstance(value, dict):
                    return value.get("continuation")
                found = walk(value)
                if found:
                    return found
        elif isinstance(node, list):
            for value in node:
                found = walk(value)
                if found:
                    return found
        return None
    return walk(data) or ""


def search_songs(query, limit=20, offset=0):
    """Songs from YouTube Music, with artist, album, length and artwork.

    Returns (items, more): the window [offset, offset+limit) of the results,
    and whether there are more after it.
    """
    from ..util import netguard
    key = query.lower()
    entry = _PAGES.get(key)
    if entry is None or offset == 0 or time.time() - entry["at"] > _PAGES_TTL:
        data = netguard.post_json(_INNERTUBE, {"context": {"client": _CLIENT},
                                               "query": query, "params": _SONGS},
                                  limit=6 << 20, seconds=15,
                                  headers={"Origin": "https://music.youtube.com"})
        entry = {"items": _songs(data), "token": _token(data), "at": time.time()}
        _PAGES[key] = entry
        while len(_PAGES) > 50:
            _PAGES.pop(next(iter(_PAGES)))
    while len(entry["items"]) < offset + limit and entry["token"]:
        token = entry["token"]
        data = netguard.post_json(
            _INNERTUBE + "&ctoken=%s&continuation=%s&type=next" % (token, token),
            {"context": {"client": _CLIENT}}, limit=6 << 20, seconds=15,
            headers={"Origin": "https://music.youtube.com"})
        page = _songs(data)
        seen = {i["uid"] for i in entry["items"]}
        entry["items"].extend(i for i in page if i["uid"] not in seen)
        entry["token"] = _token(data) if page else ""
    window = entry["items"][offset:offset + limit]
    return window, bool(entry["token"]) or len(entry["items"]) > offset + limit


def _songs(data):
    from .youtube import WATCH
    out = []
    for renderer in _walk(data):
        video_id = textutil.text((renderer.get("playlistItemData") or {})
                                 .get("videoId"), 32)
        columns = renderer.get("flexColumns") or []
        if not video_id or not columns:
            continue
        title = textutil.text("".join(_runs(columns[0])), 300)
        # The second column reads "Artist & Other • Album • 3:45": split on
        # the bullets, not on the runs, or "Dave & Central Cee" came out as
        # the artist "Dave" and the album "&".
        joined = "".join(_runs(columns[1])) if len(columns) > 1 else ""
        details = [part.strip() for part in joined.split("\u2022") if part.strip()]
        duration = _seconds(details[-1]) if details else 0
        if duration:
            details = details[:-1]
        artist = textutil.text(details[0], 200) if details else ""
        album = textutil.text(details[1], 200) if len(details) > 1 else ""
        plays = textutil.text("".join(_runs(columns[2])), 40) if len(columns) > 2 else ""
        thumbs = (((renderer.get("thumbnail") or {}).get("musicThumbnailRenderer")
                   or {}).get("thumbnail") or {}).get("thumbnails") or []
        thumb = str((thumbs[-1] if thumbs else {}).get("url") or "")
        # The listed size is tiny; the same image is served at any size.
        thumb = re.sub(r"=w\d+-h\d+", "=w400-h400", thumb)
        if not title:
            continue
        out.append(item(
            "music", "ym-search-%s" % video_id, title, url=WATCH + video_id,
            kind="track", artist=artist, album=album, duration=duration,
            art=art_for(thumb, "yt-%s" % video_id, _fetcher()),
            extra={"video_id": video_id, "artists": artist, "album": album,
                   "plays": plays, "thumbnail": textutil.text(thumb, 2048)},
        ))
    return out


def search(query, limit=20, cache_dir=None, budget=None, offset=0):
    query = textutil.text(query, 200)
    if not query:
        raise SourceError("type something to search for", "empty")
    try:
        songs, _more = search_songs(query, limit, offset)
        if songs or offset:
            return songs
    except Exception:
        if offset:
            return []
        # the yt-dlp path below still answers, with less detail
    try:
        raw = resolver.search(query, limit=limit, music=True)
    except resolver.ResolveError as exc:
        raise SourceError(str(exc), exc.reason) from exc
    out = []
    for index, entry in enumerate(_entries(raw)):
        if len(out) >= limit:
            break
        if not looks_like_song(entry):
            continue
        built = to_track(entry, cache_dir, index, "ym-search")
        if built:
            out.append(built)
    return out


def _entries(raw):
    from .youtube import _entries as shared
    return shared(raw)


def _listing(url, cache_dir, prefix, limit, timeout=45):
    try:
        raw = resolver._ytdlp(["--dump-single-json", "--flat-playlist", url],
                              timeout=timeout, listing=True)
    except resolver.ResolveError as exc:
        raise SourceError(str(exc), exc.reason) from exc
    out = []
    for index, entry in enumerate(_entries(raw)):
        if len(out) >= limit:
            break
        built = to_track(entry, cache_dir, index, prefix)
        if built:
            built["album"] = built.get("album") or textutil.text(
                raw.get("title") if isinstance(raw, dict) else "", 200)
            out.append(built)
    return out


def playlist(playlist_id, cache_dir=None, limit=300, budget=None):
    """Music playlists are ordinary YouTube playlists behind a redirect."""
    pid = textutil.text(playlist_id, 120)
    if not pid:
        raise SourceError("that is not a playlist", "unsupported")
    if pid.startswith("http"):
        return _listing(pid, cache_dir, "ym-pl", limit)
    return _listing("https://music.youtube.com/playlist?list=%s" % pid,
                    cache_dir, "ym-pl", limit)


def channel(channel_id, cache_dir=None, limit=100, budget=None):
    cid = textutil.text(channel_id, 120)
    if not cid:
        raise SourceError("that is not an artist", "unsupported")
    if cid.startswith("http"):
        return _listing(cid, cache_dir, "ym-ch", limit)
    if not cid.startswith("UC"):
        raise SourceError("paste a channel id starting with UC, or a link",
                          "unsupported")
    return _listing("https://music.youtube.com/playlist?list=UU%s" % cid[2:],
                    cache_dir, "ym-ch", limit)


# -- lyrics ---------------------------------------------------------------

_TIMED = re.compile(r"\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\]")
_LINE_TAG = re.compile(r"^\d+:\d{2}(?:\.\d+)?$")


def lyrics_from_youtube(item_dict, budget=None):
    """Music publishes timed lyrics for most catalogue tracks.

    They are not a documented API, so this reads the watch page's own player
    response, which is the only place the data appears. If anything about the
    shape changes we return nothing and fall back to the other providers.
    """
    if netguard.offline():
        return None
    video_id = (item_dict.get("extra") or {}).get("video_id")
    if not video_id:
        return None
    try:
        body = netguard.fetch(
            "https://www.youtube.com/watch?v=%s" % textutil.text(video_id, 32),
            limit=6 << 20, seconds=20, allowed=netguard.API_PORTS)
    except netguard.Blocked:
        return None
    except OSError:
        return None

    text = body.decode("utf-8", "replace")
    marker = '"timedLyricsData"'
    if marker not in text:
        return None
    chunk = text.split(marker, 1)[1][:200000]

    lines = []
    for piece in re.findall(r'\{"cueGroup":\{"transcriptRenderer":\{"'
                            r'cues":\[(.*?)\]\}\}\}', chunk, re.DOTALL)[:1]:
        for cue in re.findall(r'\{"cueRenderer":(\{.*?\})\}\]', piece)[:400]:
            millis = re.search(r'"startTimeMs":"(\d+)"', cue)
            rendered = re.search(r'"simpleText":"((?:[^"\\]|\\.)*)"', cue)
            if not millis or not rendered:
                continue
            body_text = rendered.group(1)
            body_text = (body_text.replace('\\"', '"').replace("\\n", " ")
                         .replace("\\\\", "\\"))
            if _LINE_TAG.match(body_text.strip()):
                body_text = ""
            lines.append({"t": int(millis.group(1)),
                          "text": textutil.text(body_text, 240)})
    if not lines:
        return None
    lines.sort(key=lambda line: line["t"])
    return {"lines": lines[:400], "source": "youtube", "synced": True}


def lyrics_for(item_dict, settings, budget=None):
    """YouTube's own lyrics first for Music tracks, then the shared providers."""
    if settings.get("useYtLyrics", True) and item_dict.get("source") == "music":
        found = lyrics_from_youtube(item_dict, budget)
        if found:
            return found
    return resolver.lyrics_for(item_dict, settings)
