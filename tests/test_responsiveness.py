"""The player has to answer while it is busy.

Every test here is one of the ways the plugin used to look broken while
nothing had technically failed: a tab that never finished loading because the
daemon's four workers were all busy answering ten state requests a second, a
station switch that waited behind a slow one, a stop button queued behind the
play it was meant to stop, a "next" that replayed the same track.

Run with:  python3 tests/test_responsiveness.py
"""

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.dont_write_bytecode = True

from apctl.core import daemon as daemon_module  # noqa: E402
from apctl.core.daemon import Daemon  # noqa: E402
from apctl.core.state import Store  # noqa: E402


def make_daemon(case):
    runtime = tempfile.mkdtemp(prefix="ap-resp-")
    case.addCleanup(shutil.rmtree, runtime, True)
    events = []
    lock = threading.Lock()

    def sink(message):
        with lock:
            events.append((time.monotonic(), message))

    instance = Daemon(store=Store(runtime, runtime, runtime), emit=sink)
    instance._log = lambda *_a, **_k: None
    return instance, events


def wait_for(events, predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for stamp, message in list(events):
            if predicate(message):
                return stamp
        time.sleep(0.01)
    return None


STATION = {"uid": "radio:a", "source": "radio", "kind": "station",
           "title": "A", "url": "https://radio.example/a", "is_live": True}


class StateNeverWaitsOnPlayback(unittest.TestCase):
    """A slow play must not hold up state, browsing or controls."""

    def test_state_answers_while_a_play_is_stuck(self):
        instance, events = make_daemon(self)
        release = threading.Event()

        def slow_resolve(item, want_video):
            release.wait(5)
            raise daemon_module.SourceError("gave up", "network")

        instance._resolve = slow_resolve
        self.addCleanup(release.set)
        instance.handle({"cmd": "play", "item": STATION, "id": 1})
        time.sleep(0.05)
        sent = time.monotonic()
        instance.handle({"cmd": "state"})
        answered = wait_for(events, lambda m: m.get("type") == "state"
                            and m.get("mode") == "loading")
        self.assertIsNotNone(answered, "no state while a play was resolving")
        self.assertLess(answered - sent, 0.5,
                        "state waited behind a play that was still resolving")

    def test_state_does_not_talk_to_the_player(self):
        """Reporting state reads the cache. It used to make three socket round
        trips and spawn hyprctl, ten times a second."""
        instance, events = make_daemon(self)

        class Exploding:
            sock = object()

            def get_property(self, *_a, **_k):
                raise AssertionError("emit_state asked the player")

            def request(self, *_a, **_k):
                raise AssertionError("emit_state asked the player")

        instance.player = Exploding()
        instance._current = dict(STATION)
        instance._props = {"pause": False, "time-pos": 12.0}
        instance._hypr = lambda argv: self.fail("emit_state ran hyprctl")
        instance.emit_state()
        self.assertEqual(events[-1][1]["mode"], "playing")


class StopIsImmediate(unittest.TestCase):

    def test_stop_reports_off_before_the_playback_lane_runs(self):
        instance, events = make_daemon(self)
        blocker = threading.Event()
        self.addCleanup(blocker.set)
        instance._playback.submit(lambda: blocker.wait(5))
        instance._current = dict(STATION)
        instance.queue = [instance._current]
        instance.handle({"cmd": "stop", "id": 9})
        stamp = wait_for(events, lambda m: m.get("type") == "state"
                         and m.get("mode") == "off", timeout=1.0)
        self.assertIsNotNone(
            stamp, "stop waited behind the playback lane instead of cutting "
                   "the sound at once")

    def test_a_newer_request_supersedes_an_older_one(self):
        instance, _events = make_daemon(self)
        first = instance._bump()
        second = instance._bump()
        self.assertTrue(instance._superseded(first))
        self.assertFalse(instance._superseded(second))


class TheQueueMoves(unittest.TestCase):

    def _with_queue(self, count=3, index=0):
        instance, events = make_daemon(self)
        instance.queue = [dict(STATION, uid="radio:%d" % i, title=str(i))
                          for i in range(count)]
        instance.index = index
        started = []
        instance._advance = lambda **kwargs: started.append(instance.index)
        return instance, started

    def test_next_moves_to_the_next_entry(self):
        """It used to call _advance without moving the index, so "next"
        restarted whatever was already playing."""
        instance, started = self._with_queue()
        instance.cmd_next({})
        self.assertEqual(started, [1])

    def test_next_at_the_end_goes_nowhere_without_repeat(self):
        instance, started = self._with_queue(index=2)
        instance.cmd_next({})
        self.assertEqual(started, [])

    def test_repeat_all_wraps(self):
        instance, started = self._with_queue(index=2)
        instance.repeat = "all"
        instance.cmd_next({})
        self.assertEqual(started, [0])

    def test_play_queues_the_list_it_came_from(self):
        instance, _events = make_daemon(self)
        instance._advance = lambda **kwargs: None
        rows = [dict(STATION, uid="radio:%d" % i, title=str(i))
                for i in range(5)]
        instance.cmd_play({"items": rows, "start": 3})
        self.assertEqual(len(instance.queue), 5)
        self.assertEqual(instance.index, 3)

    def test_the_end_of_a_track_advances(self):
        instance, _events = make_daemon(self)
        handled = []
        instance.handle = lambda message: handled.append(message.get("cmd"))
        instance._current = {"uid": "local:x", "source": "local", "title": "x",
                             "is_live": False}
        instance._on_mpv({"event": "end-file", "reason": "eof"})
        self.assertEqual(handled, ["auto_next"],
                         "a finished track did not move on to the next one")


class ReplacedPlayersAreIgnored(unittest.TestCase):

    def test_a_replaced_players_disconnect_does_not_kill_the_new_one(self):
        instance, _events = make_daemon(self)
        old, new = object(), object()
        instance.player = new
        instance._on_mpv_from(old, {"event": "aurora-disconnected"})
        self.assertIs(instance.player, new,
                      "the old connection's disconnect cleared the new player")


class TeardownKeepsArtwork(unittest.TestCase):

    def test_switching_players_does_not_stop_the_artwork_pool(self):
        instance, _events = make_daemon(self)

        class Art:
            stopped = False

            def shutdown(self, *a, **k):
                self.stopped = True

        instance.artwork = Art()
        instance._teardown_session()
        self.assertFalse(instance.artwork.stopped,
                         "a station change shut the artwork pool down for good")


class RepliesHaveTheirOwnTypes(unittest.TestCase):
    """The panel only listens for typed events; a generic `result` is dropped."""

    def test_lyrics_arrive_as_a_lyrics_event(self):
        instance, events = make_daemon(self)
        original = daemon_module.music.lyrics_for
        daemon_module.music.lyrics_for = lambda item, settings: {
            "lines": [{"t": 0, "text": "la"}], "synced": True}
        self.addCleanup(setattr, daemon_module.music, "lyrics_for", original)
        instance._current = {"uid": "local:x", "source": "local", "title": "x"}
        instance.cmd_lyrics({})
        self.assertEqual(events[-1][1]["type"], "lyrics")

    def test_repeat_and_shuffle_push_state(self):
        instance, events = make_daemon(self)
        instance.cmd_repeat({})
        self.assertEqual(events[-1][1]["type"], "state")
        self.assertEqual(events[-1][1]["repeat"], "all")
        instance.cmd_shuffle({})
        self.assertTrue(events[-1][1]["shuffle"])


class AlternatesAreFound(unittest.TestCase):

    def test_the_uid_prefix_is_removed_before_asking_the_mirror(self):
        instance, _events = make_daemon(self)
        asked = []

        class Catalogue:
            def station_candidates(self, uuid, limit):
                asked.append(uuid)
                return ["https://alt.example/1"]

            def station_siblings(self, uuid, name, limit):
                return []

        instance.catalogue = Catalogue()
        instance._current = dict(STATION, uid="radio:abc-123")
        self.assertEqual(instance._alternates(), ["https://alt.example/1"])
        self.assertEqual(asked, ["abc-123"])


class SavesAreDebounced(unittest.TestCase):

    def test_a_volume_drag_is_one_write(self):
        runtime = tempfile.mkdtemp(prefix="ap-save-")
        self.addCleanup(shutil.rmtree, runtime, True)
        store = Store(runtime, runtime, runtime)
        store.prepare()
        writes = []
        original = store.save

        def counting_save(data=None):
            writes.append(1)
            original(data)

        store.save = counting_save
        for _ in range(30):
            store.save_soon(delay=0.2)
        time.sleep(0.5)
        self.assertEqual(len(writes), 1)


class ReadsDoNotWaitForTheWriter(unittest.TestCase):

    def test_a_query_runs_while_a_sync_holds_the_write_lock(self):
        from apctl.core.db import Catalogue
        folder = tempfile.mkdtemp(prefix="ap-db-")
        self.addCleanup(shutil.rmtree, folder, True)
        catalogue = Catalogue(os.path.join(folder, "c.db"))
        catalogue.open()
        self.addCleanup(catalogue.close)
        held = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def writer():
            with catalogue._lock:
                held.set()
                release.wait(5)

        threading.Thread(target=writer, daemon=True).start()
        held.wait(2)
        started = time.monotonic()
        catalogue.counts()
        catalogue.search_radio(term="", limit=5)
        self.assertLess(time.monotonic() - started, 1.0,
                        "a browse queued behind the sync's write lock")


class SyncAlwaysFinishes(unittest.TestCase):

    def test_a_failed_download_reports_failed_not_downloading(self):
        from apctl.core.sync import Syncer
        reports = []

        class Db:
            def counts(self):
                return {}

            def close(self):
                pass

        syncer = Syncer(Db(), on_progress=lambda s, info: reports.append(
            (s, dict(info))))

        def explode(full=False):
            syncer._report("radio", stage="download", rows=10)
            raise OSError("network went away")

        syncer.sync_radio = explode
        syncer.sync_tv = lambda full=False: None
        syncer._one(True, "radio")
        self.assertEqual(reports[-1][1]["stage"], "failed")


class LocalTagsAreRead(unittest.TestCase):

    def test_title_and_artist_come_from_the_file(self):
        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            self.skipTest("ffmpeg is not installed")
        import subprocess
        from apctl.sources import local
        folder = tempfile.mkdtemp(prefix="ap-tags-")
        self.addCleanup(shutil.rmtree, folder, True)
        path = os.path.join(folder, "x.mp3")
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi",
                        "-i", "sine=frequency=440:duration=1",
                        "-metadata", "title=Real Title",
                        "-metadata", "artist=Real Artist", path], check=True)
        tags = local._read_tags(path)
        self.assertEqual(tags.get("title"), "Real Title",
                         "a second -show_entries flag discarded the tags")
        self.assertEqual(tags.get("artist"), "Real Artist")


