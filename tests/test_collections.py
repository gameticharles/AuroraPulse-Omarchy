"""Playlists, albums and folders: opened, played and queued whole.

Run with:  python3 tests/test_collections.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from apctl.core import daemon  # noqa: E402
from apctl.sources import local, music, youtube  # noqa: E402


def read(name):
    with open(os.path.join(ROOT, name), "r", encoding="utf-8") as fh:
        return fh.read()


class PlaylistLinks(unittest.TestCase):
    def test_links_and_ids_that_name_a_playlist(self):
        pid = "PLOzDu-MXXLliO9fBNZOQTBDddoA3FzZUo"
        for raw in (pid,
                    "https://www.youtube.com/playlist?list=" + pid,
                    "https://www.youtube.com/watch?v=jfKfPfyJRdk&list=%s&index=2&si=x" % pid,
                    "https://m.youtube.com/playlist?list=%s#top" % pid,
                    "https://music.youtube.com/playlist?list=" + pid,
                    "VL" + pid):
            self.assertEqual(youtube.playlist_id(raw), pid, raw)

    def test_albums_and_mixes(self):
        self.assertEqual(youtube.playlist_id("OLAK5uy_n7zeeXzEN1gfE5dCsZdSv1Wf_aMrDuY7w"),
                         "OLAK5uy_n7zeeXzEN1gfE5dCsZdSv1Wf_aMrDuY7w")
        self.assertEqual(youtube.playlist_id("RDCLAK5uy_nxt6HHLwT3Jw4-CkWVe4aIWsBLSQS2tPQ"),
                         "RDCLAK5uy_nxt6HHLwT3Jw4-CkWVe4aIWsBLSQS2tPQ")

    def test_anything_else_is_not_a_playlist(self):
        """A one-word search must never be read as a playlist id, and a
        `list=` on another site is not YouTube's."""
        for raw in ("lofi", "daft punk", "",
                    "https://example.com/?list=PLxxxxxxxxxxxxxx",
                    "https://www.youtube.com/watch?v=jfKfPfyJRdk"):
            self.assertEqual(youtube.playlist_id(raw), "", raw)

    def test_a_pasted_link_is_not_cut_short(self):
        """The old playlist() kept 80 characters of what it was given, which
        cut a long watch link before its list id."""
        pid = "PLOzDu-MXXLliO9fBNZOQTBDddoA3FzZUo"
        link = "https://www.youtube.com/watch?v=jfKfPfyJRdk&pp=" + "x" * 90 + "&list=" + pid
        self.assertEqual(youtube.playlist_id(link), pid)


