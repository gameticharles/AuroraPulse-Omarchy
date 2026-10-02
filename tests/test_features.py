"""Tests for the library, playlists, health checks, fades, subtitles, lyrics
files, casting, alarms, podcasts, scrobbling, the TV guide and MPRIS.

Run with:  python3 tests/test_features.py
"""

import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from apctl.core import cast, daemon, doctor, library, resolver  # noqa: E402
from apctl.core.daemon import Daemon  # noqa: E402
from apctl.core.state import Store  # noqa: E402
from apctl.sources import local, podcast, tv  # noqa: E402
from apctl.util import sandbox  # noqa: E402


def entry(uid, title="T", **rest):
    out = {"uid": uid, "source": "radio", "kind": "station", "title": title,
           "url": "https://example.com/%s" % uid}
    out.update(rest)
    return out


class TempDir(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ap-test-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)


# -- the library database ------------------------------------------------------

class Library(TempDir):
    def lib(self):
        return library.Library(os.path.join(self.dir, "library.db")).open()

    def test_import_moves_keys_out_of_state(self):
        state = {
            "settings": {"volume": 40},
            "favorites": [entry("radio:a", "A"), entry("radio:b", "B")],
            "history": [entry("radio:c", "C", extra={"playedAt": 100})],
            "positions": {"local:x": 321},
            "last": {"queue": [entry("radio:a"), entry("radio:c")], "index": 1},
            "local": {"index_version": local.INDEX_VERSION, "roots": ["/m"], "scanned_at": 5,
                      "index": {"/m/a.mp3": {"uid": "local:a", "title": "Song", "artist": "X",
                                             "album": "Al", "f": "1:2", "seen": 5,
                                             "extra": {"path": "/m/a.mp3", "folder": "/m",
                                                       "genre": "Jazz", "media": "audio"}}}},
        }
        lib = self.lib()
        self.assertTrue(lib.import_state(state, local.INDEX_VERSION))
        self.assertEqual(sorted(state), ["settings"])
        self.assertEqual([e["uid"] for e in lib.favorites()], ["radio:a", "radio:b"])
        self.assertEqual([e["uid"] for e in lib.history()], ["radio:c"])
        self.assertEqual(lib.position("local:x"), 321)
        queue_, index = lib.load_queue()
        self.assertEqual(([e["uid"] for e in queue_], index), (["radio:a", "radio:c"], 1))
        self.assertEqual(lib.count_tracks(), 1)
        self.assertEqual(lib.groups("genre")[0]["name"], "Jazz")
        # Once only: a second import is a no-op, even with keys present.
        self.assertFalse(lib.import_state({"favorites": [entry("radio:z")]}, 4))
        self.assertEqual(len(lib.favorites()), 2)

    def test_an_old_index_is_not_imported(self):
        lib = self.lib()
        lib.import_state({"local": {"index_version": -1, "index": {"/a": {"uid": "local:a"}}}},
                         local.INDEX_VERSION)
        self.assertEqual(lib.count_tracks(), 0)

    def test_favorites_newest_first_and_toggle(self):
        lib = self.lib()
        lib.set_favorite(entry("radio:a"), True)
        lib.set_favorite(entry("radio:b"), True)
        self.assertEqual([e["uid"] for e in lib.favorites()], ["radio:b", "radio:a"])
        lib.set_favorite(entry("radio:b"), False)
        self.assertFalse(lib.is_favorite("radio:b"))
        self.assertTrue(lib.is_favorite("radio:a"))

    def test_history_is_capped_and_counts_survive_forgetting(self):
        lib = self.lib()
        for n in range(5):
            lib.played(entry("radio:%d" % n), keep=3)
        lib.played(entry("radio:4"), keep=3)
        self.assertEqual(len(lib.history()), 3)
        self.assertEqual(lib.play_count("radio:4"), 2)
        lib.forget("")
        self.assertEqual(lib.history(), [])
        self.assertEqual(lib.play_count("radio:4"), 2)

    def test_positions_clear_and_cap(self):
        lib = self.lib()
        lib.set_position("a", 100)
        lib.set_position("a", None)
        self.assertEqual(lib.position("a"), 0.0)
        for n in range(5):
            lib.set_position("p%d" % n, 50, keep=2)
        self.assertEqual(sum(1 for n in range(5) if lib.position("p%d" % n)), 2)

    def test_playlists(self):
        lib = self.lib()
        made = lib.create_playlist("  Road trip  ")
        self.assertEqual(made["name"], "Road trip")
        added = lib.add_to_playlist(made["id"], [entry("radio:a"), entry("radio:b"),
                                                 entry("radio:a"), entry("radio:c")])
        self.assertEqual(added, 3)                # the duplicate is skipped
        lib.move_in_playlist(made["id"], 2, 0)
        lib.remove_from_playlist(made["id"], "radio:a")
        self.assertEqual([e["uid"] for e in lib.playlist_items(made["id"])],
                         ["radio:c", "radio:b"])
        self.assertTrue(lib.rename_playlist(made["id"], "Drive"))
        names = [p["name"] for p in lib.playlists()]
        self.assertIn("Drive", names)
        self.assertIn("Most played", names)       # the smart ones are always listed
        self.assertTrue(lib.delete_playlist(made["id"]))
        self.assertEqual(lib.playlist_items(made["id"]), [])
        with self.assertRaises(KeyError):
            lib.add_to_playlist("nope", [entry("radio:a")])

    def test_smart_playlists(self):
        lib = self.lib()
        lib.played(entry("radio:once"))
        for _ in range(3):
            lib.played(entry("radio:often"))
        self.assertEqual([e["uid"] for e in lib.playlist_items("smart:most")], ["radio:often"])
        self.assertEqual(lib.playlist_items("smart:recent")[0]["uid"], "radio:often")
        lib.replace_index({"/m/new.mp3": {"uid": "local:new", "title": "New", "f": "1:1",
                                          "extra": {"path": "/m/new.mp3", "folder": "/m"}}},
                          local.INDEX_VERSION, ["/m"])
        self.assertEqual([e["uid"] for e in lib.playlist_items("smart:added")], ["local:new"])

    def test_index_keeps_added_time_and_drops_missing(self):
        lib = self.lib()
        first = {"/m/a.mp3": {"uid": "local:a", "title": "A", "f": "1:1", "added": 10,
                              "extra": {"path": "/m/a.mp3"}}}
        lib.replace_index(first, 4, ["/m"])
        again = lib.index()
        again["/m/a.mp3"]["f"] = "2:2"            # the file changed
        again["/m/b.mp3"] = {"uid": "local:b", "title": "B", "f": "1:1",
                             "extra": {"path": "/m/b.mp3"}}
        lib.replace_index(again, 4, ["/m"])
        index = lib.index()
        self.assertEqual(index["/m/a.mp3"]["added"], 10)
        lib.replace_index({"/m/b.mp3": index["/m/b.mp3"]}, 4, ["/m"])
        self.assertEqual(sorted(lib.index()), ["/m/b.mp3"])

    def test_track_queries(self):
        lib = self.lib()
        rows = {}
        for n, (title, artist, album, media) in enumerate((
                ("Alpha", "Ann", "One", "audio"), ("Beta", "Bob", "One", "audio"),
                ("Clip", "Cat", "", "video"))):
            path = "/m/%d.x" % n
            rows[path] = {"uid": "local:%d" % n, "title": title, "artist": artist,
                          "album": album, "f": "1:1",
                          "extra": {"path": path, "folder": "/m", "media": media}}
        lib.replace_index(rows, 4, ["/m"])
        self.assertEqual([e["title"] for e in lib.tracks(media="video")], ["Clip"])
        self.assertEqual([e["title"] for e in lib.tracks(query="bob")], ["Beta"])
        self.assertEqual(len(lib.tracks(album="One")), 2)
        self.assertEqual(lib.count_tracks(media="audio", artist="Ann"), 1)
        albums = lib.groups("album")
        self.assertEqual([(g["name"], g["count"]) for g in albums], [("One", 2)])

    def test_queue_and_downloads_round_trip(self):
        lib = self.lib()
        lib.save_queue([entry("radio:a"), entry("radio:b")], 5)
        items, index = lib.load_queue()
        self.assertEqual(index, 1)                # clamped to the list
        lib.save_downloads([{"id": "j1", "added": 1, "state": "done"}])
        self.assertEqual(lib.load_downloads()[0]["id"], "j1")

    def test_a_corrupt_file_is_set_aside(self):
        path = os.path.join(self.dir, "library.db")
        with open(path, "wb") as handle:
            handle.write(b"this is not a database" * 100)
        lib = library.Library(path).open()
        self.assertEqual(lib.favorites(), [])
        self.assertTrue(any(n.startswith("library.db.broken-") for n in os.listdir(self.dir)))

    def test_store_migrates_on_first_use(self):
        with open(os.path.join(self.dir, "state.json"), "w") as handle:
            json.dump({"settings": {}, "favorites": [entry("radio:a")]}, handle)
        store = Store(self.dir, self.dir, self.dir)
        store.load()
        self.assertEqual(store.library.favorites()[0]["uid"], "radio:a")
        with open(os.path.join(self.dir, "state.json")) as handle:
            self.assertNotIn("favorites", json.load(handle))


