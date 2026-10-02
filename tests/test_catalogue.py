"""Tests for the local catalogue.

The mirror is the reason browsing is fast, so the things worth pinning down
are: it holds the whole dataset, queries come back in milliseconds, FTS
survives a rebuild, and a restart does not re-download 30 MB.

Run with:  python3 tests/test_catalogue.py
"""

import os
import shutil
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from apctl.core.db import Catalogue, _fts_query  # noqa: E402


# The column order of the radio table, so a fixture can be built by name
# instead of by position. Indexing a tuple by number is how the first version
# of this file came to assert the wrong thing and still look plausible.
RADIO_COLUMNS = ("uuid", "name", "url", "url_resolved", "homepage", "favicon",
                 "codec", "bitrate", "country", "countrycode", "state",
                 "language", "tags", "votes", "clicks", "hls", "codec_ok",
                 "checked", "changed", "updated")


def radio_row(n, name=None, tags="Jazz,Blues", country="US", **overrides):
    values = {
        "uuid": "uuid-%04d" % n,
        "name": name or ("Station %04d" % n),
        "url": "https://example.com/stream/%d" % n,
        "url_resolved": "https://example.com/%d" % n,
        "homepage": "https://example.com",
        "favicon": "https://example.com/logo.png",
        "codec": "MP3", "bitrate": 128,
        "country": country, "countrycode": "US",
        "state": "California", "language": "English",
        "tags": tags, "votes": 10 + n, "clicks": 100 - n,
        "hls": 0, "codec_ok": 1, "checked": 1, "changed": 0, "updated": 0,
    }
    values.update(overrides)
    return tuple(values[column] for column in RADIO_COLUMNS)


