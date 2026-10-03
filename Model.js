.pragma library

// Pure helpers for the AuroraPulse panel. No QML types, no state, no side
// effects: everything here is a function of its arguments so the panel stays
// declarative and these stay testable.

// Nerd Font glyphs (Material Design), matching the rest of the shell.
//
// Every codepoint here was checked against the glyph names in the installed
// JetBrainsMono Nerd Font (md-play, md-repeat, ...). Several used to be off by
// a few codepoints and drew something unrelated: repeat was a tree, shuffle
// the Skype logo, radio a lightning bolt, lyrics a knife and fork, the pin a
// house and the loading spinner a grid. A wrong icon reads as a broken
// control, so the name each one was taken from is kept beside it.
var ICON = {
  play: String.fromCodePoint(0xF040A),         // md-play
  pause: String.fromCodePoint(0xF03E4),        // md-pause
  previous: String.fromCodePoint(0xF04AE),     // md-skip_previous
  next: String.fromCodePoint(0xF04AD),         // md-skip_next
  stop: String.fromCodePoint(0xF04DB),         // md-stop
  power: String.fromCodePoint(0xF0425),        // md-power
  heart: String.fromCodePoint(0xF02D1),        // md-heart
  heartOutline: String.fromCodePoint(0xF02D5), // md-heart_outline
  search: String.fromCodePoint(0xF0349),       // md-magnify
  close: String.fromCodePoint(0xF0156),        // md-close
  volumeHigh: String.fromCodePoint(0xF057E),   // md-volume_high
  volumeMedium: String.fromCodePoint(0xF0580), // md-volume_medium
  volumeLow: String.fromCodePoint(0xF057F),    // md-volume_low
  volumeOff: String.fromCodePoint(0xF0581),    // md-volume_off
  repeat: String.fromCodePoint(0xF0456),       // md-repeat
  repeatOne: String.fromCodePoint(0xF0458),    // md-repeat_once
  shuffle: String.fromCodePoint(0xF049D),      // md-shuffle
  music: String.fromCodePoint(0xF0387),        // md-music_note
  radio: String.fromCodePoint(0xF0439),        // md-radio
  tv: String.fromCodePoint(0xF0502),           // md-television
  guide: String.fromCodePoint(0xF0503),        // md-television_guide
  youtube: String.fromCodePoint(0xF05C3),      // md-youtube
  ytMusic: String.fromCodePoint(0xF0333),      // md-music_box_multiple
  playlist: String.fromCodePoint(0xF0412),     // md-playlist_plus
  lyrics: String.fromCodePoint(0xF039E),       // md-note_text
  pin: String.fromCodePoint(0xF0403),          // md-pin
  settings: String.fromCodePoint(0xF0493),     // md-cog
  folder: String.fromCodePoint(0xF1359),       // md-folder_music
  alert: String.fromCodePoint(0xF0026),        // md-alert
  spinner: String.fromCodePoint(0xF0772),      // md-loading
  refresh: String.fromCodePoint(0xF0450),      // md-refresh
  download: String.fromCodePoint(0xF01DA),     // md-download
  chevronDown: String.fromCodePoint(0xF0140),  // md-chevron_down
  chevronRight: String.fromCodePoint(0xF0142), // md-chevron_right
  back: String.fromCodePoint(0xF004D),         // md-arrow_left
  grid: String.fromCodePoint(0xF0570),         // md-view_grid
  list: String.fromCodePoint(0xF0572),         // md-view_list
  plus: String.fromCodePoint(0xF0415),         // md-plus
  remove: String.fromCodePoint(0xF09E7),       // md-delete_outline
  paste: String.fromCodePoint(0xF0192),        // md-content_paste
  copy: String.fromCodePoint(0xF018F),         // md-content_copy
  exportFile: String.fromCodePoint(0xF021D),   // md-file_export
  importFile: String.fromCodePoint(0xF0220),   // md-file_import
  playlistTv: String.fromCodePoint(0xF0CB8),   // md-playlist_music
  folderAdd: String.fromCodePoint(0xF0257),    // md-folder_plus
  star: String.fromCodePoint(0xF04CE),         // md-star
  earth: String.fromCodePoint(0xF01E7),        // md-earth
  server: String.fromCodePoint(0xF048B),       // md-server
  sync: String.fromCodePoint(0xF04E6),         // md-sync
  sleep: String.fromCodePoint(0xF051B),        // md-timer_outline
  tune: String.fromCodePoint(0xF062E),         // md-tune
  checked: String.fromCodePoint(0xF0132),      // md-checkbox_marked
  unchecked: String.fromCodePoint(0xF0131),    // md-checkbox_blank_outline
  queue: String.fromCodePoint(0xF0411),        // md-playlist_play
  up: String.fromCodePoint(0xF005D),           // md-arrow_up
  down: String.fromCodePoint(0xF0045),         // md-arrow_down
  history: String.fromCodePoint(0xF02DA),      // md-history
  saved: String.fromCodePoint(0xF0E15),        // md-bookmark_multiple
  clearAll: String.fromCodePoint(0xF05E9),     // md-delete_sweep
  equalizer: String.fromCodePoint(0xF0EA2),   // md-equalizer
  podcast: String.fromCodePoint(0xF0994),      // md-podcast
  video: String.fromCodePoint(0xF0567),        // md-video
  downloadOutline: String.fromCodePoint(0xF0B8F), // md-download_outline
  record: String.fromCodePoint(0xF044A),       // md-record
  fullscreen: String.fromCodePoint(0xF0293),   // md-fullscreen
  pip: String.fromCodePoint(0xF0E57),          // md-picture_in_picture_bottom_right
  unpin: String.fromCodePoint(0xF0404),        // md-pin_off
  windowClose: String.fromCodePoint(0xF05AD),  // md-window_close
  cornerTL: String.fromCodePoint(0xF005B),     // md-arrow_top_left
  cornerTR: String.fromCodePoint(0xF005C),     // md-arrow_top_right
  cornerBL: String.fromCodePoint(0xF0042),     // md-arrow_bottom_left
  cornerBR: String.fromCodePoint(0xF0043),     // md-arrow_bottom_right
  rss: String.fromCodePoint(0xF046B),          // md-rss
  ok: String.fromCodePoint(0xF05E0),           // md-check_circle
  failed: String.fromCodePoint(0xF0028),       // md-alert_circle
  folderDownload: String.fromCodePoint(0xF024D), // md-folder_download
  health: String.fromCodePoint(0xF05F6),       // md-heart_pulse
  alarm: String.fromCodePoint(0xF0020),        // md-alarm
  cast: String.fromCodePoint(0xF0118),         // md-cast
  subtitles: String.fromCodePoint(0xF0A16),    // md-subtitles
  album: String.fromCodePoint(0xF0025),        // md-album
  artist: String.fromCodePoint(0xF0803),       // md-account_music
  genre: String.fromCodePoint(0xF04F9),        // md-tag
  save: String.fromCodePoint(0xF0193),         // md-content_save
  textSize: String.fromCodePoint(0xF027F),     // md-format_size
  cloud: String.fromCodePoint(0xF0163),        // md-cloud_outline
  cloudOff: String.fromCodePoint(0xF0164),     // md-cloud_off_outline
  speaker: String.fromCodePoint(0xF04C3),      // md-speaker
  headphones: String.fromCodePoint(0xF02CB),   // md-headphones
  bluetooth: String.fromCodePoint(0xF00AF),    // md-bluetooth
  monitor: String.fromCodePoint(0xF0379)       // md-monitor
}

