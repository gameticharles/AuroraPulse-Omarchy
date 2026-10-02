"""Scrobbling: tell ListenBrainz and/or Last.fm what you listened to.

The usual rule decides what counts as a listen: half the track or four
minutes, whichever comes first, of actual playing time (pauses do not count).
A radio station is scrobbled by its "now playing" line when it reads
"Artist - Title", after a minute of it; a station that sends no titles is not
scrobbled at all. TV and podcasts are never scrobbled.

ListenBrainz needs only a user token. Last.fm needs an API key and secret
(free, from last.fm/api/account/create) and one sign-in, which is exchanged
for a session key; the password itself is never stored.
"""

import hashlib
import json
import threading
import time
import urllib.parse

from ..util import netguard, textutil

LISTENBRAINZ = "https://api.listenbrainz.org/1/submit-listens"
LASTFM = "https://ws.audioscrobbler.com/2.0/"
SOURCES = ("music", "local", "youtube", "radio")


def split_title(text):
    """'Artist - Title' -> (artist, title), or ("", text)."""
    for sep in (" - ", " – ", " — "):
        if sep in text:
            artist, _, title = text.partition(sep)
            if artist.strip() and title.strip():
                return artist.strip(), title.strip()
    return "", text.strip()


def lastfm_sign(params, secret):
    raw = "".join("%s%s" % (k, params[k]) for k in sorted(params)
                  if k not in ("format", "callback"))
    return hashlib.md5((raw + secret).encode("utf-8")).hexdigest()


def lastfm_call(params, secret, seconds=15):
    params = dict(params)
    params["api_sig"] = lastfm_sign(params, secret)
    params["format"] = "json"
    status, body = netguard.post(LASTFM, urllib.parse.urlencode(params).encode(),
                                 "application/x-www-form-urlencoded", seconds=seconds)
    data = json.loads(body.decode("utf-8", "replace") or "{}")
    if status != 200 or "error" in data:
        raise OSError(data.get("message") or "Last.fm answered HTTP %d" % status)
    return data


def lastfm_login(api_key, secret, user, password):
    """Exchange a username and password for a session key."""
    data = lastfm_call({"method": "auth.getMobileSession", "api_key": api_key,
                        "username": user, "password": password}, secret)
    session = (data.get("session") or {}).get("key")
    if not session:
        raise OSError("Last.fm did not return a session")
    return session, (data.get("session") or {}).get("name") or user


class Scrobbler:
    """Follows the daemon's state and submits listens in the background."""

    def __init__(self, settings, submit_async, report):
        self._settings = settings          # () -> dict
        self._async = submit_async         # (fn) -> None, runs off-thread
        self._report = report              # (service, ok, message)
        self._lock = threading.Lock()
        self._track = None                 # the listen being timed
        self._played = 0.0
        self._last = None

    def _enabled(self):
        s = self._settings()
        if not s.get("scrobbleEnabled"):
            return {}
        out = {}
        if s.get("listenbrainzToken"):
            out["listenbrainz"] = s["listenbrainzToken"]
        if s.get("lastfmSession") and s.get("lastfmApiKey") and s.get("lastfmApiSecret"):
            out["lastfm"] = (s["lastfmApiKey"], s["lastfmApiSecret"], s["lastfmSession"])
        return out

    def _identify(self, state):
        item = state.get("item") or {}
        source = item.get("source")
        if not item.get("uid") or source not in SOURCES:
            return None
        if source == "radio":
            artist, title = split_title(state.get("streamTitle") or "")
            if not artist:
                return None
            return {"key": "%s|%s|%s" % (item["uid"], artist, title), "artist": artist,
                    "title": title, "album": "", "duration": 0, "live": True}
        artist = item.get("artist") or ""
        title = item.get("title") or ""
        if source == "youtube":
            guess_artist, guess_title = split_title(title)
            if guess_artist:
                artist, title = guess_artist, guess_title
        if not artist or not title:
            return None
        return {"key": item["uid"], "artist": artist, "title": title,
                "album": item.get("album") or "",
                "duration": int(state.get("duration") or item.get("duration") or 0),
                "live": False}

    def update(self, state):
        services = self._enabled()
        if not services:
            self._track = None
            return
        now = time.monotonic()
        track = self._identify(state)
        playing = state.get("mode") == "playing"
        with self._lock:
            current = self._track
            if current is not None and self._last is not None and current.get("playing"):
                self._played += now - self._last
            self._last = now
            if track is None:
                self._track = None
                return
            if current is None or current["key"] != track["key"]:
                track.update(started=int(time.time()), done=False, playing=playing)
                self._track = track
                self._played = 0.0
                if playing:
                    self._async(lambda t=dict(track): self._now_playing(services, t))
                return
            current["playing"] = playing
            if current["done"]:
                return
            if current["live"]:
                needed = 60
            else:
                duration = current.get("duration") or track.get("duration") or 0
                needed = min(240, duration / 2) if duration >= 30 else 240
            if self._played >= needed:
                current["done"] = True
                self._async(lambda t=dict(current): self._scrobble(services, t))

    # -- services ----------------------------------------------------------

    def _now_playing(self, services, track):
        for service, creds in services.items():
            try:
                if service == "listenbrainz":
                    self._lb(creds, "playing_now", track)
                else:
                    key, secret, session = creds
                    params = {"method": "track.updateNowPlaying", "api_key": key,
                              "sk": session, "artist": track["artist"],
                              "track": track["title"]}
                    if track["album"]:
                        params["album"] = track["album"]
                    if track["duration"]:
                        params["duration"] = str(track["duration"])
                    lastfm_call(params, secret)
            except Exception:  # noqa: BLE001 - now-playing is best effort
                pass

    def _scrobble(self, services, track):
        for service, creds in services.items():
            try:
                if service == "listenbrainz":
                    self._lb(creds, "single", track)
                else:
                    key, secret, session = creds
                    params = {"method": "track.scrobble", "api_key": key, "sk": session,
                              "artist[0]": track["artist"], "track[0]": track["title"],
                              "timestamp[0]": str(track["started"])}
                    if track["album"]:
                        params["album[0]"] = track["album"]
                    if track["duration"]:
                        params["duration[0]"] = str(track["duration"])
                    lastfm_call(params, secret)
                self._report(service, True, "%s - %s" % (track["artist"], track["title"]))
            except Exception as exc:  # noqa: BLE001
                self._report(service, False, textutil.text(str(exc), 160))

    def _lb(self, token, kind, track):
        meta = {"artist_name": track["artist"], "track_name": track["title"],
                "additional_info": {"media_player": "AuroraPulse",
                                    "submission_client": "AuroraPulse",
                                    "submission_client_version": "0.2"}}
        if track["album"]:
            meta["release_name"] = track["album"]
        if track["duration"]:
            meta["additional_info"]["duration_ms"] = int(track["duration"]) * 1000
        listen = {"track_metadata": meta}
        if kind == "single":
            listen["listened_at"] = int(track["started"])
        status, body = netguard.post(
            LISTENBRAINZ, json.dumps({"listen_type": kind, "payload": [listen]}).encode(),
            "application/json", seconds=15,
            headers={"Authorization": "Token %s" % token})
        if status != 200:
            raise OSError("ListenBrainz answered HTTP %d: %s"
                          % (status, body[:120].decode("utf-8", "replace")))
