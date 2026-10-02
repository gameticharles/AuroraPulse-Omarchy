"""The mpv JSON IPC client.

Deliberately hand-rolled rather than driven by a library: it is roughly a
hundred lines, and owning it means the sandbox only has to hand over one
inherited file descriptor instead of a socket path.
"""

import errno
import json
import os
import shutil
import socket
import subprocess
import threading
import time

from ..util.sandbox import (PCM_CHANNELS, PCM_FORMAT, PCM_RATE,
                          SANDBOX_RUNTIME)

SANDBOX_SOCKET = SANDBOX_RUNTIME + "/mpv.sock"

# What the daemon keeps a live copy of. Everything the panel shows is read
# from that copy, so drawing the state never costs a round trip to the player.
# demuxer-cache-state is deliberately absent: it changes many times a second
# and carries a large structure nobody reads.
OBSERVED = [
    "pause", "core-idle", "idle-active", "paused-for-cache", "volume",
    "mute", "metadata", "media-title", "audio-codec-name", "time-pos",
    "duration", "seekable", "speed",
]

MAX_IPC_LINE = 1 << 20


def _ipc_value(value):
    """One argument on its way to mpv's JSON control socket.

    The types matter and used to be thrown away. Every argument was stringified,
    so `set_property("pause", True)` went out as the string "True". mpv does
    not read that as a boolean: it answers "success" and leaves the property
    alone. That is why the play/pause button did nothing at all while looking
    like it had worked. Numbers happened to survive, because mpv will read
    "55" as 55, so volume and seeking quietly kept working and hid the bug.

    Booleans, numbers and strings can all be sent as themselves. Anything else
    is the caller's problem to stringify.
    """
    if isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