class Downloads(TempDir):
    def test_an_interrupted_job_is_queued_again(self):
        from apctl.core import downloads
        lib = library.Library(os.path.join(self.dir, "l.db")).open()
        lib.save_downloads([{"id": "a", "added": 2, "state": "running", "progress": 40.0},
                            {"id": "b", "added": 1, "state": "done", "progress": 100.0}])
        job = downloads.Downloads.__new__(downloads.Downloads)
        job.library, job.log = lib, (lambda _m: None)
        restored = {j["id"]: j for j in job._restore()}
        self.assertEqual(restored["a"]["state"], "queued")
        self.assertEqual(restored["a"]["progress"], 0.0)
        self.assertEqual(restored["b"]["state"], "done")


# -- health -----------------------------------------------------------------------

class Doctor(unittest.TestCase):
    def test_ytdlp_age(self):
        today = time.strftime("%Y.%m.%d")
        self.assertEqual(doctor.ytdlp_age(today), 0)
        self.assertGreater(doctor.ytdlp_age("2020.01.01"), 365)
        self.assertIsNone(doctor.ytdlp_age("nightly"))
        self.assertIsNone(doctor.ytdlp_age("2024.13.40"))

    def test_summary_levels(self):
        self.assertEqual(doctor.summary([{"level": "ok"}])[0], "ok")
        self.assertEqual(doctor.summary([{"level": "ok"}, {"level": "warn"}])[0], "warn")
        self.assertEqual(doctor.summary([{"level": "warn"}, {"level": "error"}])[0], "error")

    def test_every_check_has_the_fields_the_page_reads(self):
        for check in doctor.binaries() + [doctor.python_modules()]:
            self.assertTrue({"name", "ok", "detail", "level", "fix"} <= set(check))


