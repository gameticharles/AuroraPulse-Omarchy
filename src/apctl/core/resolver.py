"""Turning a directory entry into something mpv can play.

Three jobs:

* resolve YouTube and YouTube Music entries with yt-dlp, under a rate limit
  and a cache, so browsing does not resolve hundreds of videos
* fetch and *verify* artwork with ffprobe before the compositor ever sees it
* fetch lyrics from LRCLib or a sidecar .lrc

Every one of these is the classic place for a remote service to hand you
something you did not ask for, so all three are strict about what they
accept.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time

from ..util import sandbox, netguard, textutil

YTDLP = shutil.which("yt-dlp") or "/usr/bin/yt-dlp"
FFPROBE = shutil.which("ffprobe") or "/usr/bin/ffprobe"

# A generous but hard ceiling, and a wall-clock budget per invocation.
YTDLP_TIMEOUT = 25
MAX_MEDIA_BYTES = 512 << 20
MAX_ART_BYTES = 8 << 20
MAX_LYRICS_BYTES = 512 << 10

# Only these containers are ever handed to mpv.
AUDIO_SUFFIXES = (".mp3", ".m4a", ".aac", ".opus", ".ogg", ".oga", ".flac",
                  ".wav", ".wma", ".aiff", ".alac", ".wv", ".mka")
VIDEO_SUFFIXES = (".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".wmv")

_ytdlp_lock = threading.Lock()
_ytdlp_last = 0.0
YTDLP_MIN_INTERVAL = 0.75


class ResolveError(Exception):
    def __init__(self, message, reason="network"):
        super().__init__(message)
        self.reason = reason


def is_youtube_url(url):
    if not isinstance(url, str):
        return False
    host = netguard.url_host(url)
    return host in ("youtube.com", "www.youtube.com", "m.youtube.com",
                    "youtu.be", "music.youtube.com", "www.youtube-nocookie.com")


def probe_duration(path):
    """Duration in whole seconds, or 0 when ffprobe cannot tell us."""
    try:
        out = sandbox.run_tool(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", "--", path],
            read=[path], timeout=15, text=True).stdout
        return max(0, int(float(out.strip())))
    except (ValueError, OSError, subprocess.SubprocessError):
        return 0


def probe_image(path):
    """True only if ffprobe agrees this file is a still image.

    This is the gate that makes remote artwork safe: the bytes are already on
    disk, but nothing renders them until they are a real image of a sane size.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return False
    if not 512 <= size <= MAX_ART_BYTES:
        return False
    try:
        # Decoding hostile bytes is exactly what the artwork sandbox is for:
        # no network, no home, only this one file. It used to run in the
        # daemon's own context, which the security notes promised it did not.
        out = sandbox.run_tool(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,width,height",
             "-of", "json", "--", path],
            read=[path], timeout=15, text=True).stdout
        info = json.loads(out or "{}")
        streams = info.get("streams") or []
        if not streams:
            return False
        stream = streams[0]
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
        return 64 <= width <= 8192 and 64 <= height <= 8192
    except sandbox.SandboxUnavailable:
        return False      # an image that cannot be checked safely is not shown
    except (ValueError, OSError, KeyError, subprocess.SubprocessError):
        return False


def fetch_art(url, cache_dir, key):
    """Download artwork once and return the path only if it verified."""
    if not url or not isinstance(url, str):
        return None
    if not url.startswith(("http://", "https://")):
        return None
    name = "%s.img" % textutil.slug(key, 120)
    target = os.path.join(cache_dir, name)
    if os.path.exists(target):
        return target
    tmp = target + ".part"
    try:
        # Verified bytes land in a private temp file, then ffprobe has to agree
        # they are an image before the name is ever handed to the shell.
        body = netguard.fetch(url, limit=MAX_ART_BYTES, seconds=20,
                              allowed=netguard.WEB_PORTS)
        with open(tmp, "wb") as handle:
            handle.write(body)
        if not probe_image(tmp):
            raise ValueError("not a usable image")
        os.replace(tmp, target)
        os.chmod(target, 0o600)
        return target
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return None


# -- yt-dlp ---------------------------------------------------------------