// The facet value meaning "only what I added" - see MINE in core/db.py.
var MINE = "__mine__"

function facetLabel(value, source) {
  if (value === MINE) return source === "tv" ? "My channels" : "My stations"
  return String(value || "")
}

// The bar icon follows the source of whatever is playing, so a glance at the
// bar says which kind of thing is making noise: a radio, a TV channel, a
// YouTube video, or a track from disk. "local" was a folder, which described
// where the file lives rather than what is playing; a note describes the
// sound. Both YouTube sources share the YouTube logo and are told apart by
// their colour and their title, since they are the same service.
var SOURCE = {
  radio: { label: "Radio", icon: ICON.radio, color: "#f0a13c" },
  tv: { label: "TV", icon: ICON.tv, color: "#5b8cff" },
  youtube: { label: "YouTube", icon: ICON.youtube, color: "#ff4a4a" },
  music: { label: "Music", icon: ICON.ytMusic, color: "#d24bd8" },
  local: { label: "Local", icon: ICON.music, color: "#3fc98a" },
  podcast: { label: "Podcasts", icon: ICON.podcast, color: "#2ec4b6" }
}

var REASON = {
  network: "Could not reach the network",
  resolve: "Could not work out where that stream is",
  expired: "That stream is no longer available",
  unsupported: "That cannot be played here",
  empty: "Nothing to play",
  "rate-limited": "YouTube is rate limiting us; try again in a bit",
  sandbox: "The player could not be started",
  cancelled: "Cancelled",
  "not-found": "That is gone",
  denied: "Blocked by the network policy"
}