class Mpv:
    """A connection to an mpv instance that may not exist yet."""

    def __init__(self, socket_path, on_event=None):
        self.socket_path = socket_path
        self.on_event = on_event
        self.sock = None
        self._reader = None
        # One lock for the socket: writes must not interleave, and exactly one
        # thread may read. That is the whole reason get_property used to
        # return nothing - it and the reader thread were racing for the same
        # bytes, and the event reader usually won.
        self._lock = threading.Lock()
        self._request_id = 0
        self._observing = False
        # request_id -> [threading.Event, slot]. Filled by the single reader.
        self._pending = {}
        self._pending_lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def attach(self, timeout=8.0, abort=None):
        """Connect to the player's control socket, waiting for it to appear.

        The existence check used to short-circuit the whole wait, so asking
        for a 30 second attach and getting a 30 millisecond one was the
        difference between a working video channel and "the player did not
        come up" - the session is a separate process and its socket arrives
        whenever it arrives.
        """
        if self.sock is not None:
            return True
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(2.0)
                sock.connect(self.socket_path)
            except OSError as exc:
                try:
                    sock.close()
                except OSError:
                    pass
                # Anything but "not there yet" is a real failure; waiting
                # longer would just hide it.
                if exc.errno not in (errno.ENOENT, errno.ECONNREFUSED,
                                     errno.EAGAIN):
                    return False
                if time.monotonic() >= deadline:
                    return False
                # A newer request can make this attach pointless; waiting out
                # the full timeout for a player nobody wants any more is what
                # made a second click feel ignored.
                if abort is not None and abort():
                    return False
                time.sleep(0.05)
                continue
            self.sock = sock
            self._observing = False
            self._reader = threading.Thread(target=self._read_loop, daemon=True)
            self._reader.start()
            self.observe()
            return True
        return False

    def close(self):
        sock, self.sock = self.sock, None
        with self._pending_lock:
            pending, self._pending = self._pending, {}
        for slot in pending.values():
            slot["event"].set()
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    # -- writing -----------------------------------------------------------

    def command(self, *args):
        with self._lock:
            if self.sock is None:
                return False
            self._request_id += 1
            payload = {
                "command": [_ipc_value(a) for a in args],
                "request_id": self._request_id,
            }
            # A fire-and-forget command still gets a request id, so its reply
            # is matched to a pending slot and not mistaken for an event.
            try:
                self.sock.sendall(
                    (json.dumps(payload, separators=(",", ":")) + "\n").encode())
                return True
            except OSError:
                self.close()
                return False

    def set_property(self, name, value, confirm=False):
        """Write one property. `confirm` waits for mpv to acknowledge it.

        The fire-and-forget form is right for anything whose result arrives
        later as an event. It is wrong for a button press. The reply to the
        caller is built from what was asked for, so if the write has not
        landed yet the daemon reports the new state while the player is still
        holding the old one, and the very next read contradicts it.
        """
        if confirm:
            return self.command_wait("set_property", name, value)
        return self.command("set_property", name, value)

    def command_wait(self, *args, timeout=2.0):
        """Send a command and wait for mpv to confirm it.

        mpv acknowledges every command with a reply carrying the request id,
        so this is the one way to know a write has actually been applied rather
        than merely queued into a socket.
        """
        if self.sock is None:
            return False
        slot = {"event": threading.Event(), "data": None, "error": None}
        with self._lock:
            self._request_id += 1
            want = self._request_id
            with self._pending_lock:
                self._pending[want] = slot
            payload = {"command": [_ipc_value(a) for a in args],
                       "request_id": want}
            try:
                self.sock.sendall(
                    (json.dumps(payload, separators=(",", ":")) + "\n").encode())
            except OSError:
                self._forget(want)
                self.close()
                return False
        if not slot["event"].wait(timeout):
            self._forget(want)
            return False
        return slot["error"] is None

    def request(self, *args, timeout=2.0):
        """Send a command and return (ok, data) once mpv has answered it.

        Never call this from the event callback: the answer is delivered by
        the reader thread, which is the thread running that callback.
        """
        if self.sock is None:
            return False, None
        slot = {"event": threading.Event(), "data": None, "error": None}
        with self._lock:
            self._request_id += 1
            want = self._request_id
            with self._pending_lock:
                self._pending[want] = slot
            payload = {"command": [_ipc_value(a) for a in args],
                       "request_id": want}
            try:
                self.sock.sendall(
                    (json.dumps(payload, separators=(",", ":")) + "\n").encode())
            except OSError:
                self._forget(want)
                self.close()
                return False, None
        if not slot["event"].wait(timeout):
            self._forget(want)
            return False, None
        return slot["error"] is None, slot["data"]

    def seek(self, seconds):
        """Seek to an absolute position in seconds.

        This used to take either seconds or a fraction and guessed which by
        the size of the number, so a lyric line at 0.8 s became "80 percent
        of the way through". Fractions are the caller's job now.
        """
        try:
            return self.command("seek", max(0.0, float(seconds)), "absolute")
        except (TypeError, ValueError):
            return False

    def add_volume(self, delta):
        return self.command("add", "volume", float(delta))

    def get_property(self, name, timeout=2.0):
        """One-shot property read.

        The answer comes back through the reader thread, which is the only
        thing that touches the read side of the socket. We register a slot,
        send the request, and wait for the reader to fill it - so a response
        can never be consumed by the wrong caller.
        """
        if self.sock is None:
            return None
        slot = {"event": threading.Event(), "data": None, "error": None}
        with self._lock:
            self._request_id += 1
            want = self._request_id
            with self._pending_lock:
                self._pending[want] = slot
            payload = {"command": ["get_property", name], "request_id": want}
            try:
                self.sock.sendall(
                    (json.dumps(payload, separators=(",", ":")) + "\n").encode())
            except OSError:
                self._forget(want)
                self.close()
                return None
        if not slot["event"].wait(timeout):
            self._forget(want)
            return None
        return slot["data"]

    def _forget(self, request_id):
        with self._pending_lock:
            self._pending.pop(request_id, None)

    # -- observing ---------------------------------------------------------

    def observe(self):
        """Re-register the property observations.

        Property ids are per-connection, so a daemon that reattaches to a
        player another daemon started has to claim its own ids or it will
        silently receive nothing.
        """
        if self._observing or self.sock is None:
            return
        for index, name in enumerate(OBSERVED, start=1):
            if not self.command("observe_property", index, name):
                return
        self._observing = True

    def _dispatch(self, message):
        """Route one decoded message: a reply to its waiter, or an event."""
        if not isinstance(message, dict):
            return
        request_id = message.get("request_id")
        if request_id is not None:
            with self._pending_lock:
                slot = self._pending.pop(request_id, None)
            if slot is not None:
                if "error" in message and message["error"] not in ("success", None):
                    slot["error"] = message["error"]
                    slot["data"] = None
                else:
                    slot["data"] = message.get("data")
                slot["event"].set()
                return
            # A reply to something we are not waiting for is an acknowledgement
            # (an observe_property ack, say). It is not a state event and must
            # not be mistaken for one.
            return
        if self.on_event:
            try:
                self.on_event(message)
            except Exception:
                # A handler bug must not take the reader down and lose every
                # later event.
                pass

    def _read_loop(self):
        self._buffer = b""
        self.sock.settimeout(None)
        while self.sock is not None:
            try:
                chunk = self.sock.recv(65536)
            except OSError:
                chunk = b""
            if not chunk:
                break
            self._buffer += chunk
            # Guard the buffer itself: a peer that never sends a newline would
            # otherwise grow it without bound.
            if len(self._buffer) > MAX_IPC_LINE:
                self._buffer = b""
                continue
            while b"\n" in self._buffer:
                line, _, self._buffer = self._buffer.partition(b"\n")
                if len(line) > MAX_IPC_LINE:
                    continue
                try:
                    self._dispatch(json.loads(line.decode("utf-8", "replace")))
                except ValueError:
                    continue
        # Release anyone still waiting so a shutdown is not a two-second stall
        # per outstanding read.
        with self._pending_lock:
            pending, self._pending = self._pending, {}
        for slot in pending.values():
            slot["event"].set()
        if self.on_event:
            try:
                self.on_event({"event": "aurora-disconnected"})
            except Exception:
                pass


