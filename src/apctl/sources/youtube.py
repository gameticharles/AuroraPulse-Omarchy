"""YouTube: search, subscriptions, playlists, and a resolution budget.

Every stream we play costs a yt-dlp invocation against a service that rate
limits aggressively and bans hard. So resolution is the expensive step, and
this module's job is to make it rare:

* a hard ceiling on lookups per hour, persisted, so a runaway loop degrades
  into "try later" instead of a ban
* a signed-out, cookieless, no-cache invocation
* results cached by video id, because the same video is usually played twice
"""

import os
import re
import time

from ..core import resolver
from ..util import netguard, textutil
from .base import SourceError, _fetcher, art_for, item

WATCH = "https://www.youtube.com/watch?v="

HOURLY_BUDGET_DEFAULT = 120


class Budget:
    """Resolutions per rolling hour."""

    def __init__(self, store, per_hour=HOURLY_BUDGET_DEFAULT):
        self.store = store
        self.per_hour = max(0, int(per_hour))

    def spent(self):
        marks = (self.store.state.get("youtube") or {}).get("resolved", [])
        cutoff = time.time() - 3600
        return [m for m in marks if isinstance(m, (int, float)) and m > cutoff]

    def remaining(self):
        if not self.per_hour:
            return 1 << 30          # unlimited
        return max(0, self.per_hour - len(self.spent()))

    def spend(self, count=1):
        state = self.store.state.setdefault("youtube", {})
        marks = [m for m in self.spent() if m > time.time() - 3600]
        marks.extend([time.time()] * count)
        state["resolved"] = marks[-600:]
        self.store.save()

    def clear(self):
        self.store.state.setdefault("youtube", {})["resolved"] = []
        self.store.save()


def _video_id(entry):
    raw = entry.get("id") or entry.get("url") or ""
    raw = textutil.text(raw, 300)
    if "v=" in raw:
        raw = raw.split("v=", 1)[1].split("&", 1)[0]
    elif "youtu.be/" in raw:
        raw = raw.rsplit("/", 1)[1].split("?", 1)[0]
    return textutil.text(raw, 32)


def _thumb(entry):
    return (entry.get("thumbnail")
            or ((entry.get("thumbnails") or [{}])[0] or {}).get("url") or "")


def thumbnail_url(video_id):
    return "https://i.ytimg.com/vi/%s/hqdefault.jpg" % video_id if video_id else ""


def to_item(entry, cache_dir=None, index=0, prefix="yt", source="youtube"):
    """A flat search/playlist entry into a MediaItem.

    `source` is a parameter because YouTube Music shares this parser but is
    not the same thing: labelling a Music result as `youtube` made the bar
    show the YouTube colour and label for a track the user picked from Music.
    """
    video_id = _video_id(entry)
    if not video_id or len(video_id) < 6:
        return None
    title = textutil.text(entry.get("title"), 400)
    if not title:
        return None
    channel = textutil.text(entry.get("uploader") or entry.get("channel"), 200)
    duration = int(entry.get("duration") or 0)
    # Flat search results often carry no thumbnail at all, which left every
    # YouTube and Music row - and the now-playing tile - with initials. Every
    # video has one at a fixed address, so that is the fallback.
    thumb = _thumb(entry) or thumbnail_url(video_id)
    return item(
        source, "%s-%s" % (prefix, video_id), title,
        url=WATCH + video_id,
        kind="track" if source == "music" else "video",
        artist=channel,
        album=textutil.text(entry.get("playlist_title") or
                            entry.get("playlist"), 200),
        duration=duration,
        is_live=bool(entry.get("is_live")),
        art=art_for(thumb, "yt-%s" % video_id, _fetcher()),
        extra={
            "video_id": video_id,
            "channel": channel,
            "views": int(entry.get("view_count") or 0),
            "verified": bool(entry.get("channel_is_verified")),
            "description": textutil.text(entry.get("description"), 200),
            "published": textutil.text(entry.get("upload_date"), 12),
            "channel_url": textutil.text(
                entry.get("channel_url") or entry.get("uploader_url"), 300),
            "thumbnail": textutil.text(thumb, 2048),
        },
    )


