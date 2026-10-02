"""The player session.

A separate, short-lived-by-design process that owns one mpv instance. It
exists as its own process so the shell can restart without interrupting
playback, and so the daemon can be killed without killing the audio.

    daemon ──spawns──▶ player session
                         ├── GuardedProxy   (unix socket, public internet only)
                         ├── mpv.sock       (in a 0700 dir it shares with us)
                         ├── bwrap ──▶ mpv ──raw PCM──▶ pw-cat ──▶ PipeWire
                         └── bwrap ──▶ ffmpeg         (one logo at a time)
"""

import json
import os
import shutil
import signal
import socket
import socketserver
import stat
import subprocess
import threading
import time
import urllib.parse

from ..util import netguard, sandbox
from . import repair

SANDBOX_RUNTIME = sandbox.SANDBOX_RUNTIME
RECORD_MOUNT = "/run/aurora-rec"

# Must match daemon.TITLE_PREFIX: it is how the panel recognises its own
# video window among every other player on the desktop.
TITLE_PREFIX = "aurorapulse:"

SANDBOX_IPC_FD = sandbox.SANDBOX_IPC_FD
SANDBOX_PROXY = sandbox.SANDBOX_PROXY
SANDBOX_SCRIPT = sandbox.SANDBOX_SCRIPT

MAX_HEADER = 64 << 10
MAX_CLIENTS = 32

BRIDGE_ADDR = ("127.0.0.1", 0)


def _unlink_stale(path, what):
    """Remove a leftover socket, but refuse to remove a regular file.

    Something that is not a socket where we expect one is either a bug or an
    attempt to make us delete or write through the wrong path.
    """
    if not os.path.lexists(path):
        return
    try:
        mode = os.lstat(path).st_mode
    except OSError:
        return
    if not stat.S_ISSOCK(mode):
        raise OSError("%s exists and is not a socket" % what)
    os.unlink(path)


class GuardedProxy(socketserver.ThreadingUnixStreamServer):
    """An HTTP and CONNECT proxy that can only reach the public internet.

    mpv resolves names, follows redirects and expands playlists itself, so it
    talks to this instead of to the network. Every redirect, every HLS
    segment and every playlist entry comes back through here and is re-checked.
    """

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 8

    def __init__(self, path):
        _unlink_stale(path, "proxy socket")
        self._slots = threading.BoundedSemaphore(MAX_CLIENTS)
        super().__init__(path, _ProxyHandler)
        os.chmod(path, 0o600)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\n"
                                b"Content-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def shutdown_request(self, request):
        try:
            super().shutdown_request(request)
        finally:
            try:
                self._slots.release()
            except ValueError:
                pass

    def handle_error(self, request, client_address):
        # A client that hangs up mid-stream is normal, not an error worth a
        # traceback on stderr.
        pass