class PlaylistRows(unittest.TestCase):
    def test_a_playlist_search_result_is_a_collection_row(self):
        built = youtube.playlist_item({
            "id": "PLfzxR9TEHkQfFPjC1wfveaO1r81gx-6eL", "title": "Upbeat Lofi",
            "uploader": "A Lofi Soul", "ie_key": "YoutubeTab",
            "url": "https://www.youtube.com/playlist?list=PLfzxR9TEHkQfFPjC1wfveaO1r81gx-6eL"})
        self.assertEqual(built["kind"], "playlist")
        self.assertEqual(built["source"], "youtube")
        self.assertEqual(built["artist"], "A Lofi Soul")
        self.assertEqual(built["extra"]["playlist_id"], "PLfzxR9TEHkQfFPjC1wfveaO1r81gx-6eL")
        self.assertTrue(built["url"].endswith("list=PLfzxR9TEHkQfFPjC1wfveaO1r81gx-6eL"))

    def _renderer(self, title, second, browse="", play_list=""):
        runs = lambda text: {"musicResponsiveListItemFlexColumnRenderer":
                             {"text": {"runs": [{"text": text}]}}}
        node = {"flexColumns": [runs(title), runs(second)]}
        if browse:
            node["navigationEndpoint"] = {"browseEndpoint": {"browseId": browse}}
        if play_list:
            node["overlay"] = {"musicItemThumbnailOverlayRenderer": {"content": {
                "musicPlayButtonRenderer": {"playNavigationEndpoint": {
                    "watchPlaylistEndpoint": {"playlistId": play_list}}}}}}
        return {"musicResponsiveListItemRenderer": node}

    def test_music_playlists_and_albums_are_read(self):
        data = {"contents": [
            self._renderer("Afrobeats Hits", "YouTube Music • 50 songs",
                           browse="VLRDCLAK5uy_nxt6HHLwT3Jw4-CkWVe4aIWsBLSQS2tPQ"),
            # An album's browse target is an album page; its list is the
            # play button's.
            self._renderer("Streets of Africa", "Album • Burna Boy • 2024",
                           browse="MPREb_T45TZeaDTTb",
                           play_list="OLAK5uy_nXydd5Dh7TJmwf_EDhDJ6qL3FrVPkqRY8"),
            # A song in the same answer is not a collection.
            {"musicResponsiveListItemRenderer": {
                "playlistItemData": {"videoId": "abcdefghijk"},
                "flexColumns": [{}]}},
        ]}
        rows = music._collections(data)
        self.assertEqual([r["kind"] for r in rows], ["playlist", "album"])
        self.assertEqual(rows[0]["extra"]["playlist_id"],
                         "RDCLAK5uy_nxt6HHLwT3Jw4-CkWVe4aIWsBLSQS2tPQ")
        self.assertEqual(rows[0]["extra"]["count"], 50)
        self.assertEqual(rows[0]["artist"], "YouTube Music")
        self.assertEqual(rows[1]["extra"]["playlist_id"],
                         "OLAK5uy_nXydd5Dh7TJmwf_EDhDJ6qL3FrVPkqRY8")
        self.assertEqual(rows[1]["artist"], "Burna Boy")
        self.assertEqual(rows[1]["extra"]["year"], "2024")
        self.assertEqual(rows[1]["extra"]["label"], "Album")

    def test_music_search_pages_are_kept_per_kind(self):
        """Songs keep their old key, so "load more" on a song search still
        finds its pages; playlists and albums have their own."""
        self.assertEqual(music.page_key("Burna Boy"), "burna boy")
        self.assertEqual(music.page_key("Burna Boy", "albums"), "albums:burna boy")


class DaemonCalls(unittest.TestCase):
    def test_collections_are_never_played_or_queued_as_themselves(self):
        for kind in ("folder", "playlist", "album", "artist", "genre", "podcast"):
            self.assertIn(kind, daemon.COLLECTION_KINDS)
        self.assertNotIn("track", daemon.COLLECTION_KINDS)
        src = read("src/apctl/core/daemon.py")
        start = src.index("def cmd_enqueue")
        self.assertIn("COLLECTION_KINDS", src[start:start + 900],
                      "a folder row queued as itself is a dead queue entry")

    def test_youtube_playlist_is_called_with_its_arguments_in_order(self):
        """It was called as playlist(query, limit, cache_dir, budget), so the
        page size went in as the art folder."""
        src = read("src/apctl/core/daemon.py")
        self.assertNotIn("youtube.playlist(query, limit, cache_dir", src)
        self.assertIn("youtube.playlist(query, cache_dir, 500", src)


