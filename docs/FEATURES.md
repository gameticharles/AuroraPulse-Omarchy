# Feature Specification

Every feature is listed with its **origin** (where the idea came from), its
**surface** (where it shows up), and its **status**.

Status key: `P0` ships in the first release · `P1` first follow-up ·
`P2` later · `✗` intentionally excluded.

---

## 1. Bar surface

The bar is a *status* surface. It shows what is playing and offers the four
actions you take without thinking. Everything else lives behind the panel.

| # | Feature | Surface | Origin | Status |
|---|---|---|---|---|
| 1.1 | Contextual icon — source glyph (radio waves / TV / ▶ / ♪ / folder) tinted by state | bar | new | P0 |
| 1.2 | Waveform/level glyph in the icon; static when paused, flat when stopped | bar | Hertz | P0 |
| 1.3 | Now-playing marquee: bold artist + regular title, 38 px/s scroll, 2.5 s dwell | bar | Hertz | P0 |
| 1.4 | `showTitle` / `maxTitleWidth` settings; title hidden on vertical bars | bar | Hertz | P0 |
| 1.5 | Live state dot — accent when playing, dim when paused, gone when stopped | bar | new | P0 |
| 1.6 | Pin indicator in the icon when the PiP window is pinned | bar | new | P0 |
| 1.7 | Multi-PiP badge (`⚡2`) when more than one video window is open | bar | AuroraPulse | P1 |
| 1.8 | Left click toggles panel · middle click play/pause · right click stop · scroll volume | bar | Hertz | P0 |
| 1.9 | Bar button is draggable to another workspace via `Panel.popoutSwitching` | bar | omarchy | P2 |

## 2. Now-playing panel (the popup)

Opened from the bar. Sized in the shell's spacing units, theme-native.

| # | Feature | Notes | Status |
|---|---|---|---|
| 2.1 | Hero: 64 px duotone artwork, title, artist, source badge, live/paused/buffering state | Hertz | P0 |
| 2.2 | Status line with a **reason** on error ("rate limited", "stream ended", "codec unsupported") — never a bare "error" | new | P0 |
| 2.3 | Metadata row: `LIVE · opus 160k · Radio Paradise` style | Hertz | P0 |
| 2.4 | Transport: prev / play-pause / next / heart, plus **seek bar** for timed media | Hertz + new | P0 |
| 2.5 | Volume: mute + slider + numeric readout | Hertz | P0 |
| 2.6 | **Source-aware controls.** Seek/speed/EQ appear for tracks and video, hide for live radio | new | P0 |
| 2.7 | Tabbed browser: `Radio · TV · YouTube · Music · Local` | new | P0 |
| 2.8 | Unified search across all sources from one field, grouped by source with counts | AuroraPulse | P0 |
| 2.9 | Queue strip (next 3) with reorder-by-drag and "clear" | AuroraPulse | P0 |
| 2.10 | Sleep timer: 15/30/45/60/90 min, `zzz` glyph, cancel | AuroraPulse | P0 |
| 2.11 | Cross-source now-playing arbitration: starting any source stops the others | AuroraPulse | P0 |
| 2.12 | Volume/mute are remembered per output device, and re-applied on device change | AuroraPulse | P1 |
| 2.13 | "Take over" — jump the panel to whichever source is currently playing | new | P1 |
| 2.14 | Session restore banner when a stream survived the last shell restart | new | P1 |

## 3. Full player window (summoned panel)

A real window. This is where video, EPG, lyrics, and the queue live.