class YouTubeVideoKeepsItsPicture(unittest.TestCase):
    """YouTube serves video and audio separately above 360p. The picker used
    to fall back to the audio track, so every video played as sound only."""

    def test_separate_streams_become_video_plus_audio_track(self):
        from apctl.core import resolver
        entry = {"requested_formats": [
            {"url": "https://v.example/video", "vcodec": "avc1", "acodec": "none",
             "height": 720, "tbr": 1500},
            {"url": "https://v.example/audio", "vcodec": "none", "acodec": "opus",
             "tbr": 130}]}
        media = resolver.direct_urls(entry, video=True)
        self.assertEqual(media["url"], "https://v.example/video")
        self.assertEqual(media["audio_url"], "https://v.example/audio")
        self.assertEqual(media["height"], 720)

    def test_the_youtube_tab_wants_a_window_and_music_does_not(self):
        instance, _events = make_daemon(self)
        self.assertTrue(instance._wants_video({"source": "youtube"}))
        self.assertFalse(instance._wants_video({"source": "music"}))


class ArtworkKeepsTheCallersKey(unittest.TestCase):
    def test_a_mixed_case_key_is_announced_as_given(self):
        from apctl.core.artwork import Artwork
        folder = tempfile.mkdtemp(prefix="ap-art-")
        self.addCleanup(shutil.rmtree, folder, True)
        art = Artwork(folder)
        art.submit("yt-AbCdEf123", "https://img.example/x.jpg", "yt-abcdef123")
        job = art._queue.get_nowait()
        self.assertEqual(job.key, "yt-AbCdEf123",
                         "the panel matches art events by the original key")