def _entries(raw):
    """yt-dlp returns either a playlist dict or a single entry."""
    if isinstance(raw, dict):
        entries = raw.get("entries")
        return [e for e in entries if isinstance(e, dict)] if entries else [raw]
    return [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []


def search(query, limit=20, cache_dir=None, budget=None, offset=0):
    query = textutil.text(query, 200)
    if not query:
        raise SourceError("type something to search for", "empty")
    if offset >= 500:
        return []
    try:
        raw = resolver.search(query, limit=limit, offset=offset)
    except resolver.ResolveError as exc:
        raise SourceError(str(exc), exc.reason) from exc
    out = []
    for index, entry in enumerate(_entries(raw)):
        built = to_item(entry, cache_dir, index, "yt-search")
        if built:
            out.append(built)
    return out


def _listing(url, cache_dir, prefix, budget, limit, timeout=45):
    try:
        raw = resolver._ytdlp(["--dump-single-json", "--flat-playlist", url],
                              timeout=timeout, listing=True)
    except resolver.ResolveError as exc:
        raise SourceError(str(exc), exc.reason) from exc
    out = []
    for index, entry in enumerate(_entries(raw)):
        if len(out) >= limit:
            break
        built = to_item(entry, cache_dir, index, prefix)
        if built:
            built["album"] = built.get("album") or textutil.text(
                raw.get("title") if isinstance(raw, dict) else "", 200)
            out.append(built)
    return out


_LIST_ID = re.compile(r"(?:[?&]list=|^)([A-Za-z0-9_-]{10,64})(?:[&#]|$)")


def playlist_id(text):
    """The playlist id in a pasted link (`...?list=PL...`), or a bare id.

    Watch links that carry a `list=` count too: the user copied the address
    while a playlist was playing and wants the playlist, not the one video.
    Returns "" for anything else.
    """
    raw = textutil.text(text, 2048).strip()
    if not raw:
        return ""
    if raw.startswith(("http://", "https://")):
        if not resolver.is_youtube_url(raw):
            return ""
        query = raw.split("?", 1)[1] if "?" in raw else ""
        match = _LIST_ID.search("?" + query)
        return match.group(1) if match else ""
    # A bare id must look like one, or every one-word search is a playlist.
    if " " in raw or not _LIST_ID.fullmatch(raw):
        return ""
    if raw.startswith("VLPL") or raw.startswith("VLRD") or raw.startswith("VLOL"):
        return raw[2:]
    return raw if raw[:2] in ("PL", "OL", "RD", "UU", "FL") else ""


def playlist_item(entry, source="youtube"):
    """A playlist search result: a row the panel opens, not a track."""
    pid = textutil.text(entry.get("id"), 64)
    if not pid:
        pid = playlist_id(entry.get("url") or "")
    title = textutil.text(entry.get("title"), 400)
    if not pid or not title:
        return None
    channel = textutil.text(entry.get("uploader") or entry.get("channel"), 200)
    thumb = _thumb(entry)
    count = int(entry.get("playlist_count") or 0)
    host = "music.youtube.com" if source == "music" else "www.youtube.com"
    return item(
        source, "%s-pl-%s" % ("ym" if source == "music" else "yt", pid), title,
        url="https://%s/playlist?list=%s" % (host, pid),
        kind="playlist",
        artist=channel,
        art=art_for(thumb, "ytpl-%s" % pid, _fetcher()),
        extra={"playlist_id": pid, "channel": channel, "count": count,
               "thumbnail": textutil.text(thumb, 2048)},
    )


def search_playlists(query, limit=20, offset=0):
    """Playlists matching a search, from YouTube's Playlists filter."""
    query = textutil.text(query, 200)
    if not query:
        raise SourceError("type something to search for", "empty")
    if offset >= 500:
        return []
    try:
        raw = resolver.search(query, limit=limit, offset=offset, playlists=True)
    except resolver.ResolveError as exc:
        raise SourceError(str(exc), exc.reason) from exc
    out = []
    for entry in _entries(raw):
        if entry.get("ie_key") not in (None, "YoutubeTab"):
            continue
        built = playlist_item(entry)
        if built:
            out.append(built)
    return out


def playlist(playlist_id_or_url, cache_dir=None, limit=500, budget=None):
    """Every video in a playlist, from its id or any link that names it."""
    pid = playlist_id(playlist_id_or_url)
    if not pid:
        raise SourceError("that is not a playlist", "unsupported")
    return _listing("https://www.youtube.com/playlist?list=%s" % pid,
                    cache_dir, "yt-pl", budget, limit)


def channel_feed(channel_id, cache_dir=None, limit=50, budget=None):
    """The uploads feed. Signing in is deliberately not supported: cookies in
    this plugin would be a much larger liability than a slightly smaller
    catalogue."""
    cid = textutil.text(channel_id, 300).strip()
    if not cid:
        raise SourceError("that is not a channel", "unsupported")
    # A channel's page lists its tabs (Videos, Shorts, Live), not videos; its
    # uploads playlist (UC... -> UU...) lists the videos, newest first.
    found = re.search(r"/channel/(UC[A-Za-z0-9_-]{10,})", cid)
    if found:
        cid = found.group(1)
    if cid.startswith("UC") and "/" not in cid:
        url = "https://www.youtube.com/playlist?list=UU%s" % cid[2:]
    elif cid.startswith("http"):
        if not resolver.is_youtube_url(cid):
            raise SourceError("that is not a YouTube channel", "unsupported")
        url = cid.split("?", 1)[0].rstrip("/")
        if not url.endswith("/videos"):
            url += "/videos"
    elif cid.startswith("@"):
        url = "https://www.youtube.com/%s/videos" % cid
    else:
        url = "https://www.youtube.com/user/%s/videos" % cid
    return _listing(url, cache_dir, "yt-ch", budget, limit)


MIX_SIZE = 40


def mix(video_id, cache_dir=None, source="youtube", limit=MIX_SIZE):
    """YouTube's own mix for a video ("more like this"), as a list.

    The mix is only reachable from a watch link that carries it
    (`watch?v=ID&list=RDID`); as a playlist link YouTube calls it
    unviewable. Music's flavour (RDAMVM) is the same mix with songs in it.
    """
    vid = textutil.text(video_id, 32)
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", vid or ""):
        raise SourceError("there is nothing to start a mix from", "unsupported")
    url = "https://www.youtube.com/watch?v=%s&list=RD%s%s" % (
        vid, "AMVM" if source == "music" else "", vid)
    try:
        raw = resolver._ytdlp(["--dump-single-json", "--flat-playlist",
                               "--playlist-items", "1:%d" % max(1, int(limit)), url],
                              timeout=45, listing=True)
    except resolver.ResolveError as exc:
        raise SourceError(str(exc), exc.reason) from exc
    out = []
    for index, entry in enumerate(_entries(raw)):
        if source == "music":
            from .music import to_track
            built = to_track(entry, cache_dir, index, "ym-mix")
        else:
            built = to_item(entry, cache_dir, index, "yt-mix")
        if built and not built.get("is_live"):
            out.append(built)
    return out


def subscriptions(cache_dir=None, limit=200, budget=None, store=None):
    """Saved for the user to paste: the RSS feed needs a channel id, and we
    will not ask anyone to hand us a signed-in cookie."""
    raise SourceError(
        "add channels by pasting a channel or playlist link, or a "
        "channel id starting with UC", "unsupported")


def resolve_for_play(item_dict, want_video, quality, budget=None,
                     cache_dir=None):
    """The resolution step, with the budget in front of it.

    Returns a dict with a fresh, direct media URL. Direct URLs expire, so this
    must be called at play time and the result must not be cached long.
    """
    if budget is not None and budget.remaining() <= 0:
        raise SourceError(
            "we have hit YouTube's hourly look-up limit for now; try again in "
            "an hour", "rate-limited")
    video_id = (item_dict.get("extra") or {}).get("video_id")
    url = item_dict.get("url") or (WATCH + video_id if video_id else "")
    if not resolver.is_youtube_url(url):
        # A direct media URL from a playlist entry can be played as-is.
        if url.startswith(("http://", "https://")):
            return {"url": url, "codec": "", "bitrate": 0, "height": 0,
                    "is_live": bool(item_dict.get("is_live"))}
        raise SourceError("that entry has no playable address", "unsupported")

    height = _height_for(quality)
    try:
        entry = resolver.resolve(url, video=want_video, quality=quality,
                                 max_height=height)
    except resolver.ResolveError as exc:
        raise SourceError(str(exc), exc.reason) from exc
    if budget is not None:
        budget.spend()
    return resolver.direct_urls(entry, video=want_video)


def _height_for(quality):
    table = {"144p": 144, "360p": 360, "480p": 480, "720p": 720,
             "1080p": 1080, "1440p": 1440, "2160p": 2160, "max": 4320}
    return table.get(str(quality), 360)


def add_to_watch_later(entries, budget=None, store=None):
    """Not supported without a signed-in session; said plainly rather than
    failing later with a confusing yt-dlp error."""
    raise SourceError(
        "this build does not sign in to YouTube, so it cannot write back to "
        "your account; open the link to add it by hand", "unsupported")