_option_cache = None


def supported_options():
    """The options this mpv build actually accepts, or None if unknown.

    mpv drops and renames options between releases, and an option it does not
    recognise is a *fatal* error: the player exits before playing anything and
    says nothing, because --really-quiet suppresses the diagnostic. Several
    options here were removed upstream and every one of them looked like a
    mysterious "the video player does not work". Filtering against the real
    binary means the list cannot rot silently again.
    """
    global _option_cache
    if _option_cache is not None:
        return _option_cache
    mpv = shutil.which("mpv")
    if not mpv:
        _option_cache = None
        return None
    try:
        proc = subprocess.run([mpv, "--list-options"], capture_output=True,
                              timeout=15, text=True)
    except (OSError, subprocess.SubprocessError):
        _option_cache = None
        return None
    names = set()
    for line in (proc.stdout or "").splitlines():
        # mpv indents the option name, so the first token is not the option.
        token = line.strip().split()[0] if line.strip().split() else ""
        if token.startswith("--") and len(token) > 2:
            names.add(token[2:].rstrip("="))
    _option_cache = names or None
    return _option_cache


def _drop_unsupported(args):
    """Remove options this mpv build does not know about."""
    supported = supported_options()
    if not supported:
        return args
    kept = []
    for arg in args:
        if arg.startswith("--"):
            name = arg[2:].split("=", 1)[0]
            # mpv lists `--config` but the flag we pass is `--no-config`; the
            # negation is how every boolean is switched off, so check both.
            positive = name[3:] if name.startswith("no-") else name
            if positive not in supported:
                continue
        kept.append(arg)
    return kept