def _ytdlp(argv, timeout=YTDLP_TIMEOUT, offline_ok=False, listing=False):
    """Run yt-dlp with a hard timeout and no cookies, config or home.

    `listing` matters more than it looks. The two kinds of call want opposite
    flags, and forcing one set on both is what broke every search:

      * Resolving one video wants `--no-playlist`, so a watch URL that also
        appears in a playlist plays that video rather than the whole list.
      * A search or a playlist is *itself* a list, and needs the opposite.
        `--no-playlist` on `ytsearch10:...` collapses the ten results to the
        first one, so a search for a popular artist returned a single video
        and looked broken. It also fights `--dump-single-json`: `--print-json`
        prints one object per entry, so combined with `--dump-single-json` the
        search wrapper was never what came back.

    So a listing asks for `--yes-playlist` and gets the single wrapper object,
    and a single resolve keeps `--print-json`.
    """
    global _ytdlp_last
    with _ytdlp_lock:
        wait = YTDLP_MIN_INTERVAL - (time.monotonic() - _ytdlp_last)
        if wait > 0:
            time.sleep(wait)
        _ytdlp_last = time.monotonic()
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt",
    }
    try:
        proc = sandbox.run_tool(
            [YTDLP, "--no-warnings", "--no-progress",
             "--yes-playlist" if listing else "--no-playlist",
             "--no-call-home", "--no-config", "--ignore-config",
             "--socket-timeout", "12", "--retries", "1",
             "--extractor-retries", "1", "--no-cache-dir", "--no-mtime",
             "--no-write-info-json", "--no-write-thumbnail",
             "--no-write-subs", "--no-write-auto-subs", "--skip-download",
             *([] if listing else ["--print-json"]), *argv],
            # Its own network, none of the user's files: yt-dlp parses pages
            # that YouTube controls, so it runs where a bug in it can see
            # nothing worth taking.
            profile="fetcher", env=[("SSL_CERT_FILE", env["SSL_CERT_FILE"])],
            timeout=timeout, text=True)
    except subprocess.TimeoutExpired as exc:
        raise ResolveError("the site did not answer in time", "expired") from exc
    except sandbox.SandboxUnavailable as exc:
        raise ResolveError(str(exc), "sandbox") from exc
    except OSError as exc:
        raise ResolveError("yt-dlp is not available: %s" % exc, "unsupported") from exc

    if proc.returncode != 0:
        message = (proc.stderr or "").strip().splitlines()
        detail = message[-1][:180] if message else "exit %d" % proc.returncode
        lowered = detail.lower()
        if "private" in lowered or "members-only" in lowered:
            raise ResolveError("that video is private: %s" % detail, "not-found")
        if "unavailable" in lowered or "removed" in lowered:
            raise ResolveError("that video is gone: %s" % detail, "not-found")
        if "sign in" in lowered or "confirm you" in lowered or "age" in lowered:
            raise ResolveError("that video needs a sign-in", "denied")
        if "rate" in lowered or "429" in lowered:
            raise ResolveError("YouTube is rate limiting us, try later",
                               "rate-limited")
        raise ResolveError("could not resolve that stream: %s" % detail, "network")

    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            # A listing wrapper legitimately has no url of its own - it is a
            # container, not a stream - so requiring one threw the results
            # away. A single resolve still has to produce something playable.
            if listing:
                if data.get("entries") is not None:
                    return data
            # A video picked as separate video and audio streams has no url of
            # its own, only `requested_formats`. Requiring a url here is why
            # YouTube could only ever play as audio.
            elif data.get("url") or data.get("requested_formats"):
                return data
    raise ResolveError("no playable stream was offered for that", "expired")


def search(query, limit=20, music=False, timeout=YTDLP_TIMEOUT, offset=0):
    """Search YouTube or YouTube Music. Returns raw yt-dlp entries.

    `offset` skips that many results: the search is asked for offset+limit
    and only the window after the offset is extracted, so "load more" is a
    page of new results rather than the first page again.
    """
    query = textutil.text(query, 200)
    if not query:
        raise ResolveError("type something to search for", "empty")
    if netguard.offline():
        raise ResolveError("offline mode is on", "denied")
    limit = max(1, min(50, int(limit)))
    offset = max(0, min(450, int(offset)))
    target = "ytsearch%d:%s" % (offset + limit, query)
    if music:
        target = "https://music.youtube.com/search?q=" + \
            query.replace(" ", "%20").replace("+", "%2B")
    window = ["--playlist-items", "%d:%d" % (offset + 1, offset + limit)] if offset else []
    return _ytdlp(["--dump-single-json", "--flat-playlist", *window, target], timeout,
                 listing=True)


def resolve(url, video=True, quality="audio", max_height=1080, timeout=40):
    """Turn a watch page into direct media URLs.

    We ask for a *format* rather than a single URL so the height ceiling is
    honoured by yt-dlp, not by us guessing at the manifest afterwards.
    """
    if netguard.offline():
        raise ResolveError("offline mode is on", "denied")
    url = textutil.text(url, 2048)
    if not is_youtube_url(url):
        raise ResolveError("that is not a YouTube link", "unsupported")

    if video and quality not in ("audio", "max"):
        wanted = str(max(8, min(4320, int(max_height))))
        selector = ("bestvideo[height<=%s]+bestaudio/best[height<=%s]/"
                    "bestaudio/best" % (wanted, wanted))
    else:
        selector = "bestaudio/best"
    return _ytdlp(["-f", selector, url], timeout=timeout)


