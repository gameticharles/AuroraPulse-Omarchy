# Daemon Protocol

`ap-ctl daemon` is a line-oriented child process of `omarchy-shell`. The QML
side never opens a socket, never makes a request, and never touches the
network. It writes one JSON object per line to the daemon's **stdin** and reads
one JSON object per line from **stdout**.

```
Panel.qml ──stdin──▶ ap-ctl daemon ──▶ Radio Browser / iptv / yt-dlp / local fs
        ◀─stdout──              └──▶ mpv (sandbox) ──▶ pw-cat ──▶ PipeWire
        ◀─stdout──                                    └──▶ Wayland (video)
```

`stderr` is diagnostics only; the shell logs it. It is never parsed.

## Framing

- One UTF-8 JSON object per line, `\n` terminated. No embedded raw newlines —
  `json.dumps(..., ensure_ascii=True)` guarantees this even when a stream
  title contains a newline or a lone surrogate.
- **Maximum line length: 1 MiB.** An overlong line drops the buffer and is
  reported as a protocol error rather than being parsed.
- Requests may carry `"id"`; the reply then echoes it, which is how a future
  request/response mode will work without breaking the fire-and-forget paths.
- Unknown `type` values are ignored, never fatal. A newer shell must be able
  to talk to an older daemon.

## Requests (shell → daemon)

Every request is `{"cmd": "<name>", ...}`. All fields are validated and
length-capped; unknown fields are dropped.

### Session

| cmd | Fields | Effect |
|---|---|---|
| `hello` | `version?` | full state snapshot + `capabilities` |
| `ping` | — | `{"type":"pong"}` |
| `caps` | — | negotiated feature set for this daemon build |

### Transport