# -- sound -------------------------------------------------------------------------

class Sound(unittest.TestCase):
    def test_custom_bands_are_clamped(self):
        self.assertEqual(daemon.eq_gains("Custom", "3, 40,-30,x"), (3, 12, -12, 0, 0))
        self.assertEqual(daemon.eq_gains("Rock"), daemon.EQ_PRESETS["Rock"])

    def test_chain_order_and_contents(self):
        chain = daemon.audio_filter_chain(True, "Custom", True, "6,0,0,0,0", skip_silence=True)
        self.assertTrue(chain.startswith("lavfi=[silenceremove"))
        self.assertIn("equalizer=f=60:t=o:w=2:g=6", chain)
        self.assertNotIn("f=230", chain)          # a flat band adds nothing
        self.assertTrue(chain.endswith("dynaudnorm=f=250:g=15:p=0.9]"))
        self.assertEqual(daemon.audio_filter_chain(False, "Flat", False), "")

    def test_fade_filter_is_labelled_and_clamped(self):
        self.assertEqual(daemon.fade_filter(2), "@apfade:lavfi=[volume=volume=1.000]")
        self.assertEqual(daemon.fade_filter(-1), "@apfade:lavfi=[volume=volume=0.000]")


class FakePlayer:
    def __init__(self):
        self.commands = []
        self.props = {}

    def command(self, *args):
        self.commands.append(args)
        return True

    def set_property(self, name, value, confirm=False):
        self.props[name] = value
        return True

    def request(self, *args, timeout=2.0):
        return True, []


class Fades(TempDir):
    def daemon(self):
        d = Daemon(store=Store(self.dir, self.dir, self.dir), emit=lambda _m: None)
        d.player = FakePlayer()
        return d

    def test_ramp_reaches_the_target_and_calls_back(self):
        d = self.daemon()
        done = threading.Event()
        d._ramp(0.0, 0.2, then=done.set)
        self.assertTrue(done.wait(3))
        self.assertEqual(d._fade_level, 0.0)
        last = [c for c in d.player.commands if c[0] == "af-command"][-1]
        self.assertEqual(last, ("af-command", "apfade", "volume", "0.000", "volume"))

    def test_a_newer_ramp_cancels_the_older(self):
        d = self.daemon()
        first = threading.Event()
        d._ramp(0.0, 1.0, then=first.set)
        d._cancel_fade()
        self.assertFalse(first.wait(1.5))
        self.assertEqual(d._fade_level, 1.0)

    def test_fade_out_only_near_the_end_of_a_track(self):
        d = self.daemon()
        d.settings = lambda: dict(daemon.DEFAULT_SETTINGS, crossfadeSec=5.0)
        d._current = {"uid": "local:a", "source": "local", "is_live": False}
        d.queue, d.index = [d._current, {"uid": "local:b"}], 0
        d._props = {"duration": 200.0}
        d._maybe_fade_out(100.0)
        self.assertFalse(d._fading_out)
        d._maybe_fade_out(196.0)
        self.assertTrue(d._fading_out)
        d._cancel_fade()


# -- lyrics, subtitles, alarm ------------------------------------------------------

