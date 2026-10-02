"""A local catalogue of stations and channels.

The problem this solves: Radio Browser holds tens of thousands of stations and
iptv-org holds tens of thousands of channels, and asking a remote API for
them on every browse is slow, rate-limited, and offline-hostile. The original
AuroraPulse ships a 34,000-line station file for the same reason.

So we mirror both into SQLite once, keep them fresh in the background, and
answer every browse, search and filter from the local copy. A query that was
answered from a hot cache and a query answered from a cold one return the same
rows; the difference is whether anyone noticed a pause.

FTS5 gives the search. A station named "Radio Paradise Main Mix" is found by
any of its words, and a typo still finds it, which `LIKE '%paradise%'` does
not.
"""

import os
import re
import sqlite3
import threading
import time

SCHEMA_VERSION = 3

# Radio Browser: 3 days between full rebuilds is generous; the upstream
# dataset changes slowly and a rebuild is tens of megabytes.
RADIO_FULL_DAYS = 3
RADIO_INCREMENTAL_MINUTES = 60
TV_REFRESH_HOURS = 24
EPG_REFRESH_HOURS = 12

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS radio (
    uuid        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    url         TEXT NOT NULL,
    url_resolved TEXT,
    homepage    TEXT,
    favicon     TEXT,
    codec       TEXT,
    bitrate     INTEGER DEFAULT 0,
    country     TEXT,
    countrycode TEXT,
    state       TEXT,
    language    TEXT,
    tags        TEXT,
    votes       INTEGER DEFAULT 0,
    clicks      INTEGER DEFAULT 0,
    hls         INTEGER DEFAULT 0,
    codec_ok    INTEGER DEFAULT 1,
    checked     INTEGER DEFAULT 0,
    changed     INTEGER DEFAULT 0,
    updated     INTEGER DEFAULT 0
);
-- One row per (channel, category). iptv-org publishes several categories per
-- channel and we were keeping only the first, so a channel filed under
-- "news, music, sports" was reachable only through whichever one happened to
-- sort first. Same reasoning as radio_tag: a filter over a single delimited
-- column cannot be exact, and cannot answer how many channels a category has.
CREATE TABLE IF NOT EXISTS channel_tag (
    id  TEXT NOT NULL,
    tag TEXT NOT NULL,
    PRIMARY KEY (id, tag)
);
CREATE INDEX IF NOT EXISTS channel_tag_tag ON channel_tag(tag);

-- One row per (station, tag). Tags arrive as one comma-separated string per
-- station, which cannot answer "filter by genre" or "how many stations are in
-- this genre" without substring matching - and a substring match on tags finds
-- "pop" inside "hip hop" and "pop rock", so a genre filter built that way
-- quietly returns the wrong stations. This is the facet table that makes the
-- filter exact and the counts honest.
CREATE TABLE IF NOT EXISTS radio_tag (
    uuid TEXT NOT NULL,
    tag  TEXT NOT NULL,
    PRIMARY KEY (uuid, tag)
);
CREATE INDEX IF NOT EXISTS radio_tag_tag ON radio_tag(tag);

-- One row per (station, tag). Tags arrive as one comma-separated string per
-- station, which cannot answer "filter by genre" or "how many stations are in
-- this genre" without substring matching - and a substring match on tags finds
-- "pop" inside "hip hop" and "pop rock", so a genre filter built that way
-- quietly returns the wrong stations. This is the facet table that makes the
-- filter exact and the counts honest.
CREATE TABLE IF NOT EXISTS radio_tag (
    uuid TEXT NOT NULL,
    tag  TEXT NOT NULL,
    PRIMARY KEY (uuid, tag)
);
CREATE INDEX IF NOT EXISTS radio_tag_tag ON radio_tag(tag);

-- Extra links for a station. The mirror lists one URL per station and any one
-- of them can be the dead one; these are the alternates worth trying before
-- telling a user the station does not work.
CREATE TABLE IF NOT EXISTS radio_link (
    uuid TEXT NOT NULL,
    url  TEXT NOT NULL,
    pos  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (uuid, url)
);
CREATE INDEX IF NOT EXISTS radio_link_pos ON radio_link(uuid, pos);

