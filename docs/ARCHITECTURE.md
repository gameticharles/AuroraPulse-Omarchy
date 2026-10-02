# AuroraPulse for Omarchy — Architecture

How the plugin is put together, as built. For the wire format see
[PROTOCOL.md](PROTOCOL.md); for the threat model see [SECURITY.md](SECURITY.md).

## 1. What it is

One bar widget that is a complete media player: internet radio, IPTV with a
programme guide, YouTube, YouTube Music, podcasts and your own music and
videos, behind one queue and one set of controls. It is the Omarchy
counterpart of the AuroraPulse Flutter app; settings, sources and features
follow that app where a desktop panel can carry them.

| Source | Where it comes from | How it plays |
|---|---|---|
| Radio | Radio Browser, mirrored into SQLite; your own stations | stream through the guarded proxy |
| TV | iptv-org, mirrored; your own M3U playlists and channels; epgshare guides | HLS through the guarded proxy, in a video window |
| YouTube / YouTube Music | yt-dlp (search, resolve), InnerTube for Music search | resolved by the daemon, played by mpv |
| Podcasts | iTunes Search API for discovery, the show's own RSS for episodes | stream, or download |
| Local | your music and video folders, indexed in SQLite | the file, bound read-only into the sandbox |

## 2. Processes

```
omarchy-shell (Quickshell)                 unsandboxed host, long-lived
  Panel.qml  ── JSON lines on stdin/stdout ──┐
                                             ▼
ap-ctl daemon                     python3, stdlib (+ PyGObject for MPRIS)
  · all state, all network I/O, all decisions
  · MPRIS, scrobbling, downloads, recording, casting, the alarm
  │ spawns, in its own process group
  ▼
ap-ctl session                    one per player; outlives a shell reload
  ├── GuardedProxy  unix socket → public internet only
  ├── bwrap (audio | video) ──► mpv ──raw PCM──► pw-cat ──► PipeWire
  │                               └─(video)──► its own Wayland window
  └── mpv.sock in a 0700 runtime dir shared by bind mount

one-shot sandboxed helpers, started by the daemon:
  bwrap (artwork) ──► ffmpeg/ffprobe       thumbnails, tags, image checks
  bwrap (artwork) ──► ap-ctl parse-feed    podcast RSS → JSON
  bwrap (fetcher) ──► yt-dlp               resolve, search, download
```

The player is a separate process on purpose. The shell can rebuild the widget
and the daemon can restart; the new daemon finds the running session through
`player.pid` (checked against `/proc/<pid>/stat` start time, so a recycled pid
is never mistaken for it) and reattaches to its socket.

### 2.1 Three lanes in the daemon

Every request is routed by command name:

| Lane | Commands | Rule |
|---|---|---|
| control | volume, pause, seek, mute, queue edits, favourites, playlists, alarm, subtitles | one thread, in order, never waits on the network |
| playback | play, next, previous, stop, retry | one thread, newest wins: each request bumps a generation and older work stops at the next checkpoint |
| pool | browse, search, lyrics, the guide, scans, downloads, casting | four threads |

So a slow YouTube search never sits in front of a pause, and clicking five
stations in a row plays the fifth without waiting for the first four.

State reaches the panel as events. mpv's properties are observed into a local
cache, and `emit_state` reads only that cache: it never asks the player.

## 3. Sandboxing

`util/sandbox.py` builds every bubblewrap command line: new user, mount, PID,
IPC and UTS namespaces, no capabilities, read-only `/usr`, a small tmpfs
`/tmp` and `/run`, no `/home`, and a seccomp filter (ptrace, mount, namespace
changes, kernel modules, bpf, perf, userfaultfd, handle-based opens and more
are refused). Memory and core limits are applied with `prlimit` *inside* the
sandbox: an `RLIMIT_NPROC` on bwrap itself makes namespace creation fail with
EAGAIN.

| Profile | Used by | Gets | Network |
|---|---|---|---|
| `audio` | the player | nothing of yours | the guarded proxy only |
| `video` | the player | `/dev/dri`, the Wayland socket | the guarded proxy only |
| `artwork` | ffmpeg, ffprobe, the RSS parser | the one file it reads, the one folder it writes | none |
| `fetcher` | yt-dlp | DNS config, the one download folder | direct |