class LyricsFile(unittest.TestCase):
    def test_synced_lines(self):
        text = Daemon.lrc_text({"title": "Song", "artist": "Band", "duration": 125},
                               {"synced": True, "lines": [{"t": 0, "text": "one"},
                                                          {"t": 61230, "text": "two"}]})
        self.assertIn("[ar:Band]\n[ti:Song]", text)
        self.assertIn("[length:2:05]", text)
        self.assertIn("[00:00.00]one\n[01:01.23]two", text)
        self.assertEqual(resolver.parse_lrc(text)[1]["t"], 61230)

    def test_unsynced_lines_have_no_stamps(self):
        text = Daemon.lrc_text({"title": "S"}, {"synced": False,
                                                "lines": [{"t": -1, "text": "plain"}]})
        self.assertTrue(text.rstrip().endswith("\nplain"))


class Subtitles(TempDir):
    def test_youtube_options_put_english_first_and_skip_translations(self):
        info = {"language": "fr",
                "subtitles": {"de": [{"ext": "vtt", "url": "https://x/de"}],
                              "en": [{"ext": "json3", "url": "https://x/j"},
                                     {"ext": "vtt", "url": "https://x/en", "name": "English"}],
                              "live_chat": [{"ext": "vtt", "url": "https://x/c"}]},
                "automatic_captions": {"fr": [{"ext": "vtt", "url": "https://x/fr"}],
                                       "ja": [{"ext": "vtt", "url": "https://x/ja"}],
                                       "en-fr": [{"ext": "vtt", "url": "https://x/t"}]}}
        options = resolver.subtitle_options(info)
        self.assertEqual([o["lang"] for o in options], ["en", "fr", "de"])
        self.assertTrue(options[1]["auto"])

    def test_local_sidecars(self):
        video = os.path.join(self.dir, "Film.mkv")
        for name in ("Film.mkv", "Film.en.srt", "Film.srt", "Other.srt", "Film.en.txt"):
            open(os.path.join(self.dir, name), "w").close()
        d = Daemon(store=Store(self.dir, self.dir, self.dir), emit=lambda _m: None)
        options = d._subtitle_options({"source": "local", "extra": {"path": video}}, {})
        self.assertEqual(sorted(o["lang"] for o in options), ["", "en"])


class Alarm(TempDir):
    def test_next_ring(self):
        d = Daemon(store=Store(self.dir, self.dir, self.dir), emit=lambda _m: None)
        settings = dict(daemon.DEFAULT_SETTINGS, alarmEnabled=True, alarmTime="06:30",
                        alarmDays="1,2,3,4,5,6,7")
        d.settings = lambda: settings
        self.assertEqual(d._alarm_next(), 0)      # no station chosen yet
        d.store.state["alarm_item"] = entry("radio:wake")
        at = d._alarm_next()
        self.assertGreater(at, time.time())
        self.assertLessEqual(at, time.time() + 86400 + 3600)
        self.assertEqual(time.strftime("%H:%M", time.localtime(at)), "06:30")
        settings["alarmEnabled"] = False
        self.assertEqual(d._alarm_next(), 0)


# -- casting -----------------------------------------------------------------------

DESCRIPTION = b"""<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0"><device>
<friendlyName>Living Room TV</friendlyName><UDN>uuid:1</UDN><serviceList>
<service><serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
<controlURL>/av</controlURL></service>
<service><serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType>
<controlURL>/rc</controlURL></service></serviceList></device></root>"""


class Casting(TempDir):
    def setUp(self):
        super().setUp()
        calls = self.calls = []

        class Renderer(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(DESCRIPTION)

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"])).decode()
                calls.append((self.path, self.headers["SOAPAction"], body))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"<ok/>")

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Renderer)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.location = "http://127.0.0.1:%d/description.xml" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def test_describe_and_control(self):
        device = cast.describe(self.location)
        self.assertEqual(device["name"], "Living Room TV")
        player = cast.Dlna(device)
        player.play("http://example.com/a.mp3", "A & <B>", "Band", "audio/mpeg")
        player.volume(40)
        actions = [c[1].split("#")[1].rstrip('"') for c in self.calls]
        self.assertEqual(actions, ["Stop", "SetAVTransportURI", "Play", "SetVolume"])
        # The metadata travels as escaped XML inside the SOAP body.
        self.assertIn("&amp;lt;B&amp;gt;", self.calls[1][2])
        self.assertIn("<DesiredVolume>40</DesiredVolume>", self.calls[3][2])

    def test_only_local_addresses(self):
        with self.assertRaises(cast.CastError):
            cast._lan_get("http://93.184.216.34/description.xml")
        with self.assertRaises(cast.CastError):
            cast._soap("https://example.com/av", cast.AVTRANSPORT, "Play", [])

    def test_file_server_serves_one_file_to_one_client(self):
        path = os.path.join(self.dir, "song.mp3")
        with open(path, "wb") as handle:
            handle.write(bytes(range(256)) * 40)
        server = cast.FileServer(path, "127.0.0.1", bind="127.0.0.1")
        try:
            request = urllib.request.Request(server.url, headers={"Range": "bytes=10-19"})
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 206)
                self.assertEqual(response.read(), bytes(range(10, 20)))
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(server.url.replace(server.token, "guess"), timeout=5)
        finally:
            server.close()
        other = cast.FileServer(path, "10.1.2.3", bind="127.0.0.1")
        try:
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(other.url, timeout=5)
        finally:
            other.close()