| cmd | Fields | Effect |
|---|---|---|
| `play` | `uid` or `url`, `source?`, `startAt?`, `video?` | resolve (if needed) and start playback |
| `toggle` | — | play/pause, preserving position for timed media |
| `pause` | — | pause; keeps the stream for `graceSec` then disconnects |
| `resume` | — | rejoin the live stream from the live edge |
| `stop` | — | stop and release the player |
| `next` / `prev` | — | move through the queue |
| `seek` | `to` (s) or `by` (s) | seek timed media |
| `setPosition` | `to` (s) | MPRIS SetPosition |
| `speed` | `value` | playback rate 0.25–4.0 |
| `volume` | `value` 0–100 | set volume |
| `mute` | `muted?` | toggle or set mute |
| `stopAfter` | `tracks` | stop after N items (AURORA-PULSE's timer) |

### Queue

| cmd | Fields | Effect |
|---|---|---|
| `queue.add` | `uid`s, `position?` (`next`\|`last`) | enqueue |
| `queue.remove` | `at` | remove by index |
| `queue.move` | `from`, `to` | reorder |
| `queue.clear` | `keepCurrent?` | clear |
| `queue.shuffle` | `on?` | seeded shuffle |
| `queue.repeat` | `mode` — `off`\|`one`\|`all` | repeat mode |

### Library and source actions

| cmd | Fields | Effect |
|---|---|---|
| `search` | `source`, `query`, `page`, `filters{}` | paged results; echoed with the same `key` |
| `countries` | — | radio country index |
| `favorites` | `op` — `list`\|`add`\|`remove`, `uid`? | favourites |
| `stations.import` | `path` or `text` | M3U import |
| `stations.export` | `format` — `m3u`\|`json`\|`csv` | writes to the cache, returns `path` |
| `playlists.sync` | `playlistId?` | sync all or one IPTV playlist |
| `epg.refresh` | `force?` | force an XMLTV refresh |
| `epg.now` | `channelId` | now/next for a channel |
| `library.scan` | `roots?`, `full?` | start a cancellable scan |
| `library.cancel` | — | cancel the running job |
| `library.query` | `view`, `filter`, `page` | tracks/albums/artists/genres/folders/playlists |
| `lyrics` | `uid`, `force?` | fetch lyrics (LRCLib → AZ → sidecar) |
| `lyrics.save` | `uid` | write a normalised `.lrc` beside the file |
| `download` | `uid`, `quality?` | enqueue a download |
| `download.cancel` | `jobId` | cancel a download |
| `downloads` | — | list download jobs |
| `subs` | `uid` | enqueue a track or video from an arbitrary source |

### Window / video

| cmd | Fields | Effect |
|---|---|---|
| `pip.open` | `uid`, `mode?` — `pip`\|`window`\|`fullscreen` | open a video window |
| `pip.close` | `id?` | close one or all |
| `pip.pin` | `id?`, `pinned?` | pin/unpin (float + `pin:2`) |
| `pip.size` | `id?`, `size?` \| `w`,`h` | resize |
| `pip.corner` | `id?`, `corner` — `tl`\|`tr`\|`bl`\|`br` | snap to a quadrant |
| `pip.focus` | `id?` | focus a specific window |
| `video.quality` | `quality` | switch, preserving position |
| `video.snapshot` | — | save a screenshot, return the path |
| `video.subtitles` | `track` | select a subtitle track |
| `video.audio` | `track` | select an audio track |

### Settings and control

| cmd | Fields | Effect |
|---|---|---|
| `settings.get` | — | every effective setting |
| `settings.set` | `key`, `value` | set one (mirrored into `shell.json` when the widget owns it) |
| `equalizer` | `preset?` \| `bands[]` | set the 5-band EQ |
| `crossfade` | `seconds` | set crossfade |
| `sleep` | `minutes` \| `0` | sleep timer |
| `alarm` | `at`, `uid?` | scheduled item |
| `notify` | `level`, `title`, `body` | shell notification request |
| `shutdown` | — | stop the player and exit cleanly |
| `doctor` | — | environment report |

## Events (daemon → shell)

### `state`

The single source of truth. Pushed on every change, and the first thing
`hello` returns.

```json
{
  "type": "state",
  "item": { "...MediaItem..." } | null,
  "mode": "off|playing|paused|buffering|error",
  "position": 41.2,
  "duration": 253.9,
  "live": true,
  "buffering": false,
  "title": "Get Lucky",
  "artist": "Daft Punk",
  "codec": "opus",
  "bitrate": 160,
  "volume": 70,
  "muted": false,
  "speed": 1.0,
  "favorite": false,
  "error": null,
  "errorReason": null,
  "hasNext": true,
  "hasPrev": false,
  "crossfade": 4.0,
  "sleepRemaining": 0,
  "windows": [{ "id": "pip-1", "uid": "yt:video:x", "pinned": true, "size": "m", "corner": "br" }],
  "rateLimitedUntil": 0,
  "libraryStats": { "tracks": 10422, "scanning": false, "progress": 0.0 }
}
```

`errorReason` is a stable enum — `network`, `resolve`, `expired`, `unsupported`,
`empty`, `rate-limited`, `sandbox`, `cancelled`, `not-found` — so the shell can
word it without string-matching a message.

### `results`

```json
{ "type": "results", "source": "youtube", "key": "q:daft punk|p:0",
  "page": 0, "items": [ ... ], "more": true, "cached": false, "error": null }
```

`key` is the canonical query. The shell drops any response whose `key` does not
match its current query, which is what makes out-of-order responses harmless.

### Other events

| type | When | Key fields |
|---|---|---|
| `art` | an artwork PNG is ready | `uid`, `path` |
| `genres`, `countries` | index loaded | `items[]` |
| `favorites` | favourites changed | `uids[]` |
| `queue` | queue changed | `items[]`, `index`, `shuffle`, `repeat` |
| `lyrics` | lyrics arrived | `uid`, `synced`, `lines[]`, `offset` |
| `epg` | guide loaded | `channels[]`, `programs[]`, `now` |
| `library` | scan progress / result | `stats`, `progress`, `phase` |
| `downloads` | a download job changed | `jobs[]` |
| `pip` | a video window changed | `windows[]` |
| `levels` | audio level meter (10 Hz) | `left`, `right`, `peak` |
| `settings` | a setting changed elsewhere | `key`, `value` |
| `notice` | something worth a toast | `level`, `title`, `body` |
| `error` | a command failed | `id?`, `message` |
| `pong` | reply to `ping` | — |

## Levels

Audio levels come from a single mpv `--af` tap and are emitted at 10 Hz, not
per frame, and **only while the panel or a video window is open**. A closed
panel emits nothing — a bar widget must not wake the CPU.

## Compatibility

- `hello` returns `protocolVersion` and a `capabilities` object. The shell
  branches on capability names, never on version numbers.
- Adding a field is always safe. Removing or retyping one is a breaking change
  and requires a new `protocolVersion`.
- The daemon is expected to outlive any single shell build. Unknown message
  types from a newer shell are ignored, not fatal.

## Terminal client

The same daemon is scriptable, so `ap-ctl` doubles as a CLI. It discovers the
running daemon through the single-instance lock in `$RUNTIME_DIR` and speaks the
identical protocol over a unix socket.

```bash
ap-ctl play "yt:video:FGBhQbmPwH8" --video
ap-ctl search radio --genre Jazz --country FI --page 0
ap-ctl search youtube "daft punk" -n 10
ap-ctl epg --now "BBC One"
ap-ctl queue add "local:/music/track.flac" next
ap-ctl lyrics --save "local:/music/track.flac"
ap-ctl download "yt:video:dQw4w9WgXcQ" --quality 1080p
ap-ctl doctor
ap-ctl dump-state            # the exact JSON the shell sees
```

## Shell IPC

Registered by `Panel.qml` on target `aurora-pulse`:

```bash
omarchy-shell aurora-pulse toggle        # open/close the panel
omarchy-shell aurora-pulse playPause
omarchy-shell aurora-pulse stop
omarchy-shell aurora-pulse next
omarchy-shell aurora-pulse previous
omarchy-shell aurora-pulse volumeUp      # and volumeDown, mute
omarchy-shell aurora-pulse powerOff      # stop playing and stop the daemon
omarchy-shell aurora-pulse powerOn
omarchy-shell aurora-pulse status        # "off", "idle" or "<mode>: <title>"
omarchy-shell aurora-pulse play "groove salad"   # first match in the open tab
omarchy-shell aurora-pulse tab queue     # radio, tv, youtube, music, local, queue, saved
omarchy-shell aurora-pulse search "jazz"
omarchy-shell aurora-pulse settings radio
omarchy-shell aurora-pulse view grid
```

Hyprland bindings:

```lua
o.bind("Super+M",       "omarchy-shell aurora-pulse toggle")
o.bind("Super+Shift+M", "omarchy-shell aurora-pulse playPause")
o.bind("Super+Ctrl+M",  "omarchy-shell aurora-pulse stop")
```

In the bar: click opens the panel, middle-click plays or pauses, right-click
stops, and the wheel changes the volume.

## How the daemon schedules work

The daemon reads requests on one thread and hands them to three lanes:

| Lane | Commands | Why |
|---|---|---|
| control (one thread, in order) | `state`, `toggle`, `volume`, `mute`, `seek`, `position`, `repeat`, `shuffle`, settings, queue edits | never wait on the network, so they answer in milliseconds |
| playback (one thread, newest wins) | `play`, `next`, `previous`, `stop`, `retry`, `shutdown` | each request takes a generation number; work a newer request has made pointless is abandoned |
| pool (four threads) | `source`, `lyrics`, `epg`, `scan`, `catalogue` | slow lookups never sit in front of a pause |

`stop` also cuts the sound directly on the reader thread, before anything
queued gets a turn. `state` reads a cache that mpv keeps current by pushing
property changes, so it never talks to the player. The shell does not poll:
state is pushed on every change and about once a second while playing.

### What `play` takes

```json
{"cmd":"play","items":[...rows...],"start":3}
```

The rows are the list the entry was picked from, so `next` and `previous`
step through the stations or tracks that were on screen. `{"cmd":"play",
"item":{...}}` plays a single entry.

### Extra `state` fields

`mode` (`off` · `loading` · `playing` · `paused` · `error`), `buffering`,
`live`, `streamTitle` (a station's ICY "now playing"), `hasNext`, `hasPrev`,
`queueLength`, `index`. The queue itself is sent as a separate `queue` event
only when it changes:

```json
{"type":"queue","index":3,"items":[{"uid":"radio:...","title":"...","source":"radio"}]}
```

Other events the panel listens for: `lyrics`, `epg`, `catalogue`, `pip`,
`notice`, `library` (a scan finished), `progress` (scan progress) and `bye`
(the daemon is exiting after `shutdown`).

### `art` (daemon → shell)

Emitted when a background artwork download lands. The list is sent
immediately with whatever was already cached, so this is how images appear
without the list ever waiting on an image host.

```json
{"type":"art","key":"radio-2940057c-...","path":"/home/you/.cache/aurora-pulse/art/radio-2940057c-....img"}
```

The shell matches `key` against `item.art.key` from the list and repaints that
row. `path` is always a local file that has already been verified with
`ffprobe`; it is never a URL for the compositor to fetch.

### `catalogue`

```json
{"cmd":"catalogue","sync":true,"full":true}
{"type":"result","catalogue":{"radio":59785,"tv":10985,"programmes":0,
  "state":{"radio":"ready","tv":"ready"},
  "radio_age":3600.0,"tv_age":7200.0}}
```

`radio` and `tv` are how many entries are in the local mirror. The first run
takes a minute or two and happens in the background; a browse before it
finishes is served from the network and is still correct.


## Library, queue and desktop integration

| cmd | Fields | Effect |
|---|---|---|
| `jump` | `index` | play that queue entry |
| `remove` / `move` / `clear` | `index` / `from`,`to` / — | edit the queue (the playing entry stays) |
| `favorite` | `item?`, `on?` | keep or drop a favourite; no item means what is playing |
| `saved` | — | emits `saved` with `favorites[]` and `history[]` |
| `forget` | `uid` (empty clears all) | remove from the history |
| `sleep` | `minutes` (0 cancels) | stop playing later; `sleepAt` in `state` |
| `custom` | `op`, `kind`, `fields`, `itemId` | your own stations, channels and playlists |
| `stations_import` / `stations_export` | `text`/`source` / `format`,`mine` | import or export stations |

`state` also carries `favorite`. The `queue` event lists each entry with its
title, artist, source, duration and artwork.

The daemon publishes **MPRIS** as `org.mpris.MediaPlayer2.aurorapulse` when
PyGObject is installed: media keys, `playerctl`, the lock screen and the
shell's media widget all control it. A radio station's ICY "now playing"
becomes the MPRIS title, with the station as the album.

Sound settings (`equalizerEnabled`, `eqPreset`, `normalizeVolume`) are applied
to the running player as an mpv `lavfi` filter chain without restarting it.

## Podcasts, downloads, recording, scrobbling, the guide, the video window

| cmd | Fields | Effect |
|---|---|---|
| `source` (podcast) | `mode`: `browse` (subscriptions, or the top shows) · `search` · `episodes` + `feed` | the list reply for `episodes` also carries `show` |
| `custom` (podcast) | `op`: `add`/`remove`, `fields.feed` | subscribe or unsubscribe |
| `download` | `item?`, `kind`: `audio`/`video` | queue a yt-dlp download into Music/Videos › AuroraPulse |
| `download_cancel` / `downloads` | `jobId` / — | cancel or remove a job; emits `downloads` with `jobs[]` |
| `record` | `on?` | record the live stream; saved to Music › AuroraPulse › Recordings, emits `recorded` |
| `epg_grid` | `channels[]` (`uid`, `title`, `id`, `country`), `hours` | emits `epg_grid` with `rows[]` of programmes |
| `pip` | `action`: `size`(+`size` s/m/l/xl) · `corner`(+`corner`) · `float` · `unfloat` · `pin` · `fullscreen` · `close` | places the video window |
| `lastfm_login` / `lastfm_logout` | `apiKey`, `apiSecret`, `user`, `password` | only the session key is stored |
| `scrobble_status` | — | emits `scrobble` with the last result per service |

TV guides come from epgshare, one file per country, fetched on demand and
cached for `epgRefreshHours`; channels are matched by normalised name and id.
`state` also carries `recording` (epoch seconds since, or 0).