class FolderFiles(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ap-folder-")
        for name in ("Disc 1/10 - ten.mp3", "Disc 1/2 - two.mp3", "Disc 1/1 - one.flac",
                     "intro.ogg", "notes.txt", ".hidden/secret.mp3", "film.mkv",
                     ".dot.mp3"):
            path = os.path.join(self.dir, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(b"x")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_playable_files_in_name_order_hidden_ones_left_out(self):
        root, found = local.folder_files(self.dir, {})
        names = [os.path.relpath(p, root) for p in found]
        self.assertEqual(names, ["Disc 1/1 - one.flac", "Disc 1/2 - two.mp3",
                                 "Disc 1/10 - ten.mp3", "film.mkv", "intro.ogg"])

    def test_a_missing_folder_or_the_root_is_refused(self):
        for bad in (os.path.join(self.dir, "nope"), "/", ""):
            with self.assertRaises(Exception) as caught:
                local.folder_files(bad, {})
            self.assertEqual(getattr(caught.exception, "reason", ""), "not-found")

    def test_a_folder_lists_without_the_library(self):
        """Files past the probe limit are named after the file, so a huge
        folder still opens; the library's own entries are reused."""

        class Library:
            def entries_under(self, folder):
                return {}

        class Store:
            library = Library()
            art_dir = self.dir

        before = local.FOLDER_PROBE_MAX
        local.FOLDER_PROBE_MAX = 0
        try:
            _root, rows = local.folder_tracks(Store(), {}, self.dir)
        finally:
            local.FOLDER_PROBE_MAX = before
        self.assertEqual([r["title"] for r in rows],
                         ["1 - one", "2 - two", "10 - ten", "film", "intro"])
        self.assertEqual(rows[0]["album"], "Disc 1")
        self.assertEqual(rows[3]["extra"]["media"], "video")
        self.assertTrue(all(r["source"] == "local" and r["kind"] == "track" for r in rows))


def _track(uid, title, **rest):
    entry = {"uid": "local:%s" % uid, "source": "local", "kind": "track",
             "title": title, "url": "/music/%s.mp3" % uid,
             "extra": {"path": "/music/%s.mp3" % uid, "media": "audio"}}
    entry.update(rest)
    return entry


class DaemonFeatures(unittest.TestCase):
    """The 1.6 commands, called on a daemon with a throwaway store."""

    def setUp(self):
        from apctl.core.daemon import Daemon
        from apctl.core.state import Store
        self.dir = tempfile.mkdtemp(prefix="ap-daemon-")
        self.events = []
        self.d = Daemon(store=Store(self.dir, self.dir, self.dir), emit=self.events.append)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def of(self, kind):
        return [e for e in self.events if e.get("type") == kind]

    def test_play_next_goes_right_after_the_current_entry(self):
        a, b, c, x = (_track(n, n) for n in "abcx")
        self.d.queue, self.d.index = [a, b, c], 0
        self.d.cmd_enqueue({"items": [x], "next": True})
        self.assertEqual([e["title"] for e in self.d.queue], ["a", "x", "b", "c"])
        # Already waiting further down: moved up, not doubled.
        self.d.cmd_enqueue({"items": [c], "next": True})
        self.assertEqual([e["title"] for e in self.d.queue], ["a", "c", "x", "b"])
        self.assertEqual(self.d.index, 0)
        self.assertEqual(self.of("notice")[-1]["title"], "Plays next")

    def test_the_queue_is_saved_as_a_playlist(self):
        self.d.queue = [_track("a", "A"), _track("b", "B")]
        made = self.d.cmd_playlist({"action": "create", "name": "Tonight", "fromQueue": True})
        items = self.d.store.library.playlist_items(made["id"])
        self.assertEqual([i["title"] for i in items], ["A", "B"])

    def test_playlists_export_and_import_as_m3u(self):
        from apctl.core import custom
        song = os.path.join(self.dir, "song.mp3")
        with open(song, "wb") as fh:
            fh.write(b"x")
        lib = self.d.store.library
        made = lib.create_playlist("Mixed")
        lib.add_to_playlist(made["id"], [
            _track("s", "Song", artist="Band", url=song, duration=200,
                   extra={"path": song, "media": "audio"}),
            {"uid": "youtube:yt-abcdefghijk", "source": "youtube", "kind": "video",
             "title": "A video", "url": "https://www.youtube.com/watch?v=abcdefghijk",
             "duration": 90, "extra": {"video_id": "abcdefghijk"}}])
        before = custom.downloads_dir
        custom.downloads_dir = lambda: self.dir
        try:
            out = self.d.cmd_playlist({"action": "export", "playlist": made["id"]})
        finally:
            custom.downloads_dir = before
        with open(out["path"], encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("#EXTM3U", body)
        self.assertIn("#EXTINF:200,Band - Song", body)
        self.assertIn(song, body)
        self.assertIn("watch?v=abcdefghijk", body)
        back = self.d.cmd_playlist({"action": "import", "path": out["path"]})
        self.assertEqual(back["count"], 2)
        items = lib.playlist_items(back["id"])
        self.assertEqual([i["source"] for i in items], ["local", "youtube"])
        self.assertEqual(items[0]["title"], "Song")
        self.assertEqual(items[0]["artist"], "Band")
        self.assertEqual(items[1]["extra"]["video_id"], "abcdefghijk")

    def test_recent_folders_newest_first_without_repeats(self):
        for path in ("/a", "/b", "/a"):
            self.d._remember_folder(path)
        self.assertEqual(self.d._recent_folders(), ["/a", "/b"])
        self.d.cmd_recent_folders({"action": "remove", "path": "/b"})
        self.assertEqual(self.of("recent_folders")[-1]["folders"][0]["path"], "/a")
        self.assertEqual(len(self.of("recent_folders")[-1]["folders"]), 1)

    def test_your_own_lyrics_and_their_offset(self):
        item = {"uid": "local:x", "title": "X"}
        self.d.cmd_lyrics_set({"item": item, "text": "[00:01.00]one\n[00:02.50]two"})
        lyrics = self.of("lyrics")[-1]["lyrics"]
        self.assertTrue(lyrics["synced"])
        self.assertEqual(lyrics["source"], "yours")
        self.assertEqual([l["t"] for l in lyrics["lines"]], [1000, 2500])
        self.d.cmd_lyrics_offset({"item": item, "offset": 500})
        self.assertEqual(self.of("lyrics")[-1]["lyrics"]["offset"], 500)
        self.d.cmd_lyrics_set({"item": item, "text": "plain words\nsecond line"})
        lyrics = self.of("lyrics")[-1]["lyrics"]
        self.assertFalse(lyrics["synced"])
        self.assertEqual(len(lyrics["lines"]), 2)

    def test_long_tracks_fill_themselves(self):
        lib = self.d.store.library
        lib.played(_track("long", "A set", duration=3600), 100)
        lib.played(_track("short", "A song", duration=200), 100)
        lib.played(_track("live", "A stream", duration=0, is_live=True), 100)
        titles = [e["title"] for e in lib.playlist_items("smart:long")]
        self.assertEqual(titles, ["A set"])
        self.assertIn("smart:long", [p["id"] for p in lib.playlists()])

    def test_podcast_episodes_carry_played_and_new(self):
        feed = "https://example.com/feed"
        self.d.store.library.set_json("podcastSeen", {feed: 1000})
        self.d._set_played(["podcast:ep-1"], True)
        rows = self.d._decorate_episodes([
            {"uid": "podcast:ep-1", "extra": {"published": 2000}},
            {"uid": "podcast:ep-2", "extra": {"published": 2000}},
            {"uid": "podcast:ep-3", "extra": {"published": 500}}], feed)
        self.assertEqual([(r["extra"]["played"], r["extra"]["new"]) for r in rows],
                         [(True, False), (False, True), (False, False)])
        self.d.cmd_podcast({"action": "mark", "uid": "podcast:ep-1", "played": False})
        self.assertNotIn("podcast:ep-1", self.d._podcast_marks())

    def test_a_radio_from_a_library_track_prefers_the_artist(self):
        lib = self.d.store.library
        index = {}
        for n, artist in enumerate(["Band"] * 3 + ["Other"] * 30):
            entry = _track("t%d" % n, "T%d" % n, artist=artist)
            entry["f"] = "1:1"
            index[entry["extra"]["path"]] = entry
        lib.replace_index(index, local.INDEX_VERSION, ["/music"])
        seed = index["/music/t0.mp3"]
        out = self.d._radio_for(seed)
        self.assertEqual(len(out), 25)
        self.assertNotIn(seed["uid"], [e["uid"] for e in out])
        self.assertEqual({e["artist"] for e in out[:2]}, {"Band"})

    def test_search_all_answers_per_source(self):
        self.d.cmd_search_all({"query": "nothing-here", "sources": ["local"], "id": 7})
        rows = self.of("search_all")
        self.assertEqual(rows[0]["source"], "local")
        self.assertEqual(rows[0]["id"], 7)
        self.assertTrue(rows[-1].get("done"))


class MoreSources(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ap-more-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_channel_links_list_the_uploads_not_the_tabs(self):
        seen = []
        before = youtube._listing
        youtube._listing = lambda url, *a, **k: seen.append(url) or []
        try:
            youtube.channel_feed("UCuAXFkgsw1L7xaCfnd5JJOw")
            youtube.channel_feed("https://www.youtube.com/channel/UCuAXFkgsw1L7xaCfnd5JJOw")
            youtube.channel_feed("https://www.youtube.com/@RickAstleyYT")
            youtube.channel_feed("@RickAstleyYT")
        finally:
            youtube._listing = before
        self.assertEqual(seen, [
            "https://www.youtube.com/playlist?list=UUuAXFkgsw1L7xaCfnd5JJOw",
            "https://www.youtube.com/playlist?list=UUuAXFkgsw1L7xaCfnd5JJOw",
            "https://www.youtube.com/@RickAstleyYT/videos",
            "https://www.youtube.com/@RickAstleyYT/videos"])

    def test_music_song_rows_keep_the_artist_id(self):
        column = {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [
            {"text": "Burna Boy", "navigationEndpoint": {"browseEndpoint": {
                "browseId": "UCr61sufuLt7_eB7ak1bXHIg",
                "browseEndpointContextSupportedConfigs": {"browseEndpointContextMusicConfig":
                                                          {"pageType": "MUSIC_PAGE_TYPE_ARTIST"}}}}},
            {"text": " \u2022 "},
            {"text": "Love, Damini", "navigationEndpoint": {"browseEndpoint": {
                "browseId": "MPREb_If75wr3HkMf"}}}]}}}
        self.assertEqual(music._artist_id(column), "UCr61sufuLt7_eB7ak1bXHIg")
        self.assertEqual(music._artist_id({}), "")

    def test_a_mix_needs_a_video_id(self):
        with self.assertRaises(Exception):
            youtube.mix("not a/video id")

    def test_an_xmltv_file_fills_in_unknown_channels(self):
        import gzip
        import time as _time
        from apctl.sources import tv
        now = _time.time()
        stamp = lambda t: _time.strftime("%Y%m%d%H%M%S +0000", _time.gmtime(t))
        xml = ('<tv><channel id="local.one"><display-name>Local One</display-name></channel>'
               '<programme start="%s" stop="%s" channel="local.one"><title>News</title></programme>'
               '</tv>') % (stamp(now - 600), stamp(now + 600))
        path = os.path.join(self.dir, "guide.xml.gz")
        with open(path, "wb") as fh:
            fh.write(gzip.compress(xml.encode()))
        channels, programmes = tv.import_guide_file(path, self.dir)
        self.assertEqual((channels, programmes), (1, 1))
        tv._GUIDES.pop("file", None)          # read back from disk
        rows = tv.schedule({"title": "Local One HD", "extra": {"country": ""}},
                           now - 3600, now + 3600, cache_dir=self.dir)
        self.assertEqual([r["title"] for r in rows], ["News"])
        tv.forget_guide_file(self.dir)
        tv._GUIDES.pop("file", None)
        self.assertEqual(tv.schedule({"title": "Local One"}, now - 3600, now + 3600,
                                     cache_dir=self.dir), [])
        with self.assertRaises(Exception):
            tv.import_guide_file(os.path.join(self.dir, "missing.xml"), self.dir)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("bwrap"),
                         "ffmpeg and bubblewrap are needed")
    def test_tags_are_written_without_touching_the_music_folder_from_the_sandbox(self):
        song = os.path.join(self.dir, "song.mp3")
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=d=1",
                        "-metadata", "title=Old", song], check=True)
        updated = []

        class Library:
            def update_track(self, path, entry, f):
                updated.append((path, entry["title"]))
                return True

        class Store:
            library = Library()
            art_dir = os.path.join(self.dir, "art")
            runtime = os.path.join(self.dir, "rt")

        os.makedirs(Store.art_dir)
        entry = local.write_tags(Store(), {}, song, {"title": "New", "artist": "Me"})
        self.assertEqual((entry["title"], entry["artist"]), ("New", "Me"))
        self.assertEqual(updated, [(os.path.realpath(song), "New")])
        self.assertEqual(sorted(os.listdir(self.dir)), ["art", "rt", "song.mp3"],
                         "no temporary file is left beside the song")