def subtitle_options(entry, limit=40):
    """The subtitle tracks a YouTube video offers, as WebVTT addresses.

    Manual subtitles are all listed. Automatic captions come in a hundred
    machine translations, so only the video's own language and English are
    kept from those.
    """
    out = []

    def pick(formats):
        for fmt in formats or []:
            if isinstance(fmt, dict) and fmt.get("ext") == "vtt" and \
                    str(fmt.get("url", "")).startswith("https://"):
                return fmt
        return None

    for auto, table in ((False, entry.get("subtitles")),
                        (True, entry.get("automatic_captions"))):
        if not isinstance(table, dict):
            continue
        own = str(entry.get("language") or "").split("-")[0]
        for lang, formats in table.items():
            lang = textutil.text(lang, 20)
            if not lang or lang == "live_chat":
                continue
            base = lang.split("-")[0]
            if auto and (base not in (own, "en") or "-" in lang):
                continue
            fmt = pick(formats)
            if fmt is None:
                continue
            name = textutil.text(fmt.get("name") or lang, 60)
            out.append({"lang": lang, "name": name + (" (auto)" if auto else ""),
                        "url": fmt["url"][:4096], "auto": auto})
    # The video's own language and English first, then by name: a talk can
    # carry a hundred translations, and the list is cut to `limit`.
    own = str(entry.get("language") or "").split("-")[0]
    out.sort(key=lambda o: (o["lang"].split("-")[0] not in (own, "en"), o["auto"],
                            o["name"].lower()))
    return out[:limit]


def direct_urls(entry, video=True):
    """Pick a URL out of a resolved entry, preferring a progressive stream.

    mpv handles a single muxed file with far fewer moving parts than it does
    separate video and audio tracks, so when one is available we take it.
    """
    if not isinstance(entry, dict):
        raise ResolveError("nothing to play", "expired")
    # What yt-dlp chose for the format selector: for a video, usually one
    # video-only and one audio-only stream. YouTube now offers a stream with
    # both only at 360p, so preferring "progressive" quietly dropped every
    # video to its audio track. mpv plays the pair: the video as the file and
    # the audio as an external track.
    requested = [f for f in entry.get("requested_formats") or []
                 if isinstance(f, dict) and str(f.get("url", "")).startswith(
                     ("http://", "https://"))]
    if video and requested:
        picture = next((f for f in requested if f.get("vcodec") not in (None, "none")), None)
        sound = next((f for f in requested if f.get("acodec") not in (None, "none")
                      and f is not picture), None)
        if picture is not None:
            return {
                "url": picture["url"],
                "audio_url": sound["url"] if sound else "",
                "codec": textutil.text(picture.get("vcodec"), 32),
                "bitrate": int((picture.get("tbr") or 0) + ((sound or {}).get("tbr") or 0)) * 1000,
                "height": int(picture.get("height") or 0),
                "is_live": bool(entry.get("is_live")),
                "title": textutil.text(entry.get("title"), 400),
                "artist": textutil.text(entry.get("uploader") or entry.get("channel"), 200),
                "duration": int(entry.get("duration") or 0),
                "thumb": textutil.text(entry.get("thumbnail"), 2048),
                "subtitles": subtitle_options(entry),
            }
    formats = entry.get("formats") or []
    progressive = [f for f in formats
                   if f.get("url") and f.get("acodec") not in (None, "none")
                   and f.get("vcodec") not in (None, "none")
                   and f.get("protocol", "").startswith("http")]
    audio = [f for f in formats
             if f.get("url") and f.get("vcodec") in (None, "none")
             and f.get("acodec") not in (None, "none")
             and f.get("protocol", "").startswith("http")]

    if not video:
        pool = audio or progressive
    else:
        pool = progressive or audio
    if not pool:
        pool = [f for f in formats if f.get("url") and
                f.get("protocol", "").startswith("http")]
    if not pool:
        raise ResolveError("only a stream we cannot play is offered here",
                           "unsupported")

    def score(f):
        return (f.get("height") or 0, f.get("tbr") or 0, f.get("filesize") or 0)

    best = max(pool, key=score)
    url = best.get("url")
    if not isinstance(url, str) or not url.startswith(
            ("http://", "https://")):
        raise ResolveError("that stream has an unsupported delivery method",
                           "unsupported")
    return {
        "url": url,
        "codec": textutil.text(best.get("vcodec") or best.get("acodec"), 32),
        "bitrate": int(best.get("tbr") or 0) * 1000,
        "height": int(best.get("height") or 0),
        "is_live": bool(entry.get("is_live")),
        "title": textutil.text(entry.get("title"), 400),
        "artist": textutil.text(entry.get("uploader") or entry.get("channel"), 200),
        "duration": int(entry.get("duration") or 0),
        "thumb": textutil.text(entry.get("thumbnail"), 2048),
        "subtitles": subtitle_options(entry) if video else [],
    }