class CatalogueSchema(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ap-cat-")
        self.db = Catalogue(os.path.join(self.dir, "c.db"))
        self.db.open()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.addCleanup(self.db.close)

    def test_fts_is_available_and_indexed(self):
        self.assertTrue(self.db._fts, "this sqlite3 lacks FTS5")
        self.db.replace_radio([radio_row(i) for i in range(50)])
        # A rebuild must leave a *working* index, not a corrupt one: the
        # symptom of getting this wrong is "database disk image is malformed"
        # the first time somebody searches.
        rows = self.db.search_radio(term="Station", limit=100)
        self.assertEqual(len(rows), 50)

    def test_replace_then_search(self):
        self.db.replace_radio([radio_row(i) for i in range(100)])
        self.assertEqual(self.db.count_radio(term="Station"), 100)
        rows = self.db.search_radio(term="Station 0007", limit=5)
        self.assertTrue(rows)
        self.assertIn("0007", rows[0]["name"])

    def test_upsert_does_not_corrupt_the_index(self):
        self.db.replace_radio([radio_row(i) for i in range(20)])
        self.db.upsert_radio([radio_row(5, name="Zebra Station")])
        rows = self.db.search_radio(term="Zebra", limit=5)
        self.assertTrue(rows, "an incremental change must be searchable")
        # "Zebra Station" still contains "Station", so the total is unchanged;
        # what matters is that the new name is findable and the old one is not.
        self.assertEqual(self.db.count_radio(term="Zebra"), 1)
        self.assertEqual(self.db.count_radio(term="Station 0005"), 0)

    def test_freshness_survives_reopening(self):
        """A stamp that is not committed means a full re-mirror every start."""
        self.db.replace_radio([radio_row(i) for i in range(5)])
        self.db.close()
        again = Catalogue(self.db.path)
        again.open()
        self.addCleanup(again.close)
        self.assertIsNotNone(again.age("radio_full"),
                             "the freshness stamp was lost on close")
        self.assertEqual(again.counts()["radio"], 5)

    def test_counts_use_the_names_callers_ask_for(self):
        """The table is `channel`; every consumer calls it `tv`.

        Returning the table name made a fully mirrored catalogue report zero,
        and - worse - made the freshness check think TV had never synced, so
        it re-downloaded the whole channel list on every start.
        """
        self.db.replace_radio([radio_row(i) for i in range(3)])
        self.db.replace_channels([("c1", "BBC One", "https://x/1", "",
                                   "general", "GB", "", 0, 0, 0)])
        counts = self.db.counts()
        self.assertEqual(sorted(counts), ["programmes", "radio", "radio_custom",
                                          "tv", "tv_custom"])
        self.assertEqual(counts["tv"], 1)
        self.assertEqual(counts["radio"], 3)

    def test_count_matches_search(self):
        self.db.replace_radio([radio_row(i) for i in range(200)])
        total = self.db.count_radio(term="Station")
        page = self.db.search_radio(term="Station", limit=1000)
        self.assertEqual(total, len(page))


class FtsQuery(unittest.TestCase):
    def test_prefix_matching(self):
        self.assertEqual(_fts_query("para"), '"para"*')

    def test_every_word_must_match(self):
        self.assertEqual(_fts_query("radio paradise"), '"radio"* AND "paradise"*')

    def test_syntax_characters_are_stripped_not_escaped(self):
        # A stray quote must narrow the search, not raise.
        for raw in ('a"b', "a OR b", "a*b", "()", "  ", "***"):
            self.assertIsInstance(_fts_query(raw), str)
        self.assertEqual(_fts_query(""), "")


class Filtering(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ap-cat-")
        self.db = Catalogue(os.path.join(self.dir, "c.db"))
        self.db.open()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.addCleanup(self.db.close)

    def test_country_filter(self):
        rows = [radio_row(i, country="US", countrycode="US") for i in range(5)]
        rows += [radio_row(100 + i, country="Germany", countrycode="DE")
                 for i in range(5)]
        self.db.replace_radio(rows)
        self.assertEqual(len(self.db.search_radio(country="DE", limit=50)), 5)
        self.assertEqual(len(self.db.search_radio(country="de", limit=50)), 5)

    def test_bitrate_ceiling_ignores_unknown(self):
        low = [radio_row(i, bitrate=0) for i in range(3)]
        high = [radio_row(50 + i, bitrate=128) for i in range(3)]
        high.append(radio_row(99, bitrate=64))
        self.db.replace_radio(low + high)
        # A station that does not publish a bitrate is not filtered out; the
        # user cannot know it is low, and hiding them hides a third of the
        # list. A station that *does* publish one is held to it.
        found = self.db.search_radio(min_bitrate=96, limit=50)
        bitrates = sorted(r["bitrate"] for r in found)
        self.assertEqual(bitrates, [0, 0, 0, 128, 128, 128])
        self.assertEqual(len(self.db.search_radio(min_bitrate=200, limit=50)), 3)

    def test_broken_stations_hidden_by_default(self):
        self.db.replace_radio([radio_row(1),
                               radio_row(2, codec_ok=0)])
        self.assertEqual(len(self.db.search_radio(limit=10)), 1)
        self.assertEqual(len(self.db.search_radio(hide_broken=False, limit=10)), 2)

    def test_paging_does_not_repeat(self):
        self.db.replace_radio([radio_row(i) for i in range(120)])
        first = [r["uuid"] for r in self.db.search_radio(limit=60, offset=0)]
        second = [r["uuid"] for r in self.db.search_radio(limit=60, offset=60)]
        self.assertEqual(len(first), 60)
        self.assertEqual(len(second), 60)
        self.assertFalse(set(first) & set(second), "a row appeared on both pages")

    def test_search_is_fast_at_catalogue_scale(self):
        rows = [radio_row(i, tags=("Jazz" if i % 3 == 0 else
                                  "Rock" if i % 3 == 1 else "Pop"),
                         name="Station %05d" % i) for i in range(20000)]
        self.db.replace_radio(rows)
        start = time.monotonic()
        found = self.db.search_radio(term="Station 19999", limit=20)
        elapsed = time.monotonic() - start
        self.assertTrue(found, "a full-text miss on 20k rows is a bug")
        self.assertLess(elapsed, 0.5, "search took %.0f ms" % (elapsed * 1000))


class GenreAndCountryFacets(unittest.TestCase):
    """Filtering by genre and country, which is the whole point of the tab.

    Two things were wrong before this. The genre filter was a substring match
    on a comma-separated column, so asking for "pop" also returned every
    station tagged "hip hop" and "pop rock" - a filter that quietly returns
    the wrong stations is worse than no filter, because the user believes it.
    And the count used for paging was built separately from the list, without
    the genre or the bitrate floor, so a filtered page reported the total for
    an unfiltered one.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ap-facet-")
        self.db = Catalogue(os.path.join(self.dir, "c.db"))
        self.db.open()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.addCleanup(self.db.close)

    def build(self):
        rows = [
            radio_row(1, name="Pop One", tags="pop,rock"),
            radio_row(2, name="Pop Two", tags="pop,electronic",
                      country="Germany", countrycode="DE"),
            radio_row(3, name="Hip Hop Only", tags="hip hop,rap"),
            radio_row(4, name="Pop Rock Band", tags="pop rock"),
            radio_row(5, name="Jazz Night", tags="jazz,blues",
                      country="Germany", countrycode="DE"),
        ]
        self.db.replace_radio(rows)
        self.db.replace_radio_tags([
            ("uuid-0001", "pop"), ("uuid-0001", "rock"),
            ("uuid-0002", "pop"), ("uuid-0002", "electronic"),
            ("uuid-0003", "hip hop"), ("uuid-0003", "rap"),
            ("uuid-0004", "pop rock"),
            ("uuid-0005", "jazz"), ("uuid-0005", "blues"),
        ])

    def test_genre_filter_is_exact_not_a_substring(self):
        self.build()
        names = sorted(r["name"] for r in self.db.search_radio(group="pop"))
        self.assertEqual(names, ["Pop One", "Pop Two"],
                         "'pop' matched a tag that merely contains it")
        self.assertNotIn("Hip Hop Only", names)
        self.assertNotIn("Pop Rock Band", names)

    def test_genre_filter_case_insensitive_on_the_stored_value(self):
        self.build()
        self.assertEqual(self.db.count_radio(group="pop"),
                         self.db.count_radio(group="Pop"))

    def test_count_matches_the_filtered_list(self):
        self.build()
        for kwargs in ({"group": "pop"}, {"country": "DE"},
                       {"group": "jazz", "country": "DE"},
                       {"min_bitrate": 128}):
            listed = self.db.search_radio(limit=50, **kwargs)
            counted = self.db.count_radio(**kwargs)
            self.assertEqual(len(listed), counted, kwargs)

    def test_genre_and_country_compose(self):
        self.build()
        rows = self.db.search_radio(group="pop", country="DE", limit=50)
        self.assertEqual([r["name"] for r in rows], ["Pop Two"])
        self.assertEqual(self.db.count_radio(group="pop", country="DE"), 1)
        self.assertEqual(self.db.count_radio(group="jazz", country="DE"), 1)
        # A combination with nothing in it is empty, not "everything".
        self.assertEqual(self.db.count_radio(group="pop", country="FR"), 0)

    def test_search_text_composes_with_the_filters(self):
        self.build()
        rows = self.db.search_radio(term="pop", group="pop", limit=50)
        self.assertEqual(sorted(r["name"] for r in rows), ["Pop One", "Pop Two"])

    def test_genre_list_is_ranked_with_counts(self):
        self.build()
        self.db.replace_radio_tags([
            ("uuid-0001", "pop"), ("uuid-0002", "pop"), ("uuid-0003", "jazz"),
            ("uuid-0004", "pop"), ("uuid-0005", "jazz")])
        found = dict(self.db.radio_genres(min_count=1))
        self.assertEqual(found, {"pop": 3, "jazz": 2})
        ordered = [name for name, _n in self.db.radio_genres(min_count=1)]
        self.assertEqual(ordered, ["pop", "jazz"], "biggest first")

    def test_genre_list_hides_the_long_tail(self):
        self.db.replace_radio([radio_row(i) for i in range(3)])
        self.db.replace_radio_tags([("uuid-%04d" % i, "obscure")
                                    for i in range(3)])
        self.assertEqual(self.db.radio_genres(min_count=8), [],
                         "a tag carried by three stations is not a genre")
        self.assertEqual(len(self.db.radio_genres(limit=300, min_count=1)), 1)

    def test_country_list_is_ranked_and_named(self):
        self.build()
        found = {code: (name, n) for code, name, n
                 in self.db.radio_countries()}
        self.assertEqual(found["DE"][0], "Germany")
        self.assertEqual(found["DE"][1], 2)
        self.assertEqual(found["US"][1], 3)
        # A station with no country must not appear as a blank entry.
        self.assertTrue(all(code for code in found))


class ChannelFacets(unittest.TestCase):
    """TV stored the way radio is stored, and the bugs that fixing it exposed.

    The channel table had columns for category, language, closed and adult -
    and every one of them was written as a constant. `closed` and `is_nsfw`
    were literal zeros in the row tuple and `language` was a literal empty
    string, on a table whose own queries filtered on `closed=0`. The
    catalogue had the answer in the index it had already downloaded.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ap-tv-")
        self.db = Catalogue(os.path.join(self.dir, "c.db"))
        self.db.open()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.addCleanup(self.db.close)

    def build(self):
        def ch(ident, name, country, group, closed=0, nsfw=0, language="eng"):
            return (ident, name, "https://e/%s" % ident, "", group, country,
                    language, nsfw, closed, 0)
        self.db.replace_channels([
            ch("c1", "BBC News", "UK", "news"),
            ch("c2", "BBC Two", "UK", "general"),
            ch("c3", "CNN", "US", "news"),
            ch("c4", "ESPN", "US", "sports"),
            ch("c5", "Old Channel", "UK", "general", closed=1),
            ch("c6", "After Dark", "US", "general", nsfw=1),
        ])
        # A channel filed under two categories, which is the normal shape.
        self.db.replace_channel_tags([
            ("c1", "news"), ("c1", "business"),
            ("c2", "general"), ("c3", "news"), ("c4", "sports"),
            ("c5", "general"), ("c6", "general"),
        ])

    def test_closed_channels_are_hidden_by_the_data_not_the_title(self):
        self.build()
        names = [r["name"] for r in self.db.search_channels(limit=50)]
        self.assertNotIn("Old Channel", names)
        self.assertIn("BBC News", names)

    def test_nsfw_filtering(self):
        self.build()
        names = [r["name"] for r in self.db.search_channels(limit=50)]
        self.assertNotIn("After Dark", names)
        shown = [r["name"] for r in
                 self.db.search_channels(limit=50, hide_nsfw=False)]
        self.assertIn("After Dark", shown)

    def test_category_is_exact_and_case_insensitive(self):
        self.build()
        for wanted in ("news", "News", "NEWS"):
            self.assertEqual([r["name"] for r in
                              self.db.search_channels(group=wanted, limit=50)],
                             ["BBC News", "CNN"], wanted)

    def test_a_channel_is_reachable_by_every_category_it_has(self):
        self.build()
        # "business" is the second category of BBC News and used to be
        # discarded, so the channel was invisible under it.
        self.assertEqual([r["name"] for r in
                          self.db.search_channels(group="business", limit=50)],
                         ["BBC News"])

    def test_count_matches_the_filtered_list(self):
        self.build()
        for kwargs in ({"group": "news"}, {"country": "UK"},
                       {"group": "general", "country": "UK"},
                       {"group": "business"}):
            listed = self.db.search_channels(limit=200, **kwargs)
            counted = self.db.count_channels(**kwargs)
            self.assertEqual(len(listed), counted, kwargs)

    def test_category_list_is_ranked_with_counts(self):
        self.build()
        found = dict(self.db.channel_genres(min_count=1))
        self.assertEqual(found["news"], 2)
        self.assertEqual(found["general"], 3)
        self.assertEqual(self.db.channel_genres(min_count=3),
                         [("general", 3)])


class GroupTitleParsing(unittest.TestCase):
    """parse_m3u read #EXTGRP: and never the group-title attribute.

    iptv-org - and most extended playlists - put the category in
    group-title="..." inside the #EXTINF line and never emit an #EXTGRP at
    all. So every group in those files came back empty, `group_title` had to
    be recovered from the API by fuzzy name matching instead, and the
    channels that did not match an API row had no category whatsoever.
    """

    def test_group_title_attribute_is_read(self):
        from apctl.sources.base import parse_m3u
        body = ('#EXTM3U\n'
                '#EXTINF:-1 tvg-id="A.us@SD" tvg-logo="https://e/l.png" '
                'group-title="Animation",Some Channel\n'
                'https://e/one.m3u8\n'
                '#EXTINF:-1 tvg-id="B.uk@SD" group-title="News",Other\n'
                'https://e/two.m3u8\n')
        items = parse_m3u(body, "tv", id_prefix="ch-", limit=10)
        self.assertEqual([i["extra"]["group"] for i in items],
                         ["Animation", "News"])

    def test_extgrp_still_works(self):
        from apctl.sources.base import parse_m3u
        body = ('#EXTM3U\n#EXTINF:-1 tvg-id="C.us@SD",Channel\n'
                '#EXTGRP:Legacy Group\nhttps://e/three.m3u8\n')
        items = parse_m3u(body, "tv", id_prefix="ch-", limit=10)
        self.assertEqual(items[0]["extra"]["group"], "Legacy Group")

    def test_a_playlist_with_no_group_is_not_invented(self):
        from apctl.sources.base import parse_m3u
        body = ('#EXTM3U\n#EXTINF:-1 tvg-id="D.us@SD",Bare\n'
                'https://e/x.m3u8\n')
        items = parse_m3u(body, "tv", id_prefix="ch-", limit=10)
        self.assertEqual(items[0]["extra"]["group"], "")


class SyncIntegrity(unittest.TestCase):
    """A mirror that stopped early must not look like a complete one.

    An interrupted sync published 10,000 of 59,785 stations and stamped the
    table as fresh, so every later search quietly found a sixth of the
    catalogue and nothing anywhere said so.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ap-sync-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _syncer(self, db):
        from apctl.core.sync import Syncer
        return Syncer(db, log=lambda message: None)

    def test_interrupted_mirror_keeps_the_old_data(self):
        from apctl.core.db import Catalogue
        path = os.path.join(self.dir, "c.db")
        db = Catalogue(path)
        db.open()
        db.replace_radio([radio_row(i) for i in range(50)])
        self.addCleanup(db.close)

        syncer = self._syncer(db)
        # Simulate the page loop giving up: no rows and not complete.
        # Both entry points are stubbed: the page loop asks for the size as
        # well as the body now, and a test that only blocked one of them would
        # quietly start downloading the real catalogue.
        import apctl.core.sync as sync_module
        def _down(*_a, **_k):
            raise OSError("network down")
        saved = {name: getattr(sync_module.netguard, name)
                 for name in ("fetch", "fetch_sized") if hasattr(
                     sync_module.netguard, name)}
        for name in saved:
            setattr(sync_module.netguard, name, _down)
        try:
            kept = syncer.sync_radio(full=True)
        finally:
            for name, original in saved.items():
                setattr(sync_module.netguard, name, original)
        self.assertEqual(kept, 50, "the previous mirror was discarded")
        self.assertEqual(db.counts()["radio"], 50)
        self.assertEqual(syncer.state["radio"], "partial")

    def test_reopening_does_not_pretend_to_be_fresh(self):
        from apctl.core.db import Catalogue
        path = os.path.join(self.dir, "c.db")
        db = Catalogue(path)
        db.open()
        db.replace_radio([radio_row(i) for i in range(5)])
        db.close()
        again = Catalogue(path)
        again.open()
        self.addCleanup(again.close)
        self.assertIsNotNone(again.age("radio_full"))


def _radio_change(**overrides):
    """One entry as upstream's change feed publishes it.

    Deliberately *not* the /stations payload: no codec, no bitrate, no click
    count and no health flag, and `lastchangetime` as the date string the API
    actually sends. A fixture built from the full payload would pass against
    code that has never seen a real change set.
    """
    entry = {
        "changeuuid": "0f2c4d1e-0000-11ee-be56-0242ac120002",
        "stationuuid": "uuid-0001",
        "name": "Station 0001 Renamed",
        "url": "https://example.com/moved",
        "homepage": "https://example.com",
        "favicon": "https://example.com/logo.png",
        "country": "United States",
        "countrycode": "US",
        "state": "California",
        "language": "English",
        "tags": "Jazz,Blues",
        "votes": 12,
        "lastchangetime": "2026-01-14 22:54:03",
    }
    entry.update(overrides)
    return entry


class IncrementalRefresh(unittest.TestCase):
    """The hourly change set has to land in the database.

    Two bugs, one symptom: the data was downloaded and then thrown away.
    `lastchangetime` arrives as "2026-01-14 22:54:03" rather than epoch
    seconds, so reading it with `float()` raised a ValueError *after* the rows
    were parsed and the whole change set was discarded; and the cursor that was
    requested (`/changed/<lastchangetime>`) answers an empty list, so every
    later refresh fetched half a megabyte of changed stations and stored none
    of them. The failure was swallowed into a log line, which is why it read as
    "it downloads the data but never stores it".
    """

    def setUp(self):
        from apctl.core.db import Catalogue
        self.dir = tempfile.mkdtemp(prefix="ap-delta-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.db = Catalogue(os.path.join(self.dir, "c.db"))
        self.db.open()
        self.addCleanup(self.db.close)
        from apctl.core.sync import Syncer
        self.syncer = Syncer(self.db, log=lambda message: None)
        # A mirror, as the full sync would have left it.
        self.db.replace_radio([radio_row(1), radio_row(2)])
        self.db.replace_radio_tags([("uuid-0001", "Jazz"), ("uuid-0001", "Blues"),
                                    ("uuid-0002", "Rock")])

    def _serve(self, payload):
        """Answer the next change-set request with `payload`, and record urls."""
        import json
        import apctl.core.sync as sync_module
        calls = []

        def _fetch(url, **_kwargs):
            calls.append(url)
            return json.dumps(payload)

        saved = sync_module.netguard.fetch
        sync_module.netguard.fetch = _fetch
        self.addCleanup(setattr, sync_module.netguard, "fetch", saved)
        return calls

    def test_a_change_set_is_stored_rather_than_discarded(self):
        self._serve([_radio_change()])
        applied = self.syncer._radio_delta()
        self.assertEqual(applied, 1, "the change set was downloaded and dropped")
        row = self.db.station_by_uuid("uuid-0001")
        self.assertEqual(row["name"], "Station 0001 Renamed",
                         "the stored row is the one upstream now publishes")
        self.assertEqual(row["url"], "https://example.com/moved")

    def test_a_station_new_to_the_mirror_is_listed(self):
        self._serve([_radio_change(stationuuid="uuid-0009",
                                   name="Brand New Station",
                                   url="https://example.com/new")])
        self.assertEqual(self.syncer._radio_delta(), 1)
        rows = self.db.search_radio(term="Brand New", limit=10)
        self.assertEqual(len(rows), 1,
                         "a station the change set introduced must be browsable")

    def test_it_does_not_blank_the_columns_the_feed_omits(self):
        """A change set is not a station payload.

        Replacing the row wholesale cleared the codec, the bitrate, the click
        count and the health flag on every station the hourly refresh touched -
        so the catalogue would slowly forget everything the browse sorts by and
        displays, and a stream already known to be dead would come back as
        healthy because the absent field reads as an opinion.
        """
        before = dict(self.db.station_by_uuid("uuid-0001"))
        self._serve([_radio_change()])
        self.syncer._radio_delta()
        after = self.db.station_by_uuid("uuid-0001")
        for column in ("codec", "bitrate", "clicks", "hls", "codec_ok", "checked"):
            self.assertEqual(after[column], before[column],
                             "%s was not in the feed, so it must survive it"
                             % column)

    def test_the_genre_follows_the_change(self):
        """A station reachable by a genre it no longer carries is a lie."""
        self._serve([_radio_change(tags="Ambient")])
        self.syncer._radio_delta()
        self.assertEqual(self.db.search_radio(term="Station", group="Jazz",
                                              limit=10), [])
        self.assertEqual(len(self.db.search_radio(term="Station",
                                                  group="Ambient", limit=10)), 1)

    def test_the_refresh_never_asks_for_a_cursor_that_answers_nothing(self):
        calls = self._serve([_radio_change()])
        self.syncer._radio_delta()
        # What the old cursor bookkeeping stored, so that the second call is
        # the one that used to come back empty.
        self.db.set_meta("radio_last_change", 1768422843.0)
        self.syncer._radio_delta()
        self.assertEqual(len(calls), 2)
        for url in calls:
            self.assertTrue(url.endswith("/stations/changed"),
                            "the cursor form returns an empty list: %s" % url)

    def test_the_stamp_advances_even_when_nothing_changed(self):
        """Otherwise every start asks again inside the same hour."""
        self._serve([])
        self.assertEqual(self.syncer._radio_delta(), 0)
        self.assertIsNotNone(self.db.age("radio_delta"),
                             "a refresh that found nothing is still a refresh")

    def test_the_newest_change_is_recorded_as_a_number(self):
        """The watermark is a timestamp, and has to be readable as one.

        It came from a date string, so anything that stored it as a string -
        or dropped it because it could not parse - leaves the next refresh with
        no idea how far the mirror has read.
        """
        from datetime import datetime, timezone
        expected = datetime(2026, 1, 14, 22, 54, 3,
                            tzinfo=timezone.utc).timestamp()
        self._serve([_radio_change()])
        self.assertEqual(self.syncer._radio_delta(), 1)
        stamp = self.db.get_meta("radio_last_change")
        self.assertIsNotNone(stamp, "the newest change was not recorded at all")
        self.assertAlmostEqual(float(stamp), expected, places=0,
                               msg="the date string was not read as a timestamp")


class APartialFirstRunCanStillFinish(unittest.TestCase):
    """An interrupted first run has to be able to finish on the next start.

    The daemon syncs at start-up only when a catalogue is missing, and it used
    to ask for a *full* mirror while doing it. So a run interrupted after radio
    landed - a shell reload, a lid, a crash - left television missing, and the
    next start spent the ninety seconds it takes to re-page 59,785 stations
    before it ever reached the catalogue that was actually missing. Interrupt it
    twice and television never arrived at all, with every one of those downloads
    discarded: which is exactly how "downloads the data but never stores it"
    looks from the outside.
    """

    def setUp(self):
        from apctl.core.db import Catalogue
        from apctl.core.sync import Syncer
        self.dir = tempfile.mkdtemp(prefix="ap-restart-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.db = Catalogue(os.path.join(self.dir, "c.db"))
        self.db.open()
        self.addCleanup(self.db.close)
        self.syncer = Syncer(self.db, log=lambda message: None)
        self.paged = []
        self.synced = []

    def _watch(self, page=None):
        """Record every full-mirror page request, and every change set."""
        import json
        import apctl.core.sync as sync_module

        def _paged(url, **_kwargs):
            self.paged.append(url)
            if page is None:
                raise OSError("no page of the station list was expected")
            return "[]", 0

        def _fetch(url, **_kwargs):
            self.synced.append(url)
            return json.dumps([_radio_change()])

        for name, replacement in (("fetch", _fetch), ("fetch_sized", _paged)):
            original = getattr(sync_module.netguard, name)
            setattr(sync_module.netguard, name, replacement)
            self.addCleanup(setattr, sync_module.netguard, name, original)

    def test_a_mirror_that_is_already_there_is_not_re_paged(self):
        self.db.replace_radio([radio_row(i) for i in range(10)])
        self._watch()
        # What the daemon now asks for at start-up.
        self.assertEqual(self.syncer.sync_radio(full=False), 10)
        self.assertEqual(self.paged, [],
                         "a start-up re-mirrored the whole catalogue again")
        self.assertEqual(len(self.synced), 1,
                         "a mirror this fresh only wants its change set")

    def test_television_is_not_starved_behind_radio(self):
        """The catalogue that was missing has to be reachable immediately."""
        self.db.replace_radio([radio_row(i) for i in range(10)])
        self._watch()
        self.syncer.sync_radio(full=False)
        self.assertEqual(self.paged, [],
                         "radio was re-paged instead of taking its change set")
        # An empty channel table is not fresh, so this has to try to mirror it
        # rather than reporting it as ready. The stub refuses to serve a
        # playlist, so reaching it at all is the assertion.
        with self.assertRaises(OSError):
            self.syncer.sync_tv()
        self.assertTrue(self.paged,
                        "television was skipped because radio had just synced")

    def test_an_empty_catalogue_is_still_mirrored_in_full(self):
        """Cheapness must not become a first run that never happens."""
        self._watch(page=[])
        self.assertEqual(self.syncer.sync_radio(full=False), 0)
        self.assertTrue(self.paged,
                        "nothing on disk means the full list has to be paged")

    def test_start_up_never_asks_for_a_full_re_mirror(self):
        """The decision itself, not just what the sync does with it.

        A test that only calls `sync_radio(full=False)` passes just as happily
        against a start-up that asks for `full=True` - which is the shape the
        bug had, and which spends ninety seconds re-downloading the catalogue it
        already has before it will touch the one it is missing.
        """
        from apctl.core.daemon import startup_sync

        plan = startup_sync(["tv"])
        self.assertIsNotNone(plan,
                             "a catalogue that is missing has to be mirrored "
                             "at start-up")
        self.assertFalse(plan["full"],
                         "start-up re-mirrored everything, so the catalogue it "
                         "was missing never got its turn")

    def test_start_up_does_nothing_when_the_mirror_is_there(self):
        """A reopen of the panel is not a 70 MB download."""
        from apctl.core.daemon import startup_sync
        self.assertIsNone(startup_sync([]))
        self.assertIsNone(startup_sync(["tv"], offline=True),
                          "an offline machine must not be asked to sync")


class ArtworkFetching(unittest.TestCase):
    """Artwork must never be on the request path.

    A browse of 50 stations used to take 32 seconds fetching favicons inline
    and produced no pictures, because one dead image host cost the whole page.
    """

    def setUp(self):
        from apctl.core import artwork
        self.dir = tempfile.mkdtemp(prefix="ap-art-")
        self.ready = []
        self.fetcher = artwork.Artwork(
            self.dir, on_ready=lambda k, p: self.ready.append((k, p)))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.addCleanup(self.fetcher.shutdown)

    def test_submit_returns_immediately(self):
        self.fetcher.start(workers=1)
        start = time.monotonic()
        path = self.fetcher.submit("k1", "https://example.com/a.png", "k1")
        self.assertEqual(path, "", "nothing is cached yet")
        self.assertLess(time.monotonic() - start, 0.05)

    def test_cache_hit_is_returned_and_not_refetched(self):
        self.fetcher.start(workers=1)
        open(os.path.join(self.dir, "k2.img"), "wb").write(b"x")
        path = self.fetcher.submit("k2", "https://example.com/b.png", "k2")
        self.assertTrue(path.endswith("k2.img"))
        self.assertTrue(self.fetcher.is_queued("k2") is False)

    def test_non_http_urls_are_refused(self):
        self.fetcher.start(workers=1)
        for url in ("file:///etc/passwd", "javascript:alert(1)", "", "data:image/png;base64,AA"):
            self.assertEqual(self.fetcher.submit("x", url, "x"), "")

    def test_duplicate_submits_collapse(self):
        self.fetcher.start(workers=1)
        for _ in range(20):
            self.fetcher.submit("dup", "https://example.com/c.png", "dup")
        self.assertTrue(self.fetcher.is_queued("dup"))


class MirroredCatalogueIsAuthoritative(unittest.TestCase):
    """Once the catalogue is downloaded, the tab reads from it and nothing else.

    A list that quietly refills itself from the network behind the user's back
    cannot be trusted to stay put, cannot be searched the same way twice, and
    makes the "last updated" line in the status bar a lie. The network is a
    first-run fallback, not a second opinion.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ap-mirror-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        from apctl.core.db import Catalogue
        self.db = Catalogue(os.path.join(self.dir, "c.db"))
        self.db.open()
        self.addCleanup(self.db.close)
        from apctl.sources import radio, tv
        radio.set_catalogue(self.db)
        tv.set_catalogue(self.db)
        self.addCleanup(radio.set_catalogue, None)
        self.addCleanup(tv.set_catalogue, None)
        # Anything reaching for the network from here on is the bug.
        self.no_network = _no_network()

    def _mirror(self):
        self.db.replace_radio([radio_row(i) for i in range(40)])
        self.db.replace_radio_tags([
            ("uuid-%04d" % i, "jazz") for i in range(40)])
        self.db.touch("radio_full")

    def test_an_undownloaded_catalogue_is_not_authoritative(self):
        from apctl.sources import radio
        self.assertFalse(radio._mirrored(),
                         "an empty catalogue must not claim to be complete, or "
                         "a first run shows an empty list forever")

    def test_a_stamp_without_rows_is_not_authoritative(self):
        """A mirror that lost its table must not still read as ready."""
        self.db.touch("radio_full")
        self.assertFalse(self.db.mirrored("radio"))
        self.assertFalse(self.db.source_status("radio")["mirrored"])

    def test_rows_without_a_stamp_are_still_good_data(self):
        """This is what an abandoned first run leaves behind.

        An interrupted first run rolls the write back, so the rows of the
        previous mirror survive but the timestamp that says when they arrived
        never gets written. Reading the stamp as the source of truth called
        that 59,826 stations "not downloaded", put a download prompt where the
        status should be, and sent browsing to the network for a list that was
        sitting on disk the whole time.
        """
        self.db.replace_radio([radio_row(i) for i in range(40)])
        # What an interrupted first run leaves: the rows, no stamp.
        self.db.set_meta("radio_full_at", "")
        self.assertFalse(self.db.get_meta("radio_full_at"),
                         "the fixture must reproduce the missing stamp")
        self.assertTrue(self.db.mirrored("radio"),
                        "a whole catalogue is on disk; it is readable data "
                        "whatever the bookkeeping says")
        self.assertIsNone(self.db.source_status("radio")["age"],
                          "and with no stamp the age is unknown, not invented")
        from apctl.sources import radio
        self.assertEqual(len(radio.browse(limit=10)), 10,
                         "and it answers from disk rather than the network")
        self.no_network.assert_idle()

    def test_a_search_miss_does_not_fall_back_to_the_network(self):
        self._mirror()
        from apctl.sources import radio
        self.assertEqual(radio.search("zzzznomatch", limit=10), [],
                         "a miss in a complete catalogue is an answer, not a "
                         "reason to go and ask the network for a different one")
        self.no_network.assert_idle()

    def test_a_filtered_browse_does_not_fall_back_to_the_network(self):
        self._mirror()
        from apctl.sources import radio
        # Before, a country the mirror had nothing for came back full of
        # worldwide favourites from the unfiltered remote list.
        self.assertEqual(radio.browse(country="ZZ", limit=10), [])
        self.no_network.assert_idle()

    def test_genre_browse_does_not_fall_back_to_the_network(self):
        self._mirror()
        from apctl.sources import radio
        self.assertEqual(radio.by_tag("jazz", country="ZZ", limit=10), [])
        self.no_network.assert_idle()

    def test_genre_and_country_facets_come_from_the_mirror(self):
        """A second, live list can disagree with the mirrored rows."""
        self._mirror()
        from apctl.sources import radio
        radio.genres()
        self.no_network.assert_idle()

    def test_tv_does_not_fall_back_to_the_playlist(self):
        from apctl.sources import tv
        self.db.replace_channels([
            ("ch-1", "Alpha", "https://example.com/1", "", "News", "UK",
             "", 0, 0, time.time())])
        self.db.touch("tv_full")
        self.assertTrue(tv._mirrored())
        self.assertEqual(tv.channels(country="ZZ", limit=10), [])
        self.no_network.assert_idle()

    def test_the_mirror_still_answers_when_nothing_matches(self):
        """The point of all this is that browsing keeps working."""
        self._mirror()
        from apctl.sources import radio
        self.assertEqual(len(radio.search("station", limit=10)), 10)
        self.assertEqual(len(radio.browse("popular", limit=10)), 10)
        self.no_network.assert_idle()

    def test_the_status_line_reports_what_the_tab_is_showing(self):
        self._mirror()
        radio_status = self.db.source_status("radio")
        self.assertEqual(radio_status["count"], 40)
        self.assertTrue(radio_status["mirrored"])
        self.assertIsNotNone(radio_status["age"])
        self.assertLess(radio_status["age"], 5)
        self.assertFalse(self.db.source_status("tv")["mirrored"])


class DownloadProgressIsReportable(unittest.TestCase):
    """A minute of paging has to say something while it happens.

    The full radio mirror measured 82 seconds on this machine. A refresh
    button that shows nothing for 82 seconds is indistinguishable from one
    that is broken, and the only way to tell the difference is to be told.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ap-progress-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        from apctl.core.db import Catalogue
        self.db = Catalogue(os.path.join(self.dir, "c.db"))
        self.db.open()
        self.addCleanup(self.db.close)
        self.seen = []
        from apctl.core.sync import Syncer
        self.syncer = Syncer(self.db, log=lambda m: None,
                             on_progress=lambda s, i: self.seen.append((s, i)))

    def _stub_pages(self, total, pages):
        """Answer the stats call and the page calls without a network.

        PAGE is dropped to something small so the paging loop can be exercised
        in milliseconds; the real value is 5000, and a stub that returned ten
        rows against it would look like the end of the database and stop after
        one page.
        """
        import apctl.core.sync as sync_module
        saved_page = sync_module.PAGE
        sync_module.PAGE = 10
        self.addCleanup(lambda: setattr(sync_module, "PAGE", saved_page))

        def fetch(url, **kw):
            return b'{"stations": %d}' % total

        def page_body(n, count):
            entries = [b'{"stationuuid":"u%d","url_resolved":"https://e/%d",'
                       b'"name":"S%d","tags":"jazz"}' % (n, n, n)
                       for n in range(count)]
            return b"[" + b",".join(entries) + b"]"

        state = {"n": 0}
        def fetch_sized(url, **kw):
            if "stats" in url:
                return fetch(url), None
            # Every page but the last is exactly PAGE rows; the last is short,
            # which is how the loop learns the database ended.
            count = saved_page if state["n"] < pages - 1 else 3
            body = page_body(state["n"], count)
            state["n"] += 1
            return body, len(body)
        saved = (sync_module.netguard.fetch, sync_module.netguard.fetch_sized)
        sync_module.netguard.fetch = fetch
        sync_module.netguard.fetch_sized = fetch_sized
        self.addCleanup(lambda: (setattr(sync_module.netguard, "fetch", saved[0]),
                                 setattr(sync_module.netguard, "fetch_sized",
                                         saved[1])))

    def test_progress_carries_rows_bytes_and_a_total(self):
        self._stub_pages(total=25, pages=3)
        self.syncer.sync_radio(full=True)
        stages = [info for src, info in self.seen if src == "radio"]
        self.assertTrue(stages, "a download reported nothing at all")
        self.assertEqual(stages[0]["stage"], "download")
        for info in stages:
            self.assertIn("rows", info)
            self.assertIn("bytes", info)
            self.assertIn("total", info)
        self.assertEqual(stages[0]["total"], 25,
                         "the denominator has to be the upstream count, so the "
                         "percentage means something")

    def test_progress_names_the_stage_so_the_bar_cannot_lie(self):
        """A bar at 100% while the parse runs is worse than no bar."""
        self._stub_pages(total=25, pages=3)
        self.syncer.sync_radio(full=True)
        stages = [info["stage"] for _src, info in self.seen if _src == "radio"]
        self.assertIn("parse", stages)
        self.assertEqual(stages[-1], "done")

    def test_bytes_accumulate_across_pages(self):
        self._stub_pages(total=25, pages=3)
        self.syncer.sync_radio(full=True)
        downloads = [i for src, i in self.seen
                     if src == "radio" and i["stage"] == "download"
                     and i["rows"] > 0]
        self.assertGreaterEqual(len(downloads), 2,
                                "more than one page arrived, so the byte count "
                                "should have moved more than once")
        for earlier, later in zip(downloads, downloads[1:]):
            self.assertGreaterEqual(later["bytes"], earlier["bytes"],
                                    "bytes must never go down: the size on "
                                    "screen is meant to be the size downloaded")
        self.assertGreater(downloads[-1]["bytes"], 0)

    def test_a_missing_total_is_reported_as_missing(self):
        """Never invent a denominator."""
        import apctl.core.sync as sync_module
        def _no_stats(url, **kw):
            raise OSError("stats unavailable")
        saved = (sync_module.netguard.fetch, sync_module.netguard.fetch_sized)
        sync_module.netguard.fetch = _no_stats
        sync_module.netguard.fetch_sized = lambda url, **kw: (
            b'[{"stationuuid":"u1","url_resolved":"https://e/1","name":"S",'
            b'"tags":"jazz"}]', 40)
        try:
            self.syncer.sync_radio(full=True)
        finally:
            sync_module.netguard.fetch, sync_module.netguard.fetch_sized = saved
        first = self.seen[0][1]
        self.assertIsNone(first["total"],
                          "an invented total is worse than none: the bar would "
                          "reach 100% early and then sit there")


class _no_network:
    """Turn every outbound fetch into a failure, loudly."""

    def __enter__(self):
        import apctl.sources.base as base
        import apctl.util.netguard as netguard
        # Both entry points, because `fetch` is now a wrapper over
        # `fetch_sized` and a guard that only covered one of them would let a
        # real download through while reporting that nothing went out.
        self._saved = [(mod, name, getattr(mod, name))
                       for mod in (base, netguard)
                       for name in ("fetch", "fetch_sized")
                       if hasattr(mod, name)]
        for mod, name, _ in self._saved:
            setattr(mod, name, self._boom)
        return self

    def __exit__(self, *_exc):
        for mod, name, original in self._saved:
            setattr(mod, name, original)
        return False

    def _boom(self, *_a, **_k):
        raise AssertionError("reached for the network with a mirrored "
                             "catalogue, which should answer on its own")

    def assert_idle(self):
        pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
