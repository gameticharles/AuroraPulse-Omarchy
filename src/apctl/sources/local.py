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
INDEX_VERSION = 5

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
             "genre,date:stream=codec_name,codec_type,channels,sample_rate:"
             "stream_tags=artist,album,title,album_artist,genre,date",
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
    # Ogg Vorbis and Opus keep their tags on the audio stream, not the file:
    # read only from the format, every such file was listed by its name.
    for stream in data.get("streams") or []:
        if stream.get("codec_type") != "audio":
            continue
        stags = stream.get("tags") or {}
        for key in ("artist", "album", "title", "album_artist", "genre", "date"):
            value = stags.get(key) or stags.get(key.upper())
            if isinstance(value, str) and value.strip() and not out.get(key):
                out[key] = value.strip()
        break
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
    # Every new file is opened by ffprobe and ffmpeg, which only ever run
    # sandboxed. Without bubblewrap the scan stops here, before the index is
    # touched, and says what to install.
    sandbox.require()
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


# -- a folder picked to play ------------------------------------------------

# A picked folder can be anywhere, not only under the music folders, so it is
# listed straight from the disk. Files the library already knows reuse its
# entry; the rest are read with ffprobe, up to this many per folder, and
# beyond that named after the file so a huge folder still opens quickly.
FOLDER_MAX_FILES = 3000
FOLDER_PROBE_MAX = 400
_probed = {}
_probed_lock = threading.Lock()


def _natural(path):
    """Sort key that puts "2 Song" before "10 Song"."""
    import re
    return [int(part) if part.isdigit() else part.lower()
            for part in re.split(r"(\d+)", path)]


def _quick(path):
    """An entry from the file name alone, for files past the probe limit."""
    is_video = path.lower().endswith(VIDEO_SUFFIXES)
    folder = os.path.dirname(path)
    return item(
        "local", path, os.path.splitext(os.path.basename(path))[0], url=path,
        kind="track", album=textutil.text(os.path.basename(folder), 200),
        art={"url": "", "path": _cover(path) or ""},
        extra={"path": path, "folder": folder, "lrc": _sidecar(path),
               "media": "video" if is_video else "audio"})


def folder_files(path, settings=None, limit=FOLDER_MAX_FILES):
    """The playable files in a folder and every folder below it, in name
    order. Hidden folders are skipped."""
    # An empty path would resolve to the daemon's own working directory.
    raw = str(path or "").strip()
    root = os.path.realpath(os.path.expanduser(raw)) if raw else ""
    if not root or root == "/" or not os.path.isdir(root):
        raise SourceError("that folder does not exist", "not-found")
    suffixes = extensions(settings) + VIDEO_SUFFIXES
    follow = bool((settings or {}).get("localFollowSymlinks"))
    found = []
    visited = set()
    for directory, dirs, files in os.walk(root, followlinks=follow):
        real = os.path.realpath(directory)
        if real in visited:
            dirs[:] = []
            continue
        visited.add(real)
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if not name.lower().endswith(suffixes) or name.startswith("."):
                continue
            candidate = os.path.join(directory, name)
            if os.path.islink(candidate) and not follow:
                continue
            try:
                if not os.path.isfile(candidate) or \
                        os.path.getsize(candidate) > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            found.append(candidate)
            if len(found) >= limit:
                break
        if len(found) >= limit:
            break
    found.sort(key=_natural)
    return root, found


