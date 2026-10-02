"""Regression tests for the failures that were silent rather than loud.

Each test here corresponds to a bug that produced no error at all: the player
looked fine, the panel rendered, and only careful measurement showed nothing
was happening. That is the class of bug worth pinning down.

Run with:  python3 tests/test_regressions.py
"""

import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from apctl.core import session  # noqa: E402
from apctl.core.player import Mpv  # noqa: E402
from apctl.util import sandbox  # noqa: E402

TMP = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"),
                   "ap-regress-%d" % os.getpid())


def _clean():
    os.makedirs(TMP, exist_ok=True)
    for name in os.listdir(TMP):
        path = os.path.join(TMP, name)
        try:
            os.unlink(path)
        except OSError:
            pass


class FilteredBrowseNeverBecomesUnfiltered(unittest.TestCase):
    """A filter that stops filtering is worse than a filter that errors.

    browse() asked the mirror for the filtered list first and, when that came
    back empty, fell through to the directory's pre-baked topclick page. Those
    pages carry no filter at all, so picking a country the mirror had no
    stations for filled the list with worldwide favourites while the country
    pill still read as if a filter were applied. Nothing failed; the list was
    just quietly not what you asked for, which is exactly the shape of bug
    that survives until someone happens to try an unusual country.
    """

    def test_no_network_when_a_filter_is_set(self):
        from apctl.sources import radio

        def explode(*a, **kw):
            raise AssertionError("network fallback ran under a filter")

        radio._mirror_get = explode
        radio.cached = lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("network fallback ran under a filter"))
        self.addCleanup(setattr, radio, "_mirror_get", radio._mirror_get)

        # No catalogue is installed here, so the mirror query legitimately
        # comes back empty - which is exactly the situation that used to
        # silently fall through to unfiltered remote pages.
        radio._CATALOGUE = None
        self.assertEqual(radio.browse("popular", 10, 0, None, country="ZZ"), [])
        self.assertEqual(radio.browse("popular", 10, 0, None, genre="jazz"), [])
        self.assertEqual(radio.by_tag("jazz", 10, 0, None, country="ZZ"), [])

    def test_unfiltered_browse_still_uses_the_network(self):
        from apctl.sources import radio
        radio._CATALOGUE = None
        calls = []

        def fake_cached(key, fn, ttl=None):
            calls.append(key)
            return ([{"name": "Some FM", "url_resolved": "https://e/s",
                      "stationuuid": "u1", "codec": "MP3",
                      "bitrate": 128, "lastcheckok": 1}], None)

        radio.cached = fake_cached
        out = radio.browse("popular", 10, 0, None)
        self.assertTrue(out, "an unfiltered browse must still work offline-first")


class RelayIsFullDuplex(unittest.TestCase):
    """A proxy that only copies in one direction is not a proxy.

    The CONNECT handler used to push the client's bytes upstream and never
    brought anything back. The handshake appeared to succeed, TLS then timed
    out, and mpv sat idle forever with no error anywhere.
    """

    def setUp(self):
        _clean()
        self.proxy_path = os.path.join(TMP, "proxy.sock")
        self.proxy = session.GuardedProxy(self.proxy_path)
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()
        time.sleep(0.2)
        self.addCleanup(self.proxy.shutdown)

    def test_get_returns_the_body(self):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(20)
        client.connect(self.proxy_path)
        client.sendall(b"GET http://ice1.somafm.com/groovesalad-128-mp3 "
                       b"HTTP/1.1\r\nHost: ice1.somafm.com\r\n"
                       b"Connection: close\r\n\r\n")
        data = self._read(client, 4000)
        self.assertIn(b"200", data[:40], "the guard proxy answered %r" % data[:60])
        self.assertGreater(len(data), 200, "headers only, no body")

    def test_private_address_is_refused(self):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(10)
        client.connect(self.proxy_path)
        client.sendall(b"GET http://127.0.0.1:9000/ HTTP/1.1\r\n"
                       b"Host: 127.0.0.1\r\nConnection: close\r\n\r\n")
        data = self._read(client, 2000)
        self.assertIn(b"403", data[:40], "private address reached: %r" % data[:80])

    def test_bridge_relays_in_both_directions(self):
        """The in-sandbox bridge had the same one-directional bug.

        The bridge is what turns the unix-socket guard into the http_proxy URL
        mpv insists on, so a half-open bridge makes every remote stream fail
        while local files keep working - a confusingly narrow failure.
        """
        bridge = session._Bridge(self.proxy_path,
                                 threading.BoundedSemaphore(8))
        threading.Thread(target=bridge.serve_forever, daemon=True).start()
        self.addCleanup(bridge.shutdown)
        port = bridge.server_address[1]

        client = socket.create_connection(("127.0.0.1", port), 10)
        client.settimeout(20)
        client.sendall(b"GET http://ice1.somafm.com/groovesalad-128-mp3 "
                       b"HTTP/1.1\r\nHost: ice1.somafm.com\r\n"
                       b"Connection: close\r\n\r\n")
        data = self._read(client, 4000)
        self.assertIn(b"200", data[:40],
                      "the bridge returned %r - it is not relaying back" % data[:60])

    @staticmethod
    def _read(sock, limit):
        data = b""
        try:
            while len(data) < limit:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
        except OSError:
            pass
        return data