class _ProxyHandler(socketserver.StreamRequestHandler):
    timeout = 30

    def handle(self):
        try:
            self._handle()
        except netguard.Blocked as exc:
            self._reply(403, str(exc)[:200])
        except OSError as exc:
            self._reply(502, "upstream failed: %s" % exc)
        except Exception:
            self._reply(502, "upstream failed")

    def _handle(self):
        request_line = self.rfile.readline(MAX_HEADER + 1)
        if not request_line or len(request_line) > MAX_HEADER:
            self._reply(431, "request line too long")
            return
        try:
            method, target, _version = request_line.decode(
                "latin-1").split()
        except ValueError:
            self._reply(400, "malformed request")
            return

        headers = []
        total = 0
        while True:
            line = self.rfile.readline(MAX_HEADER + 1)
            if not line or line in (b"\r\n", b"\n"):
                break
            total += len(line)
            if total > MAX_HEADER:
                self._reply(431, "headers too large")
                return
            name, _, value = line.decode("latin-1").partition(":")
            # Hop-by-hop headers are dropped rather than forwarded.
            if name.strip().lower() not in (
                "connection", "proxy-connection", "keep-alive",
                "transfer-encoding", "upgrade", "te", "trailer",
            ):
                headers.append((name.strip(), value.strip()))

        if method == "CONNECT":
            self._connect(target)
        elif method in ("GET", "HEAD"):
            self._get(method, target, headers)
        else:
            self._reply(405, "only GET, HEAD and CONNECT")

    def _connect(self, target):
        host, _, raw_port = target.rpartition(":")
        if not host or "/" in target or "@" in target:
            self._reply(400, "malformed CONNECT target")
            return
        try:
            port = int(raw_port)
        except ValueError:
            self._reply(400, "malformed CONNECT port")
            return

        # The media port policy, for the same reason as _get: channels that
        # serve HTTPS from an odd port are just as common as ones on 443, and
        # this tunnel is the only path they can take.
        upstream = netguard.public_connection(host, port, netguard.MEDIA_PORTS,
                                              seconds=15)
        self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        self.wfile.flush()
        # From here the tunnel is raw bytes in both directions; the client
        # starts a TLS handshake immediately and every byte of it has to come
        # back the other way.
        self.connection.settimeout(None)
        _relay(self.connection, upstream)
        try:
            upstream.close()
        except OSError:
            pass

    def _get(self, method, target, headers):
        parsed = urllib.parse.urlsplit(target)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            self._reply(400, "only absolute http and https URLs are proxied")
            return
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        # Media ports, not web ports: too many channels stream from outside
        # 80/443 to make the narrow list workable for playback.
        allowed = netguard.MEDIA_PORTS
        upstream = netguard.public_connection(
            parsed.hostname, port, allowed, seconds=15)

        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        lines = ["%s %s HTTP/1.1" % (method, path)]
        sent_host = False
        for name, value in headers:
            if name.lower() == "host":
                sent_host = True
            lines.append("%s: %s" % (name, value))
        if not sent_host:
            lines.append("Host: %s" % parsed.netloc)
        lines.append("Connection: close")
        self._proxy = upstream
        upstream.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        self.connection.settimeout(None)
        _relay(upstream, self.connection)
        self._proxy = None

    _proxy = None

    def _reply(self, code, message):
        body = (message or "").encode("utf-8", "replace")[:200]
        try:
            self.wfile.write(
                ("HTTP/1.1 %d %s\r\nContent-Type: text/plain\r\n"
                 "Content-Length: %d\r\nConnection: close\r\n\r\n"
                 % (code, message.split(" ")[0][:32], len(body))).encode("latin-1")
                + body)
            self.wfile.flush()
        except OSError:
            pass


def _relay(a, b):
    """Copy bytes between two sockets until either side closes.

    A proxy is only a proxy if it goes both ways. A one-directional copy
    answers the CONNECT handshake and then silently drops the TLS bytes, which
    looks exactly like a network that has gone away.
    """
    up = threading.Thread(target=_pump, args=(a, b, False), daemon=True)
    up.start()
    _pump(b, a, False)
    for sock in (a, b):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def _pump(source, sink, teardown=True):
    """Copy bytes from source to sink until one side closes.

    `teardown` is False when two pumps run concurrently as one relay: each
    direction then has to leave the other side's socket open, or the second
    direction dies the moment the first one finishes.
    """
    try:
        while True:
            chunk = source.recv(65536)
            if not chunk:
                break
            sink.sendall(chunk)
    except OSError:
        pass
    finally:
        if teardown:
            for sock in (source, sink):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


class _Bridge(socketserver.ThreadingTCPServer):
    """Inside the sandbox: 127.0.0.1:<port> ⇄ the proxy's unix socket.

    The sandbox has an empty network namespace, so this loopback listener is
    unreachable from anywhere. It exists only so mpv can be handed an
    http_proxy URL, which it needs in order to make any request at all.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, proxy_path, slots):
        self._proxy_path = proxy_path
        self._slots = slots
        super().__init__(BRIDGE_ADDR, _BridgeHandler)

    def handle_error(self, request, client_address):
        pass


class _BridgeHandler(socketserver.BaseRequestHandler):
    """Relay one connection in both directions.

    The old version only pushed the client's bytes upstream, so every request
    came back as an empty response and mpv saw a dead proxy. A relay has to be
    full duplex or it is not a relay.
    """

    def handle(self):
        if not self.server._slots.acquire(blocking=False):
            return
        try:
            upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            upstream.settimeout(10)
            upstream.connect(self.server._proxy_path)
        except OSError:
            self.server._slots.release()
            return
        try:
            left = threading.Thread(
                target=_pump, args=(self.request, upstream, False), daemon=True)
            left.start()
            # This thread carries the response back; when it ends, tear the
            # other direction down too.
            _pump(upstream, self.request)
        finally:
            try:
                self.request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                upstream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.server._slots.release()
            try:
                upstream.close()
            except OSError:
                pass


def run_sandbox(proxy_path, argv, profile="audio"):
    """Exec'd *inside* bubblewrap. Verifies its own isolation, then runs."""
    if not sandbox.sandbox_is_sealed():
        sys_stderr("sandbox isolation check failed; refusing to start the player")
        return 4

    extra_env = [("MALLOC_ARENA_MAX", "2")]
    argv = list(argv)

    if proxy_path:
        # mpv insists on a URL-shaped proxy, but a unix socket is not a URL, so
        # the sandbox gets a loopback listener that forwards to the guarded
        # unix socket. Both ends are inside the session's own network
        # namespace, so the listener is unreachable from anywhere else.
        slots = threading.BoundedSemaphore(MAX_CLIENTS)
        bridge = _Bridge(proxy_path, slots)
        threading.Thread(target=bridge.serve_forever, daemon=True).start()
        url = "http://127.0.0.1:%d" % bridge.server_address[1]
        extra_env += [
            ("http_proxy", url), ("https_proxy", url),
            ("HTTP_PROXY", url), ("HTTPS_PROXY", url),
            ("no_proxy", ""), ("NO_PROXY", ""),
        ]
        argv.append("--http-proxy=" + url)

    # Start from the environment bubblewrap handed us and add to it. Passing
    # a hand-built dict here *replaces* the environment, which silently drops
    # everything bwrap set - including WAYLAND_DISPLAY - so a video player
    # could never reach the compositor and exited with no message at all.
    child_env = dict(os.environ)
    child_env.update(dict(extra_env))
    # Only strip the proxy variables when there is no guard to use. Leaving
    # them pointing at a bridge that does not exist would send the player
    # somewhere useless, and *keeping* a stale one from the host environment
    # would bypass the guard entirely - so both directions matter.
    if not proxy_path:
        for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            child_env.pop(name, None)
    child_env.setdefault("HOME", SANDBOX_RUNTIME)

    try:
        return subprocess.run(
            argv,
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=None,
            stderr=None,
            preexec_fn=sandbox.rlimits(profile),
        ).returncode
    except OSError as exc:
        sys_stderr("could not run %s: %s" % (argv[0], exc))
        return 5