| # | Feature | Notes | Status |
|---|---|---|---|
| 3.1 | Video surface via mpv's own Wayland window, positioned by Hyprland | §3.2 arch | P0 |
| 3.2 | **PiP with pin**: `pin:2` + float + nofullscreenblocking; toggle from the bar or the player | user request | P0 |
| 3.3 | Size presets S/M/L (240/400/620 px) + free resize + corner snap to quadrant on release | AuroraPulse | P0 |
| 3.4 | Aspect-ratio cycle (`audio 16/9 4/3 21/9 fill`) | AuroraPulse | P0 |
| 3.5 | Auto-hiding overlay controls (3 s idle) | AuroraPulse | P0 |
| 3.6 | Screenshot to `~/Pictures/aurora-pulse/`, flash confirm in the shell | AuroraPulse | P0 |
| 3.7 | Subtitle track + audio track selection; subtitle size/colour/background settings | AuroraPulse | P0 |
| 3.8 | Quality selector (auto/144p…2160p) with **position-preserving** switching | AuroraPulse | P0 |
| 3.9 | Fullscreen; multiple windows each keep their own queue position | AuroraPulse | P1 |
| 3.10 | A glass PiP manager panel: thumbnails, per-window play/pause/mute/close | AuroraPulse | P2 |
| 3.11 | Live bandwidth + data-used meter (the height→bitrate heuristic, done properly) | AuroraPulse | P0 |
| 3.12 | Direct-dial numeric channel entry for TV | AuroraPulse | P1 |

## 4. Radio source

| # | Feature | Origin | Status |
|---|---|---|---|
| 4.1 | Radio Browser search with genre chips (28), country picker (240+), and free text | Hertz | P0 |
| 4.2 | Mirror discovery by reverse DNS + 4 fallbacks, tried in order | Hertz | P0 |
| 4.3 | Server-side paging, de-duplicated by uuid across pages (rankings shift) | Hertz | P0 |
| 4.4 | Favourites, with local substring/country filtering — no API call | Hertz | P0 |
| 4.5 | Result caching; offline shows the cache with a badge instead of a spinner | Hertz | P0 |
| 4.6 | Play reporting only after playback actually starts | Hertz | P0 |
| 4.7 | **M3U import/export**, with `#EXTINF` group/country parsing | AuroraPulse | P0 |
| 4.8 | CSV + JSON export/import of favourites and playlists | AuroraPulse | P1 |
| 4.9 | Custom station add (name/URL/tags/country) | AuroraPulse | P0 |
| 4.10 | **Full offline directory sync** (200 k stations, ~30 MB) with progress and a size warning | AuroraPulse | P1 |
| 4.11 | Minimum-bitrate and codec filters | AuroraPulse | P0 |
| 4.12 | Station recorder (stream → file) with a live level meter and size estimate | AuroraPulse | P1 |
| 4.13 | Scheduled station alarm with countdown | AuroraPulse | P1 |
| 4.14 | Geo coordinates → compact world map in the picker (a *lightweight* picker, not a WebGL globe) | AuroraPulse (simplified) | P2 |
| 4.15 | Auto-sync interval + manual "sync now" + last-sync display | AuroraPulse | P1 |
| 4.16 | Multiple stream URLs per station, cycled with a quality preference | AuroraPulse | P0 |

## 5. TV source

| # | Feature | Origin | Status |
|---|---|---|---|
| 5.1 | Playlist manager: add by URL, enable/disable, per-playlist sync, index browse | AuroraPulse | P0 |
| 5.2 | Curated default playlist index (iptv-org, Pluto, Plex, PBS, Stirr, tvpass) | AuroraPulse | P0 |
| 5.3 | Group by category / country / language; search across all enabled playlists | AuroraPulse | P0 |
| 5.4 | Favourites preserved across re-syncs | AuroraPulse | P0 |
| 5.5 | Channel logo rendering through the duotone shader, with a generated fallback tile | new | P0 |
| 5.6 | **EPG**: XMLTV gz, parsed on a worker, indexed per channel, TTL 12 h | AuroraPulse | P0 |
| 5.7 | Now/next in the channel row, and a "what's on" grid in the player | AuroraPulse (improved) | P0 |
| 5.8 | Program progress bar with elapsed/remaining | new | P0 |
| 5.9 | Custom EPG source URL, with the guide's channel-id → channel mapping table | AuroraPulse | P0 |
| 5.10 | Live stream kept alive across pause (10 s grace), like radio | Hertz | P0 |
| 5.11 | Direct dial numeric entry | AuroraPulse | P1 |
| 5.12 | XMLTV import from a local file | new | P1 |

## 6. YouTube source