The plugin's own code runs inside the sandbox from a sealed memfd mounted at
`/opt/aurora-pulse/ap-ctl`, so nothing under `$HOME` has to be visible.

`fetcher` is the deliberate gap: yt-dlp talks to YouTube's APIs, which a
stream proxy cannot vet, so it shares the host network. It sees no files but
its own staging folder. SECURITY.md says what that leaves open.

Downloads are written by yt-dlp into a private staging folder
(`.aurorapulse-<job>` next to the destination) and moved into place under a
name nothing else uses. A download can therefore never overwrite, or delete as
an "intermediate", a file it did not create.

Recordings are written by mpv itself (`stream-record`) into
`Music/AuroraPulse/Recordings/.recording`, bound into the session at
`/run/aurora-rec`, and moved out when they end. mpv buffers a recording in
256 KB blocks and writes the last one about a second after being told to
stop, so whatever ends the player waits for that flush first.

## 4. Persistence

| Path | Holds | How it is written |
|---|---|---|
| `~/.local/share/aurora-pulse/state.json` | settings and small bookkeeping | atomic replace, 0600, debounced |
| `~/.local/share/aurora-pulse/library.db` | favourites, history, play counts, resume points, playlists, the queue, downloads, the local index | SQLite WAL, one row per change |
| `~/.local/share/aurora-pulse/catalogue.db` | the radio and TV mirror | SQLite + FTS5, rebuilt atomically |
| `~/.local/share/aurora-pulse/custom.json` | your stations, channels, M3U playlists, podcasts | atomic JSON |
| `$XDG_RUNTIME_DIR/aurora-pulse/` | session.json, player.pid, mpv.sock, proxy.sock, subtitles in use | 0700, gone at reboot |
| `~/.cache/aurora-pulse/` | artwork, list and YouTube caches, guides | pruned |

`core/library.py` replaced the library half of state.json. On first open it
imports favourites, history, resume points, the queue and the local index,
commits, and only then removes those keys from state.json. A damaged database
is moved aside (`library.db.broken-<time>`) and a new one started; it is never
deleted.

The queue is saved (debounced) whenever it changes and restored at launch
without playing. Download jobs are saved on every state change; one that was
running when the daemon went away starts again from nothing.

## 5. Playback details

**One player per shape.** Switching between two streams of the same kind
(audio→audio, video→video) loads into the running mpv. Changing shape, or
playing a local file (which has to be bound into the sandbox), starts a new
session.

**Audio chain.** mpv's `af` is, in order: a labelled gain stage
(`@apfade`), skip-silence (tracks only, never live), the equalizer (a preset or
five custom bands), and loudness normalisation. Fades move only the gain stage
with `af-command`, so the volume you set is never touched. Fade between tracks,
the sleep timer's fade-out and the alarm's fade-in all use that one ramp.

**Video window.** Before a video session starts, the daemon registers a
Hyprland window rule over `hyprctl eval` (Lua, Hyprland 0.56): match the app id
`org.aurorapulse.video`, float, size and corner on the chosen monitor. mpv is
started with that app id, so Omarchy's generic "centre every mpv window" rule
does not apply and the window opens in place. Older Hyprlands without `eval`
get the window placed after it appears. Later moves use `hl.dsp.window.*`
dispatches, with the legacy syntax as a fallback.

**Subtitles.** The daemon lists what a video offers: YouTube's tracks (its own
language and English first, machine translations skipped) or files beside a
local video (`Film.srt`, `Film.en.srt`). It fetches or copies the chosen one
into the runtime folder the player already sees, then `sub-add`s it. Embedded
tracks are picked by mpv id.

**Volume per output.** The default PipeWire sink is polled with `wpctl`; when
it changes, the volume last used on the new output comes back.

