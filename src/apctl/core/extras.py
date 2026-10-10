"""Commands added in 1.6: radio from a track, recent folders, audio tracks,
screenshots, an imported TV guide, your own lyrics, tags, playlist files,
podcast marks and alerts, and one search across every source.

A mixin of the Daemon, kept in its own file so daemon.py stays the player
and the queue. Everything here runs on the same lanes as the rest: a method
named cmd_<name> is a protocol command (see docs/PROTOCOL.md).
"""

import os
import random
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from ..sources import local, podcast, tv, youtube
from ..sources.base import SourceError
from ..util import netguard, textutil
from . import protocol

RADIO_SIZE = 25
RECENT_FOLDERS = 8
PODCAST_MARKS_KEPT = 5000
SEARCH_ALL_LIMIT = 8


class Extras:
    MAX_QUEUE_LEN = 500

    # -- keep playing: a radio from a track ---------------------------------

    def _radio_for(self, seed, skip=()):
        """Entries like `seed` to play after it: YouTube's mix for a video or a
        song, or the library's tracks by the same artist, then genre."""
        seed = seed or {}
        source = seed.get("source")
        extra = seed.get("extra") or {}
        skip = set(skip) | {seed.get("uid")}
        found = []
        if source in ("youtube", "music") and extra.get("video_id"):
            found = youtube.mix(extra["video_id"], self.store.art_dir, source)
        elif source == "local":
            library = self.store.library
            media = extra.get("media") or "audio"
            pools = []
            if seed.get("artist"):
                pools.append(library.tracks(media=media, artist=seed["artist"], limit=200))
            if extra.get("genre"):
                pools.append(library.tracks(media=media, genre=extra["genre"], limit=400))
            pools.append(library.tracks(media=media, limit=2000))
            for pool in pools:
                pool = list(pool)
                random.shuffle(pool)
                found.extend(pool)
        out, seen = [], set()
        for entry in found:
            uid = entry.get("uid")
            if not uid or uid in skip or uid in seen:
                continue
            seen.add(uid)
            out.append(entry)
            if len(out) >= RADIO_SIZE:
                break
        return out

    def _keep_playing(self, gen=None):
        """At the end of the queue: add more like the last entry and go on.
        Returns True when something was added and is now playing."""
        if not self._on("keepPlaying", True) or not self.queue:
            return False
        last = self.queue[-1]
        if last.get("is_live") or last.get("source") not in ("youtube", "music", "local"):
            return False
        if netguard.offline() and last.get("source") != "local":
            return False
        try:
            more = self._radio_for(last, [e.get("uid") for e in self.queue])
        except SourceError as exc:
            self._log("keep playing: %s" % exc)
            return False
        clean = [c for c in (protocol.clean_item(e) for e in more) if c]
        if not clean:
            return False
        start = len(self.queue)
        self.queue.extend(clean)
        overflow = len(self.queue) - self.MAX_QUEUE_LEN
        if overflow > 0:
            # Drop what has already played, never what is still to come.
            del self.queue[:overflow]
            start -= overflow
        self.index = start
        self._emit_queue()
        self._notice("Keeping the music going", "%d more like %s"
                     % (len(clean), textutil.text(last.get("title"), 80)))
        self._advance(auto=True, gen=gen)
        return True

    def cmd_radio(self, message):
        """Play a track now, with more like it after: a mix from YouTube or
        YouTube Music, or similar tracks from the library."""
        seed = protocol.clean_item(message.get("item") or self._current or {})
        if not seed:
            raise SourceError("there is nothing to start a radio from", "empty")
        more = [c for c in (protocol.clean_item(e) for e in self._radio_for(seed)) if c]
        if not more:
            raise SourceError("no similar tracks were found", "empty")
        # A mix starts with its seed; do not play it twice.
        items = [seed] + [e for e in more if e.get("uid") != seed.get("uid")]
        self.handle({"cmd": "play", "items": items, "start": 0})
        self._notice("Radio from %s" % textutil.text(seed.get("title"), 80),
                     "%d tracks queued" % (len(items) - 1))
        return {"queue": len(items)}

    # -- recent folders ------------------------------------------------------

    def _recent_folders(self):
        rows = self.store.library.get_json("recentFolders", []) or []
        return [r for r in rows if isinstance(r, str)][:RECENT_FOLDERS]

    def _remember_folder(self, path):
        rows = [p for p in self._recent_folders() if p != path]
        self.store.library.set_json("recentFolders", ([path] + rows)[:RECENT_FOLDERS])
        self._emit_recent_folders()

    def _emit_recent_folders(self, ident=None):
        rows = [{"path": p, "name": os.path.basename(p.rstrip("/")) or p,
                 "exists": os.path.isdir(p)} for p in self._recent_folders()]
        self.emit({"type": "recent_folders", "id": ident, "folders": rows})

    def cmd_recent_folders(self, message):
        """List the folders played from "Play a folder", newest first; remove
        one, or clear them."""
        action = textutil.text(message.get("action") or "list", 12)
        if action == "remove":
            path = textutil.text(message.get("path"), 4096)
            self.store.library.set_json(
                "recentFolders", [p for p in self._recent_folders() if p != path])
        elif action == "clear":
            self.store.library.set_json("recentFolders", [])
        self._emit_recent_folders(message.get("id"))
        return None

    # -- audio tracks (languages) ------------------------------------------

    def _audio_tracks(self):
        player = self.player
        if player is None:
            return []
        ok, tracks = player.request("get_property", "track-list")
        if not ok or not isinstance(tracks, list):
            return []
        return [t for t in tracks if isinstance(t, dict) and t.get("type") == "audio"]

    @staticmethod
    def _audio_label(track):
        lang = str(track.get("lang") or "").strip()
        title = str(track.get("title") or "").strip()
        parts = [p for p in (title, lang.upper() if lang and lang.lower() not in
                             title.lower() else "") if p]
        codec = str(track.get("codec") or "")
        channels = track.get("demux-channel-count")
        if channels and int(channels) > 2:
            parts.append("%s.1" % (int(channels) - 1))
        return textutil.text(" · ".join(parts) or ("%s %s" % ("Track", track.get("id"))
                                                 + (" (%s)" % codec if codec else "")), 60)

    def _emit_audio_tracks(self, ident=None):
        """The audio tracks of what is playing, one row per language: an
        adaptive stream lists the same language once per rendition."""
        choices, seen, selected = [], {}, ""
        tracks = self._audio_tracks()
        for track in tracks:
            label = self._audio_label(track)
            key = "%s|%s" % (str(track.get("lang") or "").lower(),
                             str(track.get("title") or "").lower())
            if track.get("selected"):
                selected = key
            if key in seen:
                continue
            seen[key] = track.get("id")
            choices.append({"key": key, "label": label,
                            "lang": textutil.text(track.get("lang") or "", 12)})
        self.emit({"type": "audiotracks", "id": ident,
                   "choices": choices if len(choices) > 1 else [],
                   "selected": selected})

    def cmd_audio_track(self, message):
        """List the audio tracks (languages) or switch to one."""
        action = textutil.text(message.get("action") or "list", 12)
        if action == "select" and self.player is not None:
            key = textutil.text(message.get("key"), 80)
            tracks = self._audio_tracks()
            current = next((t for t in tracks if t.get("selected")), {})
            matches = [t for t in tracks if "%s|%s" % (
                str(t.get("lang") or "").lower(), str(t.get("title") or "").lower()) == key]
            # In an adaptive stream, the match in the rendition playing now.
            chosen = next((t for t in matches if t.get("program-id") is not None
                           and t.get("program-id") == current.get("program-id")),
                          matches[0] if matches else None)
            if chosen is None:
                raise SourceError("that audio track is gone", "not-found")
            self.player.set_property("aid", chosen.get("id"))
            time.sleep(0.15)
        self._emit_audio_tracks(message.get("id"))
        return None

    # -- screenshots ---------------------------------------------------------

    def cmd_screenshot(self, message):
        """Save the video frame on screen to Pictures › AuroraPulse."""
        if self.player is None or not self._session_video:
            raise SourceError("there is no video playing", "empty")
        from .downloads import _safe_name, _user_dir
        from .session import SANDBOX_RUNTIME
        shots = os.path.join(self.store.runtime, "shots")
        os.makedirs(shots, mode=0o700, exist_ok=True)
        name = "shot-%d.png" % int(time.time() * 1000)
        staged = os.path.join(shots, name)
        # The player has no access to $HOME: it writes into the runtime folder
        # bound into its sandbox, and the file is moved out from here.
        self.player.command_wait("screenshot-to-file", SANDBOX_RUNTIME + "/shots/" + name,
                                 "video", timeout=6.0)
        deadline = time.time() + 6
        while time.time() < deadline and not (os.path.isfile(staged)
                                              and os.path.getsize(staged) > 0):
            time.sleep(0.1)
        if os.path.islink(staged) or not os.path.isfile(staged) or not os.path.getsize(staged):
            raise SourceError("the player did not save the picture", "unsupported")
        folder = os.path.join(_user_dir("PICTURES", "~/Pictures"), "AuroraPulse")
        os.makedirs(folder, exist_ok=True)
        current = self._current or {}
        stem = _safe_name(current.get("title") or "AuroraPulse", 80)
        position = int(self._position() or 0)
        target = os.path.join(folder, "%s %02d-%02d-%02d.png" % (
            stem, position // 3600, (position // 60) % 60, position % 60))
        n = 2
        while os.path.exists(target):
            target = os.path.join(folder, "%s %02d-%02d-%02d (%d).png" % (
                stem, position // 3600, (position // 60) % 60, position % 60, n))
            n += 1
        shutil.move(staged, target)
        self._notice("Saved a screenshot", target)
        self.emit({"type": "exported", "path": target})
        return {"path": target}

    # -- an imported TV guide -----------------------------------------------

    def cmd_epg_import(self, message):
        """Read an XMLTV guide file (or forget the one imported)."""
        if message.get("action") == "forget":
            tv.forget_guide_file(self.store.cache)
            self._notice("Forgot the imported guide")
            return {"channels": 0}
        channels, programmes = tv.import_guide_file(message.get("path"), self.store.cache)
        self._notice("Imported the guide", "%d channels, %d programmes"
                     % (channels, programmes))
        return {"channels": channels, "programmes": programmes}

    # -- lyrics: your own, and their timing ---------------------------------

    def _lyrics_key(self, uid, kind):
        return "lyrics-%s:%s" % (kind, textutil.text(uid, 200))

    def _own_lyrics(self, uid):
        found = self.store.library.get_json(self._lyrics_key(uid, "own"))
        return found if isinstance(found, dict) and found.get("lines") else None

    def _lyrics_offset(self, uid):
        try:
            return int(self.store.library.get_meta(self._lyrics_key(uid, "offset"), 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _emit_lyrics(self, item, found):
        uid = item.get("uid", "")
        if found is not None:
            found = dict(found, offset=self._lyrics_offset(uid))
        self._last_lyrics = (uid, found or None)
        self.emit({"type": "lyrics", "uid": uid, "lyrics": found or None})

    def cmd_lyrics_set(self, message):
        """Your own lyrics for a track, pasted or typed: timed when the text
        has [mm:ss] stamps (an .lrc), plain otherwise. Empty text forgets them
        and the providers are asked again."""
        from .resolver import parse_lrc
        item = message.get("item") or self._current or {}
        uid = item.get("uid", "")
        if not uid:
            raise SourceError("there is no track to add lyrics to", "empty")
        text = textutil.multiline(message.get("text") or "", limit=40000, lines=1500)
        key = self._lyrics_key(uid, "own")
        if not text.strip():
            self.store.library.set_meta(key, "")
            self.cmd_lyrics({"item": item})
            return None
        lines = parse_lrc(text)
        synced = bool(lines)
        if not synced:
            lines = [{"t": -1, "text": textutil.text(line, 240)}
                     for line in text.splitlines()][:1200]
        found = {"lines": lines, "source": "yours", "synced": synced}
        self.store.library.set_json(key, found)
        self._emit_lyrics(item, found)
        return None

    def cmd_lyrics_offset(self, message):
        """Shift synced lyrics: positive shows each line later, in ms."""
        item = message.get("item") or self._current or {}
        uid = item.get("uid", "")
        offset = textutil.int_in(message.get("offset"), -30000, 30000, 0)
        self.store.library.set_meta(self._lyrics_key(uid, "offset"), offset)
        uid_shown, found = getattr(self, "_last_lyrics", ("", None))
        if found and uid_shown == uid:
            self._emit_lyrics(item, found)
        return {"offset": offset}

    # -- tags ----------------------------------------------------------------

    def cmd_tags(self, message):
        """Edit a local file's tags and, optionally, its cover."""
        path = textutil.text(message.get("path"), 4096)
        fields = message.get("fields") if isinstance(message.get("fields"), dict) else {}
        entry = local.write_tags(self.store, self.settings(), path, fields,
                                 textutil.text(message.get("art"), 4096))
        # Whatever holds the old entry gets the new one.
        for i, queued in enumerate(self.queue):
            if (queued.get("extra") or {}).get("path") == entry["extra"]["path"]:
                self.queue[i] = protocol.clean_item(entry) or queued
        if self._current and (self._current.get("extra") or {}).get("path") == \
                entry["extra"]["path"]:
            self._current = protocol.clean_item(entry) or self._current
            self.emit_state()
        self._emit_queue()
        self._notice("Saved the tags", entry.get("title", ""))
        self.emit({"type": "tagged", "id": message.get("id"),
                   "item": protocol.clean_item(entry)})
        return None

    # -- playlists as files ---------------------------------------------------

    def _playlist_export(self, ident):
        from .downloads import _safe_name
        from .custom import downloads_dir
        library = self.store.library
        items = library.playlist_items(ident)
        if not items:
            raise SourceError("that playlist is empty", "empty")
        name = library.playlist_name(ident) or "Playlist"
        lines = ["#EXTM3U", "#PLAYLIST:%s" % textutil.text(name, 120)]
        for entry in items:
            url = str((entry.get("extra") or {}).get("path") or entry.get("url") or "")
            if not url or "\n" in url:
                continue
            label = textutil.text(entry.get("title"), 300).replace(",", " ")
            if entry.get("artist"):
                label = "%s - %s" % (textutil.text(entry.get("artist"), 200).replace(",", " "),
                                     label)
            lines.append("#EXTINF:%d,%s" % (int(entry.get("duration") or -1) or -1, label))
            lines.append(url)
        target = os.path.join(downloads_dir(), "%s.m3u8" % _safe_name(name, 80))
        n = 2
        while os.path.exists(target):
            target = os.path.join(downloads_dir(), "%s (%d).m3u8" % (_safe_name(name, 80), n))
            n += 1
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        self._notice("Exported %s" % name, target)
        self.emit({"type": "exported", "path": target})
        return {"path": target, "count": (len(lines) - 2) // 2}

    def _entry_for_line(self, line, title, duration, known):
        """A playlist line as an item: a file on disk, a YouTube link, or any
        other stream address."""
        if line.startswith("file://"):
            from urllib.parse import unquote, urlsplit
            line = unquote(urlsplit(line).path)
        if line.startswith("/") or line.startswith("~"):
            path = os.path.realpath(os.path.expanduser(line))
            if not os.path.isfile(path):
                return None
            if path in known:
                return known[path]
            built = local._quick(path)
            if title:
                artist, _, song = title.partition(" - ")
                if song:
                    built["artist"], built["title"] = artist.strip(), song.strip()
                else:
                    built["title"] = title
            built["duration"] = duration
            return built
        if not line.startswith(("http://", "https://")):
            return None
        from ..sources.base import clean_url, item
        from .resolver import is_youtube_url
        if is_youtube_url(line):
            match = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{6,20})", line)
            if match:
                return youtube.to_item({"id": match.group(1),
                                        "title": title or match.group(1),
                                        "duration": duration}, self.store.art_dir)
        url = clean_url(line)
        if not url:
            return None
        return item("radio", "import-%s" % url, title or url, url=url, kind="station",
                    is_live=duration <= 0, duration=max(0, duration))

    def _playlist_import(self, path):
        raw = textutil.text(path, 4096).strip()
        path = os.path.realpath(os.path.expanduser(raw)) if raw else ""
        if not path or not os.path.isfile(path):
            raise SourceError("that playlist file does not exist", "not-found")
        if os.path.getsize(path) > 4 << 20:
            raise SourceError("that playlist file is larger than 4 MB", "unsupported")
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            body = handle.read()
        base_dir = os.path.dirname(path)
        name = os.path.splitext(os.path.basename(path))[0]
        title, duration, entries = "", -1, []
        known = {}
        for line in body.splitlines():
            line = line.strip().lstrip("﻿")
            if not line:
                continue
            if line.startswith("#PLAYLIST:"):
                name = line[10:].strip() or name
                continue
            if line.startswith("#EXTINF:"):
                head, _, title = line[8:].partition(",")
                try:
                    duration = int(float(head.split()[0]))
                except (ValueError, IndexError):
                    duration = -1
                title = title.strip()
                continue
            if line.startswith("#"):
                continue
            if not line.startswith(("/", "~", "http://", "https://", "file://")):
                line = os.path.join(base_dir, line)        # relative to the file
            if line.startswith("/") and not known:
                known = {}
                folder = os.path.dirname(os.path.realpath(line))
                for p, e in self.store.library.entries_under(folder).items():
                    known[p] = {k: v for k, v in e.items() if k not in ("f", "seen", "added")}
            built = self._entry_for_line(line, title, duration, known)
            title, duration = "", -1
            if built:
                entries.append(built)
        if not entries:
            raise SourceError("nothing in that file could be played", "empty")
        library = self.store.library
        made = library.create_playlist(name)
        clean = [c for c in (protocol.clean_item(e) for e in entries) if c]
        added = library.add_to_playlist(made["id"], [self._keepable(c) for c in clean])
        self._notice("Imported %s" % made["name"], "%d items" % added)
        return dict(made, count=added)

    # -- podcasts: played, new, downloaded ------------------------------------

    def _podcast_marks(self):
        marks = self.store.library.get_json("podcastPlayed", []) or []
        return set(m for m in marks if isinstance(m, str))

    def _set_played(self, uids, played):
        marks = self.store.library.get_json("podcastPlayed", []) or []
        marks = [m for m in marks if isinstance(m, str) and m not in uids]
        if played:
            marks = list(uids) + marks
        self.store.library.set_json("podcastPlayed", marks[:PODCAST_MARKS_KEPT])

    def _mark_episode_played(self, entry):
        if (entry or {}).get("source") == "podcast" and entry.get("kind") == "episode":
            self._set_played([entry.get("uid")], True)

    def _decorate_episodes(self, episodes, feed_url):
        """Played marks on episode rows, and "new" on the ones that came after
        the last time the show was opened or checked."""
        marks = self._podcast_marks()
        seen = self.store.library.get_json("podcastSeen", {}) or {}
        since = int(seen.get(feed_url) or 0)
        out = []
        for entry in episodes:
            entry = dict(entry)
            extra = dict(entry.get("extra") or {})
            extra["played"] = entry.get("uid") in marks
            extra["new"] = bool(since) and int(extra.get("published") or 0) > since \
                and not extra["played"]
            entry["extra"] = extra
            out.append(entry)
        return out

    def _show_opened(self, feed_url, episodes):
        """Opening a show catches it up: what is there now is no longer new."""
        newest = max([int((e.get("extra") or {}).get("published") or 0) for e in episodes]
                     or [0])
        seen = self.store.library.get_json("podcastSeen", {}) or {}
        if newest and int(seen.get(feed_url) or 0) < newest:
            seen[feed_url] = newest
            self.store.library.set_json("podcastSeen", seen)

    def _new_counts(self):
        return self.store.library.get_json("podcastNew", {}) or {}

    def cmd_podcast(self, message):
        """Mark episodes played or unplayed, or check the subscriptions now."""
        action = textutil.text(message.get("action"), 12)
        if action == "mark":
            uids = [textutil.text(u, 200) for u in (message.get("uids") or
                                                     [message.get("uid")]) if u]
            self._set_played(uids, textutil.bool_of(message.get("played"), True))
            return {"marked": len(uids)}
        if action == "check":
            POOL_ONE.submit(lambda: self._check_podcasts(manual=True))
            return {"checking": True}
        raise SourceError("unknown podcast action %r" % action, "unsupported")

    def _check_podcasts(self, manual=False):
        """Look for new episodes in every subscription: notify, count them on
        the show's row, and download them when Settings say so."""
        if netguard.offline():
            return
        seen = self.store.library.get_json("podcastSeen", {}) or {}
        counts = {}
        fresh_total = 0
        for show in list(self.custom.data().get("podcasts") or []):
            feed_url = show.get("feed") or ""
            try:
                info, episodes = podcast.feed(feed_url)
            except Exception as exc:  # noqa: BLE001 - one dead feed is not all
                self._log("podcast check %s: %s" % (feed_url[:80], exc))
                continue
            since = int(seen.get(feed_url) or 0)
            if not since:
                # First look at this show: from now on, not its whole back
                # catalogue.
                newest = max([int((e.get("extra") or {}).get("published") or 0)
                              for e in episodes] or [0])
                seen[feed_url] = newest
                continue
            fresh = [e for e in episodes
                     if int((e.get("extra") or {}).get("published") or 0) > since]
            if not fresh:
                continue
            counts[feed_url] = len(fresh)
            fresh_total += len(fresh)
            known = self.store.library.get_json("podcastAlerted", []) or []
            unseen = [e for e in fresh if e.get("uid") not in known]
            if unseen and self._on("podcastNotify", True):
                self._notice("New episode of %s" % textutil.text(info.get("title")
                                                                 or show.get("title"), 80),
                             textutil.text(unseen[0].get("title"), 120)
                             + ("" if len(unseen) == 1 else " (+%d more)" % (len(unseen) - 1)))
            if unseen and self._on("podcastAutoDownload", False):
                for entry in unseen[:3]:
                    try:
                        self._dl().add(entry, "audio")
                    except Exception as exc:  # noqa: BLE001
                        self._log("auto-download failed: %s" % exc)
            if unseen:
                self.store.library.set_json(
                    "podcastAlerted", ([e.get("uid") for e in unseen] + known)[:2000])
        self.store.library.set_json("podcastSeen", seen)
        self.store.library.set_json("podcastNew", counts)
        if manual:
            self._notice("Checked your podcasts",
                         "%d new episode%s" % (fresh_total, "" if fresh_total == 1 else "s"))

    def podcast_loop(self):
        """Check the subscriptions every few hours (Settings › Podcasts)."""
        time.sleep(120)
        while not self._shutting_down:
            try:
                if self.custom.data().get("podcasts"):
                    self._check_podcasts()
            except Exception as exc:  # noqa: BLE001
                self._log("podcast check failed: %s" % exc)
            hours = max(1, int(self._on("podcastCheckHours", 6) or 6))
            for _ in range(hours * 60):
                if self._shutting_down:
                    return
                time.sleep(60)

    # -- one search across every source --------------------------------------

    def cmd_search_all(self, message):
        """Search every source at once. Each source answers on its own, as a
        `search_all` event, so the quick ones show while YouTube still looks."""
        query = textutil.text(message.get("query"), 200)
        ident = message.get("id")
        if not query:
            raise SourceError("type something to search for", "empty")
        limit = textutil.int_in(message.get("limit"), 1, 20, SEARCH_ALL_LIMIT)
        sources = [s for s in ("radio", "tv", "local", "podcast", "youtube", "music")
                   if s in (message.get("sources") or
                            ("radio", "tv", "local", "podcast", "youtube", "music"))]
        if netguard.offline():
            sources = [s for s in sources if s in ("radio", "tv", "local")]
        settings = self.settings()
        budget = youtube.Budget(self.store, self._on("ytResolutionBudgetPerHour", 120))

        def one(source):
            mode = "search"
            try:
                rows = self._source_call(source, mode, {"query": query, "offset": 0,
                                                        "source": source},
                                         limit, settings, self.store.art_dir, budget)
                self.emit({"type": "search_all", "id": ident, "query": query,
                           "source": source, "items": (rows or [])[:limit]})
            except Exception as exc:  # noqa: BLE001 - a source failing is a row
                self.emit({"type": "search_all", "id": ident, "query": query,
                           "source": source, "items": [],
                           "error": textutil.text(str(exc), 200)})

        threads = [threading.Thread(target=one, args=(s,), daemon=True) for s in sources]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.emit({"type": "search_all", "id": ident, "query": query, "done": True})
        return None


# One background lane for slow podcast checks started from the panel.
POOL_ONE = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ap-podcast")
