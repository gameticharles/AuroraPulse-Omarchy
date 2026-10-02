"""The local music library.

Scanning is incremental: a file's mtime and size decide whether its metadata
is re-read, so a library of ten thousand tracks re-scans in a second or two
rather than a minute. The index lives in the library database (core/library),
one row per file, so albums, artists, genres and folders are SQL groupings
rather than a walk over every entry.
"""

import json
import os
import shutil
import subprocess
import threading
import time

from ..core import resolver
from ..util import sandbox, textutil
from .base import SourceError, item

AUDIO_SUFFIXES = resolver.AUDIO_SUFFIXES
VIDEO_SUFFIXES = resolver.VIDEO_SUFFIXES

MAX_FILE_BYTES = 4 << 30
MAX_ENTRIES = 200000
SCAN_THREADS = 4

# Bump when the shape of an index entry changes. A stored index from an older
# version is discarded rather than half-migrated, so a plugin upgrade never
# leaves the library in a state where the fields mean something new but the
# data was parsed with the old meaning.
INDEX_VERSION = 4

_lock = threading.Lock()
_scan = {"running": False, "done": 0, "total": 0, "roots": []}


def default_roots(settings):
    configured = str(settings.get("scanRoots", "") or "").strip()
    if configured:
        roots = [os.path.expanduser(p) for p in configured.split(":") if p.strip()]
    else:
        base = os.environ.get("XDG_MUSIC_DIR") or os.path.expanduser("~/Music")
        roots = [base]
    # Downloads and recordings land in these, so they are always scanned, even
    # when the user has chosen other music folders.
    try:
        from ..core.downloads import audio_dir, video_dir
        roots = roots + [audio_dir(), video_dir()]
    except Exception:  # noqa: BLE001
        pass
    out = []
    for root in roots:
        try:
            root = os.path.realpath(root)
        except OSError:
            continue
        if os.path.isdir(root) and root not in out:
            out.append(root)
    return out


def extensions(settings):
    """The audio file types to index, from the "Audio extensions" setting.

    Falls back to the built-in list when the setting is empty or unreadable,
    so a stray edit cannot make the whole library disappear.
    """
    raw = str((settings or {}).get("audioExtensions") or "")
    wanted = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip().lower().lstrip("*").lstrip(".")
        if part and part.isalnum() and len(part) <= 8:
            wanted.append("." + part)
    return tuple(wanted) or tuple(AUDIO_SUFFIXES)


def _sidecar(path):
    """A .lrc next to the file, if there is one."""
    base, _ext = os.path.splitext(path)
    for candidate in (base + ".lrc", base + ".LRC"):
        if os.path.isfile(candidate):
            return candidate
    return ""


def _read_tags(path):
    """Tags via ffprobe.

    mutagen would be nicer, but it is not installed and adding a dependency
    for tag reading is not worth it. ffprobe is already a hard requirement for
    duration and for the EQ, so it does both jobs.
    """
    out = {}
    try:
        proc = sandbox.run_tool(
            # One -show_entries: ffprobe keeps only the last one it is given,
            # so the tags requested by a first flag were silently dropped and
            # every track was listed under its file name.
            [resolver.FFPROBE, "-v", "error", "-show_entries",
             "format=duration:format_tags=artist,album,title,album_artist,"
             "genre,date:stream=codec_name,codec_type,channels,sample_rate",
             "-of", "json", "--", path],
            read=[path], timeout=20)
        data = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    except (OSError, ValueError, json.JSONDecodeError):
        return out

    fmt = data.get("format") or {}
    tags = fmt.get("tags") or {}
    for key in ("artist", "album", "title", "album_artist", "genre", "date"):
        value = tags.get(key) or tags.get(key.upper())
        if isinstance(value, str):
            out[key] = value.strip()
    for stream in data.get("streams") or []:
        if stream.get("codec_type") == "audio":
            out["codec"] = textutil.text(stream.get("codec_name"), 32)
            out["channels"] = int(stream.get("channels") or 0)
            out["sample_rate"] = int(stream.get("sample_rate") or 0)
            break
    out["duration"] = int(float(fmt.get("duration") or 0))
    return out


