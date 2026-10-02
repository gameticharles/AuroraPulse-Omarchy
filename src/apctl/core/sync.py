"""Filling the catalogue, in the background, exactly once.

Two rules shape this:

* The user never waits for it. A browse that arrives before the mirror is
  ready falls back to the network, and the answer is still correct - just
  slower. That is why every source has a database path *and* a network path.
* It never blocks the daemon. Sync runs on its own thread, holds its own
  connection, and can be cancelled when the daemon is asked to shut down.
"""

import json
import re
import threading
import time

from ..sources import radio as radio_source
from ..sources import tv
from ..util import health, netguard, textutil
from . import legacy
from .db import (Catalogue, RADIO_FULL_DAYS, RADIO_INCREMENTAL_MINUTES,
                 TV_REFRESH_HOURS)

PAGE = 5000
MAX_PAGES = 40          # 200k stations, far more than exist today


def _number(value, default=0):
    """Radio Browser mixes epoch seconds, ISO strings and "0" in the same
    fields depending on the column, so nothing here can trust its type."""
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return default
    try:
        return int(text)
    except ValueError:
        pass
    import datetime
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(datetime.datetime.strptime(
                text[:19], pattern).replace(
                    tzinfo=datetime.timezone.utc).timestamp())
        except ValueError:
            continue
    return default


def _radio_row(entry, now):
    tags = entry.get("tags")
    if isinstance(tags, list):
        tags = ", ".join(str(t) for t in tags)
    return (
        textutil.text(entry.get("stationuuid") or entry.get("serveruuid"), 64),
        textutil.text(entry.get("name"), 300) or "Unknown",
        textutil.text(entry.get("url_resolved") or entry.get("url"), 2048),
        textutil.text(entry.get("url"), 2048),
        textutil.text(entry.get("homepage"), 500),
        textutil.text(entry.get("favicon"), 2048),
        textutil.text(entry.get("codec"), 32).upper(),
        int(entry.get("bitrate") or 0),
        textutil.text(entry.get("country"), 80),
        textutil.text(entry.get("countrycode"), 4).upper(),
        textutil.text(entry.get("state"), 80),
        textutil.text(entry.get("language"), 80),
        textutil.text(tags, 600),
        _number(entry.get("votes")),
        _number(entry.get("clickcount")),
        1 if entry.get("hls") else 0,
        health.checked_flag(entry.get("lastcheckok")),
        _number(entry.get("lastchecktime")),
        _number(entry.get("lastchangetime")),
        int(now),
    )


# Position of the comma-separated tag string inside a radio row tuple.
_TAG_INDEX = 12


def _tag_rows(rows):
    """(uuid, tag) for every tag on every mirrored station.

    Split out of the station row so the facet table is built from the same
    data in the same pass, rather than by re-reading 60,000 rows afterwards
    and splitting the same strings a second time.
    """
    out = []
    for row in rows:
        uuid = row[0]
        tags = row[_TAG_INDEX]
        if not tags:
            continue
        seen = set()
        for tag in str(tags).split(","):
            tag = tag.strip()
            # Case is kept as sent but matched on the stored form, so "Pop"
            # and "pop" are one entry rather than two half-sized ones.
            key = tag.lower()
            if tag and key not in seen:
                seen.add(key)
                out.append((uuid, tag))
    return out


def _channel_row(entry, now, index):
    urls = entry.get("url") or []
    if isinstance(urls, str):
        urls = [urls]
    url = ""
    for candidate in urls:
        if isinstance(candidate, str) and candidate.startswith(
                ("http://", "https://")):
            url = candidate
            break
    if not url:
        return None
    closed = entry.get("closed") or 0
    name = textutil.text(entry.get("name"), 300)
    if not name:
        return None
    return (
        textutil.text(entry.get("id"), 64) or ("ch%d" % index),
        name,
        url,
        textutil.text(entry.get("logo"), 2048),
        textutil.text(entry.get("categories") and
                      (entry["categories"][0] if entry["categories"] else ""), 120),
        textutil.text(entry.get("country"), 8).upper(),
        textutil.text(entry.get("language"), 80),
        1 if entry.get("is_nsfw") else 0,
        1 if closed else 0,
        int(now),
    )