CREATE INDEX IF NOT EXISTS radio_votes  ON radio(votes DESC, clicks DESC);
CREATE INDEX IF NOT EXISTS radio_cc     ON radio(countrycode);
CREATE INDEX IF NOT EXISTS radio_bitrate ON radio(bitrate);

CREATE TABLE IF NOT EXISTS channel (
    id       TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    url      TEXT NOT NULL,
    logo     TEXT,
    group_title TEXT,
    country  TEXT,
    language TEXT,
    is_nsfw  INTEGER DEFAULT 0,
    closed   INTEGER DEFAULT 0,
    updated  INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS channel_group ON channel(group_title);
CREATE INDEX IF NOT EXISTS channel_country ON channel(country);

CREATE TABLE IF NOT EXISTS programme (
    channel_id TEXT NOT NULL,
    start_at   INTEGER NOT NULL,
    stop_at    INTEGER NOT NULL,
    title      TEXT,
    description TEXT,
    PRIMARY KEY (channel_id, start_at)
);
CREATE INDEX IF NOT EXISTS programme_time ON programme(stop_at);
"""


def _fts_schema(version):
    """Full-text tables.

    FTS5 is a compile-time option; if the sqlite3 on this machine lacks it we
    fall back to LIKE scanning rather than failing to have a catalogue at all.
    """
    try:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        conn.close()
        return True
    except sqlite3.Error:
        return False


HAVE_FTS = _fts_schema(None)


def _fts_ddl():
    if not HAVE_FTS:
        return []
    return [
        """CREATE VIRTUAL TABLE IF NOT EXISTS radio_fts USING fts5(
               name, tags, country, language, content='radio',
               content_rowid='rowid', tokenize="unicode61 remove_diacritics 2")""",
        """CREATE VIRTUAL TABLE IF NOT EXISTS channel_fts USING fts5(
               name, group_title, country, content='channel',
               content_rowid='rowid', tokenize="unicode61 remove_diacritics 2")""",
    ]


# The facet value that means "only what the user added themselves". Custom
# rows carry ids starting with `custom-`, so this needs no schema change - and
# a schema change here rebuilds the whole mirror.
MINE = "__mine__"


class Catalogue:
    """The mirror. One connection, guarded by a lock, shared by every source."""

    def __init__(self, path, log=None):
        self.path = path
        self.log = log or (lambda message: None)
        self._lock = threading.RLock()
        self._conn = None
        self._fts = HAVE_FTS
        # Reads get a connection per thread. They used to share the writer's
        # connection and its lock, so while a sync replaced sixty thousand
        # stations and rebuilt the search index - a minute, on a first run -
        # every browse queued behind it and the tabs sat on "loading". WAL
        # lets readers see the last committed mirror while the writer works.
        self._local = threading.local()
        self._readers = []
        self._readers_lock = threading.Lock()
        self._epoch = 0
        self._counts = None
        self._writes = 0

    # -- lifecycle ---------------------------------------------------------

    def open(self):
        with self._lock:
            if self._conn is not None:
                return self._conn
            folder = os.path.dirname(self.path)
            if folder:
                os.makedirs(folder, mode=0o700, exist_ok=True)
            conn = sqlite3.connect(self.path, check_same_thread=False,
                                   timeout=20)
            conn.row_factory = sqlite3.Row
            # WAL so a long background sync never blocks a user query, which
            # is the entire point of having a local mirror.
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.Error:
                pass
            conn.executescript(SCHEMA)
            for ddl in _fts_ddl():
                try:
                    conn.executescript(ddl)
                except sqlite3.Error as exc:
                    self._fts = False
                    self.log("full-text search unavailable: %s" % exc)
            conn.commit()
            self._conn = conn
            self._migrate()
            return conn

    def close(self):
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None
            with self._readers_lock:
                readers, self._readers = self._readers, []
                self._epoch += 1
            for conn in readers:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass

    def _reader(self):
        """This thread's read connection, opened on first use."""
        conn = getattr(self._local, "conn", None)
        if conn is not None and getattr(self._local, "epoch", -1) == self._epoch:
            return conn
        if self._conn is None:
            self.open()
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=20)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only=1")
        except sqlite3.Error:
            pass
        self._local.conn = conn
        self._local.epoch = self._epoch
        with self._readers_lock:
            self._readers.append(conn)
        return conn

    def _changed(self):
        """Forget anything derived from the tables; a write just landed."""
        self._writes += 1
        self._counts = None

    def _migrate(self):
        """A schema change drops and rebuilds rather than migrating.

        A half-migrated catalogue is worse than none: the columns would mean
        something new while the rows were written by the old code.
        """
        row = self.get_meta("schema")
        if row == str(SCHEMA_VERSION):
            return
        if row is not None:
            self.log("catalogue schema %s -> %d, rebuilding"
                     % (row, SCHEMA_VERSION))
            with self._lock:
                for table in ("radio", "channel", "programme", "radio_fts",
                              "channel_fts"):
                    try:
                        self._conn.execute("DROP TABLE IF EXISTS %s" % table)
                    except sqlite3.Error:
                        pass
                self._conn.executescript(SCHEMA)
                for ddl in _fts_ddl():
                    self._conn.executescript(ddl)
        self.set_meta("schema", str(SCHEMA_VERSION))
        self._conn.commit()

    # -- meta --------------------------------------------------------------

    def get_meta(self, key, default=None):
        try:
            row = self._reader().execute(
                "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        except sqlite3.Error:
            return default
        return row["value"] if row else default

    def set_meta(self, key, value):
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)))
            # Without this the write sits in an open transaction and is lost
            # when the connection closes - which meant the freshness stamp
            # never persisted and every start re-mirrored the whole database.
            self._conn.commit()
        self._changed()

    def age(self, key):
        """Seconds since a key was last written, or None."""
        stamp = self.get_meta(key + "_at")
        if not stamp:
            return None
        try:
            return max(0.0, time.time() - float(stamp))
        except (TypeError, ValueError):
            return None

    def touch(self, key):
        self.set_meta(key + "_at", time.time())

    # Keyed by what the caller asks for, not by the table name: the table is
    # `channel`, and every consumer wants it as "tv", so returning the table
    # name made a correct count look like an empty catalogue.
    COUNT_TABLES = {"radio": "radio", "tv": "channel", "programmes": "programme"}

    def counts(self):
        """Rows per catalogue. Cached until the next write: every browse asks
        whether a source is mirrored, and that is three table scans."""
        cached = self._counts
        if cached is not None:
            return dict(cached)
        writes = self._writes
        out = {}
        try:
            reader = self._reader()
        except sqlite3.Error:
            reader = None
        for key, table in self.COUNT_TABLES.items():
            try:
                out[key] = reader.execute(
                    "SELECT COUNT(*) FROM %s" % table).fetchone()[0]
            except (sqlite3.Error, AttributeError):
                out[key] = 0
        # The user's own rows, by key range so the primary key index answers.
        for key, table, column in (("radio_custom", "radio", "uuid"),
                                   ("tv_custom", "channel", "id")):
            try:
                out[key] = reader.execute(
                    "SELECT COUNT(*) FROM %s WHERE %s >= 'custom-' AND %s < "
                    "'custom.'" % (table, column, column)).fetchone()[0]
            except (sqlite3.Error, AttributeError):
                out[key] = 0
        if writes == self._writes:
            self._counts = out
        return dict(out)

    # The stamp each source writes when a full mirror lands, so "when was this
    # downloaded" is a question about the catalogue rather than a guess.
    FULL_STAMPS = {"radio": "radio_full", "tv": "tv_full", "epg": "epg_full"}

    def mirrored(self, source):
        """Is there a complete mirror of `source` on disk to read from?

        The rows are the evidence, not the timestamp. A mirror is only ever
        written by a completed, atomic replace, so rows present means a whole
        catalogue is there. The stamp is bookkeeping and can be missing - an
        interrupted first run rolls the write back and leaves the previous
        mirror's rows without ever stamping them, and treating that as "never
        downloaded" hides a perfectly good list behind a download prompt and
        sends browsing to the network for data already on disk.
        """
        counts = self.counts()
        return counts.get(source, 0) - counts.get(source + "_custom", 0) > 0

    def source_status(self, source):
        """Count, age and readiness for one source, for the status bar."""
        stamp = self.FULL_STAMPS.get(source, source + "_full")
        return {
            "count": self.counts().get(source, 0),
            "age": self.age(stamp),
            "mirrored": self.mirrored(source),
        }

    # -- writes ------------------------------------------------------------

    def replace_radio(self, rows, batch=2000):
        """Replace the whole station table in one transaction.

        A wholesale replace is the right shape for a full mirror: it is
        atomic, so a query never sees a half-populated table, and it does not
        accumulate the rows that upstream deleted.
        """
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute("DELETE FROM radio")
                self._conn.executemany(
                    "INSERT OR REPLACE INTO radio(uuid,name,url,url_resolved,"
                    "homepage,favicon,codec,bitrate,country,countrycode,state,"
                    "language,tags,votes,clicks,hls,codec_ok,checked,changed,"
                    "updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    rows)
                if self._fts:
                    self._rebuild_fts("radio_fts")
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            self.touch("radio_full")
        return len(rows)

    def upsert_radio(self, rows, batch=2000):
        """Apply an incremental change set."""
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO radio(uuid,name,url,url_resolved,"
                    "homepage,favicon,codec,bitrate,country,countrycode,state,"
                    "language,tags,votes,clicks,hls,codec_ok,checked,changed,"
                    "updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    rows)
                if self._fts:
                    self._rebuild_fts("radio_fts")
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            self.touch("radio_delta")
        return len(rows)

    # The columns upstream's change feed actually carries. It is not the same
    # payload as /stations: no codec, no bitrate, no click count, no health
    # flag. Replacing the row wholesale therefore blanks all four on every
    # station the refresh touches - an hourly refresh that quietly stripped the
    # mirror of the bitrates it displays and the popularity it sorts by, and
    # could mark a known-dead stream as healthy because the missing field reads
    # as an opinion.
    #
    # So a change updates the descriptive fields and leaves the rest of the row
    # exactly as the full mirror wrote it. A station that is genuinely new is
    # still inserted, with whatever the feed says and defaults for the rest,
    # which is honest: unknown until the next full mirror, not invented.
    RADIO_CHANGE_COLUMNS = (
        "name", "url", "homepage", "favicon", "country", "countrycode",
        "state", "language", "tags", "votes", "changed", "updated",
    )

    def apply_radio_changes(self, rows, tags=()):
        """Apply an upstream change set, and the tags that go with it.

        One transaction for both, because a station whose row landed and whose
        genre did not is reachable by no filter at all until the next full
        mirror - and the facet counts would silently disagree with the list
        while saying so nowhere.
        """
        updates = ", ".join("%s=excluded.%s" % (column, column)
                            for column in self.RADIO_CHANGE_COLUMNS)
        statement = (
            "INSERT INTO radio(uuid,name,url,url_resolved,homepage,favicon,"
            "codec,bitrate,country,countrycode,state,language,tags,votes,"
            "clicks,hls,codec_ok,checked,changed,updated) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(uuid) DO UPDATE SET " + updates)
        uuids = sorted({row[0] for row in rows})
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.executemany(statement, rows)
                if tags:
                    self._conn.executemany(
                        "DELETE FROM radio_tag WHERE uuid=?", [(u,) for u in uuids])
                    self._conn.executemany(
                        "INSERT OR IGNORE INTO radio_tag(uuid,tag) VALUES(?,?)",
                        tags)
                if self._fts:
                    self._rebuild_fts("radio_fts")
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            self.touch("radio_delta")
        return len(rows)

    def replace_channels(self, rows):
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute("DELETE FROM channel")
                self._conn.executemany(
                    "INSERT OR REPLACE INTO channel(id,name,url,logo,"
                    "group_title,country,language,is_nsfw,closed,updated) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
                if self._fts:
                    self._rebuild_fts("channel_fts")
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            self.touch("tv_full")
        return len(rows)

    def replace_programmes(self, rows):
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute("DELETE FROM programme")
                self._conn.executemany(
                    "INSERT OR REPLACE INTO programme(channel_id,start_at,"
                    "stop_at,title,description) VALUES(?,?,?,?,?)", rows)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            self.touch("epg_full")
        return len(rows)

    def _rebuild_fts(self, table):
        """Reindex an external-content FTS table from its content table.

        The only supported way. DELETE against an external-content index
        leaves it pointing at rows that no longer exist, and the next MATCH
        raises "database disk image is malformed".
        """
        try:
            self._conn.execute(
                "INSERT INTO %s(%s) VALUES('rebuild')" % (table, table))
        except sqlite3.Error as exc:
            self.log("could not reindex %s: %s" % (table, exc))
            self._fts = False

    # -- queries -----------------------------------------------------------

    def _query(self, sql, args=()):
        try:
            return self._reader().execute(sql, args).fetchall()
        except sqlite3.Error as exc:
            self.log("query failed: %s" % exc)
            return []

    def _radio_where(self, term, country, group, min_bitrate, hide_broken):
        """The shared WHERE for listing and counting radio.

        These used to be built separately and had drifted: counting ignored the
        genre and the bitrate floor, so a filtered page reported the total for
        an unfiltered one and the pager happily offered "next" past the end.
        """
        where = []
        args = []
        if hide_broken:
            where.append("codec_ok=1")
        if country:
            where.append("countrycode=?")
            args.append(str(country).upper())
        if group == MINE:
            where.append("uuid LIKE 'custom-%'")
        elif group:
            # Exact tag membership, not a substring. "pop" has to mean the pop
            # tag, not every tag that happens to contain those letters.
            # COLLATE NOCASE, not "=": SQLite compares TEXT byte for byte
            # unless told otherwise, so a filter of "Pop" against a stored
            # "pop" matched nothing and returned an empty list, which reads
            # as "no stations in that genre" rather than as a bug. The stored
            # case is whatever the station's owner typed, and the facet list
            # keeps the first spelling seen, so the two cannot be assumed to
            # agree.
            where.append("uuid IN (SELECT uuid FROM radio_tag "
                         "WHERE tag=? COLLATE NOCASE)")
            args.append(str(group).strip())
        if min_bitrate:
            where.append("(bitrate=0 OR bitrate>=?)")
            args.append(int(min_bitrate))

        term = (term or "").strip()
        if term and self._fts:
            match = _fts_query(term)
            where.append("rowid IN (SELECT rowid FROM radio_fts WHERE "
                         "radio_fts MATCH ?)" if match else "0")
            if match:
                args.append(match)
        elif term:
            like = "%" + term + "%"
            where.append("(name LIKE ? OR tags LIKE ? OR country LIKE ?)")
            args.extend([like, like, like])
        return where, args

    def radio_genres(self, limit=60, min_count=8):
        """(genre, station count), biggest first.

        The count floor is the point: the mirror holds over eleven thousand
        distinct tags, most used by a single station and many of them not
        genres at all - "fm", "radio", a presenter's name. Offering that as a
        genre list is noise, so the list is ranked by how many stations carry
        the tag and the long tail is left to the search box.
        """
        try:
            # MIN(tag) picks one spelling per tag, so "Pop" and "pop" are one
            # entry with a combined count rather than two half-sized ones.
            return [(row[0], row[1]) for row in self._query(
                "SELECT MIN(tag), COUNT(*) AS n FROM radio_tag "
                "WHERE tag<>'' GROUP BY tag COLLATE NOCASE "
                "HAVING n>=? ORDER BY n DESC, MIN(tag) LIMIT ?",
                (int(min_count), int(limit)))]
        except sqlite3.Error:
            return []

    def radio_countries(self, limit=300):
        """(code, name, station count), biggest first."""
        try:
            return [(row[0], row[1], row[2]) for row in self._query(
                "SELECT countrycode, MAX(country), COUNT(*) AS n FROM radio "
                "WHERE countrycode<>'' AND codec_ok=1 GROUP BY countrycode "
                "HAVING n>0 ORDER BY n DESC, countrycode LIMIT ?",
                (int(limit),))]
        except sqlite3.Error:
            return []

    def replace_radio_tags(self, rows):
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute("DELETE FROM radio_tag")
                self._conn.executemany(
                    "INSERT OR IGNORE INTO radio_tag(uuid,tag) VALUES(?,?)", rows)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        self._changed()
        return len(rows)

    def search_radio(self, term="", limit=60, offset=0, country=None,
                     group=None, min_bitrate=0, order="votes", hide_broken=True):
        """The radio browse. Every filter is applied in SQL, not in Python:
        filtering 30,000 rows per keystroke in Python is what makes a search
        feel slow."""
        where, args = self._radio_where(term, country, group, min_bitrate,
                                        hide_broken)

        order_sql = {
            "votes": "votes DESC, clicks DESC, name",
            "name": "name",
            "clicks": "clicks DESC, name",
            "bitrate": "bitrate DESC, name",
            "newest": "checked DESC, name",
            "country": "country, name",
            "random": "RANDOM()",
        }.get(order, "votes DESC, clicks DESC, name")

        sql = "SELECT * FROM radio"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY " + order_sql + " LIMIT ? OFFSET ?"
        args.extend([int(limit), int(offset)])
        return self._query(sql, args)

    def count_radio(self, term="", country=None, group=None, min_bitrate=0,
                    hide_broken=True):
        where, args = self._radio_where(term, country, group, min_bitrate,
                                        hide_broken)
        sql = "SELECT COUNT(*) FROM radio"
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = self._query(sql, args)
        return rows[0][0] if rows else 0

    def _channel_where(self, term, country, group, hide_nsfw, hide_geo=False,
                       hide_not247=False):
        """The shared WHERE for listing and counting TV channels.

        Same correction as radio: these were built separately and had drifted.
        Counting ignored the category filter entirely, so a category page
        reported the total for the whole catalogue and the pager offered
        "next" long past the end.
        """
        where = ["closed=0"]
        args = []
        if hide_nsfw:
            where.append("is_nsfw=0")
        # iptv-org marks these in the channel name. Filtering them here rather
        # than on the page that came back keeps the count and the pages
        # honest: dropping rows afterwards made a "60 per page" list return
        # 52, which read as the end of the list while the count said 170.
        if hide_geo:
            where.append("name NOT LIKE '%geo-block%' AND name NOT LIKE '%geo block%'")
        if hide_not247:
            where.append("name NOT LIKE '%not 24/7%'")
        if country:
            where.append("country=?")
            args.append(str(country).upper())
        if group == MINE:
            where.append("id LIKE 'custom-%'")
        elif group:
            # A facet lookup, not group_title=?. Two problems with the
            # equality: only the first category a channel was filed under was
            # ever stored, and SQLite compares TEXT byte for byte, so a filter
            # of "News" against a stored "news" matched nothing and returned an
            # empty list - which reads as "no channels in that category".
            where.append("id IN (SELECT id FROM channel_tag "
                         "WHERE tag=? COLLATE NOCASE)")
            args.append(str(group).strip())
        term = (term or "").strip()
        if term and self._fts:
            match = _fts_query(term)
            where.append("rowid IN (SELECT rowid FROM channel_fts WHERE "
                         "channel_fts MATCH ?)" if match else "0")
            if match:
                args.append(match)
        elif term:
            where.append("(name LIKE ? OR group_title LIKE ?)")
            like = "%" + term + "%"
            args.extend([like, like])
        return where, args

    def channel_genres(self, limit=60, min_count=4):
        """(category, channel count), biggest first.

        Ranked with a floor for the same reason as radio: a category with one
        channel is not something a filter should offer.
        """
        try:
            return [(row[0], row[1]) for row in self._query(
                "SELECT MIN(tag), COUNT(*) AS n FROM channel_tag "
                "WHERE tag<>'' GROUP BY tag COLLATE NOCASE "
                "HAVING n>=? ORDER BY n DESC, MIN(tag) LIMIT ?",
                (int(min_count), int(limit)))]
        except sqlite3.Error:
            return []

    def replace_channel_tags(self, rows):
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute("DELETE FROM channel_tag")
                self._conn.executemany(
                    "INSERT OR IGNORE INTO channel_tag(id,tag) VALUES(?,?)", rows)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return len(rows)

    def search_channels(self, term="", limit=60, offset=0, country=None,
                        group=None, hide_nsfw=True, hide_geo=False,
                        hide_not247=False):
        where, args = self._channel_where(term, country, group, hide_nsfw,
                                          hide_geo, hide_not247)
        sql = "SELECT * FROM channel"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY name LIMIT ? OFFSET ?"
        args.extend([int(limit), int(offset)])
        return self._query(sql, args)

    def count_radio(self, term="", country=None, group=None, min_bitrate=0,
                    hide_broken=True):
        """How many stations the current filter matches.

        The status bar needs the size of what is being looked at, not just the
        size of the page that came back: "60 results" under a filter that
        matches 3,204 stations is a different statement from "60 results" out
        of 59,826. The same WHERE builder as the search, or the two would
        quietly disagree - which is exactly the bug this count used to have.
        """
        where, args = self._radio_where(term, country, group, min_bitrate,
                                        hide_broken)
        sql = "SELECT COUNT(*) FROM radio" + (
            " WHERE " + " AND ".join(where) if where else "")
        rows = self._query(sql, args)
        return rows[0][0] if rows else 0

    def count_channels(self, term="", country=None, group=None,
                       hide_nsfw=True, hide_geo=False, hide_not247=False):
        where, args = self._channel_where(term, country, group, hide_nsfw,
                                          hide_geo, hide_not247)
        sql = "SELECT COUNT(*) FROM channel" + (
            " WHERE " + " AND ".join(where) if where else "")
        rows = self._query(sql, args)
        return rows[0][0] if rows else 0

    def channel_groups(self, limit=80):
        rows = self._query(
            "SELECT group_title, COUNT(*) AS n FROM channel "
            "WHERE closed=0 AND group_title<>'' "
            "GROUP BY group_title ORDER BY n DESC LIMIT ?", (int(limit),))
        return [(r["group_title"], r["n"]) for r in rows]

    def channel_countries(self, limit=250):
        rows = self._query(
            "SELECT country, COUNT(*) AS n FROM channel "
            "WHERE closed=0 AND country<>'' GROUP BY country "
            "ORDER BY n DESC LIMIT ?", (int(limit),))
        return [(r["country"], r["n"]) for r in rows]

    def station_names(self):
        """(uuid, name, listed url) for every mirrored station."""
        try:
            return [(row[0], row[1], row[2]) for row in self._query(
                "SELECT uuid, name, url_resolved FROM radio")]
        except sqlite3.Error:
            return []

    def station_candidates(self, uuid, limit=6):
        """Every link worth trying for a station, listed order first.

        The mirror's own URL is not repeated here; the caller already has it
        and puts it first, because that is the link a user would recognise.
        """
        try:
            rows = self._query(
                "SELECT url FROM radio_link WHERE uuid=? "
                "ORDER BY pos, url LIMIT ?", (uuid, int(limit)))
        except sqlite3.Error:
            return []
        return [row[0] for row in rows if row[0]]

    def station_siblings(self, uuid, name, limit=3):
        """Other rows for the same station name, which are often a live link.

        The mirror keeps every row radio-browser knows about, and the same
        station shows up more than once with different stream URLs. When the
        row we picked has a dead link, one of those is a free second try.
        """
        if not name:
            return []
        try:
            rows = self._query(
                "SELECT url, url_resolved FROM radio "
                "WHERE name=? AND uuid<>? AND url<>'' LIMIT ?",
                (name, uuid, int(limit)))
        except sqlite3.Error:
            return []
        out = []
        for url, resolved in rows:
            for candidate in (resolved, url):
                if candidate and candidate not in out:
                    out.append(candidate)
        return out[:limit]

    def replace_radio_links(self, rows):
        """Swap in the alternate-link table wholesale, like the mirror."""
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute("DELETE FROM radio_link")
                self._conn.executemany(
                    "INSERT OR IGNORE INTO radio_link(uuid,url,pos) "
                    "VALUES(?,?,?)", rows)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return len(rows)

    # -- the user's own rows ---------------------------------------------

    _RADIO_COLUMNS = ("uuid,name,url,url_resolved,homepage,favicon,codec,"
                      "bitrate,country,countrycode,state,language,tags,votes,"
                      "clicks,hls,codec_ok,checked,changed,updated")

    def replace_custom_radio(self, rows, tags):
        """Make the mirror's custom stations exactly `rows`."""
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                # Only the custom rows are taken out of and put back into the
                # search index. Rebuilding the whole index for one added
                # station meant re-indexing sixty thousand of them.
                mine = "uuid >= 'custom-' AND uuid < 'custom.'"
                if self._fts:
                    self._conn.execute(
                        "INSERT INTO radio_fts(radio_fts, rowid, name, tags, "
                        "country, language) SELECT 'delete', rowid, name, tags, "
                        "country, language FROM radio WHERE " + mine)
                self._conn.execute("DELETE FROM radio_tag WHERE " + mine)
                self._conn.execute("DELETE FROM radio WHERE " + mine)
                self._conn.executemany(
                    "INSERT OR REPLACE INTO radio(%s) VALUES(%s)"
                    % (self._RADIO_COLUMNS, ",".join("?" * 20)), rows)
                self._conn.executemany(
                    "INSERT OR IGNORE INTO radio_tag(uuid,tag) VALUES(?,?)", tags)
                if self._fts:
                    self._conn.execute(
                        "INSERT INTO radio_fts(rowid, name, tags, country, "
                        "language) SELECT rowid, name, tags, country, language "
                        "FROM radio WHERE " + mine)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        self._changed()
        return len(rows)

    def replace_custom_channels(self, rows, tags):
        """Make the mirror's custom channels exactly `rows`."""
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                mine = "id >= 'custom-' AND id < 'custom.'"
                if self._fts:
                    self._conn.execute(
                        "INSERT INTO channel_fts(channel_fts, rowid, name, "
                        "group_title, country) SELECT 'delete', rowid, name, "
                        "group_title, country FROM channel WHERE " + mine)
                self._conn.execute("DELETE FROM channel_tag WHERE " + mine)
                self._conn.execute("DELETE FROM channel WHERE " + mine)
                self._conn.executemany(
                    "INSERT OR REPLACE INTO channel(id,name,url,logo,"
                    "group_title,country,language,is_nsfw,closed,updated) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
                self._conn.executemany(
                    "INSERT OR IGNORE INTO channel_tag(id,tag) VALUES(?,?)", tags)
                if self._fts:
                    self._conn.execute(
                        "INSERT INTO channel_fts(rowid, name, group_title, "
                        "country) SELECT rowid, name, group_title, country "
                        "FROM channel WHERE " + mine)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        self._changed()
        return len(rows)

    def clear(self, source):
        """Forget a downloaded catalogue. The user's own rows are kept."""
        tables = {
            "radio": (("radio_tag", "uuid"), ("radio_link", "uuid"),
                      ("radio", "uuid")),
            "tv": (("channel_tag", "id"), ("channel", "id")),
        }.get(source)
        if not tables:
            return
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                for table, key in tables:
                    self._conn.execute(
                        "DELETE FROM %s WHERE %s NOT LIKE 'custom-%%'"
                        % (table, key))
                for stamp in ("%s_full_at" % source, "%s_delta_at" % source):
                    self._conn.execute("DELETE FROM meta WHERE key=?", (stamp,))
                if self._fts:
                    self._rebuild_fts("radio_fts" if source == "radio"
                                      else "channel_fts")
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        self._changed()

    def radio_rows(self, mine_only=False):
        """Every station row, for an export."""
        sql = "SELECT * FROM radio"
        if mine_only:
            sql += " WHERE uuid LIKE 'custom-%'"
        return self._query(sql + " ORDER BY name")

    def custom_counts(self):
        counts = self.counts()
        return {"radio": counts.get("radio_custom", 0),
                "tv": counts.get("tv_custom", 0)}

    def station_by_uuid(self, uuid):
        rows = self._query("SELECT * FROM radio WHERE uuid=?", (uuid,))
        return rows[0] if rows else None

    def programmes(self, channel_id, at=None, upcoming=8):
        at = int(at if at is not None else time.time())
        rows = self._query(
            "SELECT * FROM programme WHERE channel_id=? AND stop_at>=? "
            "ORDER BY start_at LIMIT ?", (channel_id, at - 3600, int(upcoming) + 1))
        now = None
        after = []
        for row in rows:
            if row["start_at"] <= at < row["stop_at"]:
                now = row
            elif row["start_at"] > at:
                after.append(row)
        return now, after[:int(upcoming)]


_FTS_SAFE = re.compile(r"[^\w\s\-]", re.UNICODE)


def _fts_query(term):
    """Turn a user's words into a safe FTS5 MATCH expression.

    Everything the user typed is treated as a *prefix* term, so "para" finds
    "Paradise" while typing. Characters FTS5 treats as syntax are stripped
    rather than escaped, because a stray quote should narrow the search, not
    raise.
    """
    cleaned = _FTS_SAFE.sub(" ", term or "")
    words = [w for w in cleaned.split() if w]
    if not words:
        return ""
    return " AND ".join('"%s"*' % w for w in words)