def _cover(path):
    """Cover art embedded in the file, or a sibling image."""
    directory = os.path.dirname(path)
    stem = os.path.splitext(os.path.basename(path))[0].lower()
    try:
        for name in sorted(os.listdir(directory)):
            lowered = name.lower()
            if not lowered.endswith((".jpg", ".jpeg", ".png", ".webp")):
                continue
            if lowered.startswith(stem) or lowered in ("cover.jpg", "folder.jpg",
                                                       "front.jpg"):
                candidate = os.path.join(directory, name)
                if os.path.getsize(candidate) <= 8 << 20:
                    return candidate
    except OSError:
        pass
    return ""


def _index_key(store, path):
    try:
        info = os.stat(path)
    except OSError:
        return None
    return "%d:%d" % (int(info.st_mtime), int(info.st_size))


def _build(path, cache_dir):
    tags = _read_tags(path)
    stem = os.path.splitext(os.path.basename(path))[0]
    title = tags.get("title") or stem
    artist = tags.get("artist") or tags.get("album_artist") or ""
    album = tags.get("album") or ""
    is_video = path.lower().endswith(VIDEO_SUFFIXES)
    cover = _cover(path) or ""
    if not cover:
        # A picture for the row: the cover embedded in a song, or a frame
        # from a video. The file itself used to be handed over as the
        # "artwork", which the panel cannot draw, so every local row showed
        # initials.
        cover = _thumbnail(path, cache_dir, is_video, tags.get("duration") or 0)

    return item(
        "local", path, title, url=path,
        kind="track",
        artist=artist,
        album=album,
        duration=int(tags.get("duration") or 0),
        codec=textutil.text(tags.get("codec"), 32).upper(),
        bitrate=0,
        art=art_ref(cover, cache_dir, path),
        extra={
            "path": path,
            "folder": os.path.dirname(path),
            "genre": textutil.text(tags.get("genre"), 60),
            "year": textutil.text(tags.get("date"), 8)[:4],
            "lrc": _sidecar(path),
            "media": "video" if is_video else "audio",
            "samplerate": int(tags.get("sample_rate") or 0),
            "channels": int(tags.get("channels") or 0),
        },
    )