| # | Feature | Origin | Status |
|---|---|---|---|
| 6.1 | Search (`ytsearchN:`), flat-playlist JSON, cancellable, cached per query | new | P0 |
| 6.2 | Video result rows: thumbnail, channel, duration, view count, publish age | new | P0 |
| 6.3 | Play a video → **PiP first** (never steals focus on play) | new | P0 |
| 6.4 | Playlists and channels: browse and enqueue whole lists | new | P0 |
| 6.5 | Watch Later / History / Favourites reading (needs opt-in cookies) | new | P1 |
| 6.6 | Format policy enum with an audio-only mode (streams the best audio, shows a note glyph) | new | P0 |
| 6.7 | Subtitle track selection + sidecar `.vtt`/`.srt` fetch into `downloads/` | new | P1 |
| 6.8 | Rate-limit backoff with jitter and an explicit "rate limited, retry in Nm" state | new | P0 |
| 6.9 | Resolution happens in a proxy sandbox; playback in a no-network sandbox | §3.1 | P0 |
| 6.10 | Downloads: quality picker, progress, pause (HTTP Range), resume, delete | new | P0 |
| 6.11 | Per-hour resolution budget with a visible counter in settings | new | P1 |
| 6.12 | "Open on the site" escape hatch for anything the extractor can't resolve | new | P0 |
| 6.13 | Age-gated / members-only detection with a precise message and a cookies prompt | new | P1 |
| 6.14 | SponsorBlock-style chapter markers — **out of scope**; noted as possible | ✗ | ✗ |

## 7. YouTube Music source

| # | Feature | Origin | Status |
|---|---|---|---|
| 7.1 | Music search via `music.youtube.com/search` + `YoutubeTab` second pass | new | P1 |
| 7.2 | Music result rows reuse `TrackRow` — album art, artist, album, duration | new | P0 |
| 7.3 | Audio-only playback path (no video window; the bar widget only) | new | P0 |
| 7.4 | Queue semantics identical to local music: shuffle, repeat, crossfade | AuroraPulse | P0 |
| 7.5 | Lyrics: YouTube timed lyrics first, LRCLib fallback | new | P0 |
| 7.6 | Library access (Liked, Library playlists) via opt-in cookies | new | P1 |
| 7.7 | Clear "sign in for your library" affordance, never auto-prompting for cookies | new | P1 |
| 7.8 | Radio-from-track (`yt-dlp` "radio" continuation) enqueued as a mix | new | P2 |

## 8. Local music source

| # | Feature | Origin | Status |
|---|---|---|---|
| 8.1 | Configurable scan roots, recursive, cancellable, progress, never blocking the panel | AuroraPulse | P0 |
| 8.2 | Tag extraction via `ffprobe -print_format json` (no Python audio deps) | new | P0 |
| 8.3 | Library views: Tracks / Albums / Artists / Genres / Folders / Playlists | AuroraPulse | P0 |
| 8.4 | SQLite store with play counts, last played, date added, favourite flag | AuroraPulse | P0 |
| 8.5 | 5 smart playlists: Recently Added · Most Played · History · Favourites · Long Tracks | AuroraPulse | P0 |
| 8.6 | **Lyrics**: LRCLib → AZLyrics → AfrikaLyrics → sidecar `.lrc` → manual entry | AuroraPulse | P0 |
| 8.7 | Synced lyrics with auto-scroll, active-line highlight, **double-tap-to-seek**, ±0.5 s offset adjuster | AuroraPulse | P0 |
| 8.8 | Manual-scroll detection pauses auto-scroll and offers "resume" | AuroraPulse | P0 |
| 8.9 | Lyrics cache on disk; can **write** a normalised `.lrc` back next to the file | AuroraPulse | P1 |
| 8.10 | 5-band EQ (60/230/910/4k/14k Hz, ±12 dB, 7 presets) via mpv `af=equalizer` | AuroraPulse | P0 |
| 8.11 | **Crossfade**: mpv `af=afade` out/in across the queue boundary, 0–12 s | AuroraPulse | P0 |
| 8.12 | Playlist create/edit/reorder/delete, persisted | AuroraPulse | P0 |
| 8.13 | Queue: reorder, remove, "play next", shuffle/repeat, progress persisted per second | AuroraPulse | P0 |
| 8.14 | Tag editor: title, artist, album, year, genre, track/disc, lyrics, artwork | AuroraPulse | P1 |
| 8.15 | Ambient accent colour extracted from artwork (border/glow only) | AuroraPulse | P1 |
| 8.16 | Playback speed, skip-silence, prevent-duplicates | AuroraPulse | P0 |
| 8.17 | Resume where you left off, per item | AuroraPulse | P0 |

