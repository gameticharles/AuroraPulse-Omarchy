"""Artwork, fetched off the request path.

A browse of 50 stations took 32 seconds and returned no pictures, because each
item blocked on its favicon before the list was sent. Fetching fifty images
inline is the wrong shape for two reasons at once: it makes the list slow, and
it makes it all-or-nothing - one dead host costs the whole page.

So an item goes out immediately with whatever is already cached, and the
downloads happen on a small pool behind it. When one lands, the daemon emits an
`art` event for that item and the shell repaints the row. The list is usable
in 15ms and fills in.
"""

import os
import queue
import threading
import time

MAX_WORKERS = 6
QUEUE_LIMIT = 2000
STALE_SECONDS = 7 * 24 * 3600

# A failed host is not retried for a while; otherwise one dead CDN costs a
# request per browse, forever.
BACKOFF_SECONDS = 6 * 3600


class _Job:
    __slots__ = ("key", "url", "path", "fails")

    def __init__(self, key, url, path):
        self.key = key
        self.url = url
        self.path = path
        self.fails = 0


class Artwork:
    """A background fetcher with a persistent cache.

    One instance per daemon. The cache on disk is the source of truth across
    restarts; this holds only the in-flight queue and the backoff table.
    """

    def __init__(self, cache_dir, on_ready=None, log=None):
        self.cache_dir = cache_dir
        self.on_ready = on_ready or (lambda key, path: None)
        self.log = log or (lambda message: None)
        self._queue = queue.Queue(maxsize=QUEUE_LIMIT)
        self._workers = []
        self._seen = {}
        self._backoff = {}
        self._seen_lock = threading.Lock()
        self._stop = threading.Event()
        self._in_flight = set()

    # -- lifecycle ---------------------------------------------------------

    def start(self, workers=MAX_WORKERS):
        if self._workers:
            return
        os.makedirs(self.cache_dir, mode=0o700, exist_ok=True)
        for index in range(workers):
            thread = threading.Thread(target=self._work, name="ap-art%d" % index,
                                      daemon=True)
            thread.start()
            self._workers.append(thread)

    def shutdown(self, timeout=2.0):
        self._stop.set()
        for _ in self._workers:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass
        for thread in self._workers:
            thread.join(timeout=timeout)
        self._workers = []

    # -- public ------------------------------------------------------------

    def path_for(self, key):
        """The cached file for a key, or "" if we have nothing yet."""
        if not key:
            return ""
        return os.path.join(self.cache_dir, "%s.img" % key)

    def submit(self, key, url, slug_key=None):
        """Ask for artwork. Returns the cached path immediately, if any.

        The key is the caller's stable identifier; slug_key is the
        filename-safe form. A submit for something already queued or already
        on disk is a no-op, so callers do not have to track state.
        """
        if not key or not url:
            return ""
        if not (url.startswith("http://") or url.startswith("https://")):
            return ""
        name = slug_key or key
        target = self.path_for(name)
        if os.path.exists(target):
            return target

        with self._seen_lock:
            if key in self._seen:
                return ""
            if self._backoff.get(key, 0) > time.time():
                return ""
            self._seen[key] = True
        try:
            # The job carries the caller's key, not the filename: the panel
            # matches the `art` event against the key it was given, and the
            # filename is lower-cased - so a YouTube id (case matters) never
            # matched and every video kept its initials.
            self._queue.put_nowait(_Job(key, url, target))
        except queue.Full:
            # The queue is only full if the network is far behind; the list
            # still renders, the art just never arrives this time.
            with self._seen_lock:
                self._seen.pop(key, None)
        return ""

    def is_queued(self, key):
        with self._seen_lock:
            return key in self._seen

    # -- worker ------------------------------------------------------------

    def _work(self):
        from ..core.resolver import fetch_art
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if job is None:
                break
            if os.path.exists(job.path):
                self._forget(job.key)
                continue
            try:
                saved = fetch_art(job.url, self.cache_dir, job.key)
            except Exception as exc:
                self.log("artwork failed for %s: %s" % (job.key, exc))
                saved = None
            self._forget(job.key)
            if saved:
                self.on_ready(job.key, saved)
            else:
                with self._seen_lock:
                    self._backoff[job.key] = time.time() + BACKOFF_SECONDS

    def _forget(self, key):
        with self._seen_lock:
            self._seen.pop(key, None)

    # -- housekeeping ------------------------------------------------------

    def sweep(self):
        """Drop cached images nothing has used in a week.

        The cache is otherwise unbounded: a user who browses widely ends up
        with tens of thousands of small files.
        """
        removed = 0
        cutoff = time.time() - STALE_SECONDS
        try:
            names = os.listdir(self.cache_dir)
        except OSError:
            return 0
        for name in names:
            path = os.path.join(self.cache_dir, name)
            try:
                if name.endswith(".img") and os.path.getmtime(path) < cutoff:
                    os.unlink(path)
                    removed += 1
            except OSError:
                continue
        return removed
