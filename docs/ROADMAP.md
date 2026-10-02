# Implementation Roadmap

Nine phases. Each ends in something you can run and something you can verify.
No phase starts before the previous one is demonstrably working — the failure
modes here are process leaks and wedged players, and those are much cheaper to
fix when the surface area is small.

---

## Phase 0 — Skeleton (the contract, proven)

**Goal:** the bar button opens, talks to a daemon, and a real radio stream
plays through PipeWire. Everything else is a variation on this.

- `manifest.json` with `service` + `bar-widget` + `panel` kinds
- `ap-ctl` skeleton: `Daemon`, `protocol`, `state`, `jobs`
- `Sandbox` audio profile, `mpv` client, `pw-cat` hand-off
- `Service.qml` (keepLoaded) owning the daemon + `Panel.qml` bar button
- `hello` handshake, `state` event, `play`/`toggle`/`stop`/`volume`
- `ap-ctl doctor`

**Done when:** `ap-ctl play <url>` plays a stream, `omarchy-shell aurora-pulse
toggle` opens the panel, killing the shell does not kill the audio, and
`ap-ctl doctor` is green.

---

## Phase 1 — Radio, complete

Port Hertz's radio path and make it ours.

- `sources/radio.py`: mirror discovery, paged search, de-dup by uuid, caching
- `Radio Browser` genre map (`data/genres.json`)
- Favourites, custom stations, M3U import/export, bitrate/codec filters
- Artwork pipeline: guarded download → sandboxed ffmpeg → strict PNG → duotone
- Panel: hero, transport, volume, genre chips, country picker, station list
- Keyboard map (§Keyboard in `FEATURES.md`)
- `mpris.py`

**Done when:** radio is genuinely better than Hertz — M3U import, multiple
stream URLs, min-bitrate filter, and a real `doctor`.

---

## Phase 2 — Local music, and the player matures

The features that make this an app rather than a radio player.

- `library.py` (SQLite) + `sources/local.py` (scan via `ffprobe`)
- Album/artist/genre/folder views, 5 smart playlists
- Queue with shuffle/repeat/crossfade, persisted per second
- `lyrics.py`: LRCLib → AZ → sidecar, synced rendering, offset, seek-on-tap
- `equalizer`: 5-band via mpv `af=equalizer`, 7 presets
- Resume-playback per item

**Done when:** a 10 k-track library scans in under 30 s without the panel
dropping a frame, and crossfade and EQ survive a track change.

---

## Phase 3 — YouTube

- `util/yt.py` — resolution in a proxy sandbox, TTL cache, format policy
- `sources/youtube.py` — search, playlists, channels, browse
- Video profile sandbox (`--vo=gpu`, no network, pre-resolved URL)
- Hyprland window management: float, pin, size, corner snap, focus
- `FullPlayer.qml`: video surface, transport, seek, quality, subtitles,
  snapshot, aspect ratio
- Downloads with Range resume
- Rate-limit backoff and a resolution budget

**Done when:** a YouTube video plays in a pinnable PiP, the shell can be
restarted mid-playback without interrupting it, and a 429 produces a calm
message instead of a retry storm.

---

## Phase 4 — TV

- `sources/iptv.py` — playlist manager, curated index, enable/disable
- `sources/epg.py` — XMLTV gz, background parse, per-channel index
- Now/next in rows, a guide grid in the player, programme progress
- Direct dial

**Done when:** a default playlist syncs, the guide lines up with the channels,
and re-syncing preserves favourites.

---

## Phase 5 — YouTube Music

- Music search via `music.youtube.com` + `YoutubeTab` second pass
- Audio-only path (no window), shared queue with local music
- Timed lyrics from YouTube with an LRCLib fallback
- Opt-in cookies for library access

**Done when:** a YT Music track plays through the same queue as a local file,
with lyrics, and no video window opens.

---

## Phase 6 — Robustness pass

This is the phase that decides whether the plugin is trusted.

- Kill/restart matrix: shell restart, daemon crash, player death, OOM
- `test_*.py` from `SECURITY.md` §10, all green
- Memory and CPU profile with a 20 k-track library and a video playing
- Idle behaviour: no timers when the panel is closed
- `ap-ctl doctor` covers every dependency
- Graceful degradation: no GPU, no bwrap, no yt-dlp, no D-Bus

**Done when:** nothing survives as a zombie, and every optional dependency has
a documented degraded path.

---

## Phase 7 — Polish

- Artwork duotone shader + a separate accent-extraction shader for the ambient
  tint (local music only)
- Notifications: track change, download complete, errors, rate limits
- Idle lock/unlock pause
- Volume per output device
- Tag editor, lyrics write-back, station recorder, station alarm
- CSV/JSON export, offline directory sync

---

## Phase 8 — Marketplace readiness

- `preview.png`, screenshots, `README.md`, `CHANGELOG.md`, `LICENSE`
- `SECURITY.md` reviewed by someone who did not write it
- `omarchy plugin add` from a clean clone
- Version, keywords, category, and the `barWidget.schema` in its final form

---

## Ordering rationale

| Why this order | |
|---|---|
| Radio first | It is the smallest complete path, and it is what Hertz already proved works in this exact host. |
| Local music second | It makes the player real (queue, crossfade, EQ, lyrics) and needs no network, so it is the safest place to build the shared machinery. |
| YouTube third | It is the biggest new risk — yt-dlp latency, format policy, GPU sandbox, window management. Building it after the player is solid means a failure is contained. |
| TV fourth | Structurally identical to YouTube (M3U + a player window); the only genuinely new part is the EPG. |
| YouTube Music fifth | It is YouTube with a worse extractor, so it only makes sense once YouTube works. |
| Robustness sixth | Deliberately *after* all the features, because robustness is only verifiable against a real feature set. |

## Definition of done for every phase

1. `python3 -m unittest discover -s tests -v` passes.
2. `ap-ctl doctor` is green.
3. `omarchy-shell shell rescanPlugins` reloads with no warnings in the log.
4. The README section for the phase is accurate.
5. No new `Popen` without a corresponding reap and a `finally` that tears down.
6. Every new remote string goes through the sanitiser and renders as `PlainText`.