class GoogleCast(TempDir):
    """The built-in Cast client against a fake device: TLS, length-prefixed
    protobuf frames, LAUNCH → RECEIVER_STATUS → LOAD → MEDIA_STATUS."""

    def setUp(self):
        super().setUp()
        import shutil as _shutil
        import ssl
        import subprocess
        if not _shutil.which("openssl"):
            self.skipTest("openssl is needed to make a test certificate")
        cert, key = os.path.join(self.dir, "c.pem"), os.path.join(self.dir, "k.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", key, "-out", cert, "-days", "1", "-subj", "/CN=cast"],
                       check=True, capture_output=True)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        import socket as _socket
        listener = _socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.port = listener.getsockname()[1]
        self.seen = []

        def serve():
            conn, _ = listener.accept()
            tls = context.wrap_socket(conn, server_side=True)

            def exactly(count):
                data = b""
                while len(data) < count:
                    chunk = tls.recv(count - len(data))
                    if not chunk:
                        raise OSError("closed")
                    data += chunk
                return data

            def read():
                return cast.decode_cast(exactly(int.from_bytes(exactly(4), "big")))

            def send(source, ns, payload):
                tls.sendall(cast.encode_cast(source, "sender-0", ns, payload))
            try:
                while True:
                    message = read()
                    payload = message["payload"]
                    self.seen.append((message["destination"], payload.get("type")))
                    kind = payload.get("type")
                    if kind == "LAUNCH":
                        send("receiver-0", cast.NS_RECEIVER, {
                            "type": "RECEIVER_STATUS", "requestId": payload["requestId"],
                            "status": {"applications": [{"appId": cast.MEDIA_RECEIVER,
                                                         "transportId": "web-1",
                                                         "sessionId": "s-1"}]}})
                    elif kind in ("LOAD", "PAUSE", "PLAY"):
                        send("web-1", cast.NS_MEDIA, {
                            "type": "MEDIA_STATUS", "requestId": payload["requestId"],
                            "status": [{"mediaSessionId": 7}]})
                    elif kind in ("SET_VOLUME", "STOP"):
                        send("receiver-0", cast.NS_RECEIVER, {
                            "type": "RECEIVER_STATUS", "requestId": payload["requestId"],
                            "status": {}})
            except (OSError, ValueError):
                pass
            finally:
                listener.close()
        threading.Thread(target=serve, daemon=True).start()

    def test_frames_round_trip(self):
        frame = cast.encode_cast("a", "b", "urn:x", {"type": "PING", "n": "é" * 200})
        message = cast.decode_cast(frame[4:])
        self.assertEqual(int.from_bytes(frame[:4], "big"), len(frame) - 4)
        self.assertEqual((message["source"], message["destination"], message["namespace"]),
                         ("a", "b", "urn:x"))
        self.assertEqual(message["payload"]["n"], "é" * 200)

    def test_play_pause_volume_stop(self):
        device = cast.Chromecast({"host": "127.0.0.1", "port": self.port, "name": "TV"})
        device.play("https://example.com/a.mp3", "Song", "Band", "audio/mpeg")
        self.assertEqual(device.media_session, 7)
        device.pause()
        device.volume(30)
        device.stop()
        kinds = [k for _d, k in self.seen]
        self.assertEqual(kinds, ["CONNECT", "LAUNCH", "CONNECT", "LOAD", "PAUSE",
                                 "SET_VOLUME", "STOP"])
        self.assertIn(("web-1", "LOAD"), self.seen)    # media goes to the app

    def test_only_local_addresses(self):
        with self.assertRaises(cast.CastError):
            cast.Chromecast({"host": "8.8.8.8", "port": 8009})


class Discovery(unittest.TestCase):
    def test_avahi_lines_are_parsed(self):
        import subprocess
        line = ('=;wlan0;IPv4;Chromecast-abc;_googlecast._tcp;local;abc.local;'
                '192.168.1.40;8009;"id=abc123" "md=Google TV" "fn=Living Room"\n'
                '=;wlan0;IPv4;Far;_googlecast._tcp;local;far.local;8.8.8.8;8009;"fn=Far"\n')
        original = subprocess.run
        subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=line)
        try:
            found = cast.chromecasts()
        finally:
            subprocess.run = original
        self.assertEqual(found, [{"id": "cc:abc123", "kind": "chromecast",
                                  "name": "Living Room", "model": "Google TV",
                                  "host": "192.168.1.40", "port": 8009}])

    def test_the_subnet_sweep_covers_our_network_only(self):
        import subprocess
        data = json.dumps([
            {"ifname": "lo", "flags": ["UP"], "addr_info": [{"local": "127.0.0.1", "prefixlen": 8}]},
            {"ifname": "wlan0", "flags": ["UP"], "addr_info": [{"local": "192.168.1.161",
                                                                "prefixlen": 24}]},
            {"ifname": "tun0", "flags": ["UP"], "addr_info": [{"local": "10.8.0.2",
                                                               "prefixlen": 8}]},
            {"ifname": "eth9", "flags": [], "addr_info": [{"local": "192.168.9.2",
                                                           "prefixlen": 24}]}])
        original = subprocess.run
        subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=data)
        try:
            nets = cast.local_networks()
        finally:
            subprocess.run = original
        self.assertEqual([(own, str(net)) for own, net in nets],
                         [("192.168.1.161", "192.168.1.0/24"), ("10.8.0.2", "10.8.0.0/24")])