class KeepPlaying(unittest.TestCase):
    def setUp(self):
        from apctl.core.daemon import Daemon
        from apctl.core.state import Store
        self.dir = tempfile.mkdtemp(prefix="ap-keep-")
        self.events = []
        self.d = Daemon(store=Store(self.dir, self.dir, self.dir), emit=self.events.append)
        self.advanced = []
        self.d._advance = lambda auto=False, start_seconds=0.0, gen=None: self.advanced.append(auto)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_the_end_of_the_queue_brings_more_like_the_last_track(self):
        more = [_track("m%d" % n, "More %d" % n) for n in range(5)]
        self.d._radio_for = lambda seed, skip=(): more
        self.d.queue, self.d.index = [_track("a", "A")], 0
        self.assertTrue(self.d._keep_playing())
        self.assertEqual(self.d.index, 1)
        self.assertEqual(len(self.d.queue), 6)
        self.assertEqual(self.advanced, [True])

    def test_switched_off_or_live_it_stops(self):
        self.d._radio_for = lambda seed, skip=(): [_track("m", "M")]
        self.d.queue, self.d.index = [_track("a", "A", is_live=True)], 0
        self.assertFalse(self.d._keep_playing())
        self.d.queue = [_track("a", "A")]
        self.d.set_setting("keepPlaying", False)
        self.assertFalse(self.d._keep_playing())
        self.assertEqual(self.advanced, [])