**Play on.** `core/outputs.py` lists PipeWire outputs (`pactl`) and paired
Bluetooth audio devices (`bluetoothctl`). Choosing one moves only this
daemon's stream - tagged with its runtime folder - and is passed to every new
player as `pw-cat --target`; PipeWire falls back to the default output when
that one is gone. A paired Bluetooth device is connected first.

**Casting.** `core/cast.py` finds DLNA renderers with SSDP and drives them with
AVTransport/RenderingControl SOAP, and finds Chromecasts and Google TVs through
avahi-daemon and drives them with its own Cast client (TLS, protobuf frames,
the default media receiver). Omarchy's firewall drops the unicast answers to a
multicast SSDP search, so every address on the local subnet is also asked
directly; those answers match a request we sent and get through. Casting is the
one module allowed to reach private addresses, and only ones that answered. A
local file is served by a one-file HTTP server: one random path, one client
address, range requests.

**Alarm.** A ten-second scheduler rings once per day at the set time and
days, with a minute of grace for a machine waking from suspend. It plays the
chosen station at the wake-up volume and fades in through the gain stage.

## 6. The catalogue

Radio Browser (~60,000 stations) and iptv-org (~11,000 channels) are mirrored
into `catalogue.db` in the background on first run, then refreshed on the
schedule in Settings. Every browse, search and filter is a local query that
takes milliseconds; the network is a fallback for a first run that has not
finished. A partial download is never published over a complete one.

Artwork is never fetched inline: rows are sent first, images are fetched on a
background pool, checked, and announced with an `art` event when they land.

## 7. Module map

```
ap-ctl                     the one executable (daemon, session, sandbox entry, selftest)
src/apctl/core/
  daemon.py                commands, lanes, state, playback, fades, alarm, subtitles
  session.py               the player session: proxy, bwrap, pw-cat
  player.py                mpv JSON IPC client, mpv command line
  library.py               SQLite library: favourites, playlists, queue, index …
  db.py, sync.py           the catalogue mirror and its sync
  downloads.py             yt-dlp download queue, staging, formats
  cast.py                  DLNA discovery and control, file server, Chromecast
  doctor.py                health checks (Settings › Health, selftest)
  mpris.py                 MPRIS over Gio
  scrobble.py              ListenBrainz and Last.fm
  resolver.py              yt-dlp resolve, subtitles, ffprobe helpers, LRC parsing
  artwork.py, custom.py, repair.py, protocol.py, state.py, selftest.py, legacy.py
src/apctl/sources/         radio, tv (+ guide), youtube, music, podcast, local
src/apctl/util/            sandbox, netguard, textutil, taxonomy, health

Panel.qml                  bar widget and the panel; owns the daemon process
Browser.qml                tabs, search, chips, list/grid, keyboard cursor, guide grid
SavedView.qml              favourites, playlists, history, downloads
QueueView.qml, SettingsView.qml, LyricsView.qml, GuideView.qml, GuideGrid.qml
DownloadMenu.qml, PlaylistMenu.qml, SeekBar.qml, VolumeControl.qml, Spinner.qml …
components/                settings rows, buttons, inputs
tests/                     unit, regression, QML-structure, feature and live-load tests
```

## 8. Testing

| Suite | Covers |
|---|---|
| `tests/test_units.py` | sanitising, the network guard, sandbox flags, parsers |
| `tests/test_regressions.py` | every bug that was fixed, pinned |
| `tests/test_responsiveness.py` | lanes, newest-wins playback, no blocking on the control lane |
| `tests/test_catalogue.py` | the mirror, FTS, facets, sync invariants |
| `tests/test_features.py` | library and migration, playlists, download resume, health, fades, EQ, LRC files, subtitles, alarm, casting (against a fake renderer), podcasts, scrobbling, the guide, MPRIS, the sandboxed tool runner, the recording flush |
| `tests/test_qml.py` | QML structure: wiring, ids, null guards, duplicate members, qmllint |
| `tests/test_panel_load.py` | opt-in (`AP_LIVE_LOAD=1`): sync, restart the shell, read its log |

`./ap-ctl selftest` runs the machine checks: binaries, yt-dlp age, PyGObject,
folders, both player sandbox profiles for real, the isolation self-check from
inside a sandbox, and the network guard.