class Outputs(unittest.TestCase):
    def test_kinds(self):
        from apctl.core import outputs
        self.assertEqual(outputs.kind_of({"device.bus": "bluetooth"}), "bluetooth")
        self.assertEqual(outputs.kind_of({}, "bluez_output.AA_BB.1"), "bluetooth")
        self.assertEqual(outputs.kind_of({}, "alsa_output.pci.hdmi-stereo"), "hdmi")
        self.assertEqual(outputs.kind_of({"device.icon_name": "audio-headphones"}), "headphones")
        self.assertEqual(outputs.kind_of({"device.bus": "pci"}, "alsa_output.analog"), "speaker")

    def test_bluetooth_needs_a_real_address(self):
        from apctl.core import outputs
        self.assertEqual(outputs.connect_bluetooth("AA:BB; reboot", wait=0), "")


class Installing(unittest.TestCase):
    def test_only_known_packages_reach_the_terminal(self):
        argv = doctor.install_command(["avahi", "x; rm -rf ~", "not-a-package"])
        self.assertIsNotNone(argv)
        self.assertIn("avahi", argv[-1])
        self.assertNotIn("rm -rf", " ".join(argv))
        self.assertNotIn("not-a-package", " ".join(argv))
        self.assertIsNone(doctor.install_command(["x; rm -rf ~"]))

    def test_updates_use_a_full_sync(self):
        argv = doctor.install_command(["yt-dlp"], update=True)
        self.assertIn("pacman -Syu --needed yt-dlp", argv[-1])

    def test_installable_and_prompt(self):
        checks = [
            {"name": "mpv", "ok": False, "required": True, "package": "mpv", "action": "install"},
            {"name": "wpctl", "ok": False, "required": False, "package": "wireplumber",
             "action": "install"},
            {"name": "yt-dlp version", "ok": False, "required": False, "package": "yt-dlp",
             "action": "update"},
            {"name": "hyprctl", "ok": False, "required": False, "package": "", "action": ""},
            {"name": "bwrap", "ok": True, "required": True, "package": "", "action": ""}]
        self.assertEqual(doctor.installable(checks), (["mpv", "wireplumber"], ["yt-dlp"]))
        self.assertEqual(doctor.prompt_names(checks), ["mpv", "yt-dlp version"])
        self.assertEqual(doctor.prompt_names(checks, first_run=True),
                         ["mpv", "wpctl", "yt-dlp version", "hyprctl"])

    def test_every_installable_check_names_a_known_package(self):
        for name, _why, _required in doctor.BINARIES:
            if name != "hyprctl":
                self.assertIn(doctor.PACKAGES[name], doctor.KNOWN_PACKAGES, name)


class HealthPrompt(TempDir):
    def test_first_run_prompts_and_not_now_holds(self):
        events = []
        d = Daemon(store=Store(self.dir, self.dir, self.dir), emit=events.append)
        original = doctor.binaries
        doctor.binaries = lambda: [{"name": "wpctl", "ok": False, "required": False,
                                    "package": "wireplumber", "action": "install",
                                    "level": "warn", "detail": "", "fix": ""}]
        try:
            d.startup_health()
            prompts = [e for e in events if e.get("type") == "health_prompt"]
            self.assertEqual(len(prompts), 1)
            self.assertTrue(prompts[0]["first_run"])
            self.assertIn("wpctl", prompts[0]["missing"])
            # Not first run any more: an optional tool alone does not nag.
            events.clear()
            d.startup_health()
            self.assertFalse([e for e in events if e.get("type") == "health_prompt"])
            # A required one does, until dismissed.
            doctor.binaries = lambda: [{"name": "mpv", "ok": False, "required": True,
                                        "package": "mpv", "action": "install",
                                        "level": "error", "detail": "", "fix": ""}]
            d.startup_health()
            self.assertTrue([e for e in events if e.get("type") == "health_prompt"])
            events.clear()
            d.cmd_health_dismiss({"names": ["mpv"]})
            d.startup_health()
            self.assertFalse([e for e in events if e.get("type") == "health_prompt"])
        finally:
            doctor.binaries = original