def _thumbnail(path, cache_dir, is_video, duration=0):
    """Extract a thumbnail into the art cache, or "" if there is none."""
    if not cache_dir:
        return ""
    import hashlib
    target = os.path.join(cache_dir, "local-%s.jpg"
                          % hashlib.sha1(path.encode("utf-8", "replace")).hexdigest()[:16])
    if os.path.exists(target):
        return target
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return ""
    if is_video:
        at = "%.1f" % min(30.0, max(1.0, float(duration or 0) * 0.1))
        # One thread for the decoder and one for the encoder: each starts one
        # per core by default, and inside the sandbox's memory ceiling the
        # extra threads fail to start.
        argv = [ffmpeg, "-v", "error", "-y", "-threads", "1", "-ss", at, "-i", path,
                "-frames:v", "1", "-vf", "scale=480:-2", "-threads", "1", "-q:v", "4", target]
    else:
        argv = [ffmpeg, "-v", "error", "-y", "-threads", "1", "-i", path, "-an",
                "-frames:v", "1", "-vf", "scale='min(400,iw)':-2", "-threads", "1", "-q:v", "4",
                target]
    try:
        # The file is the user's, but its bytes may not be: run the decoder
        # where it can read that one file and write only into the art cache.
        sandbox.run_tool(argv, read=[path], write=[cache_dir], timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return target if os.path.exists(target) and os.path.getsize(target) > 0 else ""


def art_ref(path, cache_dir, key):
    if not path:
        return {"url": "", "path": ""}
    if os.path.isfile(path) and os.path.splitext(path)[1].lower() in (
            ".jpg", ".jpeg", ".png", ".webp"):
        return {"url": "", "path": path}
    return {"url": "", "path": path}


def scan(store, settings, on_progress=None):
    """Walk the roots and bring the index up to date. Returns the count of
    entries that changed."""
    roots = default_roots(settings)
    suffixes = extensions(settings) + VIDEO_SUFFIXES
    with _lock:
        if _scan["running"]:
            return 0
        _scan.update({"running": True, "done": 0, "total": 0, "roots": roots})

    try:
        library = store.library
        index = library.index() \
            if library.index_info()["index_version"] == INDEX_VERSION else {}
        seen = {}
        found = 0
        follow = bool((settings or {}).get("localFollowSymlinks"))
        visited = set()

        for root in roots:
            for directory, dirs, files in os.walk(root, followlinks=follow):
                if follow:
                    # A link back up the tree would otherwise be walked
                    # forever; each real directory is entered once.
                    try:
                        real = os.path.realpath(directory)
                    except OSError:
                        dirs[:] = []
                        continue
                    if real in visited:
                        dirs[:] = []
                        continue
                    visited.add(real)
                for name in sorted(files):
                    if not name.lower().endswith(suffixes):
                        continue
                    path = os.path.join(directory, name)
                    if not os.path.isfile(path):
                        continue
                    if os.path.islink(path) and not follow:
                        continue
                    try:
                        if os.path.getsize(path) > MAX_FILE_BYTES:
                            continue
                    except OSError:
                        continue
                    found += 1
                    if found > MAX_ENTRIES:
                        break
                    seen[path] = None
                    _scan["done"] += 1
                    if _scan["done"] % 25 == 0 and on_progress:
                        on_progress(_scan["done"], found)

        to_probe = []
        for path in seen:
            fingerprint = _index_key(store, path)
            if fingerprint is None:
                continue
            entry = index.get(path)
            if isinstance(entry, dict) and entry.get("f") == fingerprint:
                entry["seen"] = int(time.time())
                continue
            to_probe.append((path, fingerprint))

        # ffprobe is a process spawn per file, so it runs on a few threads and
        # is the only slow part of a scan.
        def work(job):
            path, fingerprint = job
            try:
                built = _build(path, store.art_dir)
            except Exception:
                return
            built["f"] = fingerprint
            built["seen"] = int(time.time())
            index[path] = built

        threads = []
        queue = list(to_probe)
        lock = threading.Lock()

        def worker():
            while True:
                with lock:
                    if not queue:
                        return
                    job = queue.pop()
                work(job)

        for _ in range(SCAN_THREADS):
            thread = threading.Thread(target=worker, daemon=True)
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join()

        # Anything we did not see this pass has been deleted or moved.
        for path in [p for p in index if p not in seen]:
            del index[path]

        library.replace_index(index, INDEX_VERSION, roots)
        return len(to_probe)
    finally:
        with _lock:
            _scan["running"] = False


def progress():
    with _lock:
        return dict(_scan)


def tracks(store, settings, folder="", query="", limit=2000, sort="title", media="",
           artist=None, album=None, genre=None):
    """The indexed library, filtered and sorted by the database."""
    if folder:
        folder = os.path.realpath(os.path.expanduser(folder))
    return store.library.tracks(media=media if media in ("audio", "video") else "",
                                query=query, sort=sort, limit=limit, artist=artist,
                                album=album, genre=genre, folder=folder or None)


def groups(store, by, media="audio", query=""):
    """Albums, artists, genres or folders in the index, with counts."""
    return store.library.groups(by, media=media, query=query)


def _media_of(entry):
    media = (entry.get("extra") or {}).get("media")
    if media:
        return media
    path = str((entry.get("extra") or {}).get("path") or "")
    return "video" if path.lower().endswith(VIDEO_SUFFIXES) else "audio"


def folders(store, settings):
    return [{"path": g["name"], "count": g["count"],
             "name": textutil.text(os.path.basename(g["name"]) or g["name"], 120)}
            for g in store.library.groups("folder", media="")]