def sys_stderr(message):
    try:
        import sys
        print("ap-ctl: " + message, file=sys.stderr, flush=True)
    except Exception:
        pass


def _load_command(media, volume):
    """The IPC commands that start one candidate and set the initial state."""
    # force-media covers a local file bound in without an extension, and a
    # stream URL that has none either.
    commands = [["loadfile", media, "replace"]]
    if os.environ.get("AP_START_MUTED") == "1":
        commands.append(["set_property", "mute", True])
    else:
        commands.append(["set_property", "volume", int(volume)])
    return commands


def _wait_for_socket(socket_path, timeout):
    """Block until the player's control socket answers, or give up."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(2)
            sock.connect(socket_path)
            return sock
        except OSError:
            time.sleep(0.05)
    return None


def _load_first(socket_path, candidates, volume, timeout=8.0):
    """Wait for the control socket, then load the first candidate into it.

    Loading over IPC rather than on the command line is what makes the queue
    work: the next track is then the identical `loadfile ... replace`, so
    switching stations or skipping costs no process restart and no gap.
    """
    sock = _wait_for_socket(socket_path, timeout)
    if sock is None:
        sys_stderr("the player never opened its control socket")
        return False
    try:
        for command in _load_command(candidates[0], volume):
            sock.sendall((json.dumps({"command": command}) + "\n").encode())
            time.sleep(0.05)
    except OSError as exc:
        sys_stderr("could not load the first track: %s" % exc)
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return True


def _watch_candidates(socket_path, candidates, volume, settle=8.0,
                      give_up=90.0):
    """Keep loading the next candidate until one of them actually plays.

    A station that is listed but dead does not announce itself: mpv accepts
    the URL, reports no error for a while, and then quietly goes idle with
    silence on the pipe. That reads to a user as "the app is broken", when
    the honest description is "this particular link is dead and there is
    another one on file".

    So the rule here is deliberately pessimistic: a candidate only counts as
    working once mpv says `file-loaded`, and anything that has not done that
    within `settle` seconds is treated as a failure and replaced by the next
    candidate. `file-loaded` is the right signal because it is the first
    moment the demuxer has accepted the stream.
    """
    if len(candidates) < 2:
        return
    started = time.monotonic()
    index = 1
    playing = False
    last_change = started

    while time.monotonic() - started < give_up and not playing:
        time.sleep(0.25)
        if time.monotonic() - last_change < settle:
            continue
        sock = _wait_for_socket(socket_path, 2.0)
        if sock is None:
            last_change = time.monotonic()
            continue
        try:
            sock.sendall((json.dumps({
                "command": ["get_property", "idle"],
                "request_id": 9001}) + "\n").encode())
            data = sock.recv(4096).decode("utf-8", "replace")
        except OSError:
            last_change = time.monotonic()
            continue
        finally:
            try:
                sock.close()
            except OSError:
                pass
        idle = True
        for line in data.splitlines():
            try:
                reply = json.loads(line)
            except ValueError:
                continue
            if reply.get("request_id") == 9001 and "data" in reply:
                idle = bool(reply["data"])
        if not idle:
            # Something is playing: either the current candidate, or a station
            # that finally took. Either way there is nothing left to do.
            playing = True
            break
        if index >= len(candidates):
            break
        sys_stderr("that link did not play, trying the next one")
        sock = _wait_for_socket(socket_path, 2.0)
        if sock is None:
            break
        try:
            for command in _load_command(candidates[index], volume):
                sock.sendall((json.dumps({"command": command}) + "\n").encode())
                time.sleep(0.05)
        except OSError:
            break
        finally:
            try:
                sock.close()
            except OSError:
                pass
        index += 1
        last_change = time.monotonic()


def run_session(runtime_dir, volume=70, video=False, title="aurorapulse",
                media=None, extra=()):
    """The player session entry point. Runs until mpv exits or we are killed."""
    os.umask(0o077)

    def on_term(_signum, _frame):
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    socket_path = os.path.join(runtime_dir, "mpv.sock")
    proxy_path = os.path.join(runtime_dir, "proxy.sock")
    log_path = os.path.join(runtime_dir, "player.log")
    session_path = os.path.join(runtime_dir, "session.log")

    profile = "video" if video else "audio"
    from .player import mpv_arguments
    extra = []
    try:
        start = float(os.environ.get("AP_START_SECONDS") or 0)
    except ValueError:
        start = 0.0
    if start > 5:
        # --start here really is a timestamp, which is the one thing it is for.
        extra.append("--start=%0.3f" % start)

    # A local file has to be handed in: the sandbox has no /home, so the only
    # way mpv can read it is a read-only bind at a path we chose. This has to
    # happen *before* the command line is built, or mpv is told to open a host
    # path that does not exist inside.
    file_binds = []
    if media and not media.startswith(("http://", "https://")):
        try:
            real = os.path.realpath(media)
            if not os.path.isfile(real):
                sys_stderr("that file is not there: %s" % media)
                return 6
            file_binds.append(("--ro-bind", real, "/media"))
            media = "/media"
        except OSError as exc:
            sys_stderr("could not bind the media file: %s" % exc)
            return 6

    # A station is a name, not a URL: the mirror lists one link per station and
    # any of them can be the dead one, so the daemon hands over the alternates
    # it knows about and this tries them in turn. Repairs are folded in here so
    # every layer above can pass raw URLs and still get a real attempt. This
    # is built after the local-file rebind, so a bound file is named the way
    # mpv will see it inside the sandbox.
    candidates = []
    if media:
        alternates = []
        if media.startswith(("http://", "https://")):
            alternates = [u for u in (os.environ.get("AP_ALT_URLS") or "").split("\n")
                          if u.strip()]
        candidates = repair.candidates(media, alternates)

    # mpv owns its own control socket. We bind the runtime directory into the
    # sandbox and tell mpv to listen inside it, so the socket the daemon talks
    # to and the socket mpv listens on are the *same inode*. That is simpler and
    # safer than proxying a control channel: there is no relay to get wrong,
    # and the socket is only reachable by us because the directory is 0700.
    _unlink_stale(socket_path, "mpv control socket")
    try:
        buffer_seconds = int(os.environ.get("AP_BUFFER_SEC") or 20)
    except ValueError:
        buffer_seconds = 20
    # The window title is how the panel finds its own video window among every
    # other player on the desktop, so the prefix is applied unconditionally.
    # A conditional "unless it already has a colon" looked reasonable and was
    # exactly wrong: a media uid is *always* full of colons, so the marker was
    # never added and the window was never found.
    if video:
        title = TITLE_PREFIX + str(title or "")
    argv = mpv_arguments(volume=volume, video=video, title=title, extra=extra,
                         buffer_seconds=buffer_seconds,
                         user_agent=os.environ.get("AP_USER_AGENT")
                         or "AuroraPulse/0.1 (Omarchy)",
                         hwdec=os.environ.get("AP_HWDEC") or "auto-safe")

    # Audio streams straight from the net, video pulls HLS segments; both are
    # long-lived and neither is something a URL can stand in for, so both go
    # through the guard.
    proxy = None
    try:
        proxy = GuardedProxy(proxy_path)
    except OSError as exc:
        sys_stderr("proxy socket: %s" % exc)
        return 3
    threading.Thread(target=proxy.serve_forever, daemon=True).start()

    extra_binds = [("--bind", runtime_dir, SANDBOX_RUNTIME)]
    # Recordings are written to disk, not to the runtime folder: that one is
    # memory, and an evening of radio would have been held in RAM.
    record_dir = os.environ.get("AP_RECORD_DIR") or ""
    if record_dir:
        try:
            os.makedirs(record_dir, mode=0o700, exist_ok=True)
            extra_binds.append(("--bind", record_dir, RECORD_MOUNT))
        except OSError:
            pass
    extra_binds.extend(file_binds)
    if proxy:
        extra_binds.append(("--ro-bind", proxy_path, SANDBOX_PROXY))

    try:
        cmd, fds = sandbox.sandbox_command(
            profile, extra_binds=extra_binds,
            extra_env=[("AP_PROXY_PATH", SANDBOX_PROXY if proxy else ""),
                       ("AP_PROFILE", profile)],
            with_script=True)
    except sandbox.SandboxUnavailable as exc:
        sys_stderr(str(exc))
        if proxy:
            proxy.shutdown()
        return 4

    cmd += ["--", SANDBOX_SCRIPT, "sandbox", "--"] + argv

    # pw-cat lives outside the sandbox: the player produces raw samples and
    # has no audio server, no session socket and no writable path at all.
    pw_cat = shutil.which("pw-cat")
    player = None
    audio = None
    try:
        log_fd = os.open(session_path,
                         os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                         0o600)
        try:
            player = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=log_fd, pass_fds=fds)
        finally:
            os.close(log_fd)

        if pw_cat:
            audio = subprocess.Popen(
                [pw_cat, "--playback", "--raw",
                 "--rate", str(sandbox.PCM_RATE),
                 "--channels", str(sandbox.PCM_CHANNELS),
                 "--format", sandbox.PCM_FORMAT,
                 "--media-role", "Music",
                 # The output chosen in "Play on"; PipeWire falls back to the
                 # default one when it is not there.
                 *(["--target", os.environ["AP_AUDIO_TARGET"]]
                   if os.environ.get("AP_AUDIO_TARGET") else []),
                 # The runtime folder tags the stream as this daemon's own, so
                 # "Play on" moves only it (see core/outputs.py).
                 "-P", '{ application.name = "AuroraPulse" '
                        'node.description = "AuroraPulse" '
                        'aurorapulse.runtime = "%s" }' % runtime_dir.replace('"', ""), "-"],
                stdin=player.stdout, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            player.stdout.close()
        elif player.stdout is not None:
            player.stdout.close()

        if not pw_cat:
            sys_stderr("pw-cat is required for audio playback")

        # mpv idles waiting for a file rather than autoloading one, so the
        # first track is loaded over the control socket as soon as it answers.
        if media:
            # A video player can take noticeably longer to open its socket
            # while the GPU context and the first manifest are negotiated;
            # give it the same patience the daemon's attach does.
            opened = _load_first(socket_path, candidates, volume,
                                 timeout=20.0 if video else 8.0)
            if opened and len(candidates) > 1:
                # A dead link is silent, so something has to be watching for
                # it. This is the only place that can see mpv's own verdict.
                threading.Thread(
                    target=_watch_candidates,
                    args=(socket_path, candidates, volume),
                    daemon=True).start()

        return player.wait()
    except Exception as exc:
        sys_stderr("player session failed: %s" % exc)
        return 1
    finally:
        for proc in (audio, player):
            if proc is None:
                continue
            try:
                proc.terminate()
            except OSError:
                pass
        for proc in (audio, player):
            if proc is None:
                continue
            try:
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except OSError:
                    pass
        if proxy:
            try:
                proxy.shutdown()
            except Exception:
                pass
        for path in (proxy_path, socket_path):
            try:
                os.unlink(path)
            except OSError:
                pass


def stop_session(runtime_dir):
    """Ask a running player to quit; escalate if it will not."""
    from ..util import netguard as _ng  # noqa: F401  (kept for symmetry)
    import time
    pid_file = os.path.join(runtime_dir, "player.pid")
    try:
        with open(pid_file, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        pid = int(data.get("pid"))
        start = str(data.get("start") or "")
    except (OSError, ValueError, TypeError):
        pid, start = None, ""

    from .state import process_alive
    if pid and process_alive(pid, start):
        for sig, wait in ((signal.SIGTERM, 2.0), (signal.SIGKILL, 1.0)):
            try:
                os.killpg(os.getpgid(pid), sig)
            except OSError:
                try:
                    os.kill(pid, sig)
                except OSError:
                    return
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                if not process_alive(pid, start):
                    return
                time.sleep(0.05)
    try:
        os.unlink(pid_file)
    except OSError:
        pass
