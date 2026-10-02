"""MPRIS: the desktop's standard way to see and control a media player.

Publishing org.mpris.MediaPlayer2.aurorapulse on the session bus is what makes
the media keys, `playerctl`, the lock screen and the shell's own media widget
work with AuroraPulse, without any of them knowing it exists.

Optional by design: it needs PyGObject (GLib/Gio). Without it the daemon runs
exactly as before and only the desktop integration is missing.
"""

import threading
import time

try:  # pragma: no cover - depends on the system
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib
except Exception:  # noqa: BLE001 - any failure means "no MPRIS"
    Gio = GLib = None

BUS_NAME = "org.mpris.MediaPlayer2.aurorapulse"
PATH = "/org/mpris/MediaPlayer2"
ROOT_IFACE = "org.mpris.MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"

XML = """
<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek"><arg direction="in" name="Offset" type="x"/></method>
    <method name="SetPosition">
      <arg direction="in" name="TrackId" type="o"/>
      <arg direction="in" name="Position" type="x"/>
    </method>
    <method name="OpenUri"><arg direction="in" name="Uri" type="s"/></method>
    <signal name="Seeked"><arg name="Position" type="x"/></signal>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="LoopStatus" type="s" access="readwrite"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Shuffle" type="b" access="readwrite"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>
"""

LOOP = {"off": "None", "one": "Track", "all": "Playlist"}
LOOP_BACK = {v: k for k, v in LOOP.items()}


def available():
    return Gio is not None


def _trackid(uid):
    """A D-Bus object path for a uid: only [A-Za-z0-9_] are allowed."""
    safe = "".join(c if c.isalnum() else "_" for c in str(uid or "none"))[:120]
    return "/org/aurorapulse/track/" + (safe or "none")