## 9. Playlists, queue and history

| # | Feature | Notes | Status |
|---|---|---|---|
| 9.1 | One cross-source queue; items from all five sources interleave | new | P0 |
| 9.2 | Shuffle (seeded, reproducible from a stored seed) + repeat off/one/all | AuroraPulse | P0 |
| 9.3 | Crossfade only between two audio items (never across a video or a live stream) | new | P0 |
| 9.4 | Play-count and last-played, per uid, in SQLite | AuroraPulse | P0 |
| 9.5 | History view with "clear history" and a retention setting | AuroraPulse | P1 |
| 9.6 | Favourites are cross-source and stored in `state.json` | new | P0 |

## 10. System integration

| # | Feature | Notes | Status |
|---|---|---|---|
| 10.1 | **MPRIS** `org.mpris.MediaPlayer2.aurorapulse` — full property set + all transport methods | Hertz | P0 |
| 10.2 | Coexists with `omarchy.media`: only one MPRIS identity claims "now playing" at a time, last-writer-wins with a clear source badge | new | P0 |
| 10.3 | Media keys work (play/pause/next/prev/seek/stop) via MPRIS | Hertz | P0 |
| 10.4 | `omarchy-shell aurora-pulse <verb>` IPC verbs (see `PROTOCOL.md`) | Hertz | P0 |
| 10.5 | `ap-ctl` CLI for scripting: `search`, `play`, `pause`, `queue`, `epg`, `doctor` | new | P0 |
| 10.6 | Idle-aware: optionally pause on screen lock, resume on unlock | new | P1 |
| 10.7 | Notifications: track change, download complete, error, rate limited (toggleable) | new | P0 |
| 10.8 | `ap-ctl doctor` — one command that verifies bwrap, seccomp, mpv, pw-cat, ffmpeg, yt-dlp, wayland, DBus | new | P0 |

## 11. Settings (all declared in `manifest.json` `barWidget.schema`)

Every one of these is a first-class setting with a label, a description, and a
sensible default — the shell renders the settings UI from the manifest.

| Group | Keys |
|---|---|
| Bar | `showTitle`, `maxTitleWidth`, `showSourceBadge`, `showPipBadge` |
| Playback | `defaultSource`, `audioOnlyYouTube`, `crossfadeSec`, `playbackSpeed`, `skipSilence`, `preventDuplicates`, `resumePlayback` |
| Audio | `volume`, `rememberVolumePerDevice`, `equalizerEnabled`, `eqPreset`, `eqBands` |
| Video | `defaultQuality`, `pipSize`, `pipCorner`, `defaultAspect`, `autoHideControlsMs`, `hardwareDecode` |
| Radio | `minBitrate`, `defaultGenre`, `defaultCountry`, `reportPlays`, `autoSyncHours` |
| TV | `enabledPlaylists`, `epgUrl`, `epgRefreshHours`, `guideChannelMap` |
| YouTube | `ytCookiesPath`, `resolutionBudgetPerHour`, `searchResults`, `prefersCookies`, `downloadDir` |
| YouTube Music | `enabled`, `useYtLyrics` |
| Local | `scanRoots`, `audioExtensions`, `lyricsProviders`, `smartPlaylists` |
| Network | `proxyUrl`, `requestTimeoutSec`, `offlineMode` |
| Privacy | `sendPlaysToDirectory`, `telemetry` (always `false`), `clearCaches` |

---

## Explicitly excluded

| Excluded | Why |
|---|---|
| Movies portal / WebView ad-blocking | No Linux WebView in the Flutter original; YouTube covers the intent |
| MovieBox / aoneroom / netnaija catalog | Unfinished, scrapes, legally fragile |
| WebGL 3-D globe | ~10 MB + a WebEngine process for a picker |
| Voice assistant / command centre | Placeholders in the original, no engine |
| Casting | Requires a discovery protocol (DLNA/Chromecast) — separate project |
| Editing audio files (trim/fade/convert) in place | ffmpeg, not a media player |
| Cloud sync of favourites | Needs an account; the original's "sync" was a local Hive box |
