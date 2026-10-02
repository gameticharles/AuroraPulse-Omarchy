"""The user's library, in SQLite: everything that is theirs rather than ours.

Favourites, history, resume points, play counts, playlists, the queue, the
download list and the index of their music folders. All of it used to live in
state.json, which is rewritten (and fsynced) whole on every change - so adding
one favourite rewrote a 20 000-track index, and a crash in the middle of a
large write could leave the previous file as the only good copy of all of it.

Here each change touches its own rows, WAL keeps readers off writers' toes,
and a corrupt or missing database degrades to an empty library, never a
daemon that will not start. The one-time import from state.json removes the
moved keys from that file only after the rows are committed.

Entries are stored as the same MediaItem JSON the panel already speaks; the
columns beside them exist only so SQL can filter and sort.
"""

import json
import os
import sqlite3
import threading
import time
import uuid

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS favorite (
    uid   TEXT PRIMARY KEY,
    added INTEGER NOT NULL,
    entry TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS history (
    uid       TEXT PRIMARY KEY,
    played_at INTEGER NOT NULL,
    entry     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS history_at ON history(played_at);
CREATE TABLE IF NOT EXISTS play (
    uid   TEXT PRIMARY KEY,
    count INTEGER NOT NULL DEFAULT 0,
    first INTEGER NOT NULL,
    last  INTEGER NOT NULL,
    entry TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS position (
    uid     TEXT PRIMARY KEY,
    seconds INTEGER NOT NULL,
    updated INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS queue (
    pos   INTEGER PRIMARY KEY,
    entry TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS download (
    id    TEXT PRIMARY KEY,
    added INTEGER NOT NULL,
    job   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS playlist (
    id      TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    created INTEGER NOT NULL,
    updated INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS playlist_item (
    playlist TEXT NOT NULL,
    pos      INTEGER NOT NULL,
    uid      TEXT NOT NULL,
    entry    TEXT NOT NULL,
    PRIMARY KEY (playlist, pos)
);
CREATE TABLE IF NOT EXISTS track (
    path    TEXT PRIMARY KEY,
    uid     TEXT NOT NULL,
    f       TEXT NOT NULL,
    added   INTEGER NOT NULL,
    seen    INTEGER NOT NULL,
    media   TEXT NOT NULL,
    title   TEXT NOT NULL,
    artist  TEXT NOT NULL,
    album   TEXT NOT NULL,
    genre   TEXT NOT NULL,
    folder  TEXT NOT NULL,
    entry   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS track_artist ON track(artist);
CREATE INDEX IF NOT EXISTS track_album ON track(album);
CREATE INDEX IF NOT EXISTS track_genre ON track(genre);
CREATE INDEX IF NOT EXISTS track_folder ON track(folder);
CREATE INDEX IF NOT EXISTS track_uid ON track(uid);
"""

# The smart playlists. Their contents are a query, not a list of rows, so
# they are always current and need no maintenance.
SMART = (
    ("smart:most", "Most played"),
    ("smart:recent", "Recently played"),
    ("smart:added", "Recently added"),
    ("smart:favorites", "Favourites"),
)

PLAYLIST_LIMIT = 5000
NAME_LIMIT = 80


def _dump(entry):
    return json.dumps(entry, separators=(",", ":"), ensure_ascii=True)


def _load(raw):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _track_columns(entry):
    extra = entry.get("extra") or {}
    path = str(extra.get("path") or "")
    media = extra.get("media") or ""
    if not media:
        from ..sources.local import VIDEO_SUFFIXES
        media = "video" if path.lower().endswith(VIDEO_SUFFIXES) else "audio"
    return {
        "uid": str(entry.get("uid") or ""),
        "media": media,
        "title": str(entry.get("title") or ""),
        "artist": str(entry.get("artist") or ""),
        "album": str(entry.get("album") or ""),
        "genre": str(extra.get("genre") or ""),
        "folder": str(extra.get("folder") or os.path.dirname(path)),
    }


class Library:
    def __init__(self, path, log=None):
        self.path = path
        self.log = log or (lambda message: None)
        self._lock = threading.RLock()
        self._conn = None

    # -- connection --------------------------------------------------------

    def open(self):
        with self._lock:
            if self._conn is not None:
                return self
            os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
            try:
                self._conn = self._connect()
            except sqlite3.DatabaseError as exc:
                # A damaged file is set aside, not deleted: it holds the
                # user's favourites, and might be recoverable by hand.
                self.log("library database unreadable (%s); starting a new one" % exc)
                try:
                    os.replace(self.path, self.path + ".broken-%d" % int(time.time()))
                except OSError:
                    pass
                self._conn = self._connect()
            return self

    def _connect(self):
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10,
                               isolation_level=None)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.executescript(SCHEMA)
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema', ?)",
                     (str(SCHEMA_VERSION),))
        conn.execute("SELECT count(*) FROM meta").fetchone()   # fails if corrupt
        return conn

    def close(self):
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None

    def _db(self):
        if self._conn is None:
            self.open()
        return self._conn

    def _rows(self, sql, args=()):
        with self._lock:
            return self._db().execute(sql, args).fetchall()

    def _write(self, fn):
        """Run fn(conn) in one transaction."""
        with self._lock:
            conn = self._db()
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn(conn)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            return result

    def get_meta(self, key, default=None):
        rows = self._rows("SELECT value FROM meta WHERE key = ?", (key,))
        return rows[0][0] if rows else default

    def set_meta(self, key, value):
        self._write(lambda c: c.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, str(value))))

    # -- migration from state.json ----------------------------------------

    MOVED_KEYS = ("favorites", "history", "positions", "local", "last", "queue",
                  "downloads")

    def import_state(self, state, index_version):
        """Move the library out of the state.json dict, once.

        Returns True when keys were moved and the caller should save the
        (now smaller) state. The local index is taken only when it was built
        by the current parser; an older one is simply rebuilt by the next scan.
        """
        if self.get_meta("imported") == "1" or not isinstance(state, dict):
            return False
        now = int(time.time())
        favorites = [e for e in state.get("favorites") or [] if isinstance(e, dict)]
        history = [e for e in state.get("history") or [] if isinstance(e, dict)]
        positions = state.get("positions") or {}
        local = state.get("local") or {}
        index = local.get("index") if local.get("index_version") == index_version else None
        last = state.get("last") or {}

        def run(conn):
            for n, entry in enumerate(favorites):
                if entry.get("uid"):
                    conn.execute("INSERT OR IGNORE INTO favorite VALUES (?, ?, ?)",
                                 (entry["uid"], now - n, _dump(entry)))
            for n, entry in enumerate(history):
                if entry.get("uid"):
                    at = int((entry.get("extra") or {}).get("playedAt") or now - n)
                    conn.execute("INSERT OR IGNORE INTO history VALUES (?, ?, ?)",
                                 (entry["uid"], at, _dump(entry)))
                    conn.execute("INSERT OR IGNORE INTO play VALUES (?, 1, ?, ?, ?)",
                                 (entry["uid"], at, at, _dump(entry)))
            if isinstance(positions, dict):
                for uid, seconds in positions.items():
                    try:
                        conn.execute("INSERT OR REPLACE INTO position VALUES (?, ?, ?)",
                                     (str(uid), int(seconds), now))
                    except (TypeError, ValueError):
                        pass
            if isinstance(index, dict):
                self._replace_index(conn, index, now)
                conn.execute("INSERT OR REPLACE INTO meta VALUES ('index_version', ?)",
                             (str(index_version),))
                conn.execute("INSERT OR REPLACE INTO meta VALUES ('roots', ?)",
                             (json.dumps(local.get("roots") or []),))
                conn.execute("INSERT OR REPLACE INTO meta VALUES ('scanned_at', ?)",
                             (str(int(local.get("scanned_at") or 0)),))
            queue_ = [e for e in last.get("queue") or [] if isinstance(e, dict)]
            for pos, entry in enumerate(queue_):
                conn.execute("INSERT OR REPLACE INTO queue VALUES (?, ?)", (pos, _dump(entry)))
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('queue_index', ?)",
                         (str(int(last.get("index") or 0)),))
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('imported', '1')")

        self._write(run)
        for key in self.MOVED_KEYS:
            state.pop(key, None)
        return True

    # -- favourites --------------------------------------------------------

    def favorites(self, limit=500):
        rows = self._rows("SELECT entry FROM favorite ORDER BY added DESC LIMIT ?", (limit,))
        return [e for e in (_load(r[0]) for r in rows) if e]

    def is_favorite(self, uid):
        return bool(uid) and bool(self._rows("SELECT 1 FROM favorite WHERE uid = ?", (uid,)))

    def set_favorite(self, entry, on):
        uid = entry.get("uid")
        if not uid:
            return

        def run(conn):
            conn.execute("DELETE FROM favorite WHERE uid = ?", (uid,))
            if on:
                conn.execute("INSERT INTO favorite VALUES (?, ?, ?)",
                             (uid, time.time_ns() // 1000, _dump(entry)))
        self._write(run)

    # -- history and play counts ------------------------------------------

    def history(self, limit=100):
        rows = self._rows("SELECT entry FROM history ORDER BY played_at DESC LIMIT ?",
                          (limit,))
        return [e for e in (_load(r[0]) for r in rows) if e]

    def played(self, entry, keep=100):
        """Note a play: the history row, and the count smart playlists use."""
        uid = entry.get("uid")
        if not uid:
            return
        now = int(time.time())
        raw = _dump(entry)

        def run(conn):
            conn.execute("INSERT OR REPLACE INTO history VALUES (?, ?, ?)", (uid, now, raw))
            conn.execute("DELETE FROM history WHERE uid NOT IN (SELECT uid FROM history "
                         "ORDER BY played_at DESC LIMIT ?)", (keep,))
            conn.execute("INSERT INTO play VALUES (?, 1, ?, ?, ?) ON CONFLICT(uid) DO UPDATE "
                         "SET count = count + 1, last = excluded.last, entry = excluded.entry",
                         (uid, now, now, raw))
        self._write(run)

    def forget(self, uid=""):
        """Drop one history entry, or all of it. Play counts stay: clearing
        the visible history is not the same as resetting "Most played"."""
        if uid:
            self._write(lambda c: c.execute("DELETE FROM history WHERE uid = ?", (uid,)))
        else:
            self._write(lambda c: c.execute("DELETE FROM history"))

    def play_count(self, uid):
        rows = self._rows("SELECT count FROM play WHERE uid = ?", (uid,))
        return int(rows[0][0]) if rows else 0

    # -- resume points -----------------------------------------------------

    def position(self, uid):
        rows = self._rows("SELECT seconds FROM position WHERE uid = ?", (uid,))
        return float(rows[0][0]) if rows else 0.0

    def set_position(self, uid, seconds, keep=200):
        if not uid:
            return

        def run(conn):
            if seconds is None:
                conn.execute("DELETE FROM position WHERE uid = ?", (uid,))
                return
            conn.execute("INSERT OR REPLACE INTO position VALUES (?, ?, ?)",
                         (uid, int(seconds), int(time.time())))
            conn.execute("DELETE FROM position WHERE uid NOT IN (SELECT uid FROM position "
                         "ORDER BY updated DESC LIMIT ?)", (keep,))
        self._write(run)

    # -- queue -------------------------------------------------------------

    def save_queue(self, entries, index):
        def run(conn):
            conn.execute("DELETE FROM queue")
            conn.executemany("INSERT INTO queue VALUES (?, ?)",
                             [(pos, _dump(e)) for pos, e in enumerate(entries)])
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('queue_index', ?)",
                         (str(int(index)),))
        self._write(run)

    def load_queue(self):
        rows = self._rows("SELECT entry FROM queue ORDER BY pos")
        entries = [e for e in (_load(r[0]) for r in rows) if e]
        try:
            index = int(self.get_meta("queue_index", 0) or 0)
        except ValueError:
            index = 0
        return entries, max(0, min(index, len(entries) - 1)) if entries else 0

    # -- downloads ---------------------------------------------------------

    def save_downloads(self, jobs):
        def run(conn):
            conn.execute("DELETE FROM download")
            conn.executemany("INSERT INTO download VALUES (?, ?, ?)",
                             [(j["id"], int(j.get("added") or 0), _dump(j))
                              for j in jobs if j.get("id")])
        self._write(run)

    def load_downloads(self):
        rows = self._rows("SELECT job FROM download ORDER BY added DESC")
        return [j for j in (_load(r[0]) for r in rows) if j]

    # -- playlists ---------------------------------------------------------

    def playlists(self):
        """User playlists then the smart ones, each with a count."""
        rows = self._rows(
            "SELECT p.id, p.name, p.updated, count(i.uid) FROM playlist p "
            "LEFT JOIN playlist_item i ON i.playlist = p.id "
            "GROUP BY p.id ORDER BY lower(p.name)")
        out = [{"id": r[0], "name": r[1], "updated": r[2], "count": r[3], "smart": False}
               for r in rows]
        for ident, name in SMART:
            out.append({"id": ident, "name": name, "updated": 0, "smart": True,
                        "count": len(self.playlist_items(ident, limit=100))})
        return out

    def create_playlist(self, name):
        name = str(name or "").strip()[:NAME_LIMIT] or "New playlist"
        ident = uuid.uuid4().hex[:12]
        now = int(time.time())
        self._write(lambda c: c.execute("INSERT INTO playlist VALUES (?, ?, ?, ?)",
                                        (ident, name, now, now)))
        return {"id": ident, "name": name}

    def rename_playlist(self, ident, name):
        name = str(name or "").strip()[:NAME_LIMIT]
        if not name:
            return False
        return self._write(lambda c: c.execute(
            "UPDATE playlist SET name = ?, updated = ? WHERE id = ?",
            (name, int(time.time()), ident)).rowcount) > 0

    def delete_playlist(self, ident):
        def run(conn):
            conn.execute("DELETE FROM playlist_item WHERE playlist = ?", (ident,))
            return conn.execute("DELETE FROM playlist WHERE id = ?", (ident,)).rowcount
        return self._write(run) > 0

    def playlist_name(self, ident):
        for smart_id, name in SMART:
            if ident == smart_id:
                return name
        rows = self._rows("SELECT name FROM playlist WHERE id = ?", (ident,))
        return rows[0][0] if rows else ""

    def playlist_items(self, ident, limit=PLAYLIST_LIMIT):
        if ident == "smart:most":
            rows = self._rows("SELECT entry FROM play WHERE count > 1 "
                              "ORDER BY count DESC, last DESC LIMIT ?", (limit,))
        elif ident == "smart:recent":
            rows = self._rows("SELECT entry FROM history ORDER BY played_at DESC LIMIT ?",
                              (limit,))
        elif ident == "smart:added":
            # Thirty days of new files in the music folders - downloads and
            # recordings included, since those land there too.
            since = int(time.time()) - 30 * 86400
            rows = self._rows("SELECT entry FROM track WHERE added >= ? "
                              "ORDER BY added DESC LIMIT ?", (since, limit))
        elif ident == "smart:favorites":
            rows = self._rows("SELECT entry FROM favorite ORDER BY added DESC LIMIT ?",
                              (limit,))
        else:
            rows = self._rows("SELECT entry FROM playlist_item WHERE playlist = ? "
                              "ORDER BY pos LIMIT ?", (ident, limit))
        return [e for e in (_load(r[0]) for r in rows) if e]

    def _exists(self, conn, ident):
        return conn.execute("SELECT 1 FROM playlist WHERE id = ?", (ident,)).fetchone()

    def add_to_playlist(self, ident, entries):
        """Append; an entry already in the playlist is not added twice.
        Returns how many were added."""
        def run(conn):
            if not self._exists(conn, ident):
                raise KeyError(ident)
            have = {r[0] for r in conn.execute(
                "SELECT uid FROM playlist_item WHERE playlist = ?", (ident,))}
            top = conn.execute("SELECT coalesce(max(pos), -1) FROM playlist_item "
                               "WHERE playlist = ?", (ident,)).fetchone()[0]
            added = 0
            for entry in entries:
                uid = entry.get("uid")
                if not uid or uid in have or top + 1 >= PLAYLIST_LIMIT:
                    continue
                top += 1
                have.add(uid)
                conn.execute("INSERT INTO playlist_item VALUES (?, ?, ?, ?)",
                             (ident, top, uid, _dump(entry)))
                added += 1
            conn.execute("UPDATE playlist SET updated = ? WHERE id = ?",
                         (int(time.time()), ident))
            return added
        return self._write(run)

    def _rewrite_items(self, conn, ident, entries):
        conn.execute("DELETE FROM playlist_item WHERE playlist = ?", (ident,))
        conn.executemany("INSERT INTO playlist_item VALUES (?, ?, ?, ?)",
                         [(ident, pos, e["uid"], _dump(e)) for pos, e in enumerate(entries)])
        conn.execute("UPDATE playlist SET updated = ? WHERE id = ?", (int(time.time()), ident))

    def remove_from_playlist(self, ident, uid):
        def run(conn):
            if not self._exists(conn, ident):
                raise KeyError(ident)
            rows = conn.execute("SELECT entry FROM playlist_item WHERE playlist = ? "
                                "ORDER BY pos", (ident,)).fetchall()
            entries = [e for e in (_load(r[0]) for r in rows) if e and e.get("uid") != uid]
            self._rewrite_items(conn, ident, entries)
        self._write(run)

    def move_in_playlist(self, ident, src, dst):
        def run(conn):
            if not self._exists(conn, ident):
                raise KeyError(ident)
            rows = conn.execute("SELECT entry FROM playlist_item WHERE playlist = ? "
                                "ORDER BY pos", (ident,)).fetchall()
            entries = [e for e in (_load(r[0]) for r in rows) if e]
            if not 0 <= src < len(entries):
                return
            moved = entries.pop(src)
            entries.insert(max(0, min(dst, len(entries))), moved)
            self._rewrite_items(conn, ident, entries)
        self._write(run)

    def playlists_with(self, uid):
        return [r[0] for r in self._rows(
            "SELECT DISTINCT playlist FROM playlist_item WHERE uid = ?", (uid,))]

    # -- the local index ---------------------------------------------------

    def index_info(self):
        try:
            roots = json.loads(self.get_meta("roots", "[]") or "[]")
        except ValueError:
            roots = []
        return {"index_version": int(self.get_meta("index_version", 0) or 0),
                "scanned_at": int(self.get_meta("scanned_at", 0) or 0),
                "roots": roots}

    def index(self):
        """{path: entry} with each entry's fingerprint and times, for a scan."""
        out = {}
        for path, f, seen, added, raw in self._rows(
                "SELECT path, f, seen, added, entry FROM track"):
            entry = _load(raw)
            if entry:
                entry["f"], entry["seen"], entry["added"] = f, seen, added
                out[path] = entry
        return out

    def _replace_index(self, conn, index, now):
        old = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT path, f, added FROM track")}
        for path in [p for p in old if p not in index]:
            conn.execute("DELETE FROM track WHERE path = ?", (path,))
        for path, entry in index.items():
            if not isinstance(entry, dict):
                continue
            f = str(entry.get("f") or "")
            seen = int(entry.get("seen") or now)
            known = old.get(path)
            if known and known[0] == f:
                conn.execute("UPDATE track SET seen = ? WHERE path = ?", (seen, path))
                continue
            added = known[1] if known else int(entry.get("added") or now)
            clean = {k: v for k, v in entry.items() if k not in ("f", "seen", "added")}
            cols = _track_columns(clean)
            conn.execute(
                "INSERT OR REPLACE INTO track VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (path, cols["uid"], f, added, seen, cols["media"], cols["title"],
                 cols["artist"], cols["album"], cols["genre"], cols["folder"],
                 _dump(clean)))

    def replace_index(self, index, version, roots):
        now = int(time.time())

        def run(conn):
            self._replace_index(conn, index, now)
            for key, value in (("index_version", str(version)),
                               ("roots", json.dumps(list(roots))),
                               ("scanned_at", str(now))):
                conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
        self._write(run)

    def clear_index(self):
        self._write(lambda c: c.execute("DELETE FROM track"))

    def _track_where(self, media="", query="", artist=None, album=None, genre=None,
                     folder=None):
        where, args = [], []
        if media in ("audio", "video"):
            where.append("media = ?")
            args.append(media)
        if query:
            for word in str(query).lower().split()[:6]:
                where.append("(lower(title) LIKE ? OR lower(artist) LIKE ? "
                             "OR lower(album) LIKE ?)")
                args += ["%" + word.replace("%", "") + "%"] * 3
        for column, value in (("artist", artist), ("album", album),
                              ("genre", genre), ("folder", folder)):
            if value is not None:
                where.append("%s = ?" % column)
                args.append(value)
        return (" WHERE " + " AND ".join(where)) if where else "", args

    def tracks(self, media="", query="", sort="title", limit=2000, artist=None,
               album=None, genre=None, folder=None):
        where, args = self._track_where(media, query, artist, album, genre, folder)
        order = {
            "artist": "lower(artist), lower(album), lower(title)",
            "album": "lower(album), lower(title)",
            "added": "added DESC, lower(title)",
        }.get(sort, "lower(title), lower(artist)")
        if album is not None:
            # An album plays in its own order: by track number when the tags
            # have one (it is kept in the file name order otherwise).
            order = "path"
        rows = self._rows("SELECT entry FROM track%s ORDER BY %s LIMIT ?" % (where, order),
                          args + [int(limit)])
        return [e for e in (_load(r[0]) for r in rows) if e]

    def count_tracks(self, media="", query="", artist=None, album=None, genre=None,
                     folder=None):
        where, args = self._track_where(media, query, artist, album, genre, folder)
        return int(self._rows("SELECT count(*) FROM track" + where, args)[0][0])

    def groups(self, by, media="audio", query=""):
        """Albums, artists, genres or folders, each with a count and a cover."""
        column = {"album": "album", "artist": "artist", "genre": "genre",
                  "folder": "folder"}.get(by)
        if not column:
            return []
        where, args = self._track_where(media, query)
        extra = "%s %s <> ''" % ("AND" if where else "WHERE", column)
        rows = self._rows(
            "SELECT %s, count(*), max(artist), min(entry) FROM track%s %s "
            "GROUP BY %s ORDER BY lower(%s)" % (column, where, extra, column, column), args)
        out = []
        for name, count, artist, raw in rows:
            sample = _load(raw) or {}
            out.append({"name": name, "count": int(count),
                        "artist": artist if by == "album" else "",
                        "art": sample.get("art") or {}})
        return out