VIDEO_APP_ID = "org.aurorapulse.video"


def mpv_arguments(volume=70, video=False, title="aurorapulse", extra=(),
                  buffer_seconds=20, user_agent="AuroraPulse/0.1 (Omarchy)",
                  hwdec="auto-safe"):
    """The mpv command line.

    Audio leaves as raw PCM on stdout and is played by pw-cat outside the
    sandbox, so the player needs no audio server, no D-Bus, and no writable
    path. Control arrives on an inherited descriptor rather than a socket
    path, so there is nothing for the sandbox to be tricked into opening.

    `media` is the thing to play, passed as a positional argument. Note that
    --start is *not* the way to do this: it takes a timestamp, so handing it
    a URL is a fatal option error and mpv exits before playing anything.
    """
    args = [
        "mpv",
        # --idle=yes keeps mpv alive and waiting for a file, but it also means
        # mpv does NOT autoload a positional file. The session therefore loads
        # the first track over IPC once mpv is up, and so does every track
        # after it. That is also what makes a gapless queue possible.
        "--no-config", "--idle=yes", "--no-terminal", "--really-quiet",
        "--no-input-terminal",
        # mpv creates and owns this socket itself, inside a 0700 directory
        # that the session shares with us by bind mount. It is the same inode
        # the daemon connects to, so there is no control-channel relay to get
        # wrong, and nothing else on the machine can reach it.
        "--input-ipc-server=%s" % SANDBOX_SOCKET,
        "--ytdl=no",                  # resolution is the daemon's job
        "--load-unsafe-playlists=no",  # an .m3u cannot spawn a process
        "--tls-verify=yes",
        "--network-timeout=12",
        "--demuxer-max-bytes=64MiB",
        "--cache=yes",
        # Both of these come from the daemon: an IPTV provider that serves
        # only known clients needs a browser string, and a poor link wants a
        # deeper buffer than the default.
        "--cache-secs=%d" % buffer_seconds,
        "--user-agent=%s" % user_agent,
    ]
    if video:
        args += [
            # No --force-window: it makes mpv abort (SIGABRT) when it is
            # idling with no video to show, which is exactly the state a
            # queue-driven player starts in. The window appears on its own
            # when a track with a video track is loaded.
            "--vo=gpu-next", "--gpu-context=wayland", "--gpu-api=opengl",
            "--hwdec=%s" % (hwdec if hwdec in ("auto-safe", "no") else "auto-safe"),
            "--opengl-swapinterval=1",
            "--keep-open=no", "--title=" + title,
            # Its own app id, not mpv's: Omarchy centres every "mpv" window
            # at a fixed size, and that rule would win over ours. With this
            # one the window opens where the daemon's rule puts it.
            "--wayland-app-id=" + VIDEO_APP_ID,
        ]

    # Audio is PCM on stdout in *both* profiles, played by pw-cat outside the
    # sandbox. Letting mpv pick its own output means it probes ALSA inside a
    # namespace that has no sound devices, fails to open one, and aborts the
    # whole file - so a video channel would not start at all.
    args += [
        "--ao=pcm", "--ao-pcm-file=/dev/stdout", "--ao-pcm-waveheader=no",
        "--audio-format=%s" % PCM_FORMAT,
        "--audio-samplerate=%d" % PCM_RATE,
        "--audio-channels=%d" % PCM_CHANNELS,
    ]
    if not video:
        args.append("--no-video")

    args += [
        "--volume=%d" % max(0, min(100, int(volume))),
        "--volume-max=100",
        # NOTE: no --reconnect here. Those are ffmpeg/libav options that are
        # only exposed by some mpv builds; passing one where it is absent is a
        # FATAL option error, so mpv exits before playing. mpv's own demuxer
        # retries a dropped live stream on its own. Use --stream-lavf-o to tune
        # reconnect behaviour on builds that support it.
    ]
    args.extend(extra)
    return _drop_unsupported(args)