class IpcRepliesReachTheWaiter(unittest.TestCase):
    """get_property used to race the event reader for the same socket.

    Both were reading the same fd, the reader usually won, and every property
    silently came back as None - which the UI renders as position 0:00 for a
    track that is playing perfectly well.
    """

    class _FakeMpv(threading.Thread):
        """A stand-in that speaks just enough of mpv's JSON IPC."""

        def __init__(self, path, count=200):
            super().__init__(daemon=True)
            self.path = path
            self.count = count
            self.served = 0
            try:
                os.unlink(path)
            except OSError:
                pass
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.bind(path)
            self.sock.listen(4)

        def run(self):
            buffer = b""
            while self.served < self.count:
                try:
                    client, _ = self.sock.accept()
                except OSError:
                    return
                buffer = b""
                client.settimeout(10)
                while self.served < self.count:
                    try:
                        chunk = client.recv(65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    buffer += chunk
                    while b"\n" in buffer:
                        line, _, buffer = buffer.partition(b"\n")
                        try:
                            import json
                            message = json.loads(line)
                        except ValueError:
                            continue
                        name = message["command"][1]
                        self.served += 1
                        reply = json.dumps(
                            {"request_id": message.get("request_id"),
                             "error": "success", "data": 42.0 if name != "bogus" else None})
                        try:
                            client.sendall((reply + "\n").encode())
                        except OSError:
                            break

    def test_get_property_answers(self):
        _clean()
        path = os.path.join(TMP, "mpv.sock")
        server = self._FakeMpv(path, count=500)
        server.start()
        self.addCleanup(server.sock.close)

        client = Mpv(path, on_event=lambda message: None)
        self.assertTrue(client.attach(timeout=5))
        self.addCleanup(client.close)

        for _ in range(30):
            self.assertEqual(client.get_property("time-pos"), 42.0)
        self.assertIsNone(client.get_property("bogus"))

    def test_reads_and_events_do_not_steal_each_other(self):
        """Observed property events arrive continuously; a reader that only
        handled replies would stop answering the moment they started."""
        _clean()
        path = os.path.join(TMP, "mpv2.sock")
        server = self._FakeMpv(path, count=500)
        server.start()
        self.addCleanup(server.sock.close)

        events = []
        client = Mpv(path, on_event=events.append)
        self.assertTrue(client.attach(timeout=5))
        self.addCleanup(client.close)

        for _ in range(20):
            self.assertEqual(client.get_property("pause"), 42.0)
        self.assertEqual(events, [], "replies must not be delivered as events")


class SessionMounts(unittest.TestCase):
    """The sandbox has no /home, so anything the player reads has to be
    handed in. These are the binds that make the player work at all."""

    def test_profiles_only_bind_what_they_need(self):
        audio, _ = sandbox.sandbox_command("audio", with_script=False)
        video, _ = sandbox.sandbox_command("video", with_script=False)
        self.assertIn("--dev-bind", video, "video needs the DRM render node")
        self.assertNotIn("--dev-bind", audio, "audio has no business with the GPU")
        for argv in (audio, video):
            self.assertNotIn("--unshare-net", argv,
                             "audio must be able to reach the guarded proxy")

    def test_video_reaches_the_network_only_through_the_guard(self):
        """A TV channel is an HLS stream: segments arrive for as long as you
        watch, so there is nothing to pre-resolve and the video player does
        need a route out.

        What must never be true is a *direct* one. Both profiles therefore get
        the guarded proxy and neither gets a network namespace of its own to
        route around it, and this asserts the negative - the property that
        would let mpv bypass the guard - is absent.
        """
        from apctl.core import session as session_module
        self.assertTrue(sandbox.PROFILES["video"]["network"])
        self.assertTrue(session_module.GuardedProxy is not None)

        argv, _ = sandbox.sandbox_command("video", with_script=False)
        # No netns: a fresh one would still route, but a *shared* one with the
        # host is what would hand mpv a way around the proxy.
        self.assertNotIn("--share-net", argv)
        # Every network destination therefore has to arrive via the bridge,
        # which exists only inside the session and only talks to the guard.
        self.assertNotIn("--unshare-net", argv)

    def test_video_profile_binds_what_the_gpu_driver_needs(self):
        """NVIDIA's userspace driver does not live on /dev/dri.

        Without its control node and /sys, Mesa cannot create a dri2 screen,
        so no video output is produced and the only symptom is a stream that
        never starts. On an NVIDIA machine this is the difference between
        working video and a silently blank one.
        """
        import glob
        argv, _ = sandbox.sandbox_command("video", with_script=False)
        if glob.glob("/dev/nvidia[0-9]*"):
            self.assertIn("/dev/nvidiactl", argv,
                          "the proprietary driver needs its control node")
            self.assertIn("/sys", argv, "libdrm needs /sys to enumerate GPUs")

    def test_video_profile_does_not_abandon_the_driver_nodes(self):
        """A read-only /dev/dri left the DRM node present but unopenable."""
        argv, _ = sandbox.sandbox_command("video", with_script=False)
        triples = [tuple(argv[i:i + 3]) for i in range(len(argv) - 2)]
        if ("--dev-bind", "/dev/dri", "/dev/dri") in triples:
            self.assertNotIn(("--ro-bind", "/dev/dri", "/dev/dri"), triples)
        # The Wayland socket is connect(2)-ed, which needs write permission.
        self.assertNotIn(("--ro-bind", "/run/wayland-1", "/run/wayland-1"),
                         triples)

    def test_script_is_self_contained(self):
        """The sandbox gets the executable over a memfd and has no host path
        to it, so the whole package has to travel inside that one file."""
        blob = sandbox.build_zipapp()
        self.assertTrue(blob.startswith(b"#!"), "a zipapp needs its shebang")
        self.assertIn(b"apctl/core/daemon.py", blob)
        self.assertIn(b"apctl/util/sandbox.py", blob)
        self.assertNotIn(b"__pycache__", blob, "stale bytecode must not ship")


class PlayerAttachment(unittest.TestCase):
    """The daemon attaches to a player that is a *separate process*.

    Two things went wrong here, both silent: `attach` checked for the socket
    once and gave up, so asking for thirty seconds got thirty milliseconds and
    a working video channel reported "the player did not come up"; and the
    window title was only prefixed when it had no colon in it, which a media
    uid always does, so the panel never found its own video window.
    """

    def test_attach_waits_for_a_socket_that_does_not_exist_yet(self):
        from apctl.core.player import Mpv
        directory = tempfile.mkdtemp(prefix="ap-attach-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "later.sock")

        def arrive():
            time.sleep(0.4)
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(path)
            server.listen(1)
            time.sleep(3)
            server.close()

        threading.Thread(target=arrive, daemon=True).start()
        client = Mpv(path, on_event=lambda message: None)
        self.addCleanup(client.close)
        self.assertTrue(client.attach(timeout=3.0),
                        "attach returned before the socket appeared")
        self.assertFalse(Mpv(os.path.join(directory, "never.sock"))
                         .attach(timeout=0.2),
                         "a socket that never arrives must not claim success")

    def test_video_window_title_is_always_marked(self):
        """A media uid is full of colons, so a conditional prefix never fires."""
        from apctl.core import session as session_module
        from apctl.core import daemon as daemon_module
        self.assertEqual(session_module.TITLE_PREFIX,
                         daemon_module.TITLE_PREFIX,
                         "the marker has to be the same string in both")
        # Whatever the session is handed, the window it creates is marked.
        for uid in ("tv:3ABN Kids", "radio:abc-123", "", "youtube:xyz"):
            marked = (session_module.TITLE_PREFIX + str(uid or ""))
            self.assertTrue(marked.startswith(daemon_module.TITLE_PREFIX))


class MpvOptions(unittest.TestCase):
    """mpv treats an option it does not know as fatal, and says so quietly.

    `--reconnect` and `--input-fs-disable` were both removed between mpv
    releases. Passing either made the player exit before playing anything,
    with no message, because --really-quiet suppresses the diagnostic. The
    first of those looked like "the internet player does not work" and the
    second like "the TV player does not work", across separate sessions.
    """

    def test_only_options_this_mpv_accepts(self):
        from apctl.core import player as player_module
        supported = player_module.supported_options()
        if not supported:
            self.skipTest("cannot ask this mpv what it supports")
        for video in (False, True):
            args = player_module.mpv_arguments(70, video, "t")
            for arg in args:
                if not arg.startswith("--"):
                    continue
                name = arg[2:].split("=", 1)[0]
                positive = name[3:] if name.startswith("no-") else name
                self.assertIn(positive, supported,
                              "%s is not an option on this mpv" % name)

    def test_safety_options_are_never_dropped(self):
        """The filter must not remove a flag that makes the player safer or
        more predictable just because the list is slightly out of date."""
        from apctl.core import player as player_module
        audio = player_module.mpv_arguments(70, False, "t")
        # Options that carry a value are matched on their name.
        names = {a.split("=", 1)[0] for a in audio}
        for required in ("--no-config", "--really-quiet", "--ytdl",
                         "--tls-verify", "--input-ipc-server",
                         "--load-unsafe-playlists"):
            self.assertIn(required, names)
        self.assertIn("--ao=pcm", audio)
        self.assertIn("--no-video", audio)

    def test_video_also_uses_the_sandboxed_audio_path(self):
        """Video needs sound, and letting mpv pick an output means it probes
        ALSA inside a namespace with no sound devices and aborts the file."""
        from apctl.core import player as player_module
        video = player_module.mpv_arguments(70, True, "t")
        self.assertIn("--ao=pcm", video)
        self.assertIn("--vo=gpu-next", video)
        self.assertNotIn("--no-video", video)
        # --force-window aborts while idling, which is how a queue-driven
        # player always starts.
        self.assertNotIn("--force-window=immediate", video)


def _test_daemon(case, events=None):
    """A real Daemon over a throwaway directory, with nothing started."""
    from apctl.core.daemon import Daemon
    from apctl.core.state import Store
    runtime = tempfile.mkdtemp(prefix="ap-test-")
    case.addCleanup(shutil.rmtree, runtime, True)
    sink = events if events is not None else []
    instance = Daemon(store=Store(runtime, runtime, runtime), emit=sink.append)
    instance._log = lambda *_a, **_k: None
    return instance


class VolumeAndMute(unittest.TestCase):
    """Mute reported the state it intended, not the state that existed.

    `cmd_mute` read `self.muted`, pushed `not self.muted` at the player, then
    read `self.muted` a second time to build the reply. `set_property` is
    fire-and-forget over a socket, so that second read almost always returned
    the value from *before* the write. The reported flag never changed, so the
    panel believed itself muted forever. That is worse than a mute that does
    nothing: the volume track in the panel draws zero width whenever it is
    muted, so a stuck mute also looks exactly like a volume slider that cannot
    be adjusted.
    """

    class _FakePlayer:
        """A player that applies writes asynchronously, like the real one."""

        def __init__(self):
            self.props = {"mute": False, "volume": 70.0, "pause": False}
            self._lock = threading.Lock()

        def get_property(self, name):
            with self._lock:
                return self.props.get(name)

        def set_property(self, name, value):
            def apply_later():
                time.sleep(0.05)
                with self._lock:
                    self.props[name] = value
            threading.Thread(target=apply_later, daemon=True).start()
            return True

    def _daemon(self, player=True):
        instance = _test_daemon(self)
        instance.player = self._FakePlayer() if player else None
        return instance

    def test_rapid_double_toggle_reports_both_states(self):
        """Two clicks in quick succession must not both claim to mute.

        This is the case a person actually hits. The write has not landed yet
        when the second press reads the flag back, so the panel is told it is
        still muted and the track stays at zero width.
        """
        instance = self._daemon()
        first = instance.cmd_mute({})
        second = instance.cmd_mute({})
        self.assertTrue(first.get("muted"), "the first press must report muted")
        self.assertFalse(
            second.get("muted"),
            "the second press reported muted again, so the panel can never "
            "unmute and the volume track stays pinned at zero width")

    def test_mute_works_before_a_player_is_attached(self):
        """With no player there is nothing to ask, so the flag must still move."""
        instance = self._daemon(player=False)
        self.assertTrue(instance.cmd_mute({}).get("muted"))
        self.assertFalse(
            instance.cmd_mute({}).get("muted"),
            "toggling with no player attached must still flip the flag")

    def test_volume_reports_the_value_it_set(self):
        instance = self._daemon()
        for wanted in (0, 30, 85, 100):
            self.assertEqual(
                instance.cmd_volume({"set": wanted}).get("volume"), wanted,
                "volume %d did not come back" % wanted)


class PlayPauseReachesMpv(unittest.TestCase):
    """The play/pause button paused the label, not the music.

    Two separate faults were stacked on top of each other, and each one on its
    own looked perfectly healthy.

    The first was in `cmd_toggle`: it computed the state it wanted and handed
    it straight back to the panel without ever writing it to the player. The
    button reported a pause and the audio carried on.

    The second is why the first was never noticed. `Mpv.command` stringified
    every argument on the way to the socket, so `set_property("pause", True)`
    left as the string "True". mpv does not read that as a boolean. It replies
    "success" and leaves the property exactly as it was, which is the most
    annoying possible failure: every layer reported success. Numbers survived
    the same treatment, because mpv will happily read "55" as 55, so volume
    and seeking kept working and made the whole control row look healthy.

    So the fake player used by VolumeAndMute cannot catch this. It records
    whatever it is handed, booleans included, and the real socket is the only
    place the difference shows up.
    """

    def test_arguments_keep_their_types_on_the_wire(self):
        """A boolean has to reach mpv as a boolean, not as text."""
        from apctl.core.player import _ipc_value
        for value in (True, False):
            sent = json.loads(json.dumps([_ipc_value(value)]))[0]
            self.assertIs(
                sent, value,
                "True went out as %r, which mpv reads as a string and "
                "ignores" % (sent,))
        self.assertEqual(json.loads(json.dumps([_ipc_value(55)]))[0], 55)
        self.assertEqual(
            json.loads(json.dumps([_ipc_value(12.5)]))[0], 12.5)

    def test_a_real_player_actually_pauses(self):
        """Against a real mpv, not a stub that agrees with anything."""
        import subprocess
        if shutil.which("mpv") is None:
            self.skipTest("mpv is not installed")
        directory = tempfile.mkdtemp(prefix="ap-pause-")
        path = os.path.join(directory, "mpv.sock")
        proc = subprocess.Popen(
            ["mpv", "--no-config", "--idle=yes", "--no-terminal",
             "--really-quiet", "--ao=null", "--vo=null",
             "--input-ipc-server=" + path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        client = None
        try:
            client = Mpv(path, on_event=lambda message: None)
            self.assertTrue(client.attach(timeout=10), "could not attach to mpv")
            self.assertIs(client.get_property("pause"), False)
            client.set_property("pause", True)
            time.sleep(1.0)
            self.assertIs(
                client.get_property("pause"), True,
                "mpv said success and then ignored the pause, which is what "
                "happens when the boolean was sent as the string 'True'")
            client.set_property("pause", False)
            time.sleep(1.0)
            self.assertIs(
                client.get_property("pause"), False,
                "the second pause did not land either, so the button could "
                "never un-pause once it had paused")
        finally:
            if client is not None:
                client.close()
            proc.kill()
            proc.wait(timeout=10)
            shutil.rmtree(directory, ignore_errors=True)

    def test_a_confirmed_write_lands_before_it_returns(self):
        """`confirm` has to mean the write is already in effect on return.

        This is what makes the button trustworthy. The reply the panel receives
        is built from the value that was asked for, so if the daemon returns
        before mpv has applied it, the panel shows the new state while the
        player is still on the old one, and the next read snaps back.
        """
        import subprocess
        if shutil.which("mpv") is None:
            self.skipTest("mpv is not installed")
        directory = tempfile.mkdtemp(prefix="ap-confirm-")
        path = os.path.join(directory, "mpv.sock")
        proc = subprocess.Popen(
            ["mpv", "--no-config", "--idle=yes", "--no-terminal",
             "--really-quiet", "--ao=null", "--vo=null",
             "--input-ipc-server=" + path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        client = None
        try:
            client = Mpv(path, on_event=lambda message: None)
            self.assertTrue(client.attach(timeout=10))
            self.assertTrue(
                client.set_property("pause", True, confirm=True),
                "mpv refused the pause")
            self.assertIs(
                client.get_property("pause"), True,
                "the confirmed write returned but had not been applied yet, so "
                "the panel and the player disagreed until the next read")
        finally:
            if client is not None:
                client.close()
            proc.kill()
            proc.wait(timeout=10)
            shutil.rmtree(directory, ignore_errors=True)

    class _RecordingPlayer:
        def __init__(self):
            self.writes = []
            self.props = {"pause": False}

        def get_property(self, name):
            return self.props.get(name)

        def set_property(self, name, value, confirm=False):
            self.writes.append((name, value, confirm))
            self.props[name] = value
            return True

    def test_toggle_tells_the_player_at_all(self):
        """The button must write to the player, not just report an opinion.

        The write is no longer confirmed before replying: waiting for mpv's
        acknowledgement on a network stream took well over a second and held
        every volume step queued behind it. The state the panel sees is built
        from the daemon's cache, which mpv's own property-change corrects.
        """
        events = []
        instance = _test_daemon(self, events)
        instance.player = self._RecordingPlayer()
        instance._current = {"uid": "radio:x", "source": "radio", "title": "X"}
        instance.queue = [instance._current]
        self.assertTrue(instance.cmd_toggle({}).get("paused"))
        self.assertEqual(
            [(n, v) for n, v, _c in instance.player.writes], [("pause", True)],
            "cmd_toggle returned a paused flag without ever asking the player "
            "to pause, so the button did nothing at all")
        self.assertEqual(events[-1].get("mode"), "paused",
                         "the panel must hear about the pause at once")
        self.assertFalse(instance.cmd_toggle({}).get("paused"))
        self.assertEqual([(n, v) for n, v, _c in instance.player.writes],
                         [("pause", True), ("pause", False)])


class SwitchingStationsDoesNotRestartThePlayer(unittest.TestCase):
    """Every station change killed the player and built a new one.

    `_start_session` began with `_teardown`, so clicking a second station, or
    pressing next, shut mpv down and started a fresh sandbox, proxy and player
    around it. On paper that is just how a new stream starts. In use it reads
    as the station stopping: there is a gap of several seconds of silence while
    all of that comes up, so a radio station appears to cut out rather than
    change.

    mpv can load the next thing into the player that is already running, and
    `session.py` already builds exactly that command for queue changes. The
    daemon simply never used it. The reuse is deliberately narrow: it applies
    only when the running session has the same shape as the new item, because
    an audio session cannot grow a video window and a video session cannot be
    reduced back to one.
    """

    class _Player:
        def __init__(self, idle=False):
            self.idle = idle
            self.props = {"idle-active": idle, "volume": 70.0, "mute": False}
            self.commands = []
            self.props_written = []

        def get_property(self, name):
            return self.props.get(name)

        def set_property(self, name, value):
            self.props_written.append((name, value))
            self.props[name] = value
            return True

        def command(self, *args):
            self.commands.append(args)
            return True

        def request(self, *args, timeout=2.0):
            self.commands.append(args)
            return True, {"playlist_entry_id": len(self.commands)}

        def seek(self, target):
            self.commands.append(("seek", target))
            return True

    def _daemon(self, player, session_video):
        instance = _test_daemon(self)
        instance.player = player
        instance.session = object()
        instance._session_video = session_video
        instance._current = {"source": "radio", "uid": "old"}
        instance._teardown_calls = 0
        instance._teardown_session = lambda: setattr(
            instance, "_teardown_calls", instance._teardown_calls + 1)
        return instance

    def test_a_second_station_is_loaded_into_the_running_player(self):
        player = self._Player()
        instance = self._daemon(player, False)
        self.assertTrue(instance._reuse_running(
            {"url": "https://radio.example/stream"}, False, 0),
            "switching between two stations must not need a new player")
        self.assertIn(
            ("loadfile", "https://radio.example/stream", "replace"),
            player.commands,
            "the new station was never handed to the running player")

    def test_a_window_cannot_be_added_or_taken_away(self):
        """Audio and video sessions are not interchangeable."""
        player = self._Player()
        instance = self._daemon(player, False)
        self.assertFalse(
            instance._reuse_running({"url": "https://tv.example/live"}, True, 0),
            "an audio session cannot grow a video window")

        player2 = self._Player()
        instance2 = self._daemon(player2, True)
        self.assertFalse(
            instance2._reuse_running({"url": "https://radio.example/s"}, False, 0),
            "a video session cannot be reduced back to audio")
        self.assertEqual(player2.commands, [])

    def test_a_local_file_is_not_handed_to_a_sandbox_that_cannot_see_it(self):
        """The sandbox has no /home, so a new local path is not reachable."""
        player = self._Player()
        instance = self._daemon(player, False)
        self.assertFalse(
            instance._reuse_running({"url": "/home/me/song.flac"}, False, 0))
        self.assertEqual(player.commands, [])

    def test_a_player_sitting_idle_is_reused(self):
        """An idle mpv - after a dead stream, say - still takes a loadfile, so
        it is reused rather than torn down and rebuilt."""
        player = self._Player(idle=True)
        instance = self._daemon(player, False)
        self.assertTrue(instance._reuse_running(
            {"url": "https://radio.example/stream"}, False, 0))
        self.assertEqual(instance._teardown_calls, 0)


class OrphanedSessionIsAdopted(unittest.TestCase):
    """A shell restart must not lose track of what is still playing.

    The queue and the "what is playing" pointer live in the daemon process, but
    mpv is a separate one. Reload the shell and the old daemon is gone while
    the audio keeps going, so a fresh daemon that reports silence is factually
    wrong and the panel shows an empty player over live sound. The daemon now
    records the session and adopts it back, but only when a player really is
    there - adopting on the strength of a stale file would show a station that
    is not playing.
    """

    class _StubPlayer:
        def get_property(self, name):
            return {"pause": False, "time-pos": 0.0, "duration": 0.0}.get(name)

    def _daemon(self, runtime):
        from apctl.core.daemon import Daemon
        from apctl.core.state import Store
        return Daemon(store=Store(runtime, runtime, runtime), emit=lambda _m: None)

    def test_a_running_player_is_picked_back_up(self):
        runtime = tempfile.mkdtemp(prefix="ap-adopt-")
        self.addCleanup(shutil.rmtree, runtime, True)
        item = {"uid": "radio:abc", "source": "radio", "title": "A Station",
                "artist": "", "url": "https://example.invalid/s"}
        first = self._daemon(runtime)
        first._current = item
        first.queue = [item]
        first.index = 0
        first._save_session()
        self.assertTrue(os.path.exists(os.path.join(runtime, "session.json")),
                        "the session was not recorded at all")

        # A new daemon over the same runtime, with a player really attached.
        revived = self._daemon(runtime)
        revived.player = self._StubPlayer()
        revived._adopt_orphaned_session()
        self.assertEqual((revived._current or {}).get("title"), "A Station",
                         "the new daemon did not pick the session back up")
        self.assertEqual(len(revived.queue), 1, "the queue was lost")

    def test_a_saved_session_with_no_player_is_not_adopted(self):
        """A crash leaves the file behind with nothing playing."""
        runtime = tempfile.mkdtemp(prefix="ap-adopt-")
        self.addCleanup(shutil.rmtree, runtime, True)
        item = {"uid": "radio:abc", "source": "radio", "title": "Ghost",
                "url": "https://example.invalid/s"}
        first = self._daemon(runtime)
        first._current = item
        first.queue = [item]
        first._save_session()

        revived = self._daemon(runtime)
        revived.player = None
        revived._adopt_orphaned_session()
        self.assertIsNone(
            revived._current,
            "adopted a session with no player behind it, so the panel would "
            "show a station as playing when nothing is")


class DeadStreamIsNotReportedAsPlaying(unittest.TestCase):
    """Starting a player is not the same as playing through it.

    A TV channel is a URL somebody published at some point. Plenty of them are
    gone, geo-blocked or malformed now, and mpv answers that with
    `end-file reason=error` followed by going straight back to idle. Treating
    every `end-file` as a normal finish meant the item stayed current and the
    panel kept saying a channel was playing while mpv held an empty playlist,
    so clicking a dead channel looked exactly like clicking a dead widget.
    """

    def _daemon(self, runtime, events):
        from apctl.core.daemon import Daemon
        from apctl.core.state import Store
        return Daemon(store=Store(runtime, runtime, runtime),
                      emit=events.append)

    def test_a_failed_load_clears_the_item_and_reports_why(self):
        runtime = tempfile.mkdtemp(prefix="ap-dead-")
        self.addCleanup(shutil.rmtree, runtime, True)
        events = []
        daemon = self._daemon(runtime, events)
        daemon._current = {"uid": "tv:3sat", "source": "tv", "title": "3sat",
                           "url": "https://example.invalid/playlist.m3u"}
        daemon.queue = [daemon._current]
        daemon.index = 0

        daemon._on_mpv({"event": "end-file", "reason": "error",
                        "file_error": "loading failed"})

        self.assertIsNone(
            daemon._current,
            "still reporting a channel as playing after mpv gave up on it")
        errors = [e for e in events if e.get("type") == "error"]
        self.assertEqual(len(errors), 1, "the failure was not reported at all")
        self.assertIn("3sat", errors[0]["message"])
        self.assertIn("loading failed", errors[0]["message"],
                      "the reason was dropped, so the user is told nothing")
        self.assertTrue(any(e.get("type") == "state" for e in events),
                        "no state event, so the bar keeps the stale title")

    def test_a_normal_end_of_file_is_not_treated_as_a_failure(self):
        """Reaching the end of a track is not an error and must not clear it."""
        runtime = tempfile.mkdtemp(prefix="ap-eof-")
        self.addCleanup(shutil.rmtree, runtime, True)
        events = []
        daemon = self._daemon(runtime, events)
        daemon._current = {"uid": "local:x", "source": "local", "title": "X",
                           "url": "/tmp/x.mp3"}
        daemon._on_mpv({"event": "end-file", "reason": "eof"})
        self.assertEqual((daemon._current or {}).get("uid"), "local:x",
                         "a track finishing was reported as a failure")
        self.assertEqual([e for e in events if e.get("type") == "error"], [])


class PluginDirectoryStaysUntouched(unittest.TestCase):
    """The plugin must never write into its own directory.

    This plugin is installed under ~/.config/omarchy/plugins/, and the shell
    watches that directory and reloads the plugin - tearing down the whole bar
    and bringing it back - when anything in it changes. Python writes a
    __pycache__ directory next to its source the first time a process imports
    a module, so selecting a station caused a brand new daemon to import
    modules it had not seen before, drop a couple of dozen .pyc files into the
    plugin, and make the shell restart. It read as "the bar dies when I press
    play", and it recurred on the first play after every daemon start.

    The fix is to not write bytecode at all. The test runs the real entry point
    against a throwaway copy of the tree and fails if a single .pyc appears.
    """

    def _copy_tree(self):
        import shutil
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        tmp = tempfile.mkdtemp(prefix="ap-clean-")
        self.addCleanup(shutil.rmtree, tmp, True)
        shutil.copy2(os.path.join(root, "ap-ctl"), os.path.join(tmp, "ap-ctl"))
        shutil.copytree(os.path.join(root, "src"),
                        os.path.join(tmp, "src"),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        return tmp

    def test_running_the_daemon_leaves_no_bytecode_behind(self):
        import subprocess
        tmp = self._copy_tree()
        env = dict(os.environ)
        env.update({"AP_DATA_DIR": os.path.join(tmp, "data"),
                    "AP_CACHE_DIR": os.path.join(tmp, "cache"),
                    "AP_RUNTIME_DIR": os.path.join(tmp, "run")})
        proc = subprocess.run([os.path.join(tmp, "ap-ctl"), "daemon"],
                              input="", capture_output=True, text=True,
                              timeout=90, env=env)
        self.assertNotIn("Traceback", proc.stderr, proc.stderr[:400])
        written = []
        for base, dirs, files in os.walk(tmp):
            if "data" in base or "/cache" in base or "/run" in base:
                continue
            written += [os.path.join(base, f) for f in files
                        if f.endswith(".pyc")]
            written += [os.path.join(base, d) for d in dirs
                        if d == "__pycache__"]
        self.assertEqual(written, [],
                         "the daemon wrote into the plugin directory, which "
                         "makes the shell restart the whole bar:\n"
                         + "\n".join(written[:10]))

    def test_the_entry_point_disables_bytecode_writing(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "ap-ctl"), "r", encoding="utf-8") as fh:
            entry = fh.read()
        self.assertIn("sys.dont_write_bytecode = True", entry,
                      "ap-ctl must set dont_write_bytecode before importing "
                      "the package, or the first play writes .pyc files into "
                      "the plugin and the shell reloads")


class Sealed(unittest.TestCase):
    def test_sandbox_reports_sealed(self):
        cmd, fds = sandbox.sandbox_command("audio", with_script=True)
        import subprocess
        proc = subprocess.run(
            cmd + ["--", sandbox.SANDBOX_SCRIPT, "selftest", "--inside"],
            capture_output=True, pass_fds=fds, timeout=60)
        self.assertEqual(proc.returncode, 0,
                         "the player refused to start: %s"
                         % proc.stdout.decode()[:200])


if __name__ == "__main__":
    unittest.main(verbosity=2)
