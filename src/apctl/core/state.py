"""Atomic, symlink-proof persistence.

Every file we write is written to a temporary file in the same directory and
then renamed into place, with O_NOFOLLOW and 0600. A crash mid-write leaves
the previous good file, and a symlink planted in our data directory does not
give us a write anywhere else.
"""

import errno
import json
import os
import tempfile
import threading


def _ensure_dir(path):
    os.makedirs(path, mode=0o700, exist_ok=True)


def write_json(path, data):
    folder = os.path.dirname(path)
    _ensure_dir(folder)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".ap-", suffix=".json")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            # ensure_ascii keeps a lone surrogate from ever reaching the file,
            # and guarantees one JSON object is one line.
            json.dump(data, handle, separators=(",", ":"), ensure_ascii=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def read_json(path, fallback=None):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ELOOP):
            return fallback
        return fallback
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (ValueError, OSError):
        # A corrupt file degrades to "nothing here", never to a crash.
        return fallback


class Store:
    """The daemon's on-disk state."""

    def __init__(self, data_dir, cache_dir, runtime_dir):
        self.data = data_dir
        self.cache = cache_dir
        self.runtime = runtime_dir
        self.state_path = os.path.join(data_dir, "state.json")
        self.session_path = os.path.join(runtime_dir, "session.json")
        self.pid_path = os.path.join(runtime_dir, "player.pid")
        self.socket_path = os.path.join(runtime_dir, "mpv.sock")
        self.proxy_path = os.path.join(runtime_dir, "proxy.sock")
        self.list_cache = os.path.join(cache_dir, "lists.json")
        self.art_dir = os.path.join(cache_dir, "art")
        self.yt_dir = os.path.join(cache_dir, "yt")
        # The station/channel mirror. Separate from the local-music index:
        # one is replaced wholesale from the network, the other is built from
        # the user's disk, and sharing a file would make one clobber the other.
        self.catalogue_path = os.path.join(data_dir, "catalogue.db")
        self.library_path = os.path.join(data_dir, "library.db")
        self.downloads_dir = os.path.join(data_dir, "downloads")
        self._state = None
        # Saves come from several threads (settings, the library scan, the
        # YouTube budget) and json.dump walks the live dict, so two at once
        # could serialise a dict mid-change. One lock for every write.
        self._save_lock = threading.RLock()
        self._save_timer = None
        self._library = None
        self._library_lock = threading.Lock()

    def prepare(self):
        for path in (self.data, self.cache, self.runtime, self.art_dir,
                     self.yt_dir, self.downloads_dir):
            _ensure_dir(path)
        try:
            os.chmod(self.runtime, 0o700)
        except OSError:
            pass

    def load(self):
        data = read_json(self.state_path, {}) or {}
        if not isinstance(data, dict):
            data = {}
        self._state = data
        return data

    @property
    def library(self):
        """The SQLite library (favourites, history, playlists, the local
        index...), opened on first use. The first open moves those out of
        state.json, which then holds only settings and small bookkeeping."""
        with self._library_lock:
            if self._library is None:
                from .library import Library
                from ..sources.local import INDEX_VERSION
                library = Library(self.library_path).open()
                with self._save_lock:
                    if library.import_state(self.state, INDEX_VERSION):
                        self.save()
                self._library = library
            return self._library

    @property
    def state(self):
        if self._state is None:
            self.load()
        return self._state

    def save(self, data=None):
        with self._save_lock:
            if self._save_timer is not None:
                self._save_timer.cancel()
                self._save_timer = None
            if data is not None:
                self._state = data
            try:
                write_json(self.state_path, self._state or {})
            except (OSError, RuntimeError, ValueError):
                # Losing a preference is not worth interrupting playback for.
                pass

    def save_soon(self, delay=1.0):
        """Save once things go quiet.

        Dragging the volume sends a value per pixel, and every one of them used
        to rewrite and fsync the whole state file - which also holds the local
        library index - dozens of times a second.
        """
        with self._save_lock:
            if self._save_timer is not None:
                return
            timer = threading.Timer(delay, self.save)
            timer.daemon = True
            self._save_timer = timer
            timer.start()

    def flush(self):
        """Write anything a pending save_soon is still holding."""
        with self._save_lock:
            pending = self._save_timer is not None
        if pending:
            self.save()

    def save_session(self, payload):
        try:
            write_json(self.session_path, payload)
        except OSError:
            pass

    def load_session(self):
        data = read_json(self.session_path, {}) or {}
        return data if isinstance(data, dict) else {}

    def read_pid(self):
        data = read_json(self.pid_path, None)
        if not isinstance(data, dict):
            return None
        try:
            return int(data.get("pid")), str(data.get("start") or "")
        except (TypeError, ValueError):
            return None

    def write_pid(self, pid, start):
        try:
            write_json(self.pid_path, {"pid": int(pid), "start": str(start)})
        except OSError:
            pass

    @property
    def owner_path(self):
        return os.path.join(self.runtime, "owner.json")

    def write_owner(self, pid):
        """Record which daemon is in charge of the running player."""
        try:
            write_json(self.owner_path, {"pid": int(pid)})
        except OSError:
            pass

    def read_owner(self):
        data = read_json(self.owner_path, None)
        try:
            return int(data.get("pid")) if isinstance(data, dict) else None
        except (TypeError, ValueError):
            return None

    def clear_pid(self):
        try:
            os.unlink(self.pid_path)
        except OSError:
            pass


def process_start(pid):
    """Field 22 of /proc/<pid>/stat: the process start time in clock ticks.

    Stored alongside the pid so a recycled pid is never mistaken for our
    player. Parsed from the last ')' because comm may contain spaces and
    parentheses.
    """
    try:
        with open("/proc/%d/stat" % int(pid), "rb") as handle:
            raw = handle.read().decode("utf-8", "replace")
    except OSError:
        return ""
    try:
        return raw.rsplit(")", 1)[1].split()[19]
    except (IndexError, ValueError):
        return ""


def process_alive(pid, start):
    """True only when this exact process is still running."""
    if not pid:
        return False
    current = process_start(pid)
    if not current:
        return False
    if start and current != start:
        return False
    try:
        with open("/proc/%d/stat" % int(pid), "rb") as handle:
            raw = handle.read().decode("utf-8", "replace")
        state = raw.rsplit(")", 1)[1].split()[0]
        if state in ("Z", "X"):
            return False
    except OSError:
        return False
    return True