function sourceOf(name) {
  return SOURCE[String(name || "")] || { label: String(name || ""), icon: ICON.music, color: "#888888" }
}

// Blend two colours in JS. The shell exposes a mix() helper, but this panel
// also has to work when it is rendered outside a bar (the popup), where the
// host's helper is not guaranteed to be in scope.
function mix(a, b, amount) {
  var t = Math.max(0, Math.min(1, Number(amount) || 0))
  return Qt.rgba(a.r + (b.r - a.r) * t,
                 a.g + (b.g - a.g) * t,
                 a.b + (b.b - a.b) * t,
                 (a.a === undefined ? 1 : a.a) + ((b.a === undefined ? 1 : b.a) - (a.a === undefined ? 1 : a.a)) * t)
}

function volumeIcon(volume, muted) {
  if (muted || volume <= 0) return ICON.volumeOff
  if (volume < 34) return ICON.volumeLow
  if (volume < 67) return ICON.volumeMedium
  return ICON.volumeHigh
}

// Two letters for the artwork fallback tile: "Radio Paradise" -> "RP".
function initials(name, fallback) {
  var words = String(name || "").replace(/[^\p{L}\p{N} ]/gu, " ").trim().split(/\s+/)
  if (!words.length || !words[0]) return fallback || "AP"
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase()
  return (words[0][0] + words[1][0]).toUpperCase()
}

// Radio Browser names carry the stream format and the country; the panel shows
// format separately, so that noise comes off the title.
function cleanName(name) {
  var n = String(name || "")
  var cleaned = n
    // A bracket holding only format words goes whole: "(128k MP3)", "[AAC+]".
    // Removing the words one at a time left the closing bracket behind, so
    // "SomaFM Groove Salad (128k MP3)" came out as "SomaFM Groove Salad)".
    .replace(/\s*[\(\[](\s*(\d{2,4}\s?k(bps)?|AAC\+?|MP3|OGG|OPUS|FLAC|HLS|AAC|kbps)\s*)+[\)\]]/gi, "")
    .replace(/\s*[\(\[]?\b\d{2,4}\s?k(bps)?\b[\)\]]?/gi, "")
    .replace(/\s*\b(AAC\+?|MP3|OGG|OPUS|FLAC|HLS)\b/gi, "")
    .replace(/\s*[-–|:]\s*$/, "")
    .replace(/\s{2,}/g, " ")
    .trim()
  return cleaned || n
}

// "Artist - Song" -> { artist, song }.
function splitTitle(title) {
  var t = String(title || "").trim()
  var m = t.match(/^(.+?)\s+[-–—]\s+(.+)$/)
  if (!m) return { artist: "", song: t }
  return { artist: m[1].trim(), song: m[2].trim() }
}

// Seconds -> m:ss, or h:mm:ss past an hour. Live streams get "" on purpose:
// showing 0:00 for something that has no end is worse than showing nothing.
function duration(seconds) {
  var total = Math.floor(Number(seconds) || 0)
  if (total <= 0) return ""
  var h = Math.floor(total / 3600)
  var m = Math.floor((total % 3600) / 60)
  var s = total % 60
  function pad(n) { return n < 10 ? "0" + n : "" + n }
  if (h) return h + ":" + pad(m) + ":" + pad(s)
  return m + ":" + pad(s)
}

// "1 station" / "59,826 stations". The thousands separator matters at the
// scale a catalogue count reaches: 59826 is a number nobody reads at a glance.
function count(n, one, many) {
  var value = Number(n) || 0
  var parts = String(Math.round(value)).split(".")
  parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ",")
  return parts.join(".") + " " + (value === 1 ? one : many)
}