def folder_tracks(store, settings, path, limit=FOLDER_MAX_FILES):
    """Everything playable in a folder, ready to play: the library's entries
    where it has them, freshly read tags for the rest."""
    root, paths = folder_files(path, settings, limit)
    known = store.library.entries_under(root)
    out = {}
    to_probe = []
    for candidate in paths:
        fingerprint = _index_key(store, candidate)
        entry = known.get(candidate)
        if entry is not None and entry.get("f") == fingerprint:
            out[candidate] = entry
            continue
        with _probed_lock:
            cached = _probed.get(candidate)
        if cached and cached[0] == fingerprint:
            out[candidate] = cached[1]
        elif len(to_probe) < FOLDER_PROBE_MAX:
            to_probe.append((candidate, fingerprint))
        else:
            out[candidate] = _quick(candidate)

    if to_probe:
        try:
            sandbox.require()
            can_probe = True
        except Exception:  # noqa: BLE001 - no bubblewrap: names only
            can_probe = False
        queue = list(to_probe)
        lock = threading.Lock()

        def worker():
            while True:
                with lock:
                    if not queue:
                        return
                    candidate, fingerprint = queue.pop()
                try:
                    built = _build(candidate, store.art_dir) if can_probe \
                        else _quick(candidate)
                except Exception:  # noqa: BLE001
                    built = _quick(candidate)
                with lock:
                    out[candidate] = built
                if can_probe:
                    with _probed_lock:
                        _probed[candidate] = (fingerprint, built)

        threads = [threading.Thread(target=worker, daemon=True)
                   for _ in range(SCAN_THREADS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        with _probed_lock:
            while len(_probed) > 5000:
                _probed.pop(next(iter(_probed)))

    rows = []
    for candidate in paths:
        entry = out.get(candidate)
        if entry:
            rows.append({k: v for k, v in entry.items()
                         if k not in ("f", "seen", "added")})
    return root, rows




# -- editing tags ------------------------------------------------------------

TAG_FIELDS = ("title", "artist", "album", "album_artist", "genre", "date", "track")
# Containers ffmpeg can embed a cover picture in. Anything else gets the
# picture beside it as "<file name>.jpg", which is where _cover looks first.
EMBED_ART = (".mp3", ".m4a", ".mp4", ".m4b", ".flac")


def write_tags(store, settings, path, fields, art_path="", staging=""):
    """Rewrite a file's tags (and optionally its cover) without re-encoding.

    ffmpeg reads the file and the picture and writes the result into a
    private staging folder: it never gets write access to the music folder.
    The finished file then replaces the original from here, and the library
    entry is read again. Returns the fresh entry.
    """
    raw = str(path or "").strip()
    path = os.path.realpath(os.path.expanduser(raw)) if raw else ""
    if not path or not os.path.isfile(path):
        raise SourceError("that file no longer exists", "not-found")
    if not path.lower().endswith(extensions(settings) + VIDEO_SUFFIXES):
        raise SourceError("only music and video files can be tagged", "unsupported")
    folder = os.path.dirname(path)
    if not os.access(folder, os.W_OK) or not os.access(path, os.W_OK):
        raise SourceError("that file's folder is read-only", "denied")
    art = ""
    if art_path:
        art = os.path.realpath(os.path.expanduser(str(art_path)))
        if not os.path.isfile(art) or not art.lower().endswith((".jpg", ".jpeg", ".png")):
            raise SourceError("the cover has to be a JPEG or PNG picture", "unsupported")
        if os.path.getsize(art) > 8 << 20:
            raise SourceError("the cover picture is larger than 8 MB", "unsupported")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SourceError("ffmpeg is needed to edit tags", "unsupported")
    sandbox.require()

    ext = os.path.splitext(path)[1].lower()
    embed = bool(art) and ext in EMBED_ART
    staging = staging or os.path.join(store.runtime, "tags")
    os.makedirs(staging, mode=0o700, exist_ok=True)
    out = os.path.join(staging, "tagged%s" % ext)
    if os.path.exists(out):
        os.remove(out)
    argv = [ffmpeg, "-v", "error", "-y", "-i", path]
    if embed:
        argv += ["-i", art, "-map", "0:a", "-map", "1:v", "-disposition:v:0", "attached_pic",
                 "-metadata:s:v", "title=Album cover", "-metadata:s:v", "comment=Cover (front)"]
        if ext == ".mp3":
            argv += ["-id3v2_version", "3"]
    else:
        argv += ["-map", "0"]
    argv += ["-c", "copy", "-map_metadata", "0"]
    for key in TAG_FIELDS:
        if key in (fields or {}):
            value = textutil.text((fields or {}).get(key), 300)
            argv += ["-metadata", "%s=%s" % (key, value)]
    argv.append(out)
    try:
        proc = sandbox.run_tool(argv, read=[path] + ([art] if embed else []),
                                write=[staging], timeout=180)
    except subprocess.TimeoutExpired as exc:
        raise SourceError("writing the tags took too long", "expired") from exc
    if proc.returncode != 0 or not os.path.isfile(out) or os.path.getsize(out) == 0:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise SourceError("could not write the tags: %s" % (detail[-1] if detail else "ffmpeg failed"),
                          "unsupported")
    # Copied next to the original and swapped in one rename, so a failure
    # halfway leaves the original untouched.
    temp = os.path.join(folder, ".%s.aurora-tags%s" % (os.path.basename(path), ext))
    try:
        shutil.copyfile(out, temp)
        shutil.copymode(path, temp)
        os.replace(temp, path)
    finally:
        for leftover in (temp, out):
            if os.path.exists(leftover):
                os.remove(leftover)
    if art and not embed:
        stem = os.path.splitext(path)[0]
        shutil.copyfile(art, stem + (".png" if art.lower().endswith(".png") else ".jpg"))
    # The old thumbnail would otherwise win over the new cover.
    import hashlib
    thumb = os.path.join(store.art_dir, "local-%s.jpg"
                         % hashlib.sha1(path.encode("utf-8", "replace")).hexdigest()[:16])
    if os.path.exists(thumb):
        os.remove(thumb)
    entry = _build(path, store.art_dir)
    fingerprint = _index_key(store, path)
    entry["f"] = fingerprint
    store.library.update_track(path, entry, fingerprint)
    with _probed_lock:
        _probed.pop(path, None)
    return {k: v for k, v in entry.items() if k not in ("f", "seen", "added")}