class Mpris:
    """Publishes the daemon's state and forwards control back into it."""

    def __init__(self, handle, log=None):
        self._handle = handle                # daemon.handle(message)
        self._log = log or (lambda message: None)
        self._state = {}
        self._stamp = time.monotonic()
        self._conn = None
        self._loop = None
        self._lock = threading.Lock()
        self._published = {}

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        if Gio is None:
            return False
        threading.Thread(target=self._run, name="ap-mpris", daemon=True).start()
        return True

    def _run(self):
        try:
            self._loop = GLib.MainLoop()
            self._node = Gio.DBusNodeInfo.new_for_xml(XML)
            # Replace an older daemon's name if one is still winding down: a
            # shell reload briefly has two daemons, and the new one is the one
            # that will stay.
            Gio.bus_own_name(
                Gio.BusType.SESSION, BUS_NAME,
                Gio.BusNameOwnerFlags.ALLOW_REPLACEMENT
                | Gio.BusNameOwnerFlags.REPLACE,
                self._acquired, None, None)
            self._loop.run()
        except Exception as exc:  # noqa: BLE001
            self._log("mpris unavailable: %s" % exc)

    def _acquired(self, connection, _name):
        self._conn = connection
        for iface in self._node.interfaces:
            connection.register_object(PATH, iface, self._call, self._get, self._set)

    # -- state from the daemon ---------------------------------------------

    def update(self, state):
        """Called with every state the daemon emits (any thread)."""
        with self._lock:
            self._state = dict(state)
            self._stamp = time.monotonic()
        if self._conn is not None:
            GLib.idle_add(self._publish)

    def _publish(self):
        changed = {}
        for name in ("PlaybackStatus", "Metadata", "Volume", "LoopStatus",
                     "Shuffle", "CanGoNext", "CanGoPrevious", "CanPlay",
                     "CanPause", "CanSeek", "Rate"):
            value = self._value(name)
            if value is None:
                continue
            key = value.print_(False)
            if self._published.get(name) != key:
                self._published[name] = key
                changed[name] = value
        if changed and self._conn is not None:
            try:
                self._conn.emit_signal(
                    None, PATH, "org.freedesktop.DBus.Properties",
                    "PropertiesChanged",
                    GLib.Variant("(sa{sv}as)", (PLAYER_IFACE, changed, [])))
            except Exception as exc:  # noqa: BLE001
                self._log("mpris signal failed: %s" % exc)
        return False

    def _position_us(self):
        with self._lock:
            state = dict(self._state)
            stamp = self._stamp
        position = float(state.get("position") or 0)
        if state.get("mode") == "playing" and not state.get("live"):
            position += (time.monotonic() - stamp) * float(state.get("speed") or 1)
        duration = float(state.get("duration") or 0)
        if duration:
            position = min(position, duration)
        return int(position * 1_000_000)

    def _value(self, name):
        with self._lock:
            state = dict(self._state)
        item = state.get("item") or {}
        has_item = bool(item.get("uid"))
        mode = state.get("mode") or "off"
        if name == "PlaybackStatus":
            status = "Stopped" if not has_item else (
                "Paused" if mode in ("paused", "error") else "Playing")
            return GLib.Variant("s", status)
        if name == "Metadata":
            meta = {"mpris:trackid": GLib.Variant("o", _trackid(item.get("uid")))}
            if has_item:
                title = item.get("title") or ""
                artist = item.get("artist") or ""
                stream = state.get("streamTitle") or ""
                if state.get("live") and stream:
                    # Radio: what the station says is on now, under its name.
                    meta["xesam:title"] = GLib.Variant("s", stream)
                    meta["xesam:album"] = GLib.Variant("s", title)
                    meta["xesam:artist"] = GLib.Variant("as", [title])
                else:
                    meta["xesam:title"] = GLib.Variant("s", title)
                    if artist:
                        meta["xesam:artist"] = GLib.Variant("as", [artist])
                    if item.get("album"):
                        meta["xesam:album"] = GLib.Variant("s", item.get("album"))
                art = (item.get("art") or {}).get("path") or ""
                if art:
                    meta["mpris:artUrl"] = GLib.Variant("s", "file://" + art)
                duration = float(state.get("duration") or 0)
                if duration and not state.get("live"):
                    meta["mpris:length"] = GLib.Variant("x", int(duration * 1_000_000))
                if item.get("url", "").startswith("http"):
                    meta["xesam:url"] = GLib.Variant("s", item.get("url"))
            return GLib.Variant("a{sv}", meta)
        if name == "Volume":
            volume = 0 if state.get("muted") else float(state.get("volume") or 0)
            return GLib.Variant("d", volume / 100.0)
        if name == "Position":
            return GLib.Variant("x", self._position_us())
        if name == "LoopStatus":
            return GLib.Variant("s", LOOP.get(state.get("repeat"), "None"))
        if name == "Shuffle":
            return GLib.Variant("b", bool(state.get("shuffle")))
        if name == "Rate":
            return GLib.Variant("d", float(state.get("speed") or 1.0))
        if name in ("MinimumRate",):
            return GLib.Variant("d", 0.5)
        if name in ("MaximumRate",):
            return GLib.Variant("d", 2.0)
        if name == "CanGoNext":
            return GLib.Variant("b", bool(state.get("hasNext")))
        if name == "CanGoPrevious":
            return GLib.Variant("b", bool(state.get("hasPrev")))
        if name in ("CanPlay", "CanPause"):
            return GLib.Variant("b", has_item or int(state.get("queueLength") or 0) > 0)
        if name == "CanSeek":
            return GLib.Variant("b", has_item and not state.get("live")
                                and float(state.get("duration") or 0) > 0)
        if name == "CanControl":
            return GLib.Variant("b", True)
        if name in ("CanQuit", "HasTrackList"):
            return GLib.Variant("b", name == "CanQuit")
        if name == "CanRaise":
            return GLib.Variant("b", True)
        if name == "Identity":
            return GLib.Variant("s", "AuroraPulse")
        if name == "DesktopEntry":
            return GLib.Variant("s", "aurorapulse")
        if name == "SupportedUriSchemes":
            return GLib.Variant("as", ["http", "https"])
        if name == "SupportedMimeTypes":
            return GLib.Variant("as", ["audio/mpeg", "audio/aac", "audio/ogg",
                                       "application/x-mpegurl", "video/mp4"])
        return None

    # -- D-Bus callbacks (GLib thread) -------------------------------------

    def _get(self, _conn, _sender, _path, _iface, name):
        return self._value(name)

    def _set(self, _conn, _sender, _path, _iface, name, value):
        value = value.unpack()
        if name == "Volume":
            self._handle({"cmd": "volume", "set": int(max(0.0, min(1.0, value)) * 100)})
        elif name == "LoopStatus":
            self._handle({"cmd": "repeat", "set": LOOP_BACK.get(value, "off")})
        elif name == "Shuffle":
            self._handle({"cmd": "shuffle", "on": bool(value)})
        elif name == "Rate":
            self._handle({"cmd": "rate", "value": float(value)})
        return True

    def _call(self, _conn, _sender, _path, _iface, method, params, invocation):
        args = params.unpack() if params is not None else ()
        with self._lock:
            state = dict(self._state)
        playing = state.get("mode") in ("playing", "loading")
        if method == "PlayPause":
            self._handle({"cmd": "toggle"})
        elif method == "Play":
            if not playing:
                self._handle({"cmd": "toggle"})
        elif method == "Pause":
            if playing:
                self._handle({"cmd": "pause"})
        elif method == "Stop":
            self._handle({"cmd": "stop"})
        elif method == "Next":
            self._handle({"cmd": "next"})
        elif method == "Previous":
            self._handle({"cmd": "previous"})
        elif method == "Seek" and args:
            self._handle({"cmd": "seek", "by": args[0] / 1_000_000})
        elif method == "SetPosition" and len(args) == 2:
            self._handle({"cmd": "seek", "seconds": max(0, args[1]) / 1_000_000})
        elif method == "OpenUri" and args:
            uri = str(args[0])
            if uri.startswith(("http://", "https://")):
                self._handle({"cmd": "play", "item": {
                    "uid": "radio:uri-%d" % (abs(hash(uri)) % 10**9),
                    "source": "radio", "kind": "station", "title": uri,
                    "url": uri, "is_live": True}})
        elif method == "Raise":
            self._handle({"cmd": "raise"})
        elif method == "Quit":
            self._handle({"cmd": "stop"})
        invocation.return_value(None)