// Bytes at the size a catalogue download reaches. 24117216 is unreadable and
// 23.0 MB is a different kind of lie once it is over a gigabyte, so the unit
// steps rather than pretending one scale covers everything.
function bytes(n) {
  var value = Number(n) || 0
  if (value < 1024) return value + " B"
  var units = ["KB", "MB", "GB", "TB"]
  var size = value / 1024
  var i = 0
  while (size >= 1024 && i < units.length - 1) { size = size / 1024; i++ }
  return (size >= 100 ? size.toFixed(0) : size.toFixed(1)) + " " + units[i]
}

function quality(item) {
  if (!item) return ""
  var parts = []
  var codec = String(item.codec || "")
  if (codec && codec !== "UNKNOWN") parts.push(codec)
  // Radio Browser publishes kbps (128); other sources bits per second. A
  // blanket divide by a thousand showed every station as "0k".
  var bitrate = Number(item.bitrate || 0)
  var kbps = bitrate >= 1000 ? Math.round(bitrate / 1000) : Math.round(bitrate)
  if (kbps > 0) parts.push(kbps + "k")
  return parts.join(" ")
}

// A local path is the only thing the compositor is ever asked to load for
// artwork. Remote art has already been downloaded and verified by the daemon,
// so an empty path here means "no artwork", never "fetch it yourself".
function artPath(item) {
  if (!item || !item.art) return ""
  return String(item.art.path || "")
}

function artUrl(item) {
  if (!item || !item.art) return ""
  return String(item.art.url || "")
}

// Highlight the lyric line that is playing right now.
function activeLyricIndex(lines, positionSeconds) {
  if (!lines || !lines.length) return -1
  var at = (Number(positionSeconds) || 0) * 1000
  var found = -1
  for (var i = 0; i < lines.length; i++) {
    if (lines[i].t >= 0 && lines[i].t <= at) found = i
    else if (lines[i].t > at) break
  }
  return found
}

function progress(position, total) {
  var p = Number(position) || 0
  var t = Number(total) || 0
  if (t <= 0) return 0
  return Math.max(0, Math.min(1, p / t))
}

// Clamp and normalise a search term before it leaves the widget, so a stray
// newline or a 4 KB paste never becomes a request.
function query(text, limit) {
  return String(text || "").replace(/\s+/g, " ").trim().slice(0, limit || 200)
}

function reasonText(reason, fallback) {
  return REASON[String(reason || "")] || String(fallback || REASON.network)
}

// Sort a track list the way a person expects, not the way it came back.
function byTitle(a, b) {
  var x = String(a.title || "").toLowerCase()
  var y = String(b.title || "").toLowerCase()
  if (x === y) return 0
  return x < y ? -1 : 1
}

// A search for Omarchy's package picker (Install › Package, an fzf list) that
// shows exactly these packages: "^mpv$ | ^bubblewrap$". Plain names with
// spaces between them match nothing there - fzf reads a space as "and".
function installerSearch(names) {
  return (names || []).map(function (n) { return "^" + n + "$" }).join(" | ")
}

function byArtist(a, b) {
  var x = String(a.artist || "").toLowerCase()
  var y = String(b.artist || "").toLowerCase()
  if (x === y) return byTitle(a, b)
  return x < y ? -1 : 1
}

// 3281405770 -> "3.3B", 958085 -> "958K": view counts at a glance.
function compact(n) {
  var v = Number(n) || 0
  if (v < 1000) return String(Math.round(v))
  var units = [["B", 1e9], ["M", 1e6], ["K", 1e3]]
  for (var i = 0; i < units.length; i++) {
    if (v >= units[i][1]) {
      var x = v / units[i][1]
      return (x >= 100 ? x.toFixed(0) : x.toFixed(1).replace(/\.0$/, "")) + units[i][0]
    }
  }
  return String(v)
}

// 1790847977 -> "3 Oct" this year, "3 Oct 2025" otherwise.
function shortDate(epoch) {
  var t = Number(epoch) || 0
  if (t <= 0) return ""
  var d = new Date(t * 1000)
  var months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
  var out = d.getDate() + " " + months[d.getMonth()]
  if (d.getFullYear() !== new Date().getFullYear()) out += " " + d.getFullYear()
  return out
}