def _upstream_total(source, fallback=0):
    """How many rows upstream says it has, for the progress denominator.

    A percentage needs a real total. Deriving one from the page count gives a
    bar that hits 100% early and then sits there, or worse, one that never
    arrives. When the count cannot be had the caller is told None and shows an
    indeterminate figure rather than a fabricated one. The previous mirror size
    is a last resort and is only used when there is a mirror to measure.
    """
    try:
        if source == "radio":
            body = netguard.fetch(
                radio_source.api_base() + "/json/stats",
                limit=1 << 20, seconds=30)
            data = json.loads(body)
            count = int(data.get("stations") or 0)
            if count > 0:
                return count
    except Exception:
        pass
    return fallback or None


class Syncer:
    """Owns the mirror. One instance per daemon."""

    def __init__(self, catalogue, log=None, on_done=None, on_progress=None,
                 custom=None):
        self.db = catalogue
        # The user's own stations, channels and playlists. A full mirror
        # replaces the tables wholesale, so these are merged back in on every
        # replace - otherwise an update would quietly delete them.
        self.custom = custom
        self.log = log or (lambda message: None)
        # Called on the sync thread once a mirror lands. The tab that asked for
        # the refresh is sitting there waiting, and polling it from QML meant
        # the footer could show a timestamp older than the data on screen.
        self.on_done = on_done or (lambda source: None)
        # Called as the download advances. A full radio mirror takes over a
        # minute; without this the button looks broken rather than busy.
        self.on_progress = on_progress or (lambda source, info: None)
        self.state = {"radio": "idle", "tv": "idle", "epg": "idle"}
        self.progress = {"radio": None, "tv": None}
        self.stop = threading.Event()
        self._thread = None
        self._current = None
        self._pending = None
        self._radio_mutex = threading.Lock()

    def _report(self, source, **info):
        self.progress[source] = info
        self.on_progress(source, info)

    def status(self):
        counts = self.db.counts()
        status = {
            "state": dict(self.state),
            "radio": counts.get("radio", 0),
            "tv": counts.get("tv", 0),
            "programmes": counts.get("programmes", 0),
            "radio_age": self.db.age("radio_full"),
            "tv_age": self.db.age("tv_full"),
            "epg_age": self.db.age("epg_full"),
        }
        for source in ("radio", "tv"):
            status["%s_mirrored" % source] = self.db.mirrored(source)
        status["pending"] = self._pending
        status["syncing"] = (self._thread is not None
                             and self._thread.is_alive())
        return status

    def start(self, full=True, source=None):
        """Mirror in the background.

        `source` limits the run to one catalogue. The status bar updates a
        single tab at a time, and having that button quietly re-download
        everything else too is the sort of surprise that makes people stop
        trusting it.

        A request that arrives while a mirror is running is queued rather than
        dropped. The first run syncs both catalogues in the background, so
        pressing the button on the other tab in that window used to do nothing
        at all - no download, no error, and a button that looked broken.
        """
        if self._thread is not None and self._thread.is_alive():
            if source and source != self._current:
                self._pending = source
            return False
        self.stop.clear()
        self._pending = None
        self._current = source
        self._thread = threading.Thread(target=self._run, args=(full, source),
                                        name="ap-sync", daemon=True)
        self._thread.start()
        return True

    def shutdown(self, timeout=5.0):
        self.stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self.db.close()

    def _run(self, full, source=None):
        self._one(full, source)
        # Whatever was asked for while this ran goes next, rather than being
        # lost. Stopping is the only thing that outranks it.
        pending, self._pending = self._pending, None
        if pending and not self.stop.is_set():
            self.log("running the queued %s mirror" % pending)
            self._run(full, pending)

    def _finish(self, source, error=None):
        """Close the progress report for `source`, whatever happened.

        A download that stopped early - a page that timed out, a partial
        mirror kept back, an exception - used to leave its last "downloading"
        report standing, and the status bar said "downloading" forever.
        """
        last = self.progress.get(source)
        if last is None or last.get("stage") in ("done", "failed"):
            return
        info = dict(last)
        info["stage"] = "failed" if error else "done"
        if error:
            info["error"] = str(error)[:200]
        self._report(source, **info)

    def _one(self, full, source=None):
        # Radio first: it is the source people open the widget for.
        if source in (None, "radio"):
            error = None
            try:
                self.sync_radio(full=full)
                if str(self.state.get("radio")).startswith(("partial",
                                                            "incomplete")):
                    error = "the download stopped early; kept the old list"
            except Exception as exc:
                self.log("radio sync failed: %s" % exc)
                self.state["radio"] = "failed: %s" % exc
                error = exc
            self._finish("radio", error)
            self.on_done("radio")
        if self.stop.is_set():
            return
        if source in (None, "tv", "epg"):
            error = None
            try:
                self.sync_tv(full=full)
            except Exception as exc:
                self.log("tv sync failed: %s" % exc)
                self.state["tv"] = "failed: %s" % exc
                error = exc
            self._finish("tv", error)
            self.on_done("tv")

    # -- radio -------------------------------------------------------------

    def sync_radio(self, full=False):
        """Mirror the station list.

        Incremental first: upstream publishes the rows that changed since a
        timestamp, which is a few hundred rows and takes a second. Only when
        that is stale or has never run do we page the whole database.
        """
        with self._radio_mutex:
            self.db.open()
            counts = self.db.counts()
            have = counts.get("radio", 0)
            age = self.db.age("radio_full")
            delta_age = self.db.age("radio_delta")

            fresh = have > 0 and age is not None and age < RADIO_FULL_DAYS * 86400
            if have and fresh and not full:
                if delta_age is None or \
                        delta_age > RADIO_INCREMENTAL_MINUTES * 60:
                    self.state["radio"] = "updating"
                    try:
                        self._radio_delta()
                    except Exception as exc:
                        self.log("radio delta failed: %s" % exc)
                self.state["radio"] = "ready"
                return have

            self.state["radio"] = "syncing" if have else "first run"
            self.log("mirroring the station list (%s so far)" % have)
            total = _upstream_total("radio", have)
            rows = []
            now = time.time()
            pages = 0
            got = 0
            self._report("radio", stage="download", rows=0, bytes=0,
                         total=total, unit="rows", page=0, pages=0)
            for page in range(MAX_PAGES):
                if self.stop.is_set():
                    self.log("station mirror cancelled after %d page(s)" % page)
                    return have          # keep whatever we already had
                pages = page + 1
                complete = False
                url = (radio_source.api_base() + "/json/stations"
                       "?limit=%d&offset=%d&hidebroken=true&order=clickcount"
                       % (PAGE, page * PAGE))
                try:
                    body, _declared = netguard.fetch_sized(
                        url, limit=96 << 20, seconds=120)
                except Exception as exc:
                    self.log("page %d failed: %s" % (page, exc))
                    break
                import json
                chunk = json.loads(body)
                if not isinstance(chunk, list) or not chunk:
                    break
                for entry in chunk:
                    if isinstance(entry, dict):
                        row = _radio_row(entry, now)
                        if row[0] and row[2]:
                            rows.append(row)
                got += len(body)
                self._report("radio", stage="download", rows=len(rows),
                             bytes=got, total=total, unit="rows",
                             page=page + 1, pages=pages)
                if len(chunk) < PAGE:
                    # A short page is the end of the database.
                    complete = True
                    break
            self._report("radio", stage="parse", rows=len(rows), bytes=got,
                         total=total, unit="rows", page=pages, pages=pages)
            # A partial mirror is worse than a stale one: it looks complete,
            # so a search quietly finds a tenth of the stations. `complete` is
            # set only when a page came back short - which is how the database
            # says it has no more rows - rather than by comparing totals, which
            # wrongly rejected every mirror whose last page was partial.
            if not complete:
                self.log("station mirror stopped after %d page(s) with %d rows, "
                         "keeping the existing %d" % (pages, len(rows), have))
                self.state["radio"] = "partial" if have else "incomplete"
                return have
            if rows:
                if self.custom is not None:
                    mine, _mine_tags = self.custom.radio_rows(now)
                    rows.extend(mine)
                self.db.replace_radio(rows)
                tags = self.db.replace_radio_tags(_tag_rows(rows))
                self.state["radio"] = "ready"
                self._report("radio", stage="done", rows=len(rows),
                             bytes=got, total=total, unit="rows",
                             page=pages, pages=pages)
                self.log("mirrored %d stations across %d tags"
                         % (len(rows), tags))
                self.import_alternates()
                return len(rows)
            self.state["radio"] = "ready" if have else "empty"
            return have

    def import_alternates(self, data_dir=None):
        """Attach the original app's extra links to the stations we hold.

        Only for names the mirror already has. The bundled list is mostly dead
        by now, so importing its unmatched names would add 30,000 stations that
        do not play; what it still has is a second and third link for stations
        we would otherwise have to write off.
        """
        path = legacy.stations_path(data_dir)
        if not path:
            self.log("no bundled station list, skipping extra links")
            return 0
        by_name = legacy.links_for_mirror(path)
        if not by_name:
            return 0
        out, matched = [], 0
        for uuid, name, listed in self.db.station_names():
            links = by_name.get(legacy.normalise(name))
            if not links:
                continue
            matched += 1
            for pos, url in enumerate(links[:6]):
                # The link the mirror already lists is not a fallback, it is
                # the station; filing it as an alternate just means retrying
                # the URL that just failed.
                if url and url != listed:
                    out.append((uuid, url, pos))
        if out:
            self.db.replace_radio_links(out)
        self.log("attached %d extra link(s) to %d stations"
                 % (len(out), matched))
        return len(out)

    def _radio_delta(self):
        """Apply the stations upstream changed recently. Cheap, and keeps the
        database honest without a full re-page.

        Upstream publishes its recent changes on one endpoint: a bounded window
        of the last thousand changed stations, half a megabyte. Applying it is
        an upsert keyed by each station's own uuid, so asking again costs a
        fetch and nothing else, and there is no cursor to keep - the window is
        the cursor. Anything older than that window is picked up by the full
        mirror, which runs every few days.

        This used to ask for `/changed/<lastchangetime>` and then read
        `lastchangetime` back with `float()`. Both halves were wrong: the cursor
        endpoint answers an empty list, and the field is a date string
        ("2026-01-14 22:54:03") rather than epoch seconds, so the whole change
        set raised a ValueError and was discarded *after* it had been
        downloaded. Every hourly refresh fetched half a megabyte of stations
        and stored none of them, then logged the failure and tried again.
        """
        import json
        body = netguard.fetch(
            radio_source.api_base() + "/json/stations/changed",
            limit=64 << 20, seconds=120)
        chunk = json.loads(body)
        if not isinstance(chunk, list) or not chunk:
            # Nothing was published. Record that we looked, so a daemon that is
            # started twice inside the hour does not ask twice; `upsert_radio`
            # only stamps when there are rows to write.
            self.db.set_meta("radio_delta_at", time.time())
            return 0
        now = time.time()
        rows = []
        newest = 0.0
        for entry in chunk:
            if not isinstance(entry, dict):
                continue
            row = _radio_row(entry, now)
            if row[0] and row[2]:
                rows.append(row)
            # This column is a date string in one payload and epoch seconds in
            # another. `_number` knows both; `float` knew neither, which is how
            # a successful download ended up as an exception.
            newest = max(newest, float(_number(entry.get("lastchangetime"))))
        if rows:
            self.db.apply_radio_changes(rows, _tag_rows(rows))
        if newest:
            self.db.set_meta("radio_last_change", newest)
        self.log("applied %d changed station(s), newest change at %d"
                 % (len(rows), newest))
        return len(rows)

    # -- tv ----------------------------------------------------------------

    def sync_tv(self, full=False):
        """Mirror iptv-org's channel list.

        The published playlist is a few megabytes and the channel metadata is a
        separate JSON file; the playlist carries the stream URLs, so it is the
        one that matters and the one we keep. `full` is the update button: it
        downloads even when the copy on disk is recent, which is the whole
        point of pressing it.
        """
        self.db.open()
        age = self.db.age("tv_full")
        have = self.db.counts().get("tv", 0)
        if (not full and have and age is not None
                and age < TV_REFRESH_HOURS * 3600):
            self.state["tv"] = "ready"
            return have

        self.state["tv"] = "syncing" if have else "first run"
        self.log("mirroring the channel list")
        from ..sources.base import fetch_json, fetch_text, parse_m3u
        # The playlist declares its own length, so the download has a real
        # denominator. It is one 2.5 MB file followed by a parse that takes
        # long enough to need saying out loud, so the stage is part of the
        # message: a bar that sits at 100% while the parse runs is a lie.
        self._report("tv", stage="download", rows=0, bytes=0, total=None,
                     unit="bytes")
        raw, declared = netguard.fetch_sized(
            tv.CHANNEL_LIST, limit=64 << 20, seconds=180,
            allowed=netguard.WEB_PORTS)
        body = raw.decode("utf-8", "replace")
        self._report("tv", stage="download", rows=0, bytes=len(raw),
                     total=declared, unit="bytes")
        self._report("tv", stage="parse", rows=0, bytes=len(raw),
                     total=declared, unit="bytes")
        parsed = parse_m3u(body, "tv", id_prefix="", limit=200000)
        self._report("tv", stage="parse", rows=len(parsed), bytes=len(raw),
                     total=declared, unit="bytes")

        # The playlist says where a channel is only inside its tvg-id, as a
        # suffix like ".us@SD", and those ids do not match the published
        # index. Matching on a normalised *name* does, for about two thirds of
        # the catalogue; the rest fall back to the suffix, and anything still
        # unknown is left empty rather than guessed at - a country filter is
        # only useful if it is right.
        index_rows = []
        try:
            index_rows = fetch_json(
                "https://iptv-org.github.io/api/channels.json",
                limit=48 << 20, seconds=90, allowed=netguard.WEB_PORTS)
        except Exception as exc:
            self.log("channel index unavailable: %s" % exc)

        def _norm(value):
            return re.sub(r"[^a-z0-9]+", "", (value or "").lower())

        def _tags(value):
            """iptv-org's comma separated category list, as a list."""
            if not value:
                return []
            if isinstance(value, list):
                raw = value
            else:
                raw = str(value).split(",")
            out = []
            for tag in raw:
                tag = textutil.text(tag, 120)
                if tag and tag.lower() not in [t.lower() for t in out]:
                    out.append(tag)
            return out

        def _plain(value):
            # "Anime Vision (1080p) [Geo-blocked]" -> "Anime Vision"
            return re.sub(r"\s*[\[(].*$", "", value or "").strip()

        # Matched on tvg-id, not on the channel's name.
        #
        # The playlist's tvg-id is the index's own id with a ".us@SD" style
        # region and quality suffix, and it lines up for every single entry in
        # the playlist - so this is an exact join. Matching on the name
        # instead was a guess that only landed for 9,990 of 11,014 channels,
        # and where it guessed it collided: "Cinema" is a channel in Mongolia
        # and an adult one in Russia, so the name join flagged the wrong one
        # and hid it. Rows are still merged within an id, because the index
        # lists a station as closed in one row and open in another, and any
        # row saying "closed" is the one that matters.
        def _tvg_base(value):
            return (value or "").split("@")[0].strip().lower()

        by_id = {}
        by_name = {}
        for entry in index_rows if isinstance(index_rows, list) else []:
            if not isinstance(entry, dict):
                continue
            ident = (entry.get("id") or "").strip().lower()
            if ident:
                by_id.setdefault(ident, []).append(entry)
            key = _norm(entry.get("name"))
            if key:
                by_name.setdefault(key, []).append(entry)

        def _merge(entries):
            """One view of an index entry, taking the worst of each flag."""
            if not entries:
                return None
            out = dict(entries[0])
            out["closed"] = any(e.get("closed") for e in entries)
            out["is_nsfw"] = any(e.get("is_nsfw") for e in entries)
            for field in ("categories", "country", "name"):
                if not out.get(field):
                    for e in entries:
                        if e.get(field):
                            out[field] = e[field]
                            break
            return out

        # Languages, from the same catalogue indexed by language. A channel's
        # group-title in that playlist *is* its language, which is a far more
        # reliable source than guessing from the name.
        language_by_url = {}
        try:
            lang_body = fetch_text(tv.LANGUAGE_LIST, limit=64 << 20,
                                   seconds=180, allowed=netguard.WEB_PORTS)
            for entry in parse_m3u(lang_body, "tv", id_prefix="lang-",
                                   limit=200000):
                group = (entry.get("extra") or {}).get("group")
                url = entry.get("url")
                if group and url:
                    language_by_url.setdefault(url, textutil.text(group, 80))
        except Exception as exc:
            self.log("language index unavailable: %s" % exc)

        now = time.time()
        rows = []
        tag_rows = []
        taken = set()
        for position, entry in enumerate(parsed):
            url = entry.get("url") or ""
            if not url:
                continue
            extra = entry.get("extra") or {}
            groups = extra.get("group") or ""
            title = entry.get("title", "Unknown")
            tvg_id = extra.get("attrs", "").split('tvg-id="')[-1].split('"')[0]
            match = _merge(by_id.get(_tvg_base(tvg_id)))
            if match is None:
                match = _merge(by_name.get(_norm(_plain(title))))
            country = language = category = ""
            closed = nsfw = 0
            cats = []
            if match:
                country = textutil.text(match.get("country"), 8).upper()
                cats = _tags(match.get("categories"))
                # The index also publishes a language, and a "closed" flag for
                # stations that have shut down. Both were being fetched and
                # then thrown away, which is why the off-air filter had to
                # guess from a channel's title instead of asking.
                language = textutil.text(match.get("language"), 80)
                closed = 1 if match.get("closed") else 0
                nsfw = 1 if match.get("is_nsfw") else 0
            if not country:
                suffix = re.search(r"\.([a-z]{2})(?:@|$)", tvg_id or "")
                if suffix:
                    country = suffix.group(1).upper()
            if not language:
                language = language_by_url.get(url, "")
            if cats:
                # The first category stays the display one; all of them go
                # into the facet table so a filter can find the channel under
                # any of them.
                groups = cats[0]
                category = cats[0]
            elif category:
                groups = category
            row_id = tvg_id[:64] or (textutil.slug(title, 60) + str(position))
            row = (
                row_id,
                title[:300],
                url[:2048],
                (entry.get("art") or {}).get("url", "")[:2048],
                groups[:120],
                country[:8],
                language[:80],
                nsfw,
                closed,
                int(now),
            )
            # tvg-id is not unique: the same id is reused across regions, so
            # keying on it silently dropped whichever channel came second. A
            # suffix keeps every one of them without changing the id of the
            # first, so favourites and saved state still resolve.
            if row_id in taken:
                suffix = 1
                while "%s#%d" % (row_id, suffix) in taken:
                    suffix += 1
                row = ("%s#%d" % (row_id, suffix),) + row[1:]
                row_id = row[0]
            taken.add(row_id)
            for tag in cats:
                if tag:
                    tag_rows.append((row_id, tag))
            rows.append(row)
        if rows:
            if self.custom is not None:
                # Subscribed playlists are refreshed with the catalogue, so
                # "update" means the same thing for every channel in the list.
                self.custom.refresh_all()
                mine, mine_tags = self.custom.channel_rows(now)
                rows.extend(mine)
                tag_rows.extend(mine_tags)
            self.db.replace_channels(rows)
            tags = self.db.replace_channel_tags(tag_rows)
            self.state["tv"] = "ready"
            self._report("tv", stage="done", rows=len(rows), bytes=len(raw),
                         total=declared, unit="bytes")
            self.log("mirrored %d channels across %d categories "
                     "(%d closed, %d not safe for work)"
                     % (len(rows), tags, sum(r[8] for r in rows),
                        sum(r[7] for r in rows)))
            return len(rows)
        self.state["tv"] = "ready" if have else "empty"
        return have