class EveryCountedRowCanBeReached(unittest.TestCase):
    """The count said 170 and scrolling stopped at 60: pages came back short
    after filtering, and a short page was read as the end of the list."""

    def test_pages_by_offset_reach_the_total(self):
        from apctl.core.db import Catalogue
        folder = tempfile.mkdtemp(prefix="ap-page-")
        self.addCleanup(shutil.rmtree, folder, True)
        catalogue = Catalogue(os.path.join(folder, "c.db"))
        catalogue.open()
        self.addCleanup(catalogue.close)
        rows = []
        for i in range(150):
            name = "Channel %03d%s" % (i, " [Geo-blocked]" if i % 5 == 0 else "")
            rows.append(("id%03d" % i, name, "https://tv.example/%d" % i, "",
                         "News", "GH", "", 0, 0, 0))
        catalogue.replace_channels(rows)
        total = catalogue.count_channels(hide_geo=True)
        seen, offset = set(), 0
        while True:
            page = catalogue.search_channels(limit=60, offset=offset, hide_geo=True)
            if not page:
                break
            seen.update(r["id"] for r in page)
            offset += len(page)
        self.assertEqual(total, 120)
        self.assertEqual(len(seen), total)


class FavouritesHistoryAndQueue(unittest.TestCase):

    def test_a_favourite_toggles_and_is_reported_in_state(self):
        instance, events = make_daemon(self)
        instance._current = dict(STATION)
        instance.cmd_favorite({})
        self.assertTrue(instance._is_favorite(STATION["uid"]))
        states = [m for _t, m in events if m.get("type") == "state"]
        self.assertTrue(states[-1]["favorite"])
        instance.cmd_favorite({})
        self.assertFalse(instance._is_favorite(STATION["uid"]))

    def test_jump_plays_the_chosen_queue_entry(self):
        instance, _events = make_daemon(self)
        instance.queue = [dict(STATION, uid="radio:%d" % i) for i in range(4)]
        started = []
        instance._advance = lambda **kw: started.append(instance.index)
        instance.cmd_jump({"index": 2})
        self.assertEqual(started, [2])

    def test_history_is_most_recent_first_without_duplicates(self):
        instance, _events = make_daemon(self)
        for uid in ("radio:a", "radio:b", "radio:a"):
            instance._remember_played(dict(STATION, uid=uid))
        self.assertEqual([e["uid"] for e in instance._history()], ["radio:a", "radio:b"])
        instance.cmd_forget({"uid": ""})
        self.assertEqual(instance._history(), [])


class SoundFilters(unittest.TestCase):

    def test_the_chain_follows_the_settings(self):
        from apctl.core.daemon import audio_filter_chain
        self.assertEqual(audio_filter_chain(False, "Rock", False), "")
        rock = audio_filter_chain(True, "Rock", False)
        self.assertTrue(rock.startswith("lavfi=[equalizer=f=60"))
        self.assertIn("dynaudnorm", audio_filter_chain(False, "Flat", True))
        self.assertEqual(audio_filter_chain(True, "Flat", False), "",
                         "a flat equalizer is no filter at all")


if __name__ == "__main__":
    unittest.main(verbosity=1)