# -- lyrics ---------------------------------------------------------------

_LRC_TIME = re.compile(r"\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\]")


def parse_lrc(body, limit=400):
    """Turn an LRC file into a sorted list of {t, text} lines.

    Timestamps out of order are common in the wild, and an unsorted list makes
    the highlight jump backwards, so they are sorted here.
    """
    lines = []
    for raw in textutil.multiline(body, limit=20000, lines=1200).splitlines():
        stamps = _LRC_TIME.findall(raw)
        if not stamps:
            continue
        text = _LRC_TIME.sub("", raw).strip()
        for minutes, seconds, fraction in stamps:
            millis = int((fraction or "0").ljust(3, "0")[:3])
            at = (int(minutes) * 60 + int(seconds)) * 1000 + millis
            lines.append({"t": at, "text": textutil.text(text, 240)})
    lines.sort(key=lambda line: line["t"])
    return lines[:limit]


def lyrics_from_sidecar(path):
    """A .lrc next to the file always beats a network guess."""
    if not path or not os.path.isfile(path):
        return None
    try:
        if os.path.getsize(path) > MAX_LYRICS_BYTES:
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            body = handle.read(MAX_LYRICS_BYTES)
    except OSError:
        return None
    lines = parse_lrc(body)
    return {"lines": lines, "source": "sidecar", "synced": bool(lines)}


def lyrics_from_lrclib(artist, title, duration=0):
    """LRCLib is the source we trust most: it is open, it is honest about
    being wrong, and it serves plain LRC."""
    if netguard.offline() or not artist or not title:
        return None
    from urllib.parse import urlencode
    query = "%s %s" % (textutil.text(artist, 120), textutil.text(title, 120))
    urls = [
        "https://lrclib.net/api/get?" + urlencode({
            "track_name": textutil.text(title, 120),
            "artist_name": textutil.text(artist, 120),
            "duration": int(duration) or "",
        }),
        "https://lrclib.net/api/search?" + urlencode({"q": query}),
    ]
    for url in urls:
        try:
            with netguard.Deadline(12):
                data = json.loads(netguard.fetch(
                    url, limit=256 << 10, seconds=12).decode("utf-8", "replace"))
        except Exception:
            continue
        candidates = data if isinstance(data, list) else [data]
        for entry in candidates:
            if not isinstance(entry, dict):
                continue
            body = entry.get("syncedLyrics") or entry.get("plainLyrics")
            if not body:
                continue
            lines = parse_lrc(body)
            return {"lines": lines, "source": "lrclib", "synced": bool(lines),
                    "offset": int(entry.get("offset") or 0)}
    return None


def lyrics_plain(url):
    """A plain-text lyrics page, for the sites that only offer that."""
    if netguard.offline() or not url or not url.startswith("https://"):
        return None
    try:
        body = netguard.fetch(url, limit=MAX_LYRICS_BYTES, seconds=12).decode(
            "utf-8", "replace")
    except Exception:
        return None
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", body)
    text = re.sub(r"(?s)<[^>]+>", "\n", text)
    lines = [textutil.text(t, 240) for t in text.splitlines()]
    lines = [t for t in lines if t][1:]
    if len(lines) < 4:
        return None
    return {"lines": [{"t": -1, "text": t} for t in lines[:300]],
            "source": textutil.text(url.split("/")[2], 40), "synced": False}


def lyrics_for(item, settings):
    """Try each configured source in order. Returns None rather than an error
    object, because 'no lyrics found' is not a failure worth interrupting for."""
    providers = str(settings.get("lyricsProviders", "lrclib,azlyrics,sidecar"))
    if providers == "off":
        return None
    if "sidecar" in providers and item.get("extra", {}).get("lrc"):
        found = lyrics_from_sidecar(item["extra"]["lrc"])
        if found:
            return found
    if "lrclib" in providers:
        found = lyrics_from_lrclib(item.get("artist", ""), item.get("title", ""),
                                   item.get("duration", 0))
        if found:
            return found
    if "azlyrics" in providers and item.get("artist") and item.get("title"):
        slug = re.sub(r"[^a-z0-9]+", "-",
                      ("%s %s" % (item["artist"], item["title"])).lower()
                      ).strip("-")
        found = lyrics_plain("https://www.azlyrics.com/lyrics/%s.html" % slug)
        if found:
            return found
    return None
