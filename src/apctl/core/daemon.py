"""The daemon.

Owns the queue, the settings, the player session, and every source. The
Omarchy service process holds nothing but this: it spawns us, talks JSON
lines, and can be restarted at any time without interrupting audio, because
the player lives in a separate process it does not own.

Concurrency model: one reader thread and three lanes behind it.

* control  - one thread, in order: pause, volume, seek, mute, state. None of
             these wait on the network, so they answer in milliseconds.
* playback - one thread, newest wins: play, next, previous, stop. A request
             that a later one has made pointless is abandoned, not finished.
* a pool   - browsing, searching, lyrics, the guide, scans.

A stop is also acted on directly by the reader thread, so silence never waits
behind anything. State is reported from a cache the player keeps up to date by
pushing property changes; nothing on the state path talks to the player.
"""

import functools
import itertools
import os
import queue
import random
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import protocol, repair, resolver
from .artwork import Artwork
from .custom import Custom, downloads_dir, export_rows, parse_import, read_source
from .db import Catalogue
from .player import Mpv, VIDEO_APP_ID
from .sync import Syncer
from .protocol import CAPABILITIES, PROTOCOL_VERSION
from .state import Store, process_alive, process_start
from ..sources import base, local, music, podcast, radio, tv, youtube
from ..sources.base import SourceError
from ..util import netguard, sandbox, taxonomy, textutil

# Every video window we create is titled with this prefix, which is how the
# panel finds its own window without guessing.
TITLE_PREFIX = "aurorapulse:"


def window_title(uid):
    return TITLE_PREFIX + str(uid or "")[:120]


def startup_sync(missing, offline=False):
    """What a starting daemon should do about the mirror, or `None` for nothing.

    Two rules, both learned the hard way.

    Only sync when something is actually missing. Re-mirroring on every launch
    made reopening the panel a full download - slow, and rude to a shared public
    API - and from then on the tab reads what is on disk and refreshes when the
    user asks it to.

    And when something *is* missing, sync incrementally. Freshness is decided
    per source inside the sync, so a catalogue that is already mirrored costs a
    change set rather than another full pass over 59,785 stations. Asking for
    `full` here is what made an interrupted first run unrecoverable: a run that
    was stopped after radio landed left television missing, and the next start
    re-downloaded radio from scratch before it would touch television - ninety
    seconds of download that was then thrown away by the next reload, so the
    missing catalogue never arrived at all.
    """
    if offline or not missing:
        return None
    return {"full": False, "source": None}


SOURCE_MODULES = {
    "radio": radio,
    "tv": tv,
    "youtube": youtube,
    "music": music,
    "local": local,
    "podcast": podcast,
}

# Browsing, lyrics, the guide and scans. Kept apart from playback and from the
# transport controls, so a slow YouTube search can never sit in front of a
# pause, and ten state requests a second can never sit in front of a browse.
POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ap-work")
CROSSFADE_DEFAULT = 0.0

# Commands that touch the player but never wait on the network. They run in
# order on one thread, so volume steps cannot arrive out of order.
CONTROL_COMMANDS = frozenset((
    "ping", "caps", "state", "settings_get", "settings_set", "toggle",
    "play_pause", "pause", "resume", "seek", "position", "volume", "mute",
    "rate", "repeat", "shuffle", "enqueue", "remove", "move", "clear",
    "video_window", "pip", "budget", "favourite", "report_play", "equalizer",
    "favorite", "saved", "forget", "raise",
    # Order matters for these - "add to playlist" then "move it up", "set the
    # alarm" then "try it" - and the pool would run them side by side.
    "playlist", "alarm", "subtitle", "quality",
))

# Commands that start, change or end playback. One thread, newest wins: a
# request records a generation number, and work that a later request has made
# pointless is abandoned at the next checkpoint instead of being finished.
PLAYBACK_COMMANDS = frozenset((
    "play", "next", "previous", "stop", "restart_player", "shutdown",
    "auto_next", "retry", "jump",
))

# Equalizer presets, as gains in dB at 60 Hz, 230 Hz, 910 Hz, 3.6 kHz, 14 kHz.
EQ_BANDS = (60, 230, 910, 3600, 14000)
EQ_PRESETS = {
    "Flat": (0, 0, 0, 0, 0),
    "Bass Boost": (6, 4, 0, 0, 0),
    "Vocal Boost": (-2, 0, 4, 3, 0),
    "Classical": (3, 2, -1, 2, 3),
    "Electronic": (5, 3, 0, 2, 4),
    "Rock": (4, 2, -2, 2, 4),
    "Pop": (-1, 2, 4, 2, -1),
    "Spoken Word": (-3, -1, 3, 4, 1),
}


def eq_gains(preset, custom=""):
    """The five band gains for a preset; "Custom" reads them from a setting
    like "3,1,0,-2,4" (dB, clamped to +-12)."""
    if preset == "Custom":
        gains = []
        for part in str(custom or "").split(",")[:len(EQ_BANDS)]:
            try:
                gains.append(max(-12, min(12, int(round(float(part))))))
            except ValueError:
                gains.append(0)
        return tuple(gains + [0] * (len(EQ_BANDS) - len(gains)))
    return EQ_PRESETS.get(preset, EQ_PRESETS["Flat"])


# Silence longer than this is cut out of tracks and episodes (never live
# streams: there it would starve the buffer and stutter).
SKIP_SILENCE = ("silenceremove=start_periods=1:start_threshold=-50dB:"
                "stop_periods=-1:stop_duration=1.5:stop_threshold=-50dB")


def audio_filter_chain(eq_on, preset, normalize, custom="", skip_silence=False):
    """The mpv `af` value for the equalizer and loudness settings, or ""."""
    parts = []
    if skip_silence:
        parts.append(SKIP_SILENCE)
    if eq_on:
        for freq, gain in zip(EQ_BANDS, eq_gains(preset, custom)):
            if gain:
                parts.append("equalizer=f=%d:t=o:w=2:g=%d" % (freq, gain))
    if normalize:
        # dynaudnorm, not loudnorm: it works on a live stream without the
        # look-ahead delay, and evens out a quiet station next to a loud one.
        parts.append("dynaudnorm=f=250:g=15:p=0.9")
    return "lavfi=[%s]" % ",".join(parts) if parts else ""


# A gain stage of its own, ahead of everything else, that fades move. It is
# separate from mpv's volume, so a fade never touches the volume the user set
# and the slider does not twitch while a track fades out.
FADE_LABEL = "apfade"


def fade_filter(level):
    return "@%s:lavfi=[volume=volume=%.3f]" % (FADE_LABEL, max(0.0, min(1.0, level)))


# How long a stream may take to produce sound before the next link is tried.
START_TIMEOUT_AUDIO = 15.0
START_TIMEOUT_VIDEO = 25.0

# The queue is the list the user picked from, so it can be long; it is capped
# so a "play all" on a 2,000-track library does not become a 2 MB event.
MAX_QUEUE = 500


class _Lane:
    """One worker thread that runs jobs strictly in arrival order."""

    def __init__(self, name):
        self._jobs = queue.Queue()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def submit(self, job):
        self._jobs.put(job)

    def _run(self):
        while True:
            job = self._jobs.get()
            if job is None:
                return
            try:
                job()
            except SystemExit:
                raise
            except Exception:
                # The job wrapper reports its own errors; this only keeps a
                # bug from killing the lane and every command after it.
                pass