class PanelHelpers(unittest.TestCase):
    def node(self, script):
        out = subprocess.run(
            ["node", "-e",
             'const fs=require("fs");'
             'const src=fs.readFileSync(process.argv[1],"utf8");'
             'for (const name of ["playlistLink","isCollection","shuffled","baseName","channelOf"])'
             '  eval(new RegExp("function "+name+"\\\\(.*?\\\\n\\\\}","s").exec(src)[0]);'
             + script, os.path.join(ROOT, "Model.js")],
            capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_pasted_links(self):
        self.assertEqual(self.node(
            'console.log([playlistLink("https://www.youtube.com/watch?v=a&list=PLOzDu-MXXLliO9f"),'
            'playlistLink("https://music.youtube.com/playlist?list=OLAK5uy_abcdefgh"),'
            'playlistLink("https://www.youtube.com/watch?v=abc"),'
            'playlistLink("https://example.com/?list=PLOzDu-MXXLliO9f"),'
            'playlistLink("lofi")].map(x => x ? "y" : "n").join(""))'), "yynnn")

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_collections_and_shuffle(self):
        self.assertEqual(self.node(
            'console.log([isCollection({source:"youtube",kind:"playlist"}),'
            'isCollection({source:"music",kind:"album"}),'
            'isCollection({source:"local",kind:"folder"}),'
            'isCollection({source:"music",kind:"track"}),'
            'isCollection({source:"local",kind:"track"})].map(x => x ? "y" : "n").join("")'
            '+"|"+shuffled([1,2,3,4,5]).sort().join(",")+"|"+baseName("/a/b/Blue/"))'),
            "yyynn|1,2,3,4,5|Blue")
        self.assertEqual(self.node(
            'const a = channelOf({source:"music",kind:"track",artist:"Burna Boy",'
            'extra:{artist_id:"UCr61"}});'
            'const b = channelOf({source:"youtube",kind:"video",artist:"X",'
            'extra:{channel_url:"https://www.youtube.com/@x",channel:"X - Topic"}});'
            'console.log([a.url,a.name,b.url,b.name,'
            'channelOf({source:"youtube",kind:"playlist",extra:{channel_url:"u"}}),'
            'channelOf({source:"local",kind:"track"})].join("|"))'),
            "UCr61|Burna Boy|https://www.youtube.com/@x|X||")


class PanelWiring(unittest.TestCase):
    def test_the_browser_offers_playlists_albums_and_folders(self):
        browser = read("Browser.qml")
        self.assertIn('["", "playlists"]', browser)
        self.assertIn('["", "playlists", "albums"]', browser)
        self.assertIn("root.pickFolder()", browser)
        for how in ('collectionAct("play")', 'collectionAct("shuffle")',
                    'collectionAct("queue")'):
            self.assertIn(how, browser)

    def test_the_panel_plays_a_picked_folder(self):
        panel = read("Panel.qml")
        self.assertIn("omarchy-file-select", panel)
        self.assertIn('if (purpose === "folder") openFolderPath(path, "play")', panel)
        for purpose in ("folder", "m3u", "epg", "art"):
            self.assertIn("%s: [" % purpose, panel)
        self.assertIn('mode: "dir"', panel)


if __name__ == "__main__":
    unittest.main(verbosity=2)