# -- podcasts, scrobbling, the guide, MPRIS ---------------------------------------

FEED = b"""<?xml version="1.0"?>
<rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel>
<title>The Show</title><itunes:author>Host</itunes:author>
<itunes:image href="https://img/show.jpg"/>
<item><title>Ep 2</title><enclosure url="https://cdn/2.mp3" type="audio/mpeg"/>
<itunes:duration>1:02:05</itunes:duration><pubDate>Tue, 01 Oct 2024 10:00:00 GMT</pubDate>
<description>&lt;p&gt;Second&lt;/p&gt;</description></item>
<item><title>Ep 1</title><enclosure url="file:///etc/passwd" type="audio/mpeg"/></item>
<item><title>Ep 0</title><enclosure url="https://cdn/0.mp3"/><itunes:duration>95</itunes:duration></item>
</channel></rss>"""


class Podcasts(unittest.TestCase):
    def test_parse(self):
        data = podcast.parse_feed(FEED)
        self.assertEqual(data["show"]["title"], "The Show")
        self.assertEqual(data["show"]["art"], "https://img/show.jpg")
        episodes = data["episodes"]
        self.assertEqual([e["title"] for e in episodes], ["Ep 2", "Ep 0"])   # no file://
        self.assertEqual(episodes[0]["duration"], 3725)
        self.assertEqual(episodes[1]["duration"], 95)
        self.assertEqual(episodes[0]["description"], "Second")
        self.assertGreater(episodes[0]["published"], 0)

    def test_bad_xml_is_an_error_not_a_crash(self):
        self.assertIn("error", podcast.parse_feed(b"<rss><channel>"))
        self.assertIn("error", podcast.parse_feed(b"<html></html>"))

    def test_entity_bombs_are_refused(self):
        bomb = (b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaaaaaaaa">'
                b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]><rss><channel>'
                b'<title>&b;</title></channel></rss>')
        result = podcast.parse_feed(bomb)
        self.assertTrue("error" in result or len(result["show"]["title"]) <= 300)


class Scrobbling(unittest.TestCase):
    def make(self, **settings):
        from apctl.core.scrobble import Scrobbler
        jobs = []
        values = dict(scrobbleEnabled=True, listenbrainzToken="t", **settings)
        return Scrobbler(lambda: values, jobs.append, lambda *a: None), jobs

    def test_split_title(self):
        from apctl.core.scrobble import split_title
        self.assertEqual(split_title("Band - Song"), ("Band", "Song"))
        self.assertEqual(split_title("Just a title"), ("", "Just a title"))

    def test_signature(self):
        from apctl.core.scrobble import lastfm_sign
        import hashlib
        self.assertEqual(lastfm_sign({"b": "2", "a": "1", "format": "json"}, "s"),
                         hashlib.md5(b"a1b2s").hexdigest())

    def test_a_track_scrobbles_once_after_half_its_length(self):
        scrobbler, jobs = self.make()
        state = {"mode": "playing", "duration": 100,
                 "item": {"uid": "music:x", "source": "music", "title": "Song",
                          "artist": "Band"}}
        scrobbler.update(state)                   # now playing
        self.assertEqual(len(jobs), 1)
        scrobbler._last -= 60                     # a minute passes
        scrobbler.update(state)
        self.assertEqual(len(jobs), 2)
        scrobbler._last -= 60
        scrobbler.update(state)
        self.assertEqual(len(jobs), 2)            # never twice

    def test_tv_and_untitled_radio_are_never_scrobbled(self):
        scrobbler, jobs = self.make()
        scrobbler.update({"mode": "playing", "item": {"uid": "tv:a", "source": "tv",
                                                      "title": "News"}})
        scrobbler.update({"mode": "playing", "streamTitle": "Station jingle",
                          "item": {"uid": "radio:a", "source": "radio", "title": "FM"}})
        self.assertEqual(jobs, [])

    def test_off_means_off(self):
        scrobbler, jobs = self.make()
        scrobbler._settings = lambda: {"scrobbleEnabled": False, "listenbrainzToken": "t"}
        scrobbler.update({"mode": "playing", "item": {"uid": "music:x", "source": "music",
                                                      "title": "S", "artist": "A"}})
        self.assertEqual(jobs, [])


XMLTV = """<tv>
<channel id="BBCOne.uk"><display-name>BBC One HD</display-name></channel>
<channel id="Sky.News.uk"><display-name>Sky News</display-name></channel>
<programme start="%s +0000" stop="%s +0000" channel="BBCOne.uk">
<title>The News &amp; Weather</title><desc>Headlines</desc></programme>
<programme start="20000101000000 +0000" stop="20000101010000 +0000" channel="BBCOne.uk">
<title>Long ago</title></programme>
</tv>"""