class Daemon:
    def __init__(self, store=None, emit=None):
        self.store = store or Store(
            os.environ.get("AP_DATA_DIR",
                           os.path.expanduser("~/.local/share/aurora-pulse")),
            os.environ.get("AP_CACHE_DIR",
                           os.path.expanduser("~/.cache/aurora-pulse")),
            os.environ.get("AP_RUNTIME_DIR") or os.path.join(
                os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "aurora-pulse"),
        )
        self.emit = emit or protocol.emit
        self.queue = []
        self.index = 0
        self.repeat = "off"          # off | one | all
        self.shuffle = False
        self.history = []
        # The local mirror. Opened lazily and shared with the sources, so a
        # browse is answered from disk rather than from a network round trip.
        self.catalogue = Catalogue(self.store.catalogue_path, log=self._log)
        self.custom = Custom(self.store.data)
        self.syncer = None
        self._sleep_timer = None
        self._sleep_at = 0
        self._mpris = None
        self._downloads = None
        self._recording = None
        self._record_flush = None
        self._cast = None             # {"name", "ctl", "server", ...} while casting
        self._installing = set()      # packages a terminal is installing now
        self._cast_devices = {}
        self._sub_options = []        # subtitles we can add: sidecars, YouTube's
        self._subs_loaded = {}        # option key -> file name inside the player
        self._fade_level = 1.0
        self._ramp_gen = 0
        self._fading_out = False
        self._next_fade_in = 0.0      # a slower fade-in for the next start
        self._alarm_fired_on = ""
        from .scrobble import Scrobbler
        self._scrobbler = Scrobbler(self.settings, lambda fn: POOL.submit(fn),
                                    self._scrobble_report)
        self._scrobble_status = {}
        # Artwork is fetched in the background and announced when it lands, so
        # a list is never held up by an image host.
        self.artwork = None
        self._art_seen = 0
        self.player = None            # the mpv client
        # Mute is tracked here rather than read back from mpv: there may be no
        # player to ask, and a write is not visible to a read straight after.
        self._mute = False
        self.session = None           # the player-session subprocess
        self._session_video = None    # whether that session has a window
        self.session_pid = None
        self.session_start = ""
        self._current = None
        self._resolved = {}

        # A live copy of the player's observed properties. emit_state reads
        # only this, so reporting the state never blocks on the player - which
        # is what used to wedge the daemon: three socket round trips and a
        # hyprctl spawn per state request, ten requests a second.
        self._props = {}
        self._loading = False         # asked to play, no sound yet
        self._error = ""
        self._entry_id = None         # mpv's id for the file now loading
        self._load_token = 0          # bumps per load, for the start watchdog
        self._started_token = -1
        self._candidates = []         # further links to try for this item
        self._reconnects = 0
        self._last_position_emit = 0.0
        self._gen = 0                 # bumped by every playback request
        self._gen_lock = threading.Lock()
        self._session_lock = threading.RLock()
        self._control = _Lane("ap-control")
        self._playback = _Lane("ap-playback")
        self._shutting_down = False

    def _log(self, message):
        if os.environ.get("AP_DEBUG") == "1":
            protocol.emit({"type": "log", "message": textutil.text(message, 300)})

    def ensure_catalogue(self):
        """Open the mirror and hand it to the sources. Cheap and idempotent."""
        try:
            self.catalogue.open()
        except Exception as exc:
            self._log("catalogue unavailable: %s" % exc)
            return None
        radio.set_catalogue(self.catalogue)
        tv.set_catalogue(self.catalogue)
        if self.artwork is None:
            self.artwork = Artwork(self.store.art_dir,
                                    on_ready=self._on_art,
                                    log=self._log)
            self.artwork.start(workers=int(self._on("artworkConcurrency", 6)))
            base.set_art_fetcher(self.artwork)
            radio.set_art_fetcher(self.artwork)
        return self.catalogue

    def _on_art(self, key, path):
        """An image finished downloading. Tell the shell, which repaints the
        row rather than re-fetching the list."""
        self._art_seen += 1
        self.emit({"type": "art", "key": textutil.text(key, 200),
                   "path": textutil.text(path, 1024)})
        # A sweep every few hundred images keeps a wide-browsing user from
        # accumulating tens of thousands of small files forever.
        if self._art_seen % 400 == 0:
            POOL.submit(self.artwork.sweep)

    def start_sync(self, full=True, source=None):
        """Mirror the catalogues in the background.

        Never on the request path: a browse that happens first is served from
        the network and is still correct.
        """
        if self.ensure_catalogue() is None:
            return False
        if self.syncer is None:
            self.syncer = Syncer(self.catalogue, log=self._log,
                                 on_done=self._sync_done,
                                 on_progress=self._sync_progress,
                                 custom=self.custom)
        return self.syncer.start(full=full, source=source)

    def _sync_progress(self, source, info):
        """Report the download as it happens."""
        self.emit({"type": "sync_progress", "source": source,
                   "progress": dict(info)})

    def _sync_done(self, source):
        """A mirror landed. Tell the tab, so its list and its timestamp are the
        same moment rather than the timestamp catching up on the next open."""
        if self.syncer is None:
            return
        self.emit({"type": "catalogue", "catalogue": self._catalogue_status()})

    # -- settings ----------------------------------------------------------

    def settings(self):
        merged = dict(DEFAULT_SETTINGS)
        merged.update(self.store.state.get("settings") or {})
        return merged

    def set_setting(self, key, value, apply=True):
        """Save a setting and put it into effect. apply=False only saves it:
        for a choice that is already in effect, such as a subtitle track picked
        from the menu, which re-applying the setting could replace."""
        key = textutil.text(key, 64)
        if key not in DEFAULT_SETTINGS:
            return False
        current = self.store.state.setdefault("settings", {})
        default = DEFAULT_SETTINGS[key]
        if isinstance(default, bool):
            value = textutil.bool_of(value, default)
        elif isinstance(default, int):
            low, high = SPEC.get(key, (0, 1 << 30))
            value = textutil.int_in(value, int(low), int(high), int(default))
        elif isinstance(default, float):
            low, high = SPEC.get(key, (0.0, 1e9))
            try:
                value = max(float(low), min(float(high), float(value or 0)))
            except (TypeError, ValueError):
                value = default
        else:
            value = textutil.text(value, 512)
        if current.get(key) == value:
            return True
        current[key] = value
        # Debounced: a volume drag is dozens of writes a second.
        save_soon = getattr(self.store, "save_soon", None)
        if save_soon is not None:
            save_soon()
        else:
            self.store.save()
        if not apply:
            return True
        if key in ("pipSize", "pipCorner", "pipFloating", "pipPinned", "videoMonitor"):
            # The next video window opens the way the last one was left.
            POOL.submit(self._video_rule)
        if key == "offlineMode":
            netguard.set_offline(bool(value))
            if not value and self.syncer is not None:
                self.start_sync(full=False)
        if key == "radioServer":
            radio.set_server(value)
        if key in ("equalizerEnabled", "eqPreset", "normalizeVolume", "eqBands",
                   "skipSilence"):
            self._apply_filters()
        if key == "defaultAspect":
            self._apply_aspect()
        if key in ("subtitlesEnabled", "subtitleSize", "subtitleLanguage"):
            # May fetch a subtitle file, so never on the caller's lane.
            POOL.submit(self._apply_subtitles)
        if key == "tvQuality":
            POOL.submit(self._apply_quality)
        if key.startswith("alarm"):
            self._alarm_fired_on = ""
            self._alarm_at = self._alarm_next()
            self.emit_state()
        # A setting that changes the sound has to reach the sound.
        if key == "playbackSpeed" and self.player is not None:
            try:
                self.player.set_property("speed", float(value))
            except (TypeError, ValueError):
                pass
        return True

    def _on(self, key, fallback=None):
        return self.settings().get(key, DEFAULT_SETTINGS.get(key, fallback))

    # -- command dispatch --------------------------------------------------

    def _bump(self):
        with self._gen_lock:
            self._gen += 1
            return self._gen

    def _superseded(self, gen):
        """True once a newer playback request has made `gen` pointless."""
        return gen is not None and gen != self._gen

    def handle(self, message):
        command = textutil.text(message.get("cmd") or message.get("command"), 40)
        ident = message.get("id")
        method = getattr(self, "cmd_" + command, None) if command.isidentifier() \
            else None
        if method is None:
            self.emit(protocol.error("unknown command %r" % command[:40],
                                     "unsupported", ident))
            return

        if command in PLAYBACK_COMMANDS:
            message = dict(message)
            message["_gen"] = self._bump()
            if command == "stop":
                # Silence first, bookkeeping after. The stop is acted on here,
                # on the reader thread, before anything queued in front of it
                # gets a turn - a stop that waits behind a slow play is a stop
                # that does not work.
                self._silence_now()

        def run():
            try:
                result = method(message)
                if result is not None and ident is not None:
                    result = dict(result)
                    result["id"] = ident
                    result.setdefault("type", "result")
                    self.emit(result)
            except SourceError as exc:
                self.emit(protocol.error(str(exc), exc.reason, ident))
            except resolver.ResolveError as exc:
                self.emit(protocol.error(str(exc), exc.reason, ident))
            except SystemExit:
                raise
            except Exception as exc:  # a bug must not kill the daemon
                self.emit(protocol.error("internal error: %s" % exc,
                                         "unsupported", ident))

        if command in PLAYBACK_COMMANDS:
            self._playback.submit(run)
        elif command in CONTROL_COMMANDS:
            self._control.submit(run)
        else:
            POOL.submit(run)

    # -- lifecycle commands ------------------------------------------------

    def cmd_ping(self, _m):
        return {"pong": True, "version": PROTOCOL_VERSION,
                "capabilities": CAPABILITIES}

    def cmd_caps(self, _m):
        self.emit({"type": "caps", "capabilities": CAPABILITIES,
                   "reasons": list(protocol.REASONS)})
        return None

    def cmd_settings_get(self, _m):
        return {"type": "settings", "settings": self.settings()}

    def cmd_settings_set(self, message):
        for key, value in (message.get("settings") or {}).items():
            self.set_setting(key, value)
        self.emit({"type": "settings", "settings": self.settings()})
        return None

    def cmd_state(self, _m):
        self.emit_state()
        self._emit_queue()
        return None

    def cmd_source(self, message):
        """The generic source call. One command with a mode keeps the QML
        side from needing a branch per source."""
        source = textutil.text(message.get("source"), 16)
        if source not in SOURCE_MODULES:
            raise SourceError("unknown source %r" % source[:16], "unsupported")
        mode = textutil.text(message.get("mode") or "search", 24)
        ident = message.get("id")
        limit = textutil.int_in(message.get("limit"), 1, 5000, 50)
        offset = textutil.int_in(message.get("offset"), 0, 1 << 20, 0)
        message = dict(message, offset=offset)
        settings = self.settings()
        cache_dir = self.store.art_dir
        budget = youtube.Budget(self.store,
                                self._on("ytResolutionBudgetPerHour", 120))

        scanning = False
        if source == "local" and mode in ("browse", "search"):
            scanning = self._ensure_library_scanned()

        show = None
        if source == "podcast" and mode == "episodes":
            feed_url = textutil.text(message.get("feed"), 2048)
            show, episodes = podcast.feed(feed_url)
            show = dict(show, subscribed=self.custom.is_subscribed(feed_url),
                        artUrl=show.get("art") or "",
                        art=base.art_for(show.get("art") or "", "pod-%s" % show["show"],
                                         base._fetcher()))
            offset = message.get("offset") or 0
            results = episodes[offset:offset + limit]
            if ident is not None:
                self.emit({"type": "list", "id": ident, "source": source,
                           "mode": mode, "items": results, "offset": offset,
                           "more": offset + len(results) < len(episodes),
                           "total": len(episodes), "show": show})
            return None
        results = self._source_call(source, mode, message, limit, settings,
                                    cache_dir, budget)
        if ident is not None:
            total = self._source_count(source, mode, message, settings)
            self.emit({"type": "list", "id": ident, "source": source,
                       "mode": mode, "items": results, "offset": offset,
                       # Whether another page exists, decided by the daemon.
                       # The panel used to guess from the page being full,
                       # which a filtered page never was.
                       "more": (offset + len(results) < total) if total is not None
                               else self._more_after(source, message, offset,
                                                     results, limit),
                       # The size of the filter, not the size of the page, so
                       # the status bar can say what the list is a window onto.
                       "total": total,
                       "scanning": scanning})
        return None

    def _more_after(self, source, message, offset, results, limit):
        """Whether a list with no known total has another page."""
        if source == "music":
            entry = music._PAGES.get(textutil.text(message.get("query"), 200).lower())
            if entry is not None:
                return bool(entry["token"]) or len(entry["items"]) > offset + len(results)
        if source == "youtube":
            return len(results) >= limit and offset + len(results) < 500
        return len(results) >= limit

    @staticmethod
    def _local_filter(message):
        """An album, artist or genre the Local tab has been opened on."""
        out = {}
        for key in ("artist", "album", "genre"):
            if message.get(key) is not None:
                out[key] = textutil.text(message.get(key), 300)
        return out

    def _ensure_library_scanned(self):
        """Start a first scan of the music folders if there has never been one.

        The Local tab used to say "No local music found" forever, because
        nothing ever scanned unless the user found the button in Settings.
        """
        if local.progress().get("running"):
            return True
        info = self.store.library.index_info()
        if info["scanned_at"] and info["index_version"] == local.INDEX_VERSION:
            return False
        POOL.submit(lambda: self.cmd_scan({}))
        return True

    def _source_count(self, source, mode, message, settings):
        """Rows matching the current filter, or None when it is not knowable.

        Only the mirrored catalogues can answer this cheaply.
        """
        if source == "local" and mode in ("browse", "search"):
            folder = message.get("folder") or ""
            return self.store.library.count_tracks(
                textutil.text(message.get("media"), 8), message.get("query") or "",
                folder=os.path.realpath(os.path.expanduser(folder)) if folder else None,
                **self._local_filter(message))
        if source not in ("radio", "tv") or mode not in ("browse", "search"):
            return None
        if self.ensure_catalogue() is None:
            return None
        country = textutil.text(message.get("country") or "", 4)
        group = textutil.text(
            (message.get("group") if source == "tv" else message.get("genre"))
            or "", 60)
        term = ""
        if mode == "search":
            term = textutil.text(message.get("query"), 120)
        try:
            if source == "radio":
                return self.catalogue.count_radio(
                    term, country or None, group or None,
                    self._on("minBitrate", 0))
            return self.catalogue.count_channels(
                term, taxonomy.country_code(country) or None, group or None,
                self._on("tvHideNsfw", True), self._on("tvHideGeoBlocked", True),
                self._on("tvHideNot247", True))
        except Exception:
            return None

    def _source_call(self, source, mode, message, limit, settings, cache_dir,
                     budget):
        # The mirror is authoritative once it is populated; the network is
        # the fallback, not the other way round.
        self.ensure_catalogue()
        query = message.get("query")
        if source == "radio":
            # The two filters apply to every listing mode, so picking a
            # country and then typing narrows further.
            country = textutil.text(message.get("country") or "", 4)
            genre = textutil.text(message.get("genre") or "", 60)
            offset = message.get("offset") or 0
            if mode == "search":
                return radio.search(query, limit, self._on("minBitrate", 0),
                                    cache_dir, country=country, genre=genre,
                                    offset=offset)
            if mode == "curated":
                return radio.curated(cache_dir, self._on("minBitrate", 0))[:limit]
            if mode == "browse":
                return radio.browse(message.get("section") or "popular", limit,
                                    self._on("minBitrate", 0), cache_dir,
                                    country=country, genre=genre, offset=offset)
            if mode == "tag":
                return radio.by_tag(query, limit, self._on("minBitrate", 0),
                                    cache_dir, country=country)
            if mode == "genres":
                return [{"uid": "radio:genre-%s" % textutil.slug(g, 40),
                         "source": "radio", "kind": "genre", "title": g,
                         "artist": str(n), "album": "", "art": {"url": "", "path": ""},
                         "duration": 0, "is_live": False, "url": "",
                         "codec": "", "bitrate": 0,
                         "extra": {"count": n}}
                        for g, n in radio.genres(cache_dir, limit=limit)]
            if mode == "countries":
                return [{"uid": "radio:cc-%s" % textutil.slug(code, 8),
                         "source": "radio", "kind": "country", "title": name,
                         "artist": str(n), "album": "", "art": {"url": "", "path": ""},
                         "duration": 0, "is_live": False, "url": "",
                         "codec": "", "bitrate": 0,
                         "extra": {"count": n, "code": code}}
                        for code, name, n in radio.countries(cache_dir,
                                                             limit=limit)]
            if mode == "favourites":
                return [base.item("radio", f["uid"], f.get("title", ""),
                                  url=f.get("url", ""), kind="station",
                                  is_live=True) for f in radio.favourites(self.store)]
        elif source == "tv":
            if mode in ("search", "browse"):
                # The requested page size, not a fixed 800. Sending eight
                # hundred rows to a list that shows twelve made every TV tab
                # switch rebuild a model eight hundred delegates long.
                return tv.channels(message.get("country") or "", limit,
                                   cache_dir, message.get("group") or "",
                                   (query or "") if mode == "search" else "",
                                   self._on("tvHideNsfw", True),
                                   offset=message.get("offset") or 0,
                                   hide_geo=self._on("tvHideGeoBlocked", True),
                                   hide_not247=self._on("tvHideNot247", True))
            if mode == "groups":
                return [{"uid": "tv:group-%s" % textutil.slug(g, 40),
                         "source": "tv", "kind": "group", "title": g,
                         "artist": "", "album": "", "art": {"url": "", "path": ""},
                         "duration": 0, "is_live": False, "url": "",
                         "codec": "", "bitrate": 0,
                         "extra": {"count": c}} for g, c in tv.channel_groups()]
            if mode == "countries":
                return [{"uid": "tv:cc-%s" % textutil.slug(code, 8),
                         "source": "tv", "kind": "country", "title": label,
                         "artist": str(n), "album": "",
                         "art": {"url": "", "path": ""}, "duration": 0,
                         "is_live": False, "url": "", "codec": "", "bitrate": 0,
                         "extra": {"count": n, "code": code}}
                        for code, label, n in tv.countries()]
        elif source == "youtube":
            if mode == "search":
                return youtube.search(query, limit, cache_dir, budget,
                                      offset=message.get("offset") or 0)
            if mode == "playlist":
                return youtube.playlist(query, limit, cache_dir, budget)
            if mode == "channel":
                return youtube.channel_feed(query, cache_dir, limit, budget)
        elif source == "music":
            if mode == "search":
                return music.search(query, limit, cache_dir, budget,
                                    offset=message.get("offset") or 0)
            if mode == "playlist":
                return music.playlist(query, cache_dir, limit, budget)
            if mode == "channel":
                return music.channel(query, cache_dir, limit, budget)
        elif source == "local":
            if mode in ("search", "browse"):
                offset = message.get("offset") or 0
                return local.tracks(self.store, settings, message.get("folder") or "",
                                    query or "", offset + limit,
                                    message.get("sort") or "title",
                                    textutil.text(message.get("media"), 8),
                                    **self._local_filter(message))[offset:]
            if mode in ("albums", "artists", "genres"):
                by = mode[:-1]
                return [{"uid": "local:%s-%s" % (by, textutil.slug(g["name"], 100)),
                         "source": "local", "kind": by, "title": g["name"],
                         "artist": g.get("artist") or "", "album": "",
                         "art": g.get("art") or {"url": "", "path": ""},
                         "duration": 0, "is_live": False, "url": "", "codec": "",
                         "bitrate": 0, "extra": {by: g["name"], "count": g["count"]}}
                        for g in local.groups(self.store, by, "audio", query or "")]
            if mode == "folders":
                return [{"uid": "local:folder-%s" % textutil.slug(f["path"], 100),
                         "source": "local", "kind": "folder", "title": f["name"],
                         "artist": "", "album": "", "art": {"url": "", "path": ""},
                         "duration": 0, "is_live": False, "url": "", "codec": "",
                         "bitrate": 0, "extra": {"path": f["path"],
                                                  "count": f["count"]}}
                        for f in local.folders(self.store, settings)]
        elif source == "podcast":
            if mode == "search":
                return podcast.search(query, limit)
            if mode == "browse":
                mine = [podcast.show_item(p["feed"], p.get("title"), p.get("author"),
                                          p.get("art"))
                        for p in self.custom.data()["podcasts"]]
                if mine or (message.get("offset") or 0):
                    return mine[(message.get("offset") or 0):][:limit]
                # Nothing subscribed yet: the directory's top shows, so the tab
                # starts with something to try rather than an empty page.
                try:
                    return podcast.top(limit=min(limit, 30))
                except SourceError:
                    return []
        raise SourceError("%s does not support %r" % (source, mode), "unsupported")

    def _catalogue_status(self):
        self.ensure_catalogue()
        status = self.syncer.status() if self.syncer else {
            "state": {}, "radio": 0, "tv": 0, "programmes": 0,
            "radio_age": self.catalogue.age("radio_full"),
            "tv_age": self.catalogue.age("tv_full")}
        counts = self.catalogue.counts()
        status.update({
            "radio": counts.get("radio", 0),
            "tv": counts.get("tv", 0),
            "programmes": counts.get("programmes", 0),
        })
        for key in ("radio", "tv"):
            status["%s_mirrored" % key] = self.catalogue.mirrored(key)
            status["%s_custom" % key] = counts.get("%s_custom" % key, 0)
        if self.syncer is not None:
            status["progress"] = dict(self.syncer.progress)
        return status

    def cmd_catalogue(self, message):
        """Catalogue status, counts and freshness, optionally starting a sync.

        Sent as a `catalogue` event: it used to come back as a generic result,
        which the panel ignores, so the status bar never heard about a sync it
        had just asked for.
        """
        self.ensure_catalogue()
        source = textutil.text(message.get("source"), 16)
        if textutil.bool_of(message.get("sync")):
            self.start_sync(full=textutil.bool_of(message.get("full"), True),
                            source=source or None)
        self.emit({"type": "catalogue", "catalogue": self._catalogue_status()})
        return None

    def cmd_scan(self, _m):
        changed = local.scan(self.store, self.settings(),
                             lambda done, total: self.emit(
                                 {"type": "progress", "task": "scan",
                                  "done": done, "total": total}))
        self.emit({"type": "library", "changed": changed})
        return None

    def cmd_lyrics(self, message):
        item = self._current or message.get("item") or {}
        found = music.lyrics_for(item, self.settings())
        self._last_lyrics = (item.get("uid", ""), found or None)
        self.emit({"type": "lyrics", "uid": item.get("uid", ""),
                   "lyrics": found or None})
        return None

    @staticmethod
    def lrc_text(item, lyrics):
        """Lyrics as an .lrc file: tags, then one [mm:ss.xx] line each (or
        plain lines when the lyrics carry no timing)."""
        out = []
        for tag, key in (("ar", "artist"), ("ti", "title"), ("al", "album")):
            value = textutil.text(item.get(key), 200).replace("]", ")")
            if value:
                out.append("[%s:%s]" % (tag, value))
        duration = int(item.get("duration") or 0)
        if duration:
            out.append("[length:%d:%02d]" % (duration // 60, duration % 60))
        out.append("[re:AuroraPulse]")
        synced = bool(lyrics.get("synced"))
        for line in lyrics.get("lines") or []:
            text = str(line.get("text") or "").replace("\n", " ").strip()
            at = int(line.get("t") or 0)
            if synced and at >= 0:
                out.append("[%02d:%02d.%02d]%s" % (at // 60000, (at // 1000) % 60,
                                                   (at % 1000) // 10, text))
            else:
                out.append(text)
        return "\n".join(out) + "\n"

    def cmd_lyrics_save(self, message):
        """Save the lyrics on screen as an .lrc file: beside a local track,
        where players (and this one) pick it up, or in Music › AuroraPulse ›
        Lyrics for anything streamed."""
        item = self._current or message.get("item") or {}
        uid, lyrics = getattr(self, "_last_lyrics", ("", None))
        if not lyrics or uid != item.get("uid") or not lyrics.get("lines"):
            raise SourceError("there are no lyrics on screen to save", "empty")
        from .downloads import audio_dir, _safe_name
        path = str((item.get("extra") or {}).get("path") or "") \
            if item.get("source") == "local" else ""
        folder = os.path.dirname(path) if path else os.path.join(audio_dir(), "Lyrics")
        if path and not os.access(folder, os.W_OK):
            folder = os.path.join(audio_dir(), "Lyrics")
        stem = os.path.splitext(os.path.basename(path))[0] if path and \
            folder == os.path.dirname(path) else _safe_name(
                "%s - %s" % (item.get("artist"), item.get("title"))
                if item.get("artist") else item.get("title"), 120)
        os.makedirs(folder, exist_ok=True)
        target = os.path.join(folder, stem + ".lrc")
        n = 2
        while os.path.exists(target):
            target = os.path.join(folder, "%s (%d).lrc" % (stem, n))
            n += 1
        # O_EXCL: never write through something already there.
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(self.lrc_text(item, lyrics))
        self._notice("Saved the lyrics", target)
        self.emit({"type": "exported", "path": target})
        return {"path": target}

    def _epg_args(self):
        url = str(self._on("epgUrl", "") or "")
        if "epg-ripper/ALL_SOURCES" in url:
            url = ""            # the old default, which no longer exists
        return {"cache_dir": self.store.cache,
                "max_age": float(self._on("epgRefreshHours", 12)) * 3600,
                "custom_url": url}

    @staticmethod
    def _guide_quietly(country, args):
        try:
            tv.country_guide(country, args["cache_dir"], args["max_age"], args["custom_url"])
        except Exception:  # noqa: BLE001 - a missing guide is an empty row
            pass

    def cmd_epg(self, message):
        """Now and next for the channel playing (the guide beside the player)."""
        channel = self._current or message.get("item") or {}
        now = time.time()
        rows = tv.schedule(channel, now - 3 * 3600, now + 24 * 3600, **self._epg_args())
        current = next((p for p in rows if p["start"] <= now < p["stop"]), None)
        upcoming = [p for p in rows if p["start"] > now][:12]
        self.emit({"type": "epg", "channel": textutil.text(channel.get("title"), 200),
                   "now": current, "upcoming": upcoming})
        return None

    def cmd_epg_grid(self, message):
        """A guide grid: programmes for many channels over the next hours."""
        hours = textutil.int_in(message.get("hours"), 1, 24, 4)
        now = time.time()
        start = now - 1800
        end = now + hours * 3600
        channels = [c for c in (message.get("channels") or [])[:80] if isinstance(c, dict)]
        # Each country is a separate guide file, a few seconds to fetch and
        # parse the first time; fetch them side by side rather than in turn.
        args = self._epg_args()
        countries = sorted({str(c.get("country") or "").upper() for c in channels} - {""})
        if countries:
            from concurrent.futures import ThreadPoolExecutor as _Pool
            with _Pool(max_workers=4) as fetchers:
                list(fetchers.map(lambda cc: self._guide_quietly(cc, args), countries))
        rows = []
        for channel in channels:
            try:
                programmes = tv.schedule(channel, start, end, **self._epg_args())
            except Exception:  # noqa: BLE001 - one country's guide failing is not all
                programmes = []
            rows.append({"uid": textutil.text(channel.get("uid"), 200),
                         "programmes": programmes})
        self.emit({"type": "epg_grid", "id": message.get("id"), "start": int(start),
                   "end": int(end), "now": int(now), "rows": rows})
        return None

    # -- favourites, history and resume points ---------------------------

    LIBRARY_LIMIT = 500
    HISTORY_LIMIT = 100
    RESUME_MIN_DURATION = 600       # only long things are worth resuming

    def _favorites(self):
        store = getattr(self, "store", None)
        if store is None:
            return []
        return store.library.favorites(self.LIBRARY_LIMIT)

    def _history(self):
        return self.store.library.history(self.HISTORY_LIMIT)

    def _is_favorite(self, uid):
        store = getattr(self, "store", None)
        return store is not None and store.library.is_favorite(uid)

    def _emit_saved(self):
        self.emit({"type": "saved", "favorites": self._favorites(),
                   "history": self._history(),
                   "playlists": self.store.library.playlists()})

    def cmd_saved(self, _m):
        self._emit_saved()
        return None

    def cmd_favorite(self, message):
        """Keep or drop a favourite - any source, not just radio.

        With no item it is the one playing, which is what the heart beside the
        title means.
        """
        entry = protocol.clean_item(message.get("item") or self._current or {})
        if not entry:
            raise SourceError("there is nothing to keep", "empty")
        present = self._is_favorite(entry["uid"])
        wanted = textutil.bool_of(message.get("on"), not present)
        entry["art"] = dict(entry.get("art") or {}, path="")
        self.store.library.set_favorite(entry, wanted)
        self._emit_saved()
        self.emit_state()
        self._notice(("Added %s to favourites" if wanted else "Removed %s from favourites")
                     % entry.get("title", "it"))
        return {"favorite": wanted}

    def cmd_forget(self, message):
        """Remove one entry from the history, or clear it."""
        uid = textutil.text(message.get("uid"), 200)
        self.store.library.forget(uid)
        self._emit_saved()
        return None

    # -- playlists -------------------------------------------------------

    def cmd_playlist(self, message):
        """Named playlists, and the smart ones that are a query.

        action: create | rename | delete | add | remove | move | items | play
        Every change answers with the fresh list (a `saved` event), so the
        Saved tab never shows a playlist the database no longer has.
        """
        library = self.store.library
        action = textutil.text(message.get("action"), 16)
        ident = textutil.text(message.get("playlist"), 40)
        smart = ident.startswith("smart:")
        if action in ("rename", "delete", "add", "remove", "move") and smart:
            raise SourceError("a smart playlist fills itself and cannot be edited",
                              "unsupported")
        result = None
        try:
            if action == "create":
                made = library.create_playlist(message.get("name"))
                items = [protocol.clean_item(i) for i in message.get("items") or []
                         if isinstance(i, dict)]
                if items:
                    library.add_to_playlist(made["id"], [self._keepable(i) for i in items if i])
                self._notice("Made the playlist %s" % made["name"])
                result = made
            elif action == "rename":
                if not library.rename_playlist(ident, message.get("name")):
                    raise SourceError("that playlist no longer exists", "not-found")
            elif action == "delete":
                name = library.playlist_name(ident)
                if not library.delete_playlist(ident):
                    raise SourceError("that playlist no longer exists", "not-found")
                self._notice("Deleted the playlist %s" % name)
            elif action == "add":
                items = message.get("items") or ([message["item"]] if message.get("item")
                                                  else [self._current or {}])
                clean = [protocol.clean_item(i) for i in items if isinstance(i, dict)]
                added = library.add_to_playlist(ident, [self._keepable(i) for i in clean if i])
                name = library.playlist_name(ident)
                self._notice("Added to %s" % name if added else "Already in %s" % name)
                result = {"added": added}
            elif action == "remove":
                library.remove_from_playlist(ident, textutil.text(message.get("uid"), 200))
            elif action == "move":
                library.move_in_playlist(ident, int(message.get("from") or 0),
                                         int(message.get("to") or 0))
            elif action in ("items", "play"):
                items = library.playlist_items(ident)
                if action == "play":
                    if not items:
                        raise SourceError("that playlist is empty", "empty")
                    self.handle({"cmd": "play", "items": items,
                                 "start": int(message.get("start") or 0)})
                    return None
                self.emit({"type": "playlist", "id": message.get("id"),
                           "playlist": ident, "name": library.playlist_name(ident),
                           "smart": smart,
                           "items": [self._with_art(dict(i)) for i in items]})
                return None
            else:
                raise SourceError("unknown playlist action %r" % action[:16], "unsupported")
        except KeyError:
            raise SourceError("that playlist no longer exists", "not-found")
        self._emit_saved()
        if action in ("remove", "move"):
            self.emit({"type": "playlist", "playlist": ident,
                       "name": library.playlist_name(ident), "smart": False,
                       "items": [self._with_art(dict(i))
                                 for i in library.playlist_items(ident)]})
        return result

    @staticmethod
    def _keepable(entry):
        """An item as it is stored: no cached artwork path, which may be
        swept from the cache, and no per-play bookkeeping."""
        entry = dict(entry)
        if entry.get("source") != "local":
            # A local track's picture is its own cover or a thumbnail made
            # from it, which a rescan keeps; anything else is re-fetched.
            entry["art"] = dict(entry.get("art") or {}, path="")
        extra = dict(entry.get("extra") or {})
        extra.pop("playedAt", None)
        entry["extra"] = extra
        return entry

    def _remember_played(self, entry):
        entry = protocol.clean_item(entry)
        if not entry:
            return
        entry["art"] = dict(entry.get("art") or {}, path="")
        entry["extra"] = dict(entry.get("extra") or {}, playedAt=int(time.time()))
        self.store.library.played(entry, self.HISTORY_LIMIT)
        self._emit_saved()

    def _remember_position(self):
        """Note how far into a long track or video we are, to resume later."""
        current = self._current or {}
        uid = current.get("uid")
        if not uid or self._is_live() or self.player is None:
            return
        duration, position = self._duration(), self._position()
        if duration < self.RESUME_MIN_DURATION or position < 30 \
                or position > duration - 30:
            self.store.library.set_position(uid, None)
        else:
            self.store.library.set_position(uid, int(position))

    def _resume_point(self, item):
        if not self._on("resumePlayback", True) or item.get("is_live"):
            return 0.0
        try:
            return self.store.library.position(item.get("uid"))
        except (TypeError, ValueError):
            return 0.0

    def _notify_track(self, item, stream_title=""):
        """A desktop notification for what just started, if the user wants one."""
        if not self._on("notifyTrackChange", False) or not shutil.which("notify-send"):
            return
        title = textutil.text(item.get("title"), 120) or "AuroraPulse"
        if stream_title:
            body = "%s\n%s" % (stream_title, title)
            title = "Now on %s" % title
        else:
            body = " · ".join(x for x in (item.get("artist"), item.get("album")) if x)
        art = (self._with_art(dict(item)).get("art") or {}).get("path") or ""
        argv = ["notify-send", "-a", "AuroraPulse", "-u", "low", "-t", "4000",
                "-h", "string:x-canonical-private-synchronous:aurorapulse",
                "-i", art if art and os.path.exists(art) else "audio-x-generic",
                title, textutil.text(body, 300)]
        try:
            subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            pass

    def cmd_raise(self, _m):
        """Something (the desktop, over MPRIS) asked to see the player."""
        self.emit({"type": "raise"})
        return None

    def cmd_jump(self, message):
        """Play the queue entry at `index` (the queue view)."""
        position = textutil.int_in(message.get("index"), 0, max(0, len(self.queue) - 1), -1)
        if 0 <= position < len(self.queue):
            self.index = position
            self._advance(gen=message.get("_gen"))
        return None

    # -- downloads ---------------------------------------------------------

    _DL_LOCK = threading.Lock()

    def _dl(self):
        # Locked: download requests arrive on the worker pool, and three at
        # once each built their own downloader - so jobs that are meant to run
        # one after another ran together and trampled each other's files.
        with self._DL_LOCK:
            if self._downloads is None:
                from .downloads import Downloads
                self._downloads = Downloads(
                    on_change=lambda jobs: self.emit({"type": "downloads", "jobs": jobs}),
                    on_done=self._download_done, on_failed=self._download_failed,
                    log=self._log, library=self.store.library)
            return self._downloads

    def resume_downloads(self):
        """Start the downloader at launch if last session left work queued."""
        try:
            jobs = self.store.library.load_downloads()
        except Exception:  # noqa: BLE001
            return
        if any(j.get("state") in ("queued", "running") for j in jobs):
            self._dl()._changed(force=True)

    def cmd_download(self, message):
        """Keep a YouTube video, a song or a podcast episode for offline."""
        entry = protocol.clean_item(message.get("item") or self._current or {})
        if not entry:
            raise SourceError("there is nothing to download", "empty")
        kind = textutil.text(message.get("kind") or "", 8)
        if kind not in ("audio", "video"):
            kind = "video" if entry.get("source") == "youtube" else "audio"
        fmt = textutil.text(message.get("format") or "", 10) or self._on(
            "downloadVideoFormat" if kind == "video" else "downloadAudioFormat", "")
        try:
            job = self._dl().add(entry, kind, fmt, self._on("downloadVideoQuality", "1080p"),
                                 bool(self._on("downloadArtwork", True)))
        except ValueError as exc:
            raise SourceError(str(exc), "unsupported")
        self._notice("Downloading %s" % job["title"],
                     "%s %s, into %s" % (kind, job["format"].upper(),
                                         "Videos" if kind == "video" else "Music"))
        return None

    def cmd_download_cancel(self, message):
        self._dl().cancel(textutil.text(message.get("jobId"), 20))
        return None

    def cmd_downloads(self, _m):
        self.emit({"type": "downloads", "jobs": self._dl().jobs()})
        return None

    def _download_failed(self, job):
        self._notice("Could not download %s" % job["title"], job.get("error") or "")
        self.emit(protocol.error("Download failed: %s - %s"
                                 % (job["title"], job.get("error") or "unknown error"),
                                 "network"))

    def _download_done(self, job):
        self._notice("Downloaded %s" % job["title"], job["path"])
        # Into the library at once, so it is in the Local tab without a rescan.
        POOL.submit(lambda: self.cmd_scan({}))

    # -- recording ---------------------------------------------------------

    RECORD_EXT = {"mp3": "mp3", "aac": "aac", "aac_latm": "aac", "opus": "ogg",
                  "vorbis": "ogg", "flac": "flac"}

    def cmd_record(self, message):
        """Record what is playing - a radio station or a TV channel - to a file."""
        want = textutil.bool_of(message.get("on"), self._recording is None)
        if want:
            self._start_recording()
        else:
            self._finish_recording()
        self.emit_state()
        return None

    def _start_recording(self):
        current = self._current or {}
        if self._recording is not None:
            return
        if not current or self.player is None or not self._is_live():
            raise SourceError("only a live station or channel can be recorded; "
                              "download a track or video instead", "unsupported")
        if self._session_video:
            ext = "ts"
        else:
            codec = str(self._props.get("audio-codec-name") or "").lower()
            ext = self.RECORD_EXT.get(codec, "mka")
        from .downloads import recordings_dir
        from .session import RECORD_MOUNT
        staging = os.path.join(recordings_dir(), ".recording")
        if not os.path.isdir(staging):
            raise SourceError("this player was started before recording to disk "
                              "was possible; play the station again to record", "unsupported")
        free = shutil.disk_usage(staging).free
        if free < 1 << 30:
            raise SourceError("less than 1 GB of disk space is free", "unsupported")
        name = "recording-%d.%s" % (int(time.time()), ext)
        host = os.path.join(staging, name)
        # The player is sandboxed and cannot see $HOME; this one folder on
        # disk is bound into it, and the file is moved out when it ends.
        self.player.set_property("stream-record", RECORD_MOUNT + "/" + name)
        threading.Thread(target=self._recording_watch, args=(host,), daemon=True).start()
        self._recording = {"since": time.time(), "title": current.get("title", ""),
                           "host": host, "ext": ext}
        self._notice("Recording %s" % current.get("title", ""))

    def _recording_watch(self, host):
        """Stop a recording before it fills the disk."""
        while self._recording is not None and self._recording.get("host") == host:
            time.sleep(30)
            try:
                if shutil.disk_usage(os.path.dirname(host)).free < 300 << 20:
                    self._notice("Recording stopped", "The disk is almost full.")
                    self._finish_recording()
                    self.emit_state()
                    return
            except OSError:
                return

    def _finish_recording(self):
        """Stop recording and move the file into ~/Music/AuroraPulse/Recordings.

        Returns an Event that is set once mpv has written the file out, or
        None if nothing was recording. mpv buffers the recording in 256 KB
        blocks and writes the last of it only when told to stop - a second or
        so later - so whoever ends the player afterwards waits on this first,
        or a short recording comes out empty.
        """
        recording, self._recording = getattr(self, "_recording", None), None
        if recording is None:
            return None
        flushed = threading.Event()
        self._record_flush = flushed
        player = self.player
        if player is not None:
            try:
                player.set_property("stream-record", "")
            except Exception:  # noqa: BLE001
                pass

        def move():
            from .downloads import recordings_dir, _safe_name
            host = recording["host"]
            # Wait until the file has something in it and has stopped
            # growing; the move to ~/Music may be a copy across filesystems.
            size, steady = -1, 0
            for _ in range(48):
                time.sleep(0.25)
                try:
                    now_size = os.path.getsize(host)
                except OSError:
                    now_size = 0
                steady = steady + 1 if now_size == size else 0
                size = now_size
                if size > 0 and steady >= 3:
                    break
            flushed.set()
            if size < 16 << 10:
                try:
                    os.unlink(host)
                except OSError:
                    pass
                self._notice("Nothing was recorded", "The stream sent no audio while recording.")
                return
            folder = recordings_dir()
            os.makedirs(folder, exist_ok=True)
            stamp = time.strftime("%Y-%m-%d %H.%M", time.localtime(recording["since"]))
            stem = "%s %s" % (_safe_name(recording["title"], 80), stamp)
            target = os.path.join(folder, "%s.%s" % (stem, recording["ext"]))
            n = 2
            while os.path.exists(target):
                target = os.path.join(folder, "%s (%d).%s" % (stem, n, recording["ext"]))
                n += 1
            try:
                shutil.move(host, target)
                os.chmod(target, 0o644)
            except OSError as exc:
                self._notice("The recording could not be saved", str(exc))
                return
            seconds = int(time.time() - recording["since"])
            length = ("%d-minute" % (seconds // 60)) if seconds >= 60 else \
                ("%d-second" % max(1, seconds))
            self._notice("Saved a %s recording" % length, target)
            self.emit({"type": "recorded", "path": target})
            self.cmd_scan({})
        POOL.submit(move)
        return flushed

    # -- scrobbling --------------------------------------------------------

    def _scrobble_report(self, service, ok, message):
        self._scrobble_status[service] = {"ok": bool(ok), "message": message,
                                          "at": int(time.time())}
        self.emit({"type": "scrobble", "status": dict(self._scrobble_status)})

    def cmd_scrobble_status(self, _m):
        self.emit({"type": "scrobble", "status": dict(self._scrobble_status)})
        return None

    def cmd_lastfm_login(self, message):
        """Sign in to Last.fm once; only the session key is kept."""
        from .scrobble import lastfm_login
        key = textutil.text(message.get("apiKey"), 64).strip()
        secret = textutil.text(message.get("apiSecret"), 64).strip()
        user = textutil.text(message.get("user"), 64).strip()
        password = str(message.get("password") or "")[:256]
        if not (key and secret and user and password):
            raise SourceError("Last.fm needs an API key, its secret, your username "
                              "and your password", "empty")
        try:
            session, name = lastfm_login(key, secret, user, password)
        except Exception as exc:  # noqa: BLE001
            raise SourceError("Last.fm sign-in failed: %s" % exc, "denied")
        for k, v in (("lastfmApiKey", key), ("lastfmApiSecret", secret),
                     ("lastfmSession", session), ("lastfmUser", name),
                     ("scrobbleEnabled", True)):
            self.set_setting(k, v)
        self.emit({"type": "settings", "settings": self.settings()})
        self._notice("Signed in to Last.fm as %s" % name)
        return None

    def cmd_lastfm_logout(self, _m):
        for k in ("lastfmSession", "lastfmUser"):
            self.set_setting(k, "")
        self.emit({"type": "settings", "settings": self.settings()})
        return None

    def cmd_favourite(self, message):
        on = textutil.bool_of(message.get("on"), True)
        station_id = (message.get("item") or {}).get("uid") or ""
        entry = message.get("item") or {}
        radio.save_favourites(self.store,
                              entry.get("extra", {}).get("uid", station_id)
                              or entry.get("uid", ""),
                              entry.get("title", ""), entry.get("url", ""), on)
        return {"favourites": len(radio.favourites(self.store))}

    def cmd_budget(self, message):
        budget = youtube.Budget(self.store, self._on("ytResolutionBudgetPerHour", 120))
        if textutil.bool_of(message.get("clear")):
            budget.clear()
        return {"remaining": budget.remaining(), "per_hour": budget.per_hour}

    _STATION_UUID = re.compile(r"^radio:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                               r"[0-9a-f]{4}-[0-9a-f]{12})$")

    def _report_play(self, item):
        """"Report plays to the station directory": one anonymous click on
        Radio Browser for a directory station, which is how it ranks them.
        Stations the user added by hand are not in the directory and are
        never reported."""
        match = self._STATION_UUID.match(str(item.get("uid") or ""))
        if not match or not self._on("reportPlays", True):
            return False
        try:
            return radio.report_play(match.group(1), self.store.state, True)
        except Exception as exc:  # noqa: BLE001 - a courtesy, never an error
            self._log("play report failed: %s" % exc)
            return False

    def cmd_report_play(self, _m):
        item = self._current or {}
        return {"reported": bool(self._report_play(item))}

    # -- the user's own sources ------------------------------------------

    def _emit_custom(self):
        summary = self.custom.summary()
        summary["playlists"] = [dict(p) for p in summary["playlists"]]
        self.emit({"type": "custom", "custom": summary})

    def _apply_custom(self, radio_too=True, tv_too=True):
        """Write the user's additions into the mirror and tell the panel."""
        if self.ensure_catalogue() is None:
            return
        if radio_too:
            rows, tags = self.custom.radio_rows()
            self.catalogue.replace_custom_radio(rows, tags)
        if tv_too:
            rows, tags = self.custom.channel_rows()
            self.catalogue.replace_custom_channels(rows, tags)
        self._emit_custom()
        self.emit({"type": "catalogue", "catalogue": self._catalogue_status()})

    def cmd_custom(self, message):
        """List, add or remove the user's stations, channels and playlists.

        The settings page in the original app could add a station or a
        channel by hand and manage a list of M3U playlists; this is that.
        """
        op = textutil.text(message.get("op") or "list", 16)
        kind = textutil.text(message.get("kind"), 16)
        fields = message.get("fields") or {}
        ident = textutil.text(message.get("itemId"), 80)
        try:
            if op == "list":
                self._emit_custom()
                return None
            if kind == "station" and op == "add":
                station = self.custom.add_station(
                    fields.get("name"), fields.get("url"), fields.get("country"),
                    fields.get("tags"), fields.get("favicon"))
                self._apply_custom(tv_too=False)
                self._notice("Added %s to your stations" % station["name"])
            elif kind == "station" and op == "remove":
                self.custom.remove_station(ident)
                self._apply_custom(tv_too=False)
            elif kind == "station" and op == "clear":
                self.custom.clear_stations()
                self._apply_custom(tv_too=False)
            elif kind == "channel" and op == "add":
                channel = self.custom.add_channel(
                    fields.get("name"), fields.get("url"), fields.get("country"),
                    fields.get("category"), fields.get("logo"))
                self._apply_custom(radio_too=False)
                self._notice("Added %s to your channels" % channel["name"])
            elif kind == "channel" and op == "remove":
                self.custom.remove_channel(ident)
                self._apply_custom(radio_too=False)
            elif kind == "podcast" and op == "add":
                self.custom.add_podcast(fields.get("feed"), fields.get("title"),
                                        fields.get("author"), fields.get("art"))
                self._emit_custom()
                self._notice("Subscribed to %s" % (fields.get("title") or "the podcast"))
            elif kind == "podcast" and op == "remove":
                self.custom.remove_podcast(fields.get("feed") or ident)
                self._emit_custom()
                self._notice("Unsubscribed")
            elif kind == "playlist" and op == "add":
                new_id = self.custom.add_playlist(
                    url=fields.get("url") or "", text=fields.get("text") or "",
                    name=fields.get("name") or "")
                self._emit_custom()
                count = self.custom.refresh_playlist(new_id)
                self._apply_custom(radio_too=False)
                self._notice("Playlist added: %d channels" % count)
            elif kind == "playlist" and op == "remove":
                self.custom.remove_playlist(ident)
                self._apply_custom(radio_too=False)
            elif kind == "playlist" and op == "enable":
                self.custom.set_playlist_enabled(
                    ident, textutil.bool_of(message.get("enabled"), True))
                self._apply_custom(radio_too=False)
            elif kind == "playlist" and op == "refresh":
                if ident:
                    self.custom.refresh_playlist(ident)
                else:
                    self.custom.refresh_all()
                self._apply_custom(radio_too=False)
                self._notice("Playlists refreshed")
            else:
                raise SourceError("unknown change %s/%s" % (kind, op), "unsupported")
        except ValueError as exc:
            self._emit_custom()
            raise SourceError(str(exc), "unsupported")
        return None

    def _notice(self, title, body=""):
        self.emit({"type": "notice", "level": "info",
                   "title": textutil.text(title, 160),
                   "body": textutil.text(body, 300)})

    def cmd_stations_import(self, message):
        """Import stations: pasted text, a web address or a file path.

        JSON and CSV exports from the original app, M3U and PLS playlists and
        plain lists of stream addresses are all understood. Imported stations
        become the user's own, so they survive every catalogue update.
        """
        text = message.get("text") or ""
        source = textutil.text(message.get("source") or "", 2048).strip()
        if not text and source:
            try:
                text = read_source(source)
            except Exception as exc:
                raise SourceError("could not read that: %s" % exc, "network")
        try:
            entries = parse_import(text)
        except ValueError as exc:
            raise SourceError("that is not a format I can read: %s" % exc,
                              "unsupported")
        added = 0
        for entry in entries:
            try:
                self.custom.add_station(
                    entry.get("name"), entry.get("url"), entry.get("country"),
                    entry.get("tags"), entry.get("favicon"),
                    entry.get("homepage"), entry.get("bitrate") or 0,
                    save=False)
                added += 1
            except (ValueError, TypeError):
                continue
        self.custom.save()
        if not added:
            raise SourceError("no stations with a playable address were found",
                              "empty")
        self._apply_custom(tv_too=False)
        self._notice("Imported %d station%s" % (added, "" if added == 1 else "s"))
        return {"imported": added}

    def cmd_stations_export(self, message):
        """Write the station list to a file in the Downloads folder."""
        fmt = textutil.text(message.get("format") or "json", 8).lower()
        if fmt not in ("json", "m3u", "csv"):
            fmt = "json"
        mine = textutil.bool_of(message.get("mine"), False)
        if self.ensure_catalogue() is None:
            raise SourceError("the station list is not available", "unsupported")
        rows = self.catalogue.radio_rows(mine_only=mine)
        if not rows:
            raise SourceError("there are no stations to export", "empty")
        name = "aurorapulse-%s-%s.%s" % ("my-stations" if mine else "stations",
                                        time.strftime("%Y%m%d-%H%M"), fmt)
        path = os.path.join(downloads_dir(), name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(export_rows(rows, fmt))
        self._notice("Exported %d stations" % len(rows), path)
        self.emit({"type": "exported", "path": path, "count": len(rows)})
        return {"path": path}

    def cmd_clear_catalogue(self, message):
        """Delete a downloaded catalogue (radio or tv). Own stations stay."""
        source = textutil.text(message.get("source"), 8)
        if source not in ("radio", "tv") or self.ensure_catalogue() is None:
            raise SourceError("nothing to clear", "unsupported")
        self.catalogue.clear(source)
        self.emit({"type": "catalogue", "catalogue": self._catalogue_status()})
        self._notice("Cleared the downloaded %s list"
                     % ("station" if source == "radio" else "channel"))
        return None

    def cmd_health(self, message):
        """Settings › Health: what is installed, working, missing or stale."""
        from . import doctor
        checks = doctor.run_all(self.store)
        level, text = doctor.summary(checks)
        installs, updates = doctor.installable(checks)
        self.emit({"type": "health", "id": message.get("id"), "checks": checks,
                   "level": level, "summary": text, "installs": installs,
                   "updates": updates, "installing": sorted(self._installing)})
        return None

    def cmd_health_install(self, message):
        """Open a terminal that installs (or updates) packages, then watch for
        them to arrive and refresh Health. Only packages that a failing check
        names can be installed; anything else in the request is ignored."""
        from . import doctor
        checks = doctor.binaries() + [c for c in (doctor.ytdlp(),
                                                  doctor.python_modules()) if c]
        installs, updates = doctor.installable(checks)
        update = bool(message.get("update"))
        allowed = updates if update else installs
        wanted = [p for p in (message.get("packages") or allowed) if p in allowed]
        if not wanted:
            self.cmd_health({})
            return None
        argv = doctor.install_command(wanted, update=update)
        if not argv:
            raise SourceError("no terminal to install from; run: sudo pacman -S %s"
                              % " ".join(wanted), "unsupported")
        try:
            subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            raise SourceError("could not open a terminal: %s" % exc, "unsupported")
        self._installing.update(wanted)
        self._notice("Finish in the terminal", "It asks for your password, then installs "
                     + ", ".join(wanted) + ".")
        self.cmd_health({})
        POOL.submit(self._watch_install, list(wanted), update,
                    (doctor.ytdlp() or {}).get("detail", "") if update else "")
        return None

    def _watch_install(self, packages, update, before):
        """Refresh Health once the packages are there (or updated), or after
        fifteen minutes - the terminal may simply have been closed."""
        from . import doctor
        end = time.time() + 15 * 60
        while time.time() < end and not self._shutting_down:
            time.sleep(3)
            if update:
                done = (doctor.ytdlp() or {}).get("detail", "") != before
            else:
                done = all(doctor.is_installed(p) for p in packages)
            if done:
                self._notice("Installed" if not update else "Updated", ", ".join(packages))
                break
        self._installing.difference_update(packages)
        self.cmd_health({})

    def startup_health(self):
        """At launch, say if something AuroraPulse needs is missing - and the
        first time, anything at all - with a way to Settings › Health, where
        each one can be installed. "Not now" holds until the list changes."""
        from . import doctor
        try:
            checks = doctor.binaries() + [c for c in (doctor.ytdlp(),
                                                      doctor.python_modules()) if c]
        except Exception:  # noqa: BLE001
            return
        health = self.store.state.setdefault("health", {})
        first_run = not health.get("seen")
        health["seen"] = int(time.time())
        self.store.save_soon()
        names = doctor.prompt_names(checks, first_run)
        dismissed = set(health.get("dismissed") or [])
        if not names or set(names) <= dismissed:
            return
        installs, updates = doctor.installable(checks)
        self.emit({"type": "health_prompt", "missing": names,
                   "required": any(c.get("required") and not c["ok"] for c in checks),
                   "installs": installs, "updates": updates, "first_run": first_run})

    def cmd_health_dismiss(self, message):
        names = [textutil.text(n, 60) for n in message.get("names") or []][:40]
        health = self.store.state.setdefault("health", {})
        health["dismissed"] = sorted(set(health.get("dismissed") or []) | set(names))
        self.store.save_soon()
        return None

    def cmd_library_stats(self, _m):
        state = self.store.state.get("local") or {}
        index = state.get("index") or {}
        folders = {(e.get("extra") or {}).get("folder") for e in index.values()
                   if isinstance(e, dict)}
        self.emit({"type": "library_stats",
                   "tracks": len(index), "folders": len(folders - {None}),
                   "scannedAt": int(state.get("scanned_at") or 0),
                   "roots": local.default_roots(self.settings())})
        return None

    def cmd_sleep(self, message):
        """Stop playing after `minutes`; 0 cancels."""
        minutes = textutil.int_in(message.get("minutes"), 0, 24 * 60, 0)
        if self._sleep_timer is not None:
            self._sleep_timer.cancel()
            self._sleep_timer = None
        self._sleep_at = 0
        if minutes:
            self._sleep_at = int(time.time() + minutes * 60)
            timer = threading.Timer(minutes * 60, self._sleep_fire)
            timer.daemon = True
            timer.start()
            self._sleep_timer = timer
        self.emit_state()
        return None

    def _sleep_fire(self):
        self._sleep_timer = None
        self._sleep_at = 0
        # Fade out over the last stretch rather than cutting off mid-word.
        seconds = float(self._on("sleepFadeSec", 30) or 0)
        if seconds > 0 and self.player is not None and self._current is not None:
            self._ramp(0.0, seconds, then=self._sleep_stop)
        else:
            self._sleep_stop()

    # -- alarm -------------------------------------------------------------

    def _alarm_item(self):
        entry = self.store.state.get("alarm_item")
        return entry if isinstance(entry, dict) and entry.get("uid") else None

    def _alarm_info(self):
        entry = self._alarm_item()
        return {"enabled": bool(self._on("alarmEnabled", False)),
                "time": self._on("alarmTime", "07:00"),
                "days": self._on("alarmDays", "1,2,3,4,5,6,7"),
                "item": entry, "next": self._alarm_next()}

    def _alarm_next(self):
        """Unix time of the next ring, or 0 when the alarm is off."""
        if not self._on("alarmEnabled", False) or not self._alarm_item():
            return 0
        try:
            hour, minute = [int(x) for x in str(self._on("alarmTime", "07:00")).split(":")[:2]]
        except ValueError:
            return 0
        days = {int(d) for d in str(self._on("alarmDays", "")).split(",")
                if d.strip().isdigit()} or set(range(1, 8))
        now = time.localtime()
        base = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, hour, minute, 0, 0, 0, -1))
        for ahead in range(0, 8):
            at = base + ahead * 86400
            when = time.localtime(at)
            # Rebuilt from the date, so a daylight-saving change is honoured.
            at = time.mktime((when.tm_year, when.tm_mon, when.tm_mday, hour, minute,
                              0, 0, 0, -1))
            if at > time.time() + 1 and time.localtime(at).tm_wday + 1 in days:
                return int(at)
        return 0

    def cmd_alarm(self, message):
        """Set what the alarm plays (the item given, or what is playing now),
        ring it now to try it, or report its state."""
        action = textutil.text(message.get("action"), 12)
        if action == "set":
            entry = protocol.clean_item(message.get("item") or self._current or {})
            if not entry:
                raise SourceError("play the station you want to wake up to first", "empty")
            self.store.state["alarm_item"] = self._keepable(entry)
            self.store.save_soon()
            self.set_setting("alarmEnabled", True)
            self._alarm_at = self._alarm_next()
            self._notice("The alarm will play %s" % entry.get("title", "it"))
        elif action == "test":
            self._ring_alarm(test=True)
        self.emit({"type": "alarm", "alarm": self._alarm_info()})
        return None

    def alarm_loop(self):
        """Ring at the set time, once per day."""
        while not self._shutting_down:
            time.sleep(10)
            try:
                self._alarm_at = self._alarm_next()
                if not self._on("alarmEnabled", False) or not self._alarm_item():
                    continue
                now = time.localtime()
                today = time.strftime("%Y-%m-%d", now)
                if self._alarm_fired_on == today:
                    continue
                days = {int(d) for d in str(self._on("alarmDays", "")).split(",")
                        if d.strip().isdigit()} or set(range(1, 8))
                if now.tm_wday + 1 not in days:
                    continue
                hour, minute = [int(x) for x in
                                str(self._on("alarmTime", "07:00")).split(":")[:2]]
                due = now.tm_hour * 60 + now.tm_min - (hour * 60 + minute)
                # A minute of grace, so a suspend across the exact minute
                # still rings when the machine wakes - but not hours late.
                if 0 <= due <= 1:
                    self._alarm_fired_on = today
                    self._ring_alarm()
            except Exception as exc:  # noqa: BLE001 - the loop must survive
                self._log("alarm: %s" % exc)

    def _ring_alarm(self, test=False):
        entry = self._alarm_item()
        if entry is None:
            return
        playing = self._current is not None and \
            self._props.get("pause") is False and not self._loading
        if playing and not test:
            return                       # already awake, already listening
        self.set_setting("volume", int(self._on("alarmVolume", 50)))
        self._fade_level = 0.0
        self._next_fade_in = float(self._on("alarmFadeSec", 60) or 0) or 1.0
        self._notice("Good morning" if not test else "Alarm test",
                     entry.get("title", ""))
        self.handle({"cmd": "play", "item": entry})

    def _sleep_stop(self):
        self._notice("Sleep timer: stopped playing")
        self.handle({"cmd": "stop"})

    def auto_update_loop(self):
        """Refresh the catalogues on the schedule set in settings.

        The original app's "Auto-update stations every N hours". Checked every
        quarter of an hour, and only ever one sync at a time.
        """
        time.sleep(90)
        while not self._shutting_down:
            try:
                if self.ensure_catalogue() is not None:
                    for source, flag, hours in (
                            ("radio", "radioAutoSync", "radioSyncHours"),
                            ("tv", "tvAutoSync", "tvSyncHours")):
                        if not self._on(flag, True) or self._on("offlineMode", False):
                            continue
                        if not self.catalogue.mirrored(source):
                            continue
                        age = self.catalogue.age("%s_full" % source)
                        limit = float(self._on(hours, 24)) * 3600
                        if age is None or age > limit:
                            self.start_sync(full=True, source=source)
                            break
            except Exception as exc:
                self._log("auto update failed: %s" % exc)
            time.sleep(15 * 60)

    QUEUE_KEPT = 1000

    def _persist_queue(self, now=False):
        """Keep the queue across a reboot - the session file lives in the
        runtime directory, which does not survive one. Debounced: a drag
        through the queue reorders it many times a second."""
        timer = getattr(self, "_queue_timer", None)
        if timer is not None:
            timer.cancel()

        def save():
            self._queue_timer = None
            try:
                start = max(0, self.index - self.QUEUE_KEPT // 4)
                self.store.library.save_queue(
                    self.queue[start:start + self.QUEUE_KEPT], self.index - start)
            except Exception as exc:  # noqa: BLE001 - never worth a crash
                self._log("queue not saved: %s" % exc)
        if now:
            if timer is not None:
                save()
            return
        self._queue_timer = threading.Timer(0.8, save)
        self._queue_timer.daemon = True
        self._queue_timer.start()

    def restore_queue(self):
        """Bring back the queue from last time, without playing it."""
        if self._current is not None or self.queue:
            return
        try:
            entries, index = self.store.library.load_queue()
        except Exception as exc:  # noqa: BLE001
            self._log("queue not restored: %s" % exc)
            return
        if not entries:
            return
        self.queue = entries
        self.index = index
        self._emit_queue()
        self.emit_state()

    def auto_play(self):
        """"Start playing on launch", with the queue from last time."""
        if not self._on("autoPlay", False) or self._current is not None:
            return
        if not self.queue:
            return
        self.handle({"cmd": "play", "items": list(self.queue), "start": self.index})

    # -- queue -------------------------------------------------------------

    def cmd_play(self, message):
        """Play an item, or a list starting at one of its entries.

        The panel sends the list the item was picked from, so next and
        previous step through the stations or tracks on screen - which is what
        a person switching radio stations actually wants from those buttons.
        """
        items = message.get("items") or ([message["item"]] if "item" in message
                                         else [])
        clean = [protocol.clean_item(i) for i in items if isinstance(i, dict)]
        clean = [c for c in clean if c and c.get("kind") not in
                 ("genre", "group", "country", "folder", "notice")]
        if not clean:
            raise SourceError("there was nothing playable in that", "empty")
        start = textutil.int_in(message.get("start"), 0, len(clean) - 1, 0)
        if len(clean) > MAX_QUEUE:
            # Keep a window around the chosen entry rather than the head of
            # the list, or picking row 700 would play row 500.
            low = max(0, min(start - MAX_QUEUE // 2, len(clean) - MAX_QUEUE))
            clean = clean[low:low + MAX_QUEUE]
            start -= low
        self.queue = clean
        self.index = start
        self._emit_queue()
        self._advance(gen=message.get("_gen"))
        return {"queue": len(self.queue)}

    def cmd_enqueue(self, message):
        items = message.get("items") or ([message["item"]] if "item" in message
                                         else [])
        clean = [c for c in (protocol.clean_item(i) for i in items
                             if isinstance(i, dict)) if c]
        if not clean:
            raise SourceError("there was nothing to add", "empty")
        unique = bool(self._on("preventDuplicates", True))
        known = {entry.get("uid") for entry in self.queue}
        for entry in clean:
            if not unique or entry["uid"] not in known:
                self.queue.append(entry)
                known.add(entry["uid"])
        del self.queue[MAX_QUEUE:]
        self._emit_queue()
        self.emit_state()
        self.emit({"type": "notice", "level": "info",
                   "title": "Added to the queue",
                   "body": textutil.text(clean[0].get("title"), 120)})
        return {"queue": len(self.queue)}

    def cmd_remove(self, message):
        position = textutil.int_in(message.get("index"), 0, len(self.queue), -1)
        if 0 <= position < len(self.queue) and position != self.index:
            del self.queue[position]
            if position < self.index:
                self.index -= 1
            self._emit_queue()
            self.emit_state()
        return {"queue": len(self.queue)}

    def cmd_clear(self, _m):
        current = self._current
        self.queue = [current] if current else []
        self.index = 0
        self._save_session()
        self._emit_queue()
        self.emit_state()
        return {"queue": len(self.queue)}

    def cmd_move(self, message):
        source = textutil.int_in(message.get("from"), 0, len(self.queue) - 1, -1)
        target = textutil.int_in(message.get("to"), 0, len(self.queue) - 1, -1)
        if 0 <= source < len(self.queue) and 0 <= target < len(self.queue):
            entry = self.queue.pop(source)
            self.queue.insert(target, entry)
            if self.index == source:
                self.index = target
            elif source < self.index <= target:
                self.index -= 1
            elif target <= self.index < source:
                self.index += 1
            self._emit_queue()
            self.emit_state()
        return {"queue": len(self.queue)}

    def _step(self, forward):
        """The index next or previous would move to, or None for nowhere."""
        count = len(self.queue)
        if count == 0:
            return None
        if self.shuffle and count > 1:
            choices = [i for i in range(count) if i != self.index]
            return random.choice(choices)
        target = self.index + (1 if forward else -1)
        if 0 <= target < count:
            return target
        if self.repeat == "all" and count > 1:
            return target % count
        return None

    def cmd_next(self, message):
        """Move to the next entry.

        This used to call _advance without moving the index at all, so "next"
        restarted whatever was already playing.
        """
        target = self._step(True)
        if target is None:
            self.emit_state()
            return None
        self.index = target
        self._advance(gen=message.get("_gen"))
        return None

    def cmd_previous(self, message):
        live = self._is_live()
        if self.player is not None and not live and self._position() > 3:
            self.player.seek(0)
            return {"position": 0}
        target = self._step(False)
        if target is None:
            if self.player is not None and not live:
                self.player.seek(0)
            return None
        self.index = target
        self._advance(gen=message.get("_gen"))
        return None

    def cmd_auto_next(self, message):
        """The current track reached its end on its own."""
        if self.repeat == "one" and self.queue:
            self._advance(gen=message.get("_gen"))
            return None
        target = self._step(True)
        if target is None:
            # The end of the list. Release the player rather than leaving an
            # idle sandbox and an item that claims to be current.
            self._teardown_session()
            self._current = None
            self._loading = False
            self._save_session()
            self.emit_state()
            return None
        self.index = target
        self._advance(gen=message.get("_gen"))
        return None

    def cmd_retry(self, message):
        """Play the current item again from the top, e.g. after an error."""
        if self.queue:
            self._advance(gen=message.get("_gen"))
        return None

    # -- audio outputs -----------------------------------------------------

    def _emit_outputs(self, ident=None):
        from . import outputs
        listing = outputs.listing(str(self._on("audioOutput", "") or ""))
        self.emit(dict(listing, type="outputs", id=ident))

    def cmd_output(self, message):
        """List this computer's outputs (and paired Bluetooth audio), or play
        through one. Only AuroraPulse's own stream moves."""
        from . import outputs
        action = textutil.text(message.get("action"), 12) or "list"
        ident = message.get("id")
        if action == "select":
            target = textutil.text(message.get("target"), 220)
            if target.startswith("bt:"):
                self._notice("Connecting %s…" % (textutil.text(message.get("label"), 80)
                                                 or "the Bluetooth device"))
                name = outputs.connect_bluetooth(target[3:])
                if not name:
                    self._emit_outputs(ident)
                    raise SourceError("could not connect that Bluetooth device; is it on "
                                      "and in range?", "network")
                target = "sink:" + name
            sink = target[5:] if target.startswith("sink:") else ""
            self.set_setting("audioOutput", sink)
            self._sink = sink or self.default_sink()
            outputs.move_to(sink, self.store.runtime)
            if self._cast is not None:
                # Choosing a speaker here brings the sound back from the TV.
                self._end_cast()
                if self.player is not None:
                    self.player.set_property("pause", False)
                self._emit_cast()
            label = next((o["description"] for o in outputs.sinks() if o["name"] == sink),
                         "the default output")
            self._notice("Playing on %s" % label)
            self.emit_state()
        self._emit_outputs(ident)
        return None

    # -- casting -----------------------------------------------------------

    CAST_MIME = {"MP3": "audio/mpeg", "AAC": "audio/aac", "AAC+": "audio/aac",
                 "OGG": "audio/ogg", "OPUS": "audio/ogg", "FLAC": "audio/flac"}

    def _cast_media(self, item):
        """(url, mime, server) a renderer can play for this item. A local file
        gets its own tiny server; YouTube is resolved to one muxed stream,
        because a TV cannot join separate audio and video like mpv can."""
        from . import cast as cast_module
        source = item.get("source")
        if source == "local":
            path = str((item.get("extra") or {}).get("path") or item.get("url") or "")
            if not os.path.isfile(path):
                raise SourceError("that file has moved or been deleted", "not-found")
            host = cast_module.device_host(self._cast_target)
            server = cast_module.FileServer(path, host)
            return server.url, server.mime, server
        if source in ("youtube", "music"):
            video = source == "youtube" and not self._on("audioOnlyYouTube", False)
            selector = ("18/best[acodec!=none][vcodec!=none][height<=720]/best" if video
                        else "bestaudio[ext=m4a]/bestaudio")
            entry = resolver._ytdlp(["-f", selector, item.get("url", "")])
            media = resolver.direct_urls(entry, video=video)
            return media["url"], "video/mp4" if video else "audio/mp4", None
        url = str(item.get("url") or "")
        if not url.startswith(("http://", "https://")):
            raise SourceError("there is nothing to cast for that", "unsupported")
        if source == "tv" or ".m3u8" in url:
            return url, "application/vnd.apple.mpegurl", None
        if source == "podcast":
            return url, (item.get("extra") or {}).get("mime") or "audio/mpeg", None
        return url, self.CAST_MIME.get(str(item.get("codec") or "").upper(), "audio/mpeg"), None

    def _emit_cast(self, ident=None, searching=False):
        from . import cast as cast_module
        self.emit({"type": "cast", "id": ident, "searching": searching,
                   "devices": [{"id": d["id"], "name": d["name"], "model": d.get("model", ""),
                                "kind": d.get("kind", "")}
                               for d in self._cast_devices.values()],
                   "active": (self._cast or {}).get("id", ""),
                   "chromecast": cast_module.chromecast_available()})

    def _end_cast(self, stop_device=True):
        active, self._cast = self._cast, None
        if not active:
            return
        if stop_device:
            try:
                active["ctl"].stop()
            except Exception:  # noqa: BLE001 - it may already be off
                pass
        if active.get("server") is not None:
            active["server"].close()

    def cmd_cast(self, message):
        """Find renderers on the network, and play, pause or stop on one."""
        from . import cast as cast_module
        action = textutil.text(message.get("action"), 12) or "discover"
        ident = message.get("id")
        if action == "discover":
            self._emit_cast(ident, searching=True)
            try:
                found = cast_module.discover(timeout=3.0)
            except OSError as exc:
                found = []
                self._notice("Could not search the network", str(exc))
            self._cast_devices = {d["id"]: d for d in found}
            self._emit_cast(ident)
            return None
        if action == "start":
            device = self._cast_devices.get(textutil.text(message.get("device"), 200))
            item = self._current or {}
            if device is None:
                raise SourceError("that device is no longer on the network", "not-found")
            if not item:
                raise SourceError("play something first, then cast it", "empty")
            self._end_cast()
            self._cast_target = device
            try:
                url, mime, server = self._cast_media(item)
            except (resolver.ResolveError, OSError) as exc:
                raise SourceError("could not get a stream to cast: %s" % exc, "network")
            try:
                ctl = cast_module.controller(device)
                art = ""
                ctl.play(url, item.get("title", ""), item.get("artist", ""), mime, art)
                ctl.volume(int(self._on("volume", 70)))
            except (cast_module.CastError, OSError) as exc:
                if server is not None:
                    server.close()
                raise SourceError("%s: %s" % (device["name"], exc), "network")
            self._cast = {"id": device["id"], "name": device["name"], "ctl": ctl,
                          "server": server, "uid": item.get("uid"), "paused": False}
            # Silence here; the sound is over there now.
            if self.player is not None:
                self.player.set_property("pause", True)
            self._notice("Casting to %s" % device["name"], item.get("title", ""))
        elif self._cast is None:
            raise SourceError("nothing is being cast", "empty")
        elif action in ("pause", "resume"):
            getattr(self._cast["ctl"], action)()
            self._cast["paused"] = action == "pause"
        elif action == "volume":
            self._cast["ctl"].volume(textutil.int_in(message.get("volume"), 0, 100, 50))
        elif action == "stop":
            name = self._cast["name"]
            self._end_cast()
            self._notice("Stopped casting to %s" % name)
        self._emit_cast(ident)
        self.emit_state()
        return None

    def cmd_toggle(self, _m):
        """Pause or resume.

        With nothing loaded - after a stop, or after a stream gave up - this
        starts the current entry again instead of doing nothing, which is what
        a play button with a title next to it promises.
        """
        if self._cast is not None:
            # While casting, the play button is the TV's play button.
            paused = bool(self._cast.get("paused"))
            self.handle({"cmd": "cast", "action": "resume" if paused else "pause"})
            return {"paused": not paused}
        player = self.player
        if self._current and self._loading and player is None:
            # Pressing the button while a stream is still connecting means
            # "never mind", not "start connecting again".
            self.handle({"cmd": "stop"})
            return {"paused": True}
        if player is None or not self._current:
            if self.queue:
                self.handle({"cmd": "retry"})
                return {"paused": False}
            return {"paused": True}
        was_paused = bool(self._props.get("pause"))
        paused = not was_paused
        self._log("toggle: player was paused=%r, writing pause=%r"
                  % (was_paused, paused))
        # Reported at once from the value asked for, and written without
        # waiting for the acknowledgement: on a network stream mpv can take
        # well over a second to answer, and waiting held every volume step
        # and seek queued behind it. mpv echoes the real value back as a
        # property change, which corrects the cache if it ever disagrees.
        self._props["pause"] = paused
        self.emit_state()
        player.set_property("pause", paused)
        return {"paused": paused}

    def cmd_play_pause(self, _m):
        return self.cmd_toggle(_m)

    def cmd_pause(self, _m):
        if self.player is not None:
            self._props["pause"] = True
            self.player.set_property("pause", True)
            self.emit_state()
        return {"paused": True}

    def cmd_resume(self, _m):
        if self.player is None:
            return self.cmd_toggle(_m)
        self._props["pause"] = False
        self.player.set_property("pause", False)
        self.emit_state()
        return {"paused": False}

    def cmd_seek(self, message):
        """Seek to `seconds`, or by `by` seconds from where we are."""
        if not self.player:
            return None
        if message.get("by") is not None:
            try:
                target = self._position() + float(message.get("by"))
            except (TypeError, ValueError):
                return None
        else:
            target = message.get("seconds")
        self.player.seek(target)
        try:
            self._props["time-pos"] = max(0.0, float(target))
        except (TypeError, ValueError):
            pass
        self.emit_state()
        return None

    def cmd_position(self, message):
        """Seek to a fraction of the duration (the progress bar)."""
        if not self.player:
            return None
        duration = self._duration()
        if duration <= 0:
            return None
        try:
            fraction = max(0.0, min(1.0, float(message.get("fraction"))))
        except (TypeError, ValueError):
            return None
        target = fraction * duration
        self.player.seek(target)
        self._props["time-pos"] = target
        self.emit_state()
        return None

    def cmd_volume(self, message):
        if message.get("set") is not None:
            value = textutil.int_in(message.get("set"), 0, 100, 70)
        elif message.get("by") is not None:
            value = textutil.int_in(
                int(self._on("volume", 70)) + int(message.get("by") or 0),
                0, 100, 70)
        else:
            return {"volume": self._on("volume", 70)}
        self.set_setting("volume", value)
        self._remember_sink_volume(value)
        if self._cast is not None:
            self.handle({"cmd": "cast", "action": "volume", "volume": value})
        if self.player:
            self.player.set_property("volume", value)
        if value > 0 and self._mute:
            # Turning the volume up is an unmute in every player people know.
            self._mute = False
            if self.player:
                self.player.set_property("mute", False)
        self.emit_state()
        return {"volume": value}

    # -- volume per output ------------------------------------------------

    @staticmethod
    def default_sink():
        """The default PipeWire output's node name, e.g. a Bluetooth speaker's
        "bluez_output.DA_F8_...", or "" when it cannot be asked."""
        if not shutil.which("wpctl"):
            return ""
        try:
            out = subprocess.run(["wpctl", "inspect", "@DEFAULT_AUDIO_SINK@"],
                                 capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.SubprocessError):
            return ""
        match = re.search(r'node\.name = "([^"]+)"', out)
        return match.group(1)[:200] if match else ""

    def _remember_sink_volume(self, value):
        sink = getattr(self, "_sink", "")
        if not sink or not self._on("rememberVolumePerDevice", True):
            return
        volumes = self.store.state.setdefault("volume_by_device", {})
        volumes.pop(sink, None)
        volumes[sink] = int(value)
        while len(volumes) > 30:
            volumes.pop(next(iter(volumes)))
        self.store.save_soon()

    def _active_sink(self):
        """The output our sound goes to: the one chosen, while it exists,
        or the default."""
        chosen = str(self._on("audioOutput", "") or "")
        if chosen:
            from . import outputs
            if any(s["name"] == chosen for s in outputs.sinks()):
                return chosen
        return self.default_sink()

    def sink_watch_loop(self):
        """Follow the output in use. Plugging in headphones or switching to
        a speaker brings back the volume last used on that output - the one
        set for speakers is rarely right for earbuds."""
        self._sink = self._active_sink()
        while not self._shutting_down:
            time.sleep(3 if self.player is not None else 8)
            if not self._on("rememberVolumePerDevice", True):
                continue
            sink = self._active_sink()
            if not sink or sink == self._sink:
                continue
            self._sink = sink
            saved = (self.store.state.get("volume_by_device") or {}).get(sink)
            if saved is None:
                # First time on this output: remember what it starts at.
                self._remember_sink_volume(int(self._on("volume", 70)))
                continue
            if int(saved) != int(self._on("volume", 70)):
                self.handle({"cmd": "volume", "set": int(saved)})
                self._notice("Volume %d%% for this output" % int(saved))

    def cmd_mute(self, message):
        # Flip the tracked flag and report that, rather than reading the
        # player back afterwards: the write may not have landed yet, and there
        # may be no player at all.
        if message.get("muted") is not None:
            self._mute = textutil.bool_of(message.get("muted"))
        else:
            self._mute = not self._mute
        if self.player:
            self.player.set_property("mute", self._mute)
        self.emit_state()
        return {"muted": self._mute}

    def cmd_rate(self, message):
        table = {0.5: 0.5, 0.75: 0.75, 1.0: 1.0, 1.25: 1.25, 1.5: 1.5,
                 1.75: 1.75, 2.0: 2.0}
        try:
            wanted = round(float(message.get("value") or 1.0), 2)
        except (TypeError, ValueError):
            wanted = 1.0
        value = table.get(wanted, 1.0)
        self.set_setting("playbackSpeed", value)
        if self.player:
            self.player.set_property("speed", value)
        self.emit_state()
        return {"speed": value}

    def cmd_repeat(self, message):
        if message.get("set") is not None:
            value = textutil.text(message.get("set"), 8)
            self.repeat = value if value in ("off", "one", "all") else "off"
        elif self.repeat == "off":
            self.repeat = "all"
        elif self.repeat == "all":
            self.repeat = "one"
        else:
            self.repeat = "off"
        self.emit_state()
        return {"repeat": self.repeat}

    def cmd_shuffle(self, message):
        if message.get("on") is not None:
            self.shuffle = textutil.bool_of(message.get("on"))
        else:
            self.shuffle = not self.shuffle
        self.emit_state()
        return {"shuffle": self.shuffle}

    def cmd_equalizer(self, message):
        preset = textutil.text(message.get("preset"), 40)
        if preset:
            self.set_setting("eqPreset", preset)
            self.set_setting("equalizerEnabled", textutil.bool_of(message.get("on"),
                                                                True))
        return {"enabled": self._on("equalizerEnabled"), "preset": self._on("eqPreset")}

    def cmd_stop(self, message):
        """Stop and release the player.

        (The position of a long track was noted on the reader thread, before
        the sound was cut, so "resume where you left off" has it.)

        The sound was already cut on the reader thread (_silence_now); this is
        the part that waits for the processes to be gone and tidies up.
        """
        if self._cast is not None:
            POOL.submit(self._end_cast)
        with self._session_lock:
            self._teardown_session()
        self._current = None
        self._loading = False
        self._error = ""
        self._save_session()
        self.emit_state()
        return {"state": "stopped"}

    def cmd_restart_player(self, message):
        resume = self._position()
        with self._session_lock:
            self._teardown_session()
        if self._current:
            self._advance(gen=message.get("_gen"), start_seconds=resume)
        return {"state": "restarted"}

    PIP_SCALE = {"s": 0.24, "m": 0.34, "l": 0.48, "xl": 0.66}

    def _monitors(self):
        out = self._hypr(["-j", "monitors"])
        try:
            import json
            monitors = [m for m in json.loads(out or "[]") if isinstance(m, dict)]
        except (ValueError, TypeError):
            return []
        return monitors

    def _monitor(self):
        """The monitor the video goes on: the one chosen in Settings if it is
        connected, otherwise the focused one - where the panel was opened."""
        monitors = self._monitors()
        if not monitors:
            return None
        wanted = str(self._on("videoMonitor", "") or "")
        return (next((m for m in monitors if wanted and m.get("name") == wanted), None)
                or next((m for m in monitors if m.get("focused")), monitors[0]))

    def _pip_geometry(self, monitor, size=None, corner=None):
        """(x, y, w, h) in layout pixels for the video window on a monitor."""
        scale = float(monitor.get("scale") or 1) or 1
        transform = int(monitor.get("transform") or 0)
        width_px = int(monitor.get("width", 1920) / scale)
        height_px = int(monitor.get("height", 1080) / scale)
        if transform % 2 == 1:          # rotated a quarter turn
            width_px, height_px = height_px, width_px
        left, top, right, bottom = (list(monitor.get("reserved") or []) + [0, 0, 0, 0])[:4]
        factor = self.PIP_SCALE.get(size or self._on("pipSize", "m"), 0.34)
        w = int(width_px * factor)
        h = int(w * 9 / 16)
        margin = 16
        corner = corner or self._on("pipCorner", "br")
        x = monitor.get("x", 0) + (left + margin if corner in ("tl", "bl")
                                   else width_px - right - w - margin)
        y = monitor.get("y", 0) + (top + margin if corner in ("tl", "tr")
                                   else height_px - bottom - h - margin)
        return x, y, w, h

    def _video_rule(self):
        """Tell Hyprland where the next video window goes, before it opens.

        A Lua window rule matched on our app id: floating, sized and in its
        corner of the chosen monitor from its first frame. Each rule gets a
        fresh name (re-using one is ignored) and the previous rule is turned
        off. Returns False where `hyprctl eval` is not there - an older
        Hyprland - and the window is then placed after it appears instead.
        """
        monitor = self._monitor()
        if not monitor or not shutil.which("hyprctl"):
            return False
        if self._on("pipFloating", True):
            x, y, w, h = self._pip_geometry(monitor)
            # A rule's move is in the monitor's own coordinates.
            x -= int(monitor.get("x", 0))
            y -= int(monitor.get("y", 0))
            effects = "float = true, size = { %d, %d }, move = { %d, %d }" % (w, h, x, y)
            if self._on("pipPinned", False):
                effects += ", pin = true"
        else:
            # Tiled, as it was left: the layout places it.
            effects = "float = false"
        self._rule_serial = getattr(self, "_rule_serial", 0) + 1
        name = re.sub(r"[^A-Za-z0-9._-]", "", str(monitor.get("name") or ""))[:40]
        lua = ('if AURORAPULSE_VIDEO_RULE then AURORAPULSE_VIDEO_RULE:set_enabled(false) end '
               'AURORAPULSE_VIDEO_RULE = hl.window_rule({ name = "aurorapulse-video-%d-%d", '
               'match = { class = "^org\\.aurorapulse\\.video$" }, %s, monitor = "%s" })'
               % (os.getpid(), self._rule_serial, effects, name))
        try:
            proc = subprocess.run(["hyprctl", "eval", lua], capture_output=True,
                                  text=True, timeout=4)
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0 and proc.stdout.strip() == "ok"

    def _place_video(self, geometry, size=None, corner=None):
        """Float the video window, size it, and snap it to a corner."""
        monitor = self._monitor()
        if not geometry or not monitor:
            return
        address = "address:%s" % geometry["address"]
        if not geometry.get("floating"):
            self._dispatch('hl.dsp.window.float({ window = "%s", action = "toggle" })' % address,
                           "togglefloating", address)
        x, y, w, h = self._pip_geometry(monitor, size, corner)
        self._dispatch('hl.dsp.window.resize({ window = "%s", x = %d, y = %d })' % (address, w, h),
                       "resizewindowpixel", "exact %d %d,%s" % (w, h, address))
        self._dispatch('hl.dsp.window.move({ window = "%s", x = %d, y = %d })' % (address, x, y),
                       "movewindowpixel", "exact %d %d,%s" % (x, y, address))

    def _place_new_window(self, session):
        """Wait for a new video window to appear and put it where it belongs."""
        for _ in range(40):
            if self.session is not session or not self._session_video:
                return
            geometry = self.video_window()
            if geometry:
                if self._on("pipFloating", True):
                    self._place_video(geometry)
                    if self._on("pipPinned", False) and not geometry.get("pinned"):
                        address = "address:%s" % geometry["address"]
                        self._dispatch('hl.dsp.window.pin({ window = "%s" })' % address,
                                       "pin", address)
                self._emit_video_window()
                return
            time.sleep(0.25)

    def cmd_pip(self, message):
        """The video window: float, size, corner, fullscreen, pin, close."""
        action = textutil.text(message.get("action"), 20)
        geometry = self.video_window()
        if not geometry:
            return self.cmd_video_window({})
        address = "address:%s" % geometry["address"]
        if action in ("float", "unfloat"):
            want = action == "float"
            self.set_setting("pipFloating", want)
            if bool(geometry.get("floating")) != want:
                self._dispatch('hl.dsp.window.float({ window = "%s", action = "toggle" })'
                               % address, "togglefloating", address)
            if want:
                self._place_video(dict(geometry, floating=True))
        elif action == "size":
            size = textutil.text(message.get("size"), 4)
            if size in self.PIP_SCALE:
                self.set_setting("pipSize", size)
                self.set_setting("pipFloating", True)
                self._place_video(geometry, size=size)
        elif action == "corner":
            corner = textutil.text(message.get("corner"), 4)
            if corner in ("tl", "tr", "bl", "br"):
                self.set_setting("pipCorner", corner)
                self.set_setting("pipFloating", True)
                self._place_video(geometry, corner=corner)
        elif action == "fullscreen":
            self._dispatch('hl.dsp.focus({ window = "%s" })' % address, "focuswindow", address)
            self._dispatch('hl.dsp.window.fullscreen({ window = "%s", mode = "fullscreen" })'
                           % address, "fullscreen", "0")
        elif action == "pin":
            self.set_setting("pipPinned", not geometry.get("pinned"))
            if not geometry.get("floating"):
                self.set_setting("pipFloating", True)
                self._place_video(geometry)
            self._dispatch('hl.dsp.window.pin({ window = "%s" })' % address, "pin", address)
        elif action == "focus":
            self._dispatch('hl.dsp.focus({ window = "%s" })' % address, "focuswindow", address)
        elif action == "close":
            self._dispatch('hl.dsp.window.close({ window = "%s" })' % address,
                           "closewindow", address)
        if action in ("float", "unfloat", "size", "corner", "pin"):
            self.emit({"type": "settings", "settings": self.settings()})
        return self._emit_video_window()

    def cmd_video_window(self, _m):
        return self._emit_video_window(learn=True)

    def _emit_video_window(self, learn=False):
        """Tell the panel about the video window. With learn, also remember
        whether it floats and is pinned, however it got that way - the buttons
        here or Hyprland's own keys - so the next window opens the same. Not
        straight after we changed it ourselves: the compositor may not have
        caught up, and the setting was saved already."""
        geometry = self.video_window()
        if learn and geometry:
            self.set_setting("pipFloating", bool(geometry.get("floating")))
            if geometry.get("floating"):
                self.set_setting("pipPinned", bool(geometry.get("pinned")))
        self.emit({"type": "pip", "window": geometry,
                   "monitors": [str(m.get("name") or "") for m in self._monitors()],
                   "floating": bool(geometry and geometry.get("floating")),
                   "pinned": bool(geometry and geometry.get("pinned")),
                   "size": self._on("pipSize", "m"), "corner": self._on("pipCorner", "br")})
        return None

    def cmd_shutdown(self, _m):
        """Stop playing and exit: the panel's power button.

        This raised SystemExit on a worker thread, which ends that thread and
        nothing else - the daemon carried on, so "off" never meant off.
        """
        self._shutting_down = True
        with self._session_lock:
            self._teardown_session()
        self._current = None
        self._save_session()
        try:
            self.store.flush()
        except Exception:
            pass
        if self.syncer is not None:
            try:
                self.syncer.shutdown(timeout=2.0)
            except Exception:
                pass
        if self.artwork is not None:
            self.artwork.shutdown(timeout=0.5)
        self.emit({"type": "bye"})
        try:
            sys.stdout.flush()
        except Exception:
            pass
        os._exit(0)

    # -- state -------------------------------------------------------------

    def _position(self):
        try:
            return float(self._props.get("time-pos") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _duration(self):
        try:
            return float(self._props.get("duration") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _is_live(self):
        current = self._current or {}
        if current.get("source") in ("radio", "tv"):
            return True
        return bool(current.get("is_live"))

    def _stream_title(self):
        """What a radio station says is on now, from its ICY metadata."""
        meta = self._props.get("metadata")
        if not isinstance(meta, dict):
            return ""
        for key in ("icy-title", "ICY-TITLE", "title", "TITLE"):
            value = meta.get(key)
            if value:
                return textutil.text(value, 300)
        return ""

    def _mode(self):
        if not self._current:
            return "off"
        if self._error:
            return "error"
        if self.player is None or self._loading:
            return "loading"
        if self._props.get("pause"):
            return "paused"
        if self._props.get("paused-for-cache") or self._props.get("idle-active"):
            return "loading"
        return "playing"

    def _with_art(self, item):
        """The item with its artwork path filled in if the image is cached.

        Items are saved when they are queued, often before their artwork has
        downloaded, so the now-playing tile showed initials for a track whose
        picture was sitting in the cache.
        """
        art = item.get("art") if isinstance(item.get("art"), dict) else {}
        if not item or art.get("path") or getattr(self, "artwork", None) is None:
            return item
        key = art.get("key")
        video = (item.get("extra") or {}).get("video_id")
        if not key and video and item.get("source") in ("youtube", "music"):
            # Queued before thumbnails had a fallback: fetch it now, and the
            # art event repaints the tile when it lands.
            key = "yt-%s" % video
            self.artwork.submit(key, youtube.thumbnail_url(video),
                                textutil.slug(key, 120))
            art = dict(art, key=key)
            item = dict(item, art=art)
            if self._current is not None and self._current.get("uid") == item.get("uid"):
                self._current = item
        if not key:
            return item
        path = self.artwork.path_for(textutil.slug(key, 120))
        if not os.path.exists(path):
            return item
        copy = dict(item)
        copy["art"] = dict(art, path=path)
        if self._current is item:
            self._current = copy
        return copy

    def emit_state(self):
        """Report what is playing. Never blocks: it reads only the cache."""
        current = self._with_art(self._current or {})
        mode = self._mode()
        count = len(self.queue)
        live = self._is_live()
        self._last_position_emit = time.monotonic()
        state = {
            "type": "state",
            "source": current.get("source", ""),
            "item": current,
            "mode": mode,
            "paused": mode in ("off", "paused", "error"),
            "buffering": mode == "loading",
            "position": round(self._position(), 2),
            "duration": round(self._duration(), 2),
            "live": live,
            "volume": self._on("volume", 70),
            "muted": self._mute,
            "speed": float(self._on("playbackSpeed", 1.0) or 1.0),
            "index": self.index,
            "queueLength": count,
            "hasNext": self._step_possible(True),
            "hasPrev": self._step_possible(False) or (bool(current) and not live),
            "repeat": self.repeat,
            "shuffle": self.shuffle,
            "has_video": bool(self._session_video and self.player is not None
                              and current),
            "now_playing": current.get("title", ""),
            "artist": current.get("artist", ""),
            "streamTitle": self._stream_title() if live else "",
            "error": self._error,
            "sleepAt": getattr(self, "_sleep_at", 0),
            "casting": (self._cast or {}).get("name", "") if getattr(self, "_cast", None) else "",
            "castPaused": bool((getattr(self, "_cast", None) or {}).get("paused")),
            "alarmAt": getattr(self, "_alarm_at", 0),
            "favorite": self._is_favorite(current.get("uid")) if current else False,
            "recording": (getattr(self, "_recording", None) or {}).get("since", 0),
        }
        self.emit(state)
        mpris = getattr(self, "_mpris", None)
        if mpris is not None:
            mpris.update(state)
        scrobbler = getattr(self, "_scrobbler", None)
        if scrobbler is not None:
            try:
                scrobbler.update(state)
            except Exception as exc:  # noqa: BLE001 - never at the cost of playback
                self._log("scrobbler: %s" % exc)

    def _step_possible(self, forward):
        count = len(self.queue)
        if count < 2:
            return False
        if self.shuffle or self.repeat == "all":
            return True
        target = self.index + (1 if forward else -1)
        return 0 <= target < count

    def _emit_queue(self):
        """The queue, sent only when it changes - never inside every state."""
        if getattr(self, "store", None) is not None:
            self._persist_queue()
        self.emit({
            "type": "queue",
            "index": self.index,
            "items": [{"uid": entry.get("uid", ""),
                       "title": entry.get("title", ""),
                       "artist": entry.get("artist", ""),
                       "album": entry.get("album", ""),
                       "source": entry.get("source", ""),
                       "kind": entry.get("kind", ""),
                       "duration": entry.get("duration", 0),
                       "is_live": entry.get("is_live", False),
                       "art": entry.get("art") or {},
                       "extra": {k: v for k, v in (entry.get("extra") or {}).items()
                                 if k in ("country", "group", "video_id")}}
                      for entry in self.queue],
        })

    def video_window(self):
        """Ask Hyprland whether one of *our* mpv windows is on screen.

        Every video window we spawn is titled with TITLE_PREFIX, so matching
        on that marker is what keeps this from grabbing somebody else's
        media window. Only asked for on a pip action, never per state.
        """
        out = self._hypr(["-j", "clients"])
        if not out:
            return None
        try:
            import json
            for client in json.loads(out):
                if not isinstance(client, dict):
                    continue
                if client.get("class", "").lower() not in ("mpv", VIDEO_APP_ID):
                    continue
                if not str(client.get("title", "")).startswith(TITLE_PREFIX):
                    continue
                at = client.get("at") or [0, 0]
                size = client.get("size") or [0, 0]
                return {
                    "address": textutil.text(client.get("address"), 32),
                    "title": textutil.text(client.get("title"), 200),
                    "x": int(at[0]), "y": int(at[1]),
                    "w": int(size[0]), "h": int(size[1]),
                    "floating": bool(client.get("floating")),
                    "pinned": bool(client.get("pinned")),
                    "fullscreen": bool(client.get("fullscreen")),
                }
        except (ValueError, IndexError, TypeError):
            return None
        return None

    def _dispatch(self, lua, *legacy):
        """One Hyprland dispatch, in the Lua form first.

        Hyprland 0.56 reads `hyprctl dispatch` as Lua, so the classic
        "resizewindowpixel exact 640 360,address:…" is a syntax error there -
        which is why the video window would not move or resize. Older
        Hyprlands still take the classic form, so it is the fallback.
        """
        if not shutil.which("hyprctl"):
            return False
        try:
            proc = subprocess.run(["hyprctl", "dispatch", lua], capture_output=True,
                                  timeout=4, text=True)
            if proc.returncode == 0 and not proc.stdout.lstrip().startswith("error"):
                return True
            if legacy:
                proc = subprocess.run(["hyprctl", "dispatch", *legacy],
                                      capture_output=True, timeout=4, text=True)
                return proc.returncode == 0 and proc.stdout.strip() == "ok"
        except (OSError, subprocess.SubprocessError):
            pass
        return False

    def _hypr(self, argv):
        if not shutil.which("hyprctl"):
            return None
        try:
            proc = subprocess.run(["hyprctl", *argv], capture_output=True,
                                  timeout=4, text=True)
        except (OSError, subprocess.SubprocessError):
            return None
        return proc.stdout if proc.returncode == 0 else None

    # -- playback ----------------------------------------------------------

    def _advance(self, auto=False, start_seconds=0.0, gen=None):
        """Start whatever self.index points at."""
        if not self.queue:
            return
        self._remember_position()
        self._finish_recording()
        if not auto and not self._next_fade_in:
            # Chosen by hand: no fade carried over from the last track.
            self._cancel_fade()
        self.index = max(0, min(self.index, len(self.queue) - 1))
        candidate = self.queue[self.index]

        # Say what is coming before doing any of the slow work, so a click
        # shows the new title and a "connecting" state at once rather than
        # leaving the old station on screen for the seconds a resolve takes.
        if not start_seconds:
            start_seconds = self._resume_point(candidate)
        self._current = candidate
        self._error = ""
        self._loading = True
        self._candidates = []
        self._reconnects = 0
        self._props.pop("time-pos", None)
        self._props.pop("duration", None)
        self._props.pop("metadata", None)
        self._emit_queue()
        self.emit_state()

        want_video = self._wants_video(candidate)
        try:
            media = self._resolve(candidate, want_video)
        except (SourceError, resolver.ResolveError) as exc:
            if self._superseded(gen):
                return
            self._loading = False
            self._error = textutil.text(str(exc), 200)
            self.emit(protocol.error("%s: %s" % (candidate.get("title", "That"),
                                                 exc),
                                     getattr(exc, "reason", "network"),
                                     candidate.get("uid")))
            self.emit_state()
            return

        if self._superseded(gen):
            return
        self._sub_options = self._subtitle_options(candidate, media)
        self._subs_loaded = {}
        self._save_session()
        self._persist_queue()
        try:
            with self._session_lock:
                if self._superseded(gen):
                    return
                self._start_session(media, want_video, start_seconds, gen)
        except SourceError as exc:
            self._loading = False
            self._error = textutil.text(str(exc), 200)
            self.emit(protocol.error(str(exc), exc.reason, candidate.get("uid")))
        self.emit_state()

    AUDIO_SUFFIXES = (".mp3", ".m4a", ".flac", ".ogg", ".oga", ".opus", ".wav",
                      ".aac", ".alac", ".mka", ".wv", ".aiff", ".wma", ".aif")

    def _wants_video(self, item):
        """Whether this item needs a video window."""
        source = item.get("source")
        if source == "tv":
            return True
        if source == "radio":
            return False
        if source == "podcast":
            return str((item.get("extra") or {}).get("mime", "")).startswith("video/")
        if source == "local":
            path = str((item.get("extra") or {}).get("path") or "")
            return bool(path) and not path.lower().endswith(self.AUDIO_SUFFIXES)
        # YouTube Music is for listening; the YouTube tab is for watching.
        # This used to follow "Video quality", whose default was "audio", so
        # every YouTube video played as sound only.
        if source == "music" or self._on("audioOnlyYouTube", False):
            return False
        return self._on("defaultQuality", "720p") != "audio"

    def _resolve(self, item, want_video):
        """Turn an item into a concrete media URL, using the cache when we
        can. Direct YouTube URLs expire in hours, so the cache is short."""
        uid = item.get("uid") or ""
        cached = self._resolved.get((uid, want_video))
        if cached and time.time() - cached[0] < 1800:
            return cached[1]

        if item.get("source") == "local":
            path = (item.get("extra") or {}).get("path") or item.get("url")
            if not path or not os.path.isfile(path):
                raise SourceError("that file has moved or been deleted",
                                  "not-found")
            # A local video has to say it has a picture, or the session is
            # started audio-only - which is how a downloaded video ended up
            # playing as sound with no window.
            return {"url": path, "codec": item.get("codec", ""),
                    "bitrate": int(item.get("bitrate") or 0),
                    "height": 1 if self._wants_video(item) else 0,
                    "is_live": False}

        if item.get("source") == "podcast":
            url = item.get("url") or ""
            if not url.startswith(("http://", "https://")):
                raise SourceError("that episode has no audio address", "unsupported")
            return {"url": url, "codec": "", "bitrate": 0,
                    "height": 1 if self._wants_video(item) else 0,
                    "is_live": False}

        if item.get("source") in ("radio", "tv"):
            url = item.get("url") or ""
            if not url.startswith(("http://", "https://")):
                raise SourceError("that station has no playable address",
                                  "unsupported")
            return {"url": url, "codec": item.get("codec", ""),
                    "bitrate": int(item.get("bitrate") or 0), "height": 0,
                    "is_live": True}

        if item.get("source") in ("youtube", "music"):
            budget = youtube.Budget(self.store,
                                    self._on("ytResolutionBudgetPerHour", 120))
            media = youtube.resolve_for_play(
                item, want_video, self._on("defaultQuality", "720p"), budget,
                self.store.art_dir)
            if len(self._resolved) > 200:
                self._resolved.clear()
            self._resolved[(uid, want_video)] = (time.time(), media)
            return media

        raise SourceError("nothing knows how to play a %s" % item.get("source"),
                          "unsupported")

    def _prefetch_next(self):
        """Look up the next YouTube entry while this one plays.

        Resolving is the slow part of a YouTube skip - several seconds of
        yt-dlp - so doing it ahead of time makes "next" as quick as a radio
        station change. Only the one entry after this, and only for YouTube,
        so the hourly lookup budget is not spent on a whole playlist.
        """
        current = self._current or {}
        if current.get("source") not in ("youtube", "music"):
            return
        if self.shuffle or len(self.queue) < 2:
            return
        target = self._step(True)
        if target is None:
            return
        upcoming = self.queue[target]
        if upcoming.get("source") not in ("youtube", "music"):
            return
        want_video = self._wants_video(upcoming)
        key = (upcoming.get("uid") or "", want_video)
        if key in self._resolved:
            return

        def work():
            try:
                self._resolve(upcoming, want_video)
            except Exception as exc:
                self._log("prefetch failed: %s" % exc)
        POOL.submit(work)

    def _load(self, url, start_seconds=0.0, audio_url=""):
        """Load `url` into the attached player and arm the start watchdog.

        `audio_url` is a separate audio stream for a video that comes as two
        (YouTube does, above 360p). It is attached as an external track.
        """
        player = self.player
        if player is None:
            return False
        self._load_token += 1
        token = self._load_token
        self._entry_id = None
        self._loading = True
        options = []
        if audio_url:
            # mpv's per-file option list; the %len% form keeps commas and
            # other separators in the URL from being read as syntax.
            options.append("audio-file=%%%d%%%s" % (len(audio_url), audio_url))
        if start_seconds > 5:
            # A start point travels with the file. A seek sent straight after
            # loadfile lands before the file is open and is dropped.
            options.append("start=%d" % int(start_seconds))
        if self._session_video and not self._on("subtitlesEnabled", False):
            # Otherwise mpv shows a track the file marks as default until the
            # subtitle setting is applied, a moment after playback starts.
            options.append("sid=no")
        if options:
            ok, data = player.request("loadfile", url, "replace", -1,
                                      ",".join(options))
        else:
            ok, data = player.request("loadfile", url, "replace")
        if not ok:
            return False
        if isinstance(data, dict) and data.get("playlist_entry_id") is not None:
            self._entry_id = data.get("playlist_entry_id")
        timeout = START_TIMEOUT_VIDEO if self._session_video else START_TIMEOUT_AUDIO
        timer = threading.Timer(timeout, self._start_watchdog, args=(token,))
        timer.daemon = True
        timer.start()
        return True

    def _start_watchdog(self, token):
        """A stream that has not made a sound by now is not going to.

        Dead links do not always fail loudly: mpv can sit connecting, or
        receive a page of HTML, without ever reporting an error. Without this,
        that looked like a station that loads forever.
        """
        if token != self._load_token or self._started_token == token:
            return
        if not self._current or self.player is None:
            return
        if self._props.get("pause"):
            return          # paused while connecting: not a dead link
        self._log("no sound after the start timeout; giving up on that link")
        self._link_failed("it did not start in time")

    def _apply_levels(self, want_video):
        player = self.player
        if player is None:
            return
        player.set_property("volume", int(self._on("volume", 70)))
        if self._on("tvOpenDefaultMuted", False) and want_video:
            self._mute = True
        player.set_property("mute", self._mute)
        speed = float(self._on("playbackSpeed", 1.0) or 1.0)
        if speed != 1.0 and not self._is_live():
            player.set_property("speed", speed)
        self._apply_filters()

    def _apply_filters(self, force=True):
        """Fade stage, skip-silence, equalizer and loudness, on the running
        player. Without `force` it is left alone when nothing changed: every
        `af set` re-initialises the audio, which is an audible blip."""
        player = self.player
        if player is None:
            return
        chain = audio_filter_chain(bool(self._on("equalizerEnabled", False)),
                                   self._on("eqPreset", "Flat"),
                                   bool(self._on("normalizeVolume", False)),
                                   self._on("eqBands", ""),
                                   bool(self._on("skipSilence", False))
                                   and self._current is not None and not self._is_live())
        if not force and chain == getattr(self, "_af_chain", None) \
                and player is getattr(self, "_af_player", None):
            return
        self._af_chain, self._af_player = chain, player
        full = fade_filter(self._fade_level) + ("," + chain if chain else "")
        player.command("af", "set", full)

    # -- subtitles ---------------------------------------------------------

    SUB_SUFFIXES = (".srt", ".ass", ".ssa", ".vtt", ".sub")
    SUB_LIMIT = 4 << 20

    def _subtitle_options(self, item, media):
        """Subtitle files that can be added to this video: the ones YouTube
        offers, or files next to a local video named like it
        ("Film.srt", "Film.en.srt")."""
        if item.get("source") == "local":
            path = str((item.get("extra") or {}).get("path") or item.get("url") or "")
            folder, name = os.path.split(path)
            stem = os.path.splitext(name)[0]
            out = []
            try:
                names = sorted(os.listdir(folder)) if folder else []
            except OSError:
                names = []
            for other in names:
                base, ext = os.path.splitext(other)
                if ext.lower() not in self.SUB_SUFFIXES or not base.startswith(stem):
                    continue
                lang = base[len(stem):].strip(" ._-")[:12]
                out.append({"lang": lang, "name": lang or other[:60], "auto": False,
                            "path": os.path.join(folder, other)})
            return out[:20]
        return [dict(o) for o in (media or {}).get("subtitles") or []][:40]

    def _sub_key(self, n):
        return "ext:%d" % n

    def _load_subtitle(self, n, select=True):
        """Put option n where the player can read it, and add it."""
        options = self._sub_options
        if not 0 <= n < len(options) or self.player is None:
            return False
        key = self._sub_key(n)
        option = options[n]
        if key not in self._subs_loaded:
            if option.get("path"):
                try:
                    if os.path.getsize(option["path"]) > self.SUB_LIMIT:
                        return False
                    with open(option["path"], "rb") as handle:
                        body = handle.read(self.SUB_LIMIT)
                except OSError:
                    return False
                ext = os.path.splitext(option["path"])[1].lower()
            else:
                try:
                    body = base.fetch(option.get("url", ""), limit=self.SUB_LIMIT, seconds=15)
                except SourceError as exc:
                    self._notice("Could not load the subtitles", str(exc))
                    return False
                ext = ".vtt"
            folder = os.path.join(self.store.runtime, "subs")
            os.makedirs(folder, mode=0o700, exist_ok=True)
            name = "sub-%d-%s%s" % (self._load_token, re.sub(r"[^a-z0-9-]", "",
                                                             option.get("lang", "").lower())
                                     or "x", ext)
            with open(os.path.join(folder, name), "wb") as handle:
                handle.write(body)
            from .session import SANDBOX_RUNTIME
            self.player.command("sub-add", SANDBOX_RUNTIME + "/subs/" + name,
                                "select" if select else "auto",
                                option.get("name", "")[:60], option.get("lang", "")[:12])
            self._subs_loaded[key] = name
        elif select:
            track = self._track_for(self._subs_loaded[key])
            if track is not None:
                self.player.set_property("sid", track)
        return True

    def _sub_tracks(self):
        player = self.player
        if player is None:
            return []
        ok, tracks = player.request("get_property", "track-list")
        if not ok or not isinstance(tracks, list):
            return []
        return [t for t in tracks if isinstance(t, dict) and t.get("type") == "sub"]

    def _track_for(self, name):
        for track in self._sub_tracks():
            if str(track.get("external-filename") or "").endswith("/" + name):
                return track.get("id")
        return None

    def _apply_subtitles(self):
        """Size, and on or off as Settings say. With subtitles on, a track in
        the preferred language is chosen: one inside the file first, else one
        we can add."""
        player = self.player
        if player is None or not self._session_video:
            return
        player.set_property("sub-scale", float(self._on("subtitleSize", 1.0) or 1.0))
        if not self._on("subtitlesEnabled", False):
            player.set_property("sid", "no")
            self._emit_subtitles()
            return
        wanted = str(self._on("subtitleLanguage", "en") or "").lower()[:12]
        tracks = self._sub_tracks()
        inside = next((t for t in tracks if str(t.get("lang") or "").lower()
                       .startswith(wanted)), None) if wanted else None
        if inside is not None:
            player.set_property("sid", inside.get("id"))
        elif self._sub_options:
            n = next((i for i, o in enumerate(self._sub_options)
                      if wanted and str(o.get("lang", "")).lower().startswith(wanted)
                      and not o.get("auto")), None)
            if n is None:
                n = next((i for i, o in enumerate(self._sub_options)
                          if wanted and str(o.get("lang", "")).lower().startswith(wanted)), None)
            if n is None and not tracks:
                n = 0 if not self._sub_options[0].get("auto") else None
            if n is not None:
                self._load_subtitle(n)
        elif tracks:
            player.set_property("sid", tracks[0].get("id"))
        self._emit_subtitles()

    def _emit_subtitles(self, ident=None):
        tracks = self._sub_tracks()
        loaded = {name: key for key, name in self._subs_loaded.items()}
        choices, selected = [], ""
        for track in tracks:
            filename = os.path.basename(str(track.get("external-filename") or ""))
            key = loaded.get(filename) or "mpv:%s" % track.get("id")
            label = track.get("title") or track.get("lang") or "Track %s" % track.get("id")
            choices.append({"key": key, "label": textutil.text(label, 60),
                            "lang": textutil.text(track.get("lang") or "", 12)})
            if track.get("selected"):
                selected = key
        for n, option in enumerate(self._sub_options):
            if self._sub_key(n) not in self._subs_loaded:
                choices.append({"key": self._sub_key(n),
                                "label": textutil.text(option.get("name") or option.get("lang")
                                                       or "Subtitles", 60),
                                "lang": textutil.text(option.get("lang", ""), 12)})
        self.emit({"type": "subtitles", "id": ident, "choices": choices,
                   "selected": selected,
                   "size": float(self._on("subtitleSize", 1.0) or 1.0)})

    def cmd_subtitle(self, message):
        """List, choose or turn off subtitles for the video playing."""
        action = textutil.text(message.get("action"), 12) or "list"
        player = self.player
        if action == "off":
            # Remembered: the next video starts without them too.
            self.set_setting("subtitlesEnabled", False, apply=False)
            if player is not None:
                player.set_property("sid", "no")
            self.emit({"type": "settings", "settings": self.settings()})
        elif action == "select":
            key = textutil.text(message.get("key"), 20)
            lang = ""
            if key.startswith("mpv:") and player is not None:
                try:
                    track = int(key[4:])
                except ValueError:
                    track = None
                if track is not None:
                    player.set_property("sid", track)
                    lang = next((str(t.get("lang") or "") for t in self._sub_tracks()
                                 if t.get("id") == track), "")
            elif key.startswith("ext:"):
                try:
                    n = int(key[4:])
                except ValueError:
                    n = -1
                if 0 <= n < len(self._sub_options):
                    lang = str(self._sub_options[n].get("lang") or "")
            # On for the next video too, in this track's language. Saved
            # without re-applying, which would pick by language and could
            # swap the track just chosen for another one.
            self.set_setting("subtitlesEnabled", True, apply=False)
            if lang:
                self.set_setting("subtitleLanguage", lang.lower()[:12], apply=False)
            self.emit({"type": "settings", "settings": self.settings()})
            if key.startswith("ext:"):
                # The fetch can take a moment; do it off the control lane.
                POOL.submit(lambda: (self._load_subtitle(n), time.sleep(0.3),
                                     self._emit_subtitles()))
                return None
        elif action == "size":
            self.set_setting("subtitleSize", message.get("size"), apply=False)
            if player is not None:
                player.set_property("sub-scale", float(self._on("subtitleSize", 1.0)))
        time.sleep(0.15)
        self._emit_subtitles(message.get("id"))
        return None

    # -- picture quality -----------------------------------------------------

    def _variants(self):
        """The renditions an adaptive (HLS) stream offers, best first.

        mpv lists each rendition as its own video and audio track, tagged
        with the rendition's program and its playlist bitrate, and switches
        between them live when both tracks of another program are chosen.
        Only tracks carrying an HLS bitrate count: an MPEG-TS broadcast can
        hold several programs too, and those are different channels."""
        player = self.player
        if player is None or not self._session_video:
            return []
        ok, tracks = player.request("get_property", "track-list")
        if not ok or not isinstance(tracks, list):
            return []
        programs = {}
        for track in tracks:
            if (not isinstance(track, dict) or track.get("program-id") is None
                    or track.get("hls-bitrate") is None):
                continue
            entry = programs.setdefault(track.get("program-id"),
                                        {"program": track.get("program-id")})
            if track.get("type") == "video" and "video" not in entry:
                entry.update(video=track.get("id"),
                             height=textutil.int_in(track.get("demux-h"), 0, 10000, 0),
                             bitrate=textutil.int_in(track.get("hls-bitrate"), 0, 1 << 31, 0),
                             selected=bool(track.get("selected")))
            elif track.get("type") == "audio" and "audio" not in entry:
                entry["audio"] = track.get("id")
        variants = [v for v in programs.values() if "video" in v]
        if len(variants) < 2:
            return []
        variants.sort(key=lambda v: (v["height"], v["bitrate"]), reverse=True)
        return variants

    @staticmethod
    def _quality_tier(height):
        if not height:
            return ""
        # Anything sharper than PAL's 576 lines is HD: streams come in
        # 684p and 640p as well as 720p.
        return "4K" if height >= 2000 else "FHD" if height >= 1000 \
            else "HD" if height > 576 else "SD"

    @staticmethod
    def _pick_variant(variants, preference):
        """The best rendition no taller than the preference; the smallest
        one when every rendition is taller."""
        if not str(preference).isdigit():
            return variants[0]
        limit = int(preference)
        fitting = [v for v in variants if v["height"] and v["height"] <= limit]
        return fitting[0] if fitting else variants[-1]

    def _select_variant(self, variant):
        player = self.player
        if player is None:
            return
        player.set_property("vid", variant["video"])
        if variant.get("audio") is not None:
            player.set_property("aid", variant["audio"])

    def _apply_quality(self):
        """Put the stream on the rendition Settings prefer. A rendition of
        the same height is left alone, so choosing one by hand and saving
        that height as the preference does not switch it again."""
        variants = self._variants()
        if variants:
            chosen = self._pick_variant(variants, self._on("tvQuality", "best"))
            current = next((v for v in variants if v["selected"]), None)
            if current is None or (chosen["height"] or chosen["bitrate"]) != \
                    (current["height"] or current["bitrate"]):
                self._select_variant(chosen)
                time.sleep(0.3)
        self._emit_quality()

    def _emit_quality(self, ident=None):
        variants = self._variants()
        choices, selected, short = [], "", ""
        for v in variants:
            key = "p:%s" % v["program"]
            size = ("%dp" % v["height"]) if v["height"] else ""
            rate = ("%.1f Mbps" % (v["bitrate"] / 1e6)) if v["bitrate"] else ""
            label = " · ".join(x for x in (self._quality_tier(v["height"]), size, rate) if x)
            choices.append({"key": key, "label": textutil.text(label or key, 40)})
            if v["selected"]:
                selected = key
                short = self._quality_tier(v["height"]) or "Q"
        self.emit({"type": "quality", "id": ident, "choices": choices,
                   "selected": selected, "short": short,
                   "preference": str(self._on("tvQuality", "best") or "best")})

    def cmd_quality(self, message):
        """List or choose the picture quality of the stream playing. The
        choice is remembered as a height, so the next channel opens at the
        nearest rendition that is no taller; the top one means "best"."""
        action = textutil.text(message.get("action"), 12) or "list"
        variants = self._variants()
        if action == "best" and variants:
            self.set_setting("tvQuality", "best")
            self._select_variant(variants[0])
        elif action == "select":
            key = textutil.text(message.get("key"), 20)
            for n, v in enumerate(variants):
                if "p:%s" % v["program"] == key:
                    self._select_variant(v)
                    self.set_setting("tvQuality", "best" if n == 0 or not v["height"]
                                     else str(v["height"]))
                    break
        if action in ("best", "select"):
            time.sleep(0.3)
        self._emit_quality(message.get("id"))
        return None

    ASPECTS = {"16:9": "16:9", "4:3": "4:3", "21:9": "2.33", "1:1": "1"}

    def _apply_aspect(self):
        """The video picture's shape: as the stream says, forced to a ratio,
        or "fill", which crops to the window instead of letterboxing."""
        player = self.player
        if player is None or not self._session_video:
            return
        aspect = str(self._on("defaultAspect", "auto") or "auto")
        player.set_property("video-aspect-override", self.ASPECTS.get(aspect, "-1"))
        player.set_property("panscan", 1.0 if aspect == "fill" else 0.0)

    # -- fades -------------------------------------------------------------

    def _set_fade(self, level):
        self._fade_level = max(0.0, min(1.0, level))
        player = self.player
        if player is not None:
            player.command("af-command", FADE_LABEL, "volume",
                           "%.3f" % self._fade_level, "volume")

    def _ramp(self, to, seconds, then=None):
        """Move the fade stage to `to` over `seconds`, then call `then`.

        A newer ramp, a stop or a manual track change cancels this one.
        """
        self._ramp_gen += 1
        gen = self._ramp_gen
        start = self._fade_level

        def run():
            steps = max(1, int(seconds * 20))
            for n in range(1, steps + 1):
                if self._ramp_gen != gen:
                    return
                t = n / steps
                # Equal-power-ish curve: a linear gain ramp sounds as if it
                # drops off a cliff at the end.
                eased = t * t if to < start else 1 - (1 - t) * (1 - t)
                self._set_fade(start + (to - start) * eased)
                time.sleep(seconds / steps)
            if self._ramp_gen == gen and then is not None:
                then()
        threading.Thread(target=run, name="ap-fade", daemon=True).start()

    def _cancel_fade(self):
        """Back to full level at once, for a stop or a track the user chose."""
        self._ramp_gen += 1
        self._fading_out = False
        if self._fade_level < 1.0:
            self._set_fade(1.0)

    def _maybe_fade_out(self, position):
        """Called with each position: start fading out the last seconds of a
        track when "fade between tracks" is on and another track follows."""
        seconds = float(self._on("crossfadeSec", 0.0) or 0.0)
        if seconds <= 0 or self._fading_out or self._is_live() or self._loading:
            return
        duration = self._duration()
        if duration < seconds * 3 or position is None:
            return
        remaining = duration - float(position)
        if remaining > seconds or remaining <= 0.2:
            return
        if self.repeat != "one" and not self._step_possible(True):
            return
        self._fading_out = True
        self._ramp(0.0, remaining)

    def _fade_in_if_faded(self):
        """A new track has started: bring the level back up if the previous
        one faded out (or the alarm wants a gentle start)."""
        self._fading_out = False
        wanted, self._next_fade_in = self._next_fade_in, 0.0
        if self._fade_level >= 1.0:
            return
        seconds = wanted or float(self._on("crossfadeSec", 0.0) or 0.0) or 1.0
        self._ramp(1.0, max(0.5, seconds))

    def _reuse_running(self, media, want_video, start_seconds):
        """Hand the new thing to the player that is already running.

        Only when the shape matches. An audio session cannot grow a video
        window and a video session cannot be reduced to one, and a local file
        is bound into the sandbox by path, so the running session cannot see a
        new one. Returns True when the running player took it.
        """
        if self.player is None or self.session is None:
            return False
        if getattr(self, "_session_video", None) != want_video:
            return False
        url = media.get("url")
        if not url or not url.startswith(("http://", "https://")):
            return False
        if getattr(self.player, "sock", True) is None:
            return False
        try:
            if not self._load(url, start_seconds, media.get("audio_url") or ""):
                return False
            self._apply_levels(want_video)
        except Exception:
            self._log("could not reuse the running player; starting a new one")
            return False
        self._log("handed the new item to the running player")
        return True

    def _start_session(self, media, want_video, start_seconds, gen=None):
        # A TV stream reports no height before it is probed, so the source is
        # the authority: TV always wants a window, and a YouTube entry only
        # when the resolved format actually has a video track.
        if (self._current or {}).get("source") == "tv":
            want_video = True
        else:
            want_video = bool(want_video) and (media.get("height", 0) > 0
                                               or media.get("is_live"))

        url = media["url"]
        is_remote = url.startswith(("http://", "https://"))
        candidates = [url]
        if is_remote:
            # A station is a name, not a URL: the mirror often has more than
            # one link for it, and any of them can be the dead one. The rest
            # are tried, in order, if this one fails.
            candidates = repair.candidates(url, self._alternates())
        self._candidates = candidates[1:]
        first = dict(media, url=candidates[0])

        # Switching between two things of the same kind does not need a new
        # player: mpv loads the next stream into itself, with no gap while a
        # sandbox, a proxy and a player come up again.
        if self._reuse_running(first, want_video, start_seconds):
            return

        self._teardown_session()
        if self._superseded(gen):
            return
        script = sandbox.find_script()
        env = dict(os.environ)
        env["AP_DATA_DIR"] = self.store.data
        env["AP_CACHE_DIR"] = self.store.cache
        env["AP_RUNTIME_DIR"] = self.store.runtime
        if self._current and self._current.get("source") == "tv":
            env["AP_BUFFER_SEC"] = str(self._on("tvBufferSec", 20))
            env["AP_USER_AGENT"] = self._on("tvUserAgent",
                                            "AuroraPulse/0.1 (Omarchy)")
        if self._on("tvOpenDefaultMuted", False) and want_video:
            env["AP_START_MUTED"] = "1"
        from .downloads import recordings_dir
        env["AP_RECORD_DIR"] = os.path.join(recordings_dir(), ".recording")
        env["AP_HWDEC"] = "auto-safe" if self._on("hardwareDecode", False) else "no"
        env["AP_AUDIO_TARGET"] = str(self._on("audioOutput", "") or "")
        if want_video:
            # Before the window exists, so it opens in place rather than
            # appearing in the tiling layout and jumping a moment later.
            self._video_placed = self._video_rule()
        # Local files have to be handed into the sandbox explicitly, by a
        # read-only bind of the real path, so the session loads those itself.
        # Streams are loaded by us, over the control socket, once it answers.
        session_media = "" if is_remote else url
        if not is_remote and start_seconds > 5:
            env["AP_START_SECONDS"] = "%0.3f" % start_seconds

        try:
            self.session = subprocess.Popen(
                [script, "session", self.store.runtime,
                 str(self._on("volume", 70)),
                 "video" if want_video else "audio",
                 (self._current or {}).get("uid", "aurorapulse")[:120],
                 session_media],
                env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
        except OSError as exc:
            raise SourceError("could not start the player: %s" % exc, "sandbox")
        self._session_video = want_video
        self.session_pid = self.session.pid
        self.session_start = process_start(self.session.pid)
        self.store.write_pid(self.session_pid, self.session_start)
        self.store.write_owner(os.getpid())
        self._save_session()

        client = Mpv(self.store.socket_path)
        client.on_event = functools.partial(self._on_mpv_from, client)
        self._props = {}
        self.player = client
        attached = client.attach(timeout=30.0 if want_video else 12.0,
                                 abort=lambda: self._superseded(gen)
                                 or self.session is None
                                 or self.session.poll() is not None)
        if not attached:
            if self._superseded(gen):
                return
            self._teardown_session()
            raise SourceError("the player did not come up", "sandbox")
        if is_remote:
            if not self._load(candidates[0], start_seconds,
                              media.get("audio_url") or ""):
                raise SourceError("the player would not take that stream",
                                  "sandbox")
        else:
            # The session loads a local file itself, and may well have done so
            # before we connected - in which case the "it started" event has
            # already gone by and the item would read as connecting forever.
            self._load_token += 1
            ok, idle = client.request("get_property", "idle-active")
            if ok and idle is False:
                self._started_token = self._load_token
                self._loading = False
        self._apply_levels(want_video)
        if want_video and not getattr(self, "_video_placed", False):
            POOL.submit(self._place_new_window, self.session)

    def _silence_now(self):
        """Cut the sound immediately, without waiting for anything.

        Runs on the reader thread. Sends the signal and returns; the playback
        lane does the waiting and the tidying up afterwards.
        """
        flushed = None
        try:
            self._remember_position()
            flushed = self._finish_recording()
        except Exception:
            pass
        self._current = None
        self._loading = False
        self._candidates = []
        session = self.session
        target = None
        if session is not None and session.poll() is None:
            target = session.pid
        else:
            saved = self.store.read_pid()
            if saved and process_alive(*saved):
                target = saved[0]
        player = self.player
        if player is not None:
            try:
                player.set_property("pause", True)
            except Exception:
                pass

        def end(pid):
            try:
                os.killpg(pid, signal.SIGTERM)
            except OSError:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
        if target and flushed is not None:
            # Paused, so it is already silent; the player lives a moment
            # longer only to write out the end of the recording.
            def later(pid=target):
                flushed.wait(3)
                end(pid)
            threading.Thread(target=later, daemon=True).start()
        elif target:
            end(target)
        self.emit_state()

    def _teardown_session(self):
        """Stop the player session and wait for it to be gone.

        This used to shut the artwork pool down as well, on every station
        change that needed a new player - and nothing ever started it again,
        so station logos stopped arriving after the first switch.
        """
        flushed = getattr(self, "_record_flush", None)
        if flushed is not None:
            flushed.wait(3)
        player, self.player = self.player, None
        self._props = {}
        self._entry_id = None
        self._load_token += 1
        if player is not None:
            player.on_event = None
            player.close()
        self._session_video = None
        session, self.session = self.session, None
        if session is not None:
            _kill_group(session, grace=1.5)
        # A session this daemon did not start (adopted after a restart) is
        # only known by its pid file.
        from . import session as session_module
        session_module.stop_session(self.store.runtime)
        self.session_pid = None
        self.session_start = ""
        self.store.clear_pid()

    def release_on_exit(self, grace=10.0):
        """Stop the player unless another daemon adopts it within `grace`."""
        me = os.getpid()
        if self.session is None and not self.store.read_pid():
            return
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            owner = self.store.read_owner()
            if owner is not None and owner != me and process_alive(owner, ""):
                return          # a new daemon has it; leave it playing
            time.sleep(0.25)
        owner = self.store.read_owner()
        if owner is not None and owner != me and process_alive(owner, ""):
            return
        self._shutting_down = True
        self._teardown()

    # Kept as the public name; older call sites and tests use it.
    def _teardown(self):
        self._end_cast()
        with self._session_lock:
            self._teardown_session()

    def _alternates(self, limit=4):
        """Other links to try for whatever is playing, best first."""
        item = self._current or {}
        if item.get("source") != "radio" or self.catalogue is None:
            return []
        uid = item.get("uid", "")
        # The mirror keys stations by their bare uuid; the item's uid carries
        # the source prefix. Asking with the prefix found nothing, ever.
        uuid = uid.split(":", 1)[1] if ":" in uid else uid
        found = []
        try:
            links = itertools.chain(
                self.catalogue.station_candidates(uuid, limit),
                self.catalogue.station_siblings(uuid, item.get("title"), limit))
            for url in links:
                if url and url != item.get("url") and url not in found:
                    found.append(url)
        except Exception:
            return []
        return found[:limit]

    def _save_session(self):
        """Record what is playing so a restarted daemon can pick it up."""
        try:
            self.store.save_session({
                "item": self._current,
                "queue": self.queue,
                "index": self.index,
                "video": bool(self._session_video),
                "at": time.time(),
            })
        except Exception as exc:  # never let bookkeeping stop playback
            self._log("could not record the session: %s" % exc)

    def adopt_running_session(self):
        """At start-up, reconnect to a player that outlived the last daemon.

        The player is a separate process on purpose, so a shell reload does not
        stop the music - but then the new daemon has to find it again, or the
        panel shows "nothing playing" over live sound with no way to stop it.
        """
        saved_pid = self.store.read_pid()
        if not saved_pid or not process_alive(*saved_pid):
            return False
        client = Mpv(self.store.socket_path)
        client.on_event = functools.partial(self._on_mpv_from, client)
        if not client.attach(timeout=1.5):
            client.close()
            return False
        self.player = client
        self.session_pid, self.session_start = saved_pid
        # Claim it, so the daemon that started it knows it can leave it be.
        self.store.write_owner(os.getpid())
        saved = self.store.load_session()
        self._session_video = bool(saved.get("video"))
        self._adopt_orphaned_session()
        if self._current is None:
            # A player with nothing we can name: stop it rather than leave a
            # sound playing that no control can reach.
            self._teardown_session()
            return False
        self._loading = False
        self._started_token = self._load_token
        self.emit_state()
        return True

    def _adopt_orphaned_session(self):
        """Take the queue and item from the saved session, if a player is up.

        Only adopts when a player really is there: a saved session with no
        player behind it is a crash, not playback.
        """
        if self._current is not None:
            return
        if self.player is None:
            return
        saved = self.store.load_session()
        item = saved.get("item")
        if not isinstance(item, dict) or not item.get("uid"):
            return
        queue_ = saved.get("queue")
        index = saved.get("index")
        self._current = item
        if isinstance(queue_, list) and queue_:
            self.queue = [entry for entry in queue_ if isinstance(entry, dict)]
            try:
                self.index = max(0, min(int(index), len(self.queue) - 1))
            except (TypeError, ValueError):
                self.index = 0
        else:
            self.queue = [item]
            self.index = 0
        self._log("adopted the running session: %s" % item.get("title", ""))
        self.emit_state()

    # -- player events -----------------------------------------------------

    def _on_mpv_from(self, client, message):
        """Events from one particular player connection.

        A player that has been replaced still has a reader thread winding
        down, and its last events - an end-file, a disconnect - used to land
        on the new player's state: a station switch could be followed by the
        old connection's disconnect setting self.player to None, leaving the
        new station playing with every control dead.
        """
        if client is not self.player:
            return
        self._on_mpv(message)

    def _on_mpv(self, message):
        event = message.get("event")
        if event == "property-change":
            self._on_property(message.get("name"), message.get("data"))
            return
        if event == "start-file":
            if message.get("playlist_entry_id") is not None:
                self._entry_id = message.get("playlist_entry_id")
            return
        if event == "playback-restart":
            # The first moment there is actually sound. Everything before is
            # "connecting".
            self._started_token = self._load_token
            if self._loading:
                self._loading = False
                self._reconnects = 0
                self.emit_state()
                self._prefetch_next()
                # Live or not decides whether silence may be skipped.
                self._apply_filters(force=False)
                self._fade_in_if_faded()
                self._apply_aspect()
                if self._session_video:
                    POOL.submit(self._apply_subtitles)
                    POOL.submit(self._apply_quality)
                current = self._current or {}
                if current.get("uid") and current.get("uid") != getattr(self, "_last_played", None):
                    self._last_played = current.get("uid")
                    POOL.submit(self._remember_played, dict(current))
                    POOL.submit(self._notify_track, dict(current), "")
                    POOL.submit(self._report_play, dict(current))
            return
        if event == "end-file":
            entry = message.get("playlist_entry_id")
            if (entry is not None and self._entry_id is not None
                    and entry != self._entry_id):
                return          # the file we just replaced, not this one
            reason = message.get("reason")
            if reason == "error":
                self._link_failed(message.get("file_error") or "")
            elif reason == "eof":
                self._ended()
            return
        if event == "file-loaded":
            self.emit_state()
            return
        if event == "aurora-disconnected":
            # The player went away underneath us (crashed, or killed from
            # outside). Say so rather than claiming it is still playing.
            self.player = None
            self._props = {}
            if self._current and not self._shutting_down:
                self._error = "the player stopped"
            self.emit_state()

    def _on_property(self, name, value):
        if not name:
            return
        previous = self._props.get(name)
        self._props[name] = value
        if name == "time-pos":
            # Position is pushed about once a second; the panel interpolates
            # between pushes, so it moves smoothly without a flood of events.
            if time.monotonic() - self._last_position_emit >= 1.0:
                self.emit_state()
            self._maybe_fade_out(value)
            return
        if name == "mute":
            # mpv can be muted from its own input, so follow the player.
            self._mute = bool(value)
        if name == "metadata" and self._is_live() and previous != value:
            stream = self._stream_title()
            if stream and stream != getattr(self, "_last_stream_title", ""):
                self._last_stream_title = stream
                POOL.submit(self._notify_track, dict(self._current or {}), stream)
        if name == "volume" or previous == value:
            return
        if name in ("pause", "mute", "paused-for-cache", "idle-active",
                    "duration", "metadata", "core-idle", "speed"):
            self.emit_state()

    def _link_failed(self, detail):
        """The link in play is dead. Try the next one, or give up and say so."""
        if not self._current:
            return
        if self._candidates and self.player is not None:
            url = self._candidates.pop(0)
            self._log("that link failed (%s); trying %s" % (detail, url))
            self.emit({"type": "notice", "level": "info",
                       "title": "Trying another link",
                       "body": textutil.text(self._current.get("title"), 120)})
            # Fire and forget: this can run on the player's reader thread,
            # which is the thread that would deliver a reply.
            self._load_token += 1
            token = self._load_token
            self._entry_id = None
            self._loading = True
            self.player.command("loadfile", url, "replace")
            timeout = (START_TIMEOUT_VIDEO if self._session_video
                       else START_TIMEOUT_AUDIO)
            timer = threading.Timer(timeout, self._start_watchdog, args=(token,))
            timer.daemon = True
            timer.start()
            self.emit_state()
            return
        self._playback_failed(detail)

    def _ended(self):
        """A file reached its end on its own."""
        current = self._current or {}
        if not current:
            return
        if self._is_live():
            # A live stream does not "end": the server dropped us. Reconnect a
            # couple of times before calling it a failure.
            if self._reconnects < 2 and self.player is not None:
                self._reconnects += 1
                url = current.get("url") or ""
                if url.startswith(("http://", "https://")):
                    self._loading = True
                    self.player.command("loadfile", url, "replace")
                    self.emit_state()
                    return
            self._playback_failed("the stream ended")
            return
        self.handle({"cmd": "auto_next"})

    def _playback_failed(self, detail):
        """Report a stream that mpv could not open, and stop claiming to play.

        The item stays in the queue (so next/previous and retry still work),
        but it is no longer reported as current, and the reason is passed
        through so the panel can say what went wrong instead of failing
        silently.
        """
        current = self._current or {}
        title = current.get("title") or "that station"
        reason = str(detail or "loading failed")
        self._finish_recording()
        self._current = None
        self._loading = False
        self._error = ""
        self._save_session()
        self.emit({
            "type": "error",
            "message": "%s could not be played (%s)" % (title, reason),
            "reason": "network",
            "uid": current.get("uid", ""),
        })
        self.emit_state()


def _kill_group(proc, grace=1.5):
    """Terminate a session and everything it started, then make sure.

    The session is started in its own process group, so one signal reaches it,
    its sandbox and pw-cat together - the audio stops at once rather than when
    the session gets round to cleaning up.
    """
    if proc.poll() is not None:
        return
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 1.0)):
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            try:
                proc.send_signal(sig)
            except OSError:
                pass
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


DEFAULT_SETTINGS = {
    # Off by default: a widget that starts making noise at every login is a
    # surprise nobody asked for. The setting is there for those who want it.
    "autoPlay": False, "resumePlayback": True,
    "radioServer": "", "radioAutoSync": True, "tvAutoSync": True,
    "preferredCountry": "", "viewMode": "list",
    "audioExtensions": "mp3,m4a,flac,ogg,oga,opus,wav,aac,alac,wv,aiff,wma",
    "radioSyncHours": 24, "tvSyncHours": 24,
    "tvBufferSec": 20, "tvUserAgent": "AuroraPulse/0.1 (Omarchy)",
    "tvHideGeoBlocked": True, "tvHideNot247": True, "tvHideNsfw": True,
    "tvOpenDefaultMuted": False, "localFollowSymlinks": False,
    "artwork": True, "artworkConcurrency": 6,
    "showBitrate": True, "showCountry": True,
    "showTitle": True, "maxTitleWidth": 180, "showSourceBadge": True,
    "showPipBadge": True, "defaultSource": "radio", "audioOnlyYouTube": False,
    "crossfadeSec": 0.0, "playbackSpeed": 1.0, "skipSilence": False,
    "preventDuplicates": True, "resumePlayback": True, "volume": 70,
    "rememberVolumePerDevice": True, "equalizerEnabled": False,
    "eqPreset": "Flat", "defaultQuality": "720p", "pipSize": "m",
    "pipCorner": "br", "defaultAspect": "auto", "autoHideControlsMs": 3000,
    "hardwareDecode": False, "videoMonitor": "", "minBitrate": 0, "reportPlays": True,
    "autoSyncHours": 24, "epgUrl": "", "epgRefreshHours": 12,
    "ytResolutionBudgetPerHour": 120, "ytSearchResults": 20,
    "useYtLyrics": True, "lyricsProviders": "lrclib,azlyrics,sidecar",
    "scanRoots": "", "requestTimeoutSec": 20, "offlineMode": False,
    "notifyTrackChange": False,
    "normalizeVolume": False, "eqBands": "0,0,0,0,0", "sleepFadeSec": 30,
    "alarmEnabled": False, "alarmTime": "07:00", "alarmDays": "1,2,3,4,5,6,7",
    "alarmFadeSec": 60, "alarmVolume": 50,
    "subtitlesEnabled": False, "subtitleLanguage": "en", "subtitleSize": 1.0,
    "tvQuality": "best",
    # How the video window was left: floating at pipSize in pipCorner, or
    # tiled; pinned on top of every workspace or not.
    "pipFloating": True, "pipPinned": False,
    # The browsing that led to what is playing, as JSON, so the panel can
    # open on it at start-up.
    "lastBrowse": "",
    "lyricsSize": "m", "audioOutput": "",
    "downloadAudioFormat": "mp3", "downloadVideoFormat": "mp4",
    "downloadVideoQuality": "1080p", "downloadArtwork": True,
    "scrobbleEnabled": False, "listenbrainzToken": "", "lastfmApiKey": "",
    "lastfmApiSecret": "", "lastfmSession": "", "lastfmUser": "",
}

SPEC = {
    "artworkConcurrency": (1, 16),
    "radioSyncHours": (1, 168), "tvSyncHours": (1, 168),
    "tvBufferSec": (5, 120),
    "maxTitleWidth": (60, 600), "crossfadeSec": (0.0, 12.0),
    "autoHideControlsMs": (0, 15000), "minBitrate": (0, 320),
    "epgRefreshHours": (1, 168), "ytResolutionBudgetPerHour": (20, 600),
    "ytSearchResults": (5, 50), "requestTimeoutSec": (5, 120),
    "volume": (0, 100), "autoSyncHours": (1, 720),
    "sleepFadeSec": (0, 120), "alarmFadeSec": (0, 600), "alarmVolume": (5, 100),
    "subtitleSize": (0.5, 3.0),
}


def _filter_tv(rows, hide_geo, hide_not247):
    """Drop the channels that are listed but will not play.

    A geo-blocked or off-air channel is not a broken player, it is a channel
    that would show a dead screen - so hiding them is kinder than letting
    somebody find out by clicking.
    """
    out = []
    for row in rows:
        title = (row.get("title") or "").lower()
        if hide_geo and ("geo-block" in title or "geo block" in title):
            continue
        if hide_not247 and "not 24/7" in title:
            continue
        out.append(row)
    return out


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    command = argv[0] if argv else "daemon"

    if command == "daemon":
        store = Store(
            os.environ.get("AP_DATA_DIR",
                           os.path.expanduser("~/.local/share/aurora-pulse")),
            os.environ.get("AP_CACHE_DIR",
                           os.path.expanduser("~/.cache/aurora-pulse")),
            os.environ.get("AP_RUNTIME_DIR") or os.path.join(
                os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "aurora-pulse"))
        store.prepare()
        store.load()
        netguard.set_offline(bool((store.state.get("settings") or {})
                                  .get("offlineMode")))
        daemon = Daemon(store)
        # Opening is instant; the mirror itself fills in the background.
        daemon.ensure_catalogue()
        offline = textutil.bool_of((store.state.get("settings") or {})
                                   .get("offlineMode"))
        # Only when the catalogue has never been downloaded, and then only for
        # what is missing - see startup_sync for why that is not a full mirror.
        missing = [src for src in ("radio", "tv")
                   if not daemon.catalogue.mirrored(src)]
        plan = startup_sync(missing, offline)
        if plan is not None:
            POOL.submit(lambda: daemon.start_sync(**plan))
        daemon.emit({"type": "hello", "protocol": PROTOCOL_VERSION,
                     "capabilities": CAPABILITIES,
                     "ready": True})
        # A player that outlived the previous daemon (a shell reload) is
        # picked back up, so it can be seen and stopped. Done on the playback
        # lane so the hello and the first browse are not held up by it.
        daemon._playback.submit(daemon.adopt_running_session)
        radio.set_server(daemon._on("radioServer", ""))
        # Media keys, playerctl, the lock screen and the shell's media widget.
        from . import mpris
        if mpris.available():
            daemon._mpris = mpris.Mpris(daemon.handle, log=daemon._log)
            daemon._mpris.start()
        threading.Thread(target=daemon.auto_update_loop, name="ap-schedule",
                         daemon=True).start()
        threading.Thread(target=daemon.sink_watch_loop, name="ap-sink",
                         daemon=True).start()
        threading.Thread(target=daemon.alarm_loop, name="ap-alarm",
                         daemon=True).start()
        daemon._playback.submit(daemon.restore_queue)
        POOL.submit(daemon.resume_downloads)
        POOL.submit(daemon.startup_health)
        daemon._playback.submit(daemon.auto_play)

        def stop(_signum, _frame):
            daemon._shutting_down = True
            try:
                daemon._persist_queue(now=True)
            except Exception:
                pass
            daemon._teardown()
            try:
                store.flush()
            except Exception:
                pass
            if daemon.syncer is not None:
                daemon.syncer.shutdown(timeout=2.0)
            os._exit(0)
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)

        for line in protocol.read_lines():
            daemon.handle(line)
        # The shell closed our stdin. Either it is reloading - and a new daemon
        # is about to adopt the player, so the music carries on - or the widget
        # is gone for good, and a player left running then is a sound that no
        # control anywhere can stop. Wait a little to find out which.
        try:
            store.flush()
        except Exception:
            pass
        daemon.release_on_exit(grace=10.0)
        os._exit(0)

    if command == "session":
        from . import session as session_module
        runtime = argv[1] if len(argv) > 1 else os.environ.get("AP_RUNTIME_DIR", "")
        volume = int(argv[2]) if len(argv) > 2 else 70
        video = len(argv) > 3 and argv[3] == "video"
        title = argv[4] if len(argv) > 4 else "aurorapulse"
        media = argv[5] if len(argv) > 5 else None
        store = Store(
            os.environ.get("AP_DATA_DIR",
                           os.path.expanduser("~/.local/share/aurora-pulse")),
            os.environ.get("AP_CACHE_DIR",
                           os.path.expanduser("~/.cache/aurora-pulse")),
            runtime)
        store.write_pid(os.getpid(), process_start(os.getpid()))
        return session_module.run_session(runtime, volume, video, title, media)

    if command == "sandbox":
        from . import session as session_module
        # Everything after the leading -- is the program to run, as a real
        # argv rather than a string we re-split, so a media URL containing
        # spaces cannot turn into two arguments. The proxy arrives as an
        # environment variable for the same reason.
        rest = list(argv[1:]) if argv and argv[0] == "sandbox" else list(argv)
        if rest and rest[0] == "--":
            rest = rest[1:]
        if not rest:
            print("ap-ctl: sandbox was given nothing to run", file=sys.stderr)
            return 2
        proxy_path = os.environ.get("AP_PROXY_PATH", "")
        return session_module.run_sandbox(proxy_path, rest,
                                          os.environ.get("AP_PROFILE", "audio"))

    if command == "parse-feed":
        # Run inside the artwork sandbox: RSS bytes in, plain JSON out.
        import json
        from ..sources import podcast as podcast_module
        body = sys.stdin.buffer.read(podcast_module.MAX_FEED_BYTES + 1)
        if len(body) > podcast_module.MAX_FEED_BYTES:
            print(json.dumps({"error": "that feed is too large"}))
            return 0
        print(json.dumps(podcast_module.parse_feed(body), ensure_ascii=True))
        return 0

    if command == "selftest":
        from .selftest import run, run_inside
        return run_inside() if "--inside" in argv else run()

    print("ap-ctl: unknown command %r" % command[:40], file=sys.stderr)
    return 2
