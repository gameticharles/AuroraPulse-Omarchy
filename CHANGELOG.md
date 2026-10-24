# Changelog

All notable changes to AuroraPulse for Omarchy. Newest first. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions
follow [Semantic Versioning](https://semver.org/).

Versions 1.0.0 to 1.5.0 were written up afterwards from the commit history.

## [1.6.1] - 2026-10-23

### Fixed

- Aurora Pulse lost the Omarchy theme colours after the Omarchy 4.0.6 update (Qt
  6.12). Qt 6.12 adds a global `Color` object to QtQuick that wins over the
  imported `qs.Commons` singleton, so `Color.background`, `Color.accent` and
  the rest were undefined and the plugin fell back to its hard-coded colours.
  Every use is now qualified as `Commons.Color` (`import qs.Commons as
  Commons`). Use the qualified form in new code; a bare `Color` means
  QtQuick's.

## [1.6.0] - 2026-10-10

### Added

- YouTube: a **Videos / Playlists** switch under the search box. Playlists
  searches YouTube's playlists; a result opens to every video in it, with
  **Play all**, **Shuffle**, **Add all to the queue** and **Save** (keeps it
  as one of your own playlists in Saved).
- YouTube Music: **Songs / Playlists / Albums**. Playlists are YouTube
  Music's own (with their song counts), falling back to community playlists
  when it has none; albums list their songs in album order, with the artist
  name cleaned of " - Topic".
- Pasting a YouTube or YouTube Music link that names a playlist (including a
  watch link with `list=`) into either tab opens that playlist at once.
- Playlist, album, folder and library-group rows play or queue whole from
  their own Play, Shuffle and Queue buttons and right-click menu, without
  opening them first.
- Local: **Play a folder…** (folder button beside the search box) opens the
  desktop's folder chooser (`omarchy-file-select`) and plays the chosen
  folder: every song and video in it and in the folders below it, in name
  order ("2" before "10"), hidden folders skipped. The folder can be
  anywhere, not only in your music folders; files the library already knows
  reuse its tags, others are read on the spot (sandboxed, as in a scan). The
  library's albums, artists, genres and folders get the same header with
  Play all, Shuffle and Queue.
- Backspace leaves an open playlist, album, folder or group.
- IPC: `folder <path>` plays a folder, `openFolder <path>` lists it without
  playing, `pickFolder` opens the chooser; `tab youtube:playlists`,
  `tab music:playlists` and `tab music:albums` open those searches.
- Daemon `source` modes: YouTube `playlists`, Music `playlists` and `albums`,
  Local `dir` (see docs/PROTOCOL.md).
- **Channel and artist pages**: right-click a YouTube video, **Go to channel**,
  for the channel's uploads; on a Music song, **Go to artist** for all of
  the artist's songs (the playlist behind their "Top songs"). Both open with
  Play all, Shuffle, Queue and Save.
- **Play next** (right-click any row): right after what is playing, for one
  entry or a whole playlist, album, folder or group, in order. An entry
  already waiting further down moves up rather than appearing twice.
- **Save the queue** as one of your playlists (Save, in the queue's header).
- **Keep playing when the queue ends** (on by default; Settings › General):
  after the last YouTube video, song or library track, more like it is added
  and plays on: YouTube's own mix for a video or song, the same artist then
  genre for a library track. **Start radio** (right-click a track) does the
  same on demand.
- **Recent folders**: the Play a folder button opens a menu with the chooser
  and the eight folders played last; a folder that is gone is marked.
- **Audio language**: a button in the player when a video, film or TV
  channel carries several audio tracks; an adaptive stream's renditions are
  listed once per language.
- **Video screenshots**: the camera button (or IPC `screenshot`) saves the
  frame on screen to Pictures › AuroraPulse, named after the video and the
  time in it.
- **TV direct dial**: on the TV tab, type a channel's number (now shown on
  each row) to play it; the channel picked shows while you type.
- **TV guide from a file**: Settings › TV › Import a guide file reads an
  XMLTV file (.xml or .xml.gz) whose programmes fill in the channels the
  automatic guides do not know, for a week.
- **Lyrics timing**: − and + move synced lyrics half a second earlier or
  later, remembered per track (click the figure to reset).
- **Your own lyrics**: Add lyrics (or the edit button) to paste or type them;
  lines with [mm:ss.xx] stamps are timed, anything else is plain. Yours win
  over every provider until you remove them.
- **Tag editor**: right-click a local track, **Edit tags…**: title, artist,
  album, album artist, genre, year, track number and a cover picture. The
  file is rewritten without re-encoding (ffmpeg, sandboxed, writing into a
  private folder; the result replaces the original in one rename) and read
  back into the library. Covers are embedded in MP3, M4A and FLAC, and saved
  beside other files.
- **Playlists as M3U files**: export one (the button in an open playlist's
  header, to Downloads) or import one (beside Create): local files, YouTube
  links and stream addresses are all understood.
- **Podcasts**: episodes are marked played when they finish (or by hand from
  the right-click menu) and new ones are marked; shows say how many episodes
  are new. Subscriptions are checked every few hours with a notification for
  new episodes, and can be downloaded automatically (Settings › Podcasts).
- **Search everything**: the magnifier in the tab row searches radio, TV,
  your library, podcasts, YouTube and YouTube Music at once, grouped by
  source as each answers, with Show all to continue in that tab. IPC
  `searchAll <words>`.
- **Long tracks**, a smart playlist of everything ten minutes or longer from
  your library and what you have played.
- IPC: `screenshot`, `radio`, `searchAll`.

### Fixed

- The YouTube `playlist` mode passed its arguments in the wrong order (the
  page size went in as the artwork folder) and cut a pasted link to 80
  characters, so a playlist could never be listed.
- A folder, playlist or album row could be added to the queue as itself and
  sit there unplayable.
- Ogg Vorbis and Opus files were listed by their file names: their tags are
  on the audio stream, and only the file's were read. The library is read
  again once to pick them up.

## [1.5.0] - 2026-10-03

### Changed

- Settings › Health and the missing-dependency banner: **Copy package names**
  copies a search for Omarchy's package picker, and **Open Install menu**
  opens Omarchy's own Install › Package menu, where the packages are picked
  and confirmed. AuroraPulse never installs anything itself.

### Security

- Publishing a finished download refuses path traversal and symlinks in the
  target folder.

## [1.0.5] - 2026-10-02

### Added

- Stream quality selection (SD, HD, Full HD) for channels that offer several,
  switched live and remembered.
- The list keeps its scroll position, and start-up opens on the tab and list
  of what is playing.

### Changed

- Hardware decoding is off by default.
- No unsandboxed fallback: without bubblewrap, features that decode or fetch
  media stop and say what to install.

## [1.0.0] - 2026-10-02

### Added

- First release: radio, TV, YouTube, YouTube Music, podcasts and local music
  and video in one bar widget, with one queue, picture-in-picture video,
  synced lyrics, a TV guide, downloads, recording, casting and MPRIS, all
  network and decoding work in a bubblewrap sandbox.