class Guide(unittest.TestCase):
    def test_parse_window_and_match(self):
        now = time.time()
        fmt = lambda t: time.strftime("%Y%m%d%H%M%S", time.gmtime(t))  # noqa: E731
        guide = tv._parse_guide(XMLTV % (fmt(now - 600), fmt(now + 600)),
                                now - 3600, now + 3600)
        rows = guide["programmes"]["BBCOne.uk"]
        self.assertEqual([r["title"] for r in rows], ["The News & Weather"])
        self.assertAlmostEqual(rows[0]["start"], now - 600, delta=2)
        self.assertEqual(tv.match_channel(guide, "BBC One (1080p)"), "BBCOne.uk")
        self.assertEqual(tv.match_channel(guide, "Sky News [Geo-blocked]"), "Sky.News.uk")
        self.assertEqual(tv.match_channel(guide, "Nothing Like It"), "")


class Mpris(unittest.TestCase):
    def setUp(self):
        from apctl.core import mpris
        if not mpris.available():
            self.skipTest("PyGObject is not installed")
        self.mpris = mpris
        self.bus = mpris.Mpris(lambda _m: None)

    def value(self, name, **state):
        self.bus._state = state
        return self.bus._value(name).unpack()

    def test_status_and_metadata(self):
        item = {"uid": "radio:a b", "title": "Station", "artist": "", "url": "https://s/x"}
        self.assertEqual(self.value("PlaybackStatus"), "Stopped")
        self.assertEqual(self.value("PlaybackStatus", item=item, mode="paused"), "Paused")
        meta = self.value("Metadata", item=item, mode="playing", live=True,
                          streamTitle="Band - Song")
        self.assertEqual(meta["xesam:title"], "Band - Song")
        self.assertEqual(meta["xesam:album"], "Station")
        self.assertTrue(meta["mpris:trackid"].startswith("/org/aurorapulse/track/radio_a_b"))
        self.assertEqual(self.value("Volume", volume=50, muted=False), 0.5)
        self.assertEqual(self.value("Volume", volume=50, muted=True), 0.0)
        self.assertFalse(self.value("CanSeek", item=item, live=True, duration=0))


# -- the sandboxed tool runner -------------------------------------------------------

class ToolRunner(TempDir):
    def test_reads_only_what_it_is_given(self):
        try:
            sandbox.sandbox_command("artwork")
        except sandbox.SandboxUnavailable:
            self.skipTest("no sandbox here")
        allowed = os.path.join(self.dir, "allowed.txt")
        secret = os.path.join(self.dir, "secret.txt")
        for path in (allowed, secret):
            with open(path, "w") as handle:
                handle.write("data")
        seen = sandbox.run_tool(["/usr/bin/cat", allowed], read=[allowed], timeout=20)
        self.assertEqual(seen.stdout, b"data")
        hidden = sandbox.run_tool(["/usr/bin/cat", secret], read=[allowed], timeout=20)
        self.assertNotEqual(hidden.returncode, 0)

    def test_writes_only_where_it_is_allowed(self):
        try:
            sandbox.sandbox_command("artwork")
        except sandbox.SandboxUnavailable:
            self.skipTest("no sandbox here")
        out = os.path.join(self.dir, "out")
        sandbox.run_tool(["/usr/bin/touch", os.path.join(out, "made")], write=[out],
                         timeout=20)
        self.assertTrue(os.path.exists(os.path.join(out, "made")))
        sandbox.run_tool(["/usr/bin/touch", os.path.join(self.dir, "escaped")], write=[out],
                         timeout=20)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "escaped")))


class Recording(TempDir):
    def test_waits_for_the_last_block(self):
        """mpv writes the end of a recording a moment after being told to
        stop; an empty file at first is not an empty recording."""
        events = []
        d = Daemon(store=Store(self.dir, self.dir, self.dir), emit=events.append)
        d.player = FakePlayer()
        host = os.path.join(self.dir, "rec.mp3")
        open(host, "wb").close()
        d._recording = {"since": time.time() - 30, "title": "Station", "host": host,
                        "ext": "mp3"}
        d.cmd_scan = lambda _m: None
        from apctl.core import downloads
        original = downloads.recordings_dir
        downloads.recordings_dir = lambda: os.path.join(self.dir, "Recordings")
        try:
            flushed = d._finish_recording()

            def late_write():
                time.sleep(1.2)
                with open(host, "wb") as handle:
                    handle.write(b"x" * (64 << 10))
            threading.Thread(target=late_write).start()
            self.assertTrue(flushed.wait(10))
            time.sleep(0.5)
        finally:
            downloads.recordings_dir = original
        saved = os.listdir(os.path.join(self.dir, "Recordings"))
        self.assertEqual(len(saved), 1)
        self.assertTrue(saved[0].startswith("Station "))
        self.assertEqual(d.player.props.get("stream-record"), "")


if __name__ == "__main__":
    unittest.main()
