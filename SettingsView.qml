import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import "components" as C
import "Model.js" as Model

// Settings, laid out the way the original AuroraPulse app did it: one page
// that leads to Radio, TV & streaming and Media & library, each with its own
// preferences, its own way to add and manage sources, and its own data and
// statistics. Every control writes straight through to the daemon.
Item {
  id: root

  property var settings: ({})
  property var catalogue: ({})
  property var custom: ({})
  property var libraryStats: ({})
  property var syncProgress: ({})
  property double sleepAt: 0
  property var scrobbleStatus: ({})
  property var health: ({})
  property var alarm: ({})
  property var monitors: []

  signal settingChanged(string key, var value)
  signal command(string cmd, var args)

  readonly property color fg: Color.popups.text
  readonly property color dim: Qt.rgba(fg.r, fg.g, fg.b, 0.55)
  readonly property color accent: Color.accent

  // home | general | radio | tv | podcasts | library | health
  property string page: "home"
  onPageChanged: {
    flick.contentY = 0
    if (page === "health") command("health", {})
    if (page === "general") command("alarm", {})
  }

  function healthColor(level) {
    return level === "error" ? "#e06c75" : level === "warn" ? "#e5c07b" : root.accent
  }

  function value(key, fallback) {
    var s = root.settings
    if (s && s[key] !== undefined && s[key] !== null) return s[key]
    return fallback
  }

  function age(seconds) {
    if (seconds === undefined || seconds === null) return "never"
    var s = Math.max(0, Math.round(seconds))
    if (s < 90) return "just now"
    if (s < 5400) return Math.round(s / 60) + " min ago"
    if (s < 172800) return Math.round(s / 3600) + " h ago"
    return Math.round(s / 86400) + " days ago"
  }

  function syncing(source) {
    var p = root.syncProgress ? root.syncProgress[source] : null
    return !!p && p.stage !== "done" && p.stage !== "failed"
  }

  function syncText(source) {
    var p = root.syncProgress ? root.syncProgress[source] : null
    if (!p) return ""
    if (p.stage === "failed") return "The last update failed" + (p.error ? ": " + p.error : "")
    if (p.stage === "done") return "Up to date"
    if (p.unit === "bytes") return "Downloading, " + Model.bytes(p.bytes || 0)
    return (p.stage === "parse" ? "Saving " : "Downloading ")
           + Model.count(p.rows || 0, "station", "stations")
  }

  function scrobbleLine(service) {
    var st = root.scrobbleStatus ? root.scrobbleStatus[service] : null
    if (!st) return ""
    return (st.ok ? "Last scrobbled: " : "Last attempt failed: ") + st.message
  }

  function roots() {
    var raw = String(value("scanRoots", "") || "")
    return raw.split(":").filter(function (r) { return r.trim() !== "" })
  }

  function setRoots(list) { settingChanged("scanRoots", list.join(":")) }

  readonly property var eqBandNames: ["60 Hz", "230 Hz", "910 Hz", "3.6 kHz", "14 kHz"]
  function eqBands() {
    var parts = String(value("eqBands", "0,0,0,0,0")).split(",")
    var out = []
    for (var i = 0; i < 5; i++) out.push(parseInt(parts[i]) || 0)
    return out
  }
  function setEqBand(i, gain) {
    var bands = eqBands()
    bands[i] = Math.round(gain)
    settingChanged("eqBands", bands.join(","))
  }

  function alarmLine() {
    var a = root.alarm || {}
    if (!a.item) return "Choose a station: play it, then press Use what's playing."
    var when = a.next ? new Date(a.next * 1000) : null
    return "Plays " + a.item.title
           + (when && a.enabled ? "  ·  next " + when.toLocaleString(Qt.locale(), "ddd HH:mm") : "")
  }

  property double now: Date.now()
  readonly property int sleepMinutesLeft: sleepAt > 0
                                          ? Math.max(0, Math.ceil((sleepAt * 1000 - now) / 60000)) : 0
  Timer {
    interval: 20000
    repeat: true
    running: root.visible && root.sleepAt > 0
    onTriggered: root.now = Date.now()
  }

  RowLayout {
    id: header
    anchors.left: parent.left
    anchors.right: parent.right
    anchors.top: parent.top
    height: 30
    spacing: 6

    IconButton {
      visible: root.page !== "home"
      icon: Model.ICON.back
      size: 28
      colorFg: root.dim
      tip: "Back to settings"
      onClicked: root.page = "home"
    }
    Text {
      Layout.fillWidth: true
      text: ({ home: "Settings", general: "Playback & general", radio: "Radio",
               tv: "TV & streaming", library: "Media & library",
               podcasts: "Podcasts", health: "Health" })[root.page]
      color: root.fg
      font.pixelSize: 14
      font.bold: true
    }
  }

  Flickable {
    id: flick
    anchors.left: parent.left
    anchors.right: parent.right
    anchors.top: header.bottom
    anchors.topMargin: 4
    anchors.bottom: parent.bottom
    contentHeight: column.implicitHeight + 16
    clip: true
    boundsBehavior: Flickable.StopAtBounds
    ScrollBar.vertical: ScrollBar {}

    ColumnLayout {
      id: column
      width: flick.width - 10
      spacing: 4

      // ---- home ---------------------------------------------------------

      ColumnLayout {
        Layout.fillWidth: true
        visible: root.page === "home"
        spacing: 6

        C.NavRow {
          icon: Model.ICON.tune
          label: "Playback & general"
          help: "Speed, sleep timer, start on launch, YouTube, the browser"
          onClicked: root.page = "general"
        }
        C.NavRow {
          icon: Model.ICON.radio
          label: "Radio"
          help: Model.count(root.catalogue.radio || 0, "station", "stations")
                + (root.catalogue.radio_custom ? "  ·  " + root.catalogue.radio_custom + " of yours" : "")
                + "  ·  updated " + root.age(root.catalogue.radio_age)
          tint: Model.SOURCE.radio.color
          onClicked: root.page = "radio"
        }
        C.NavRow {
          icon: Model.ICON.tv
          label: "TV & streaming"
          help: Model.count(root.catalogue.tv || 0, "channel", "channels")
                + "  ·  " + Model.count((root.custom.playlists || []).length, "playlist", "playlists")
                + "  ·  updated " + root.age(root.catalogue.tv_age)
          tint: Model.SOURCE.tv.color
          onClicked: root.page = "tv"
        }
        C.NavRow {
          icon: Model.ICON.podcast
          label: "Podcasts"
          help: Model.count((root.custom.podcasts || []).length, "subscription", "subscriptions")
                + "  ·  add a show by its feed address"
          tint: Model.SOURCE.podcast.color
          onClicked: root.page = "podcasts"
        }
        C.NavRow {
          icon: Model.ICON.music
          label: "Media & library"
          help: Model.count(root.libraryStats.tracks || 0, "track", "tracks")
                + "  ·  scan folders, file types, lyrics"
          tint: Model.SOURCE.local.color
          onClicked: root.page = "library"
        }
        C.NavRow {
          icon: Model.ICON.health
          label: "Health"
          help: root.health.summary
                ? root.health.summary + "  ·  players, downloads, sandbox, folders"
                : "Check that everything AuroraPulse needs is installed and working"
          tint: root.health.level ? root.healthColor(root.health.level) : root.dim
          onClicked: root.page = "health"
        }
      }

      // ---- health -------------------------------------------------------

      ColumnLayout {
        Layout.fillWidth: true
        visible: root.page === "health"
        spacing: 2

        RowLayout {
          Layout.fillWidth: true
          spacing: 6
          Text {
            Layout.fillWidth: true
            text: root.health.summary || "Checking…"
            color: root.health.level ? root.healthColor(root.health.level) : root.dim
            font.pixelSize: 12
            font.bold: true
          }
          C.Button {
            visible: (root.health.installs || []).length > 1
            text: "Install all"
            icon: Model.ICON.download
            primary: true
            onClicked: root.command("health_install", {})
          }
          C.Button {
            text: "Check again"
            icon: Model.ICON.sync
            onClicked: root.command("health", {})
          }
        }
        Text {
          Layout.fillWidth: true
          visible: (root.health.installing || []).length > 0
          text: "Installing " + (root.health.installing || []).join(", ")
                + " in a terminal window: enter your password there. This page updates by itself."
          color: root.accent
          font.pixelSize: 10
          wrapMode: Text.WordWrap
        }
        Repeater {
          model: root.health.checks || []
          delegate: RowLayout {
            required property var modelData
            Layout.fillWidth: true
            Layout.topMargin: 4
            spacing: 8
            Rectangle {
              Layout.alignment: Qt.AlignTop
              Layout.topMargin: 5
              width: 8; height: 8; radius: 4
              color: root.healthColor(modelData.level)
            }
            ColumnLayout {
              Layout.fillWidth: true
              spacing: 0
              Text {
                Layout.fillWidth: true
                text: modelData.name
                color: root.fg
                font.pixelSize: 12
              }
              Text {
                Layout.fillWidth: true
                text: modelData.detail + (modelData.fix && !modelData.package ? "\n" + modelData.fix : "")
                color: root.dim
                font.pixelSize: 10
                wrapMode: Text.WordWrap
              }
            }
            C.Button {
              Layout.alignment: Qt.AlignVCenter
              visible: !modelData.ok && !!modelData.package
              readonly property bool busy: (root.health.installing || []).indexOf(modelData.package) >= 0
              enabled: !busy
              text: busy ? "Installing…" : modelData.action === "update" ? "Update" : "Install"
              icon: Model.ICON.download
              onClicked: root.command("health_install", { packages: [modelData.package],
                                                          update: modelData.action === "update" })
            }
          }
        }
      }

      // ---- playback & general ------------------------------------------

      ColumnLayout {
        Layout.fillWidth: true
        visible: root.page === "general"
        spacing: 2

        C.SectionHeader { title: "Playback" }
        C.ChoiceRow {
          label: "Playback speed"
          help: "For tracks and videos. Live radio and TV always play at normal speed."
          options: [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0]
          value: root.value("playbackSpeed", 1.0)
          format: function (v) { return v + "×" }
          onPicked: function (v) { root.settingChanged("playbackSpeed", v) }
        }
        C.ChoiceRow {
          label: "Sleep timer"
          help: root.sleepMinutesLeft > 0 ? "Stops playing in " + root.sleepMinutesLeft + " min."
                                          : "Stop playing after a while."
          options: [0, 15, 30, 45, 60, 90]
          value: -1
          format: function (v) { return v === 0 ? "off" : v + " min" }
          onPicked: function (v) { root.command("sleep", { minutes: v }) }
        }
        C.SliderRow {
          label: "Sleep timer fade-out"
          help: "The sound fades away over this long before the sleep timer stops it."
          from: 0; to: 120; stepSize: 5
          value: root.value("sleepFadeSec", 30)
          format: function (v) { return v === 0 ? "off" : v + " s" }
          onMoved: function (v) { root.settingChanged("sleepFadeSec", v) }
        }
        C.SliderRow {
          label: "Fade between tracks"
          help: "The end of a track fades out and the next one fades in."
          from: 0; to: 12; stepSize: 1
          value: root.value("crossfadeSec", 0)
          format: function (v) { return v === 0 ? "off" : v + " s" }
          onMoved: function (v) { root.settingChanged("crossfadeSec", v) }
        }
        C.ToggleRow {
          label: "Skip silence"
          help: "Cuts long silent stretches out of tracks and podcasts. Never live radio."
          checked: root.value("skipSilence", false)
          onToggled: function (v) { root.settingChanged("skipSilence", v) }
        }
        C.ToggleRow {
          label: "Start playing on launch"
          help: "Resume the last station or track when the widget starts."
          checked: root.value("autoPlay", false)
          onToggled: function (v) { root.settingChanged("autoPlay", v) }
        }

        C.ToggleRow {
          label: "Resume long tracks where you left off"
          help: "Videos and tracks over ten minutes reopen at the point you stopped."
          checked: root.value("resumePlayback", true)
          onToggled: function (v) { root.settingChanged("resumePlayback", v) }
        }
        C.ToggleRow {
          label: "Notify when the track changes"
          help: "A desktop notification for each new track, and for what a radio station says is on."
          checked: root.value("notifyTrackChange", false)
          onToggled: function (v) { root.settingChanged("notifyTrackChange", v) }
        }

        C.SectionHeader { title: "Alarm" }
        C.ToggleRow {
          label: "Wake me up with music"
          help: root.alarmLine()
          checked: root.value("alarmEnabled", false)
          onToggled: function (v) { root.settingChanged("alarmEnabled", v) }
        }
        C.RowBase {
          label: "Time"
          help: "24-hour, like 06:45. Enter to save."
          controlWidth: 90
          C.InputField {
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.topMargin: 3
            width: 90
            placeholder: "07:00"
            text: root.value("alarmTime", "07:00")
            onAccepted: {
              var m = text.trim().match(/^(\d{1,2}):(\d{2})$/)
              if (m && +m[1] < 24 && +m[2] < 60)
                root.settingChanged("alarmTime", (m[1].length < 2 ? "0" : "") + m[1] + ":" + m[2])
            }
          }
        }
        C.ChoiceRow {
          label: "On"
          options: ["1,2,3,4,5,6,7", "1,2,3,4,5", "6,7"]
          value: root.value("alarmDays", "1,2,3,4,5,6,7")
          format: function (v) {
            return ({ "1,2,3,4,5,6,7": "every day", "1,2,3,4,5": "weekdays", "6,7": "weekends" })[v] || v
          }
          onPicked: function (v) { root.settingChanged("alarmDays", v) }
        }
        C.SliderRow {
          label: "Wake-up volume"
          from: 5; to: 100; stepSize: 5
          value: root.value("alarmVolume", 50)
          format: function (v) { return v + "%" }
          onMoved: function (v) { root.settingChanged("alarmVolume", v) }
        }
        C.SliderRow {
          label: "Fade in over"
          from: 0; to: 300; stepSize: 15
          value: root.value("alarmFadeSec", 60)
          format: function (v) { return v === 0 ? "at once" : v < 60 ? v + " s" : (v / 60) + " min" }
          onMoved: function (v) { root.settingChanged("alarmFadeSec", v) }
        }
        RowLayout {
          Layout.fillWidth: true
          spacing: 4
          Item { Layout.fillWidth: true }
          C.Button {
            text: "Use what's playing"
            icon: Model.ICON.alarm
            onClicked: root.command("alarm", { action: "set" })
          }
          C.Button {
            text: "Try it now"
            enabled: !!(root.alarm && root.alarm.item)
            onClicked: root.command("alarm", { action: "test" })
          }
        }

        C.SectionHeader {
          title: "Sound"
        }
        C.ToggleRow {
          label: "Remember the volume for each output"
          help: "Headphones, speakers and Bluetooth each get back the volume you last used on them."
          checked: root.value("rememberVolumePerDevice", true)
          onToggled: function (v) { root.settingChanged("rememberVolumePerDevice", v) }
        }
        C.ToggleRow {
          label: "Even out loudness"
          help: "Brings quiet and loud stations to a similar level, live, as they play."
          checked: root.value("normalizeVolume", false)
          onToggled: function (v) { root.settingChanged("normalizeVolume", v) }
        }
        C.ToggleRow {
          label: "Equalizer"
          checked: root.value("equalizerEnabled", false)
          onToggled: function (v) { root.settingChanged("equalizerEnabled", v) }
        }
        C.ChoiceRow {
          visible: root.value("equalizerEnabled", false)
          label: "Preset"
          help: "Applied straight away, without restarting what is playing."
          options: ["Flat", "Bass Boost", "Vocal Boost", "Spoken Word", "Classical",
                    "Electronic", "Rock", "Pop", "Custom"]
          value: root.value("eqPreset", "Flat")
          onPicked: function (v) { root.settingChanged("eqPreset", v) }
        }
        Repeater {
          model: root.value("equalizerEnabled", false) && root.value("eqPreset", "Flat") === "Custom"
                 ? 5 : 0
          delegate: C.SliderRow {
            required property int index
            label: root.eqBandNames[index]
            from: -12; to: 12; stepSize: 1
            value: root.eqBands()[index]
            format: function (v) { return (v > 0 ? "+" : "") + v + " dB" }
            onMoved: function (v) { root.setEqBand(index, v) }
          }
        }

        C.SectionHeader { title: "Video" }
        C.ToggleRow {
          label: "Hardware decoding"
          help: "Uses the graphics card for video. Turn off if videos show as green or garbled."
          checked: root.value("hardwareDecode", true)
          onToggled: function (v) { root.settingChanged("hardwareDecode", v) }
        }
        C.ChoiceRow {
          label: "Picture shape"
          help: "Fill crops the picture to the window instead of adding black bars."
          options: ["auto", "16:9", "4:3", "21:9", "fill"]
          value: root.value("defaultAspect", "auto")
          onPicked: function (v) { root.settingChanged("defaultAspect", v) }
        }
        C.ChoiceRow {
          visible: root.monitors.length > 1
          label: "Open videos on"
          options: [""].concat(root.monitors)
          value: root.value("videoMonitor", "")
          format: function (v) { return v === "" ? "the focused screen" : v }
          onPicked: function (v) { root.settingChanged("videoMonitor", v) }
        }
        C.ToggleRow {
          label: "Subtitles"
          help: "Shown when the video has them in your language: inside the file, beside it "
                + "(Film.en.srt), or from YouTube. The CC button picks another."
          checked: root.value("subtitlesEnabled", false)
          onToggled: function (v) { root.settingChanged("subtitlesEnabled", v) }
        }
        C.RowBase {
          visible: root.value("subtitlesEnabled", false)
          label: "Subtitle language"
          help: "A code like en, fr or es. Enter to save."
          controlWidth: 90
          C.InputField {
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.topMargin: 3
            width: 90
            placeholder: "en"
            text: root.value("subtitleLanguage", "en")
            onAccepted: root.settingChanged("subtitleLanguage", text.trim().toLowerCase())
          }
        }
        C.SliderRow {
          visible: root.value("subtitlesEnabled", false)
          label: "Subtitle size"
          from: 0.5; to: 3.0; stepSize: 0.1
          value: root.value("subtitleSize", 1.0)
          format: function (v) { return Math.round(v * 100) + "%" }
          onMoved: function (v) { root.settingChanged("subtitleSize", Math.round(v * 10) / 10) }
        }

        C.SectionHeader { title: "Browser" }
        C.ChoiceRow {
          label: "Open the browser on"
          options: ["radio", "tv", "youtube", "music", "local"]
          value: root.value("defaultSource", "radio")
          format: function (v) { return Model.sourceOf(v).label }
          onPicked: function (v) { root.settingChanged("defaultSource", v) }
        }
        C.ChoiceRow {
          label: "Show lists as"
          help: "The grid shows large artwork; the list fits more on screen."
          options: ["list", "grid"]
          value: root.value("viewMode", "list")
          onPicked: function (v) { root.settingChanged("viewMode", v) }
        }
        C.ToggleRow {
          label: "Show the bitrate"
          help: "On station rows, like 128k."
          checked: root.value("showBitrate", true)
          onToggled: function (v) { root.settingChanged("showBitrate", v) }
        }
        C.ToggleRow {
          label: "Show the country"
          help: "On station and channel rows."
          checked: root.value("showCountry", true)
          onToggled: function (v) { root.settingChanged("showCountry", v) }
        }
        C.ToggleRow {
          label: "Source badge in the bar"
          help: "The small radio, TV or YouTube mark next to the bar icon while playing."
          checked: root.value("showSourceBadge", true)
          onToggled: function (v) { root.settingChanged("showSourceBadge", v) }
        }
        C.ToggleRow {
          label: "Picture-in-picture badge in the bar"
          help: "A marker in the bar while a video window is open."
          checked: root.value("showPipBadge", true)
          onToggled: function (v) { root.settingChanged("showPipBadge", v) }
        }

        C.SectionHeader { title: "YouTube" }
        C.ToggleRow {
          label: "YouTube is audio only"
          help: "Never opens a video window."
          checked: root.value("audioOnlyYouTube", false)
          onToggled: function (v) { root.settingChanged("audioOnlyYouTube", v) }
        }
        C.ChoiceRow {
          label: "Video quality"
          help: "A ceiling, not a demand. \"audio\" plays YouTube without a window."
          options: ["audio", "360p", "480p", "720p", "1080p", "max"]
          value: root.value("defaultQuality", "720p")
          onPicked: function (v) { root.settingChanged("defaultQuality", v) }
        }
        C.SliderRow {
          label: "Lookups per hour"
          help: "A ceiling so a loop cannot get you rate limited."
          from: 20; to: 600; stepSize: 20
          value: root.value("ytResolutionBudgetPerHour", 120)
          onMoved: function (v) { root.settingChanged("ytResolutionBudgetPerHour", v) }
        }

        C.SectionHeader { title: "Network" }
        C.ToggleRow {
          label: "Offline mode"
          help: "No network at all. Downloaded lists and the local library still work."
          checked: root.value("offlineMode", false)
          onToggled: function (v) { root.settingChanged("offlineMode", v) }
        }

        C.SectionHeader {
          title: "Scrobbling"
        }
        C.ToggleRow {
          label: "Scrobble what you listen to"
          help: "Songs count after half their length or four minutes; radio by its now-playing "
                + "line after a minute. TV and podcasts are never scrobbled."
          checked: root.value("scrobbleEnabled", false)
          onToggled: function (v) { root.settingChanged("scrobbleEnabled", v) }
        }
        ColumnLayout {
          Layout.fillWidth: true
          spacing: 4
          Text {
            text: "ListenBrainz user token (Enter to save)"
            color: root.fg
            font.pixelSize: 12
          }
          Text {
            Layout.fillWidth: true
            text: "From listenbrainz.org/settings. " + root.scrobbleLine("listenbrainz")
            color: root.dim
            font.pixelSize: 10
            wrapMode: Text.WordWrap
          }
          C.InputField {
            Layout.fillWidth: true
            secret: true
            text: root.value("listenbrainzToken", "")
            placeholder: "paste your token"
            onAccepted: root.settingChanged("listenbrainzToken", text.trim())
          }
          Text {
            Layout.topMargin: 8
            text: "Last.fm"
            color: root.fg
            font.pixelSize: 12
          }
          Text {
            Layout.fillWidth: true
            text: root.value("lastfmSession", "")
                  ? "Signed in as " + root.value("lastfmUser", "") + ". " + root.scrobbleLine("lastfm")
                  : "Needs a free API key and secret from last.fm/api/account/create, then one "
                    + "sign-in. Your password is used once and never stored."
            color: root.dim
            font.pixelSize: 10
            wrapMode: Text.WordWrap
          }
          RowLayout {
            Layout.fillWidth: true
            visible: !root.value("lastfmSession", "")
            spacing: 4
            C.InputField {
              id: lfKey
              Layout.fillWidth: true
              placeholder: "API key"
              text: root.value("lastfmApiKey", "")
            }
            C.InputField {
              id: lfSecret
              Layout.fillWidth: true
              secret: true
              placeholder: "shared secret"
              text: root.value("lastfmApiSecret", "")
            }
          }
          RowLayout {
            Layout.fillWidth: true
            visible: !root.value("lastfmSession", "")
            spacing: 4
            C.InputField {
              id: lfUser
              Layout.fillWidth: true
              placeholder: "username"
            }
            C.InputField {
              id: lfPass
              Layout.fillWidth: true
              secret: true
              placeholder: "password"
              onAccepted: lfLogin.clicked()
            }
            C.Button {
              id: lfLogin
              text: "Sign in"
              primary: true
              enabled: lfKey.text && lfSecret.text && lfUser.text && lfPass.text
              onClicked: {
                root.command("lastfm_login", { apiKey: lfKey.text, apiSecret: lfSecret.text,
                                               user: lfUser.text, password: lfPass.text })
                lfPass.text = ""
              }
            }
          }
          C.Button {
            visible: !!root.value("lastfmSession", "")
            text: "Sign out of Last.fm"
            onClicked: root.command("lastfm_logout", {})
          }
        }

        C.SectionHeader {
          title: "Keyboard (while the panel is open)"
        }
        Text {
          Layout.fillWidth: true
          text: "↑ ↓  move through the list      Enter  play it      A  add it to the queue\n"
                + "Space  play / pause      ← →  seek 10 s      + −  or  Shift ↑ ↓  volume\n"
                + "N / P  next / previous      M  mute      S  stop      F  favourite\n"
                + "Q  queue      /  search (↓ to go to the results)      1–5  tabs      Esc  close"
          color: root.dim
          font.family: Style.font.family
          font.pixelSize: 10
          lineHeight: 1.4
        }
        Text {
          Layout.fillWidth: true
          Layout.topMargin: 4
          text: "Media keys and playerctl work too: AuroraPulse appears to the desktop as a media player."
          color: root.dim
          font.pixelSize: 10
          wrapMode: Text.WordWrap
        }
      }

      // ---- radio --------------------------------------------------------

      ColumnLayout {
        Layout.fillWidth: true
        visible: root.page === "radio"
        spacing: 2

        C.SectionHeader { title: "Preferences" }
        C.ChoiceRow {
          label: "Minimum bitrate"
          help: "Hides the very low quality streams directories are full of."
          options: [0, 64, 128, 192]
          value: root.value("minBitrate", 0)
          format: function (v) {
            return ({ 0: "any quality", 64: "standard, 64k+", 128: "high, 128k+",
                      192: "premier, 192k+" })[v]
          }
          onPicked: function (v) { root.settingChanged("minBitrate", v) }
        }
        C.RowBase {
          label: "Preferred country"
          help: "The Radio tab opens on this country: a code like US, DE or GH. Enter to save."
          controlWidth: 90
          C.InputField {
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.topMargin: 3
            width: 90
            placeholder: "e.g. GH"
            text: root.value("preferredCountry", "")
            onAccepted: root.settingChanged("preferredCountry", text.trim().toUpperCase())
          }
        }
        C.ToggleRow {
          label: "Report plays to the station directory"
          help: "Anonymous; it is what keeps stations ranked on Radio Browser."
          checked: root.value("reportPlays", true)
          onToggled: function (v) { root.settingChanged("reportPlays", v) }
        }

        C.SectionHeader { title: "Your stations" }
        Text {
          Layout.fillWidth: true
          text: "Stations you add or import are kept apart from the downloaded list, so updates "
                + "never remove them. Find them under ★ My stations on the Radio tab."
          color: root.dim; font.pixelSize: 10; wrapMode: Text.WordWrap
        }
        ColumnLayout {
          Layout.fillWidth: true
          Layout.topMargin: 6
          spacing: 4
          Text {
            text: "Add a station"
            color: root.fg
            font.pixelSize: 12
            font.bold: true
          }
          C.InputField {
            id: stName
            Layout.fillWidth: true
            placeholder: "Station name"
          }
          C.InputField {
            id: stUrl
            Layout.fillWidth: true
            placeholder: "Stream address, e.g. https://stream.example.com/live.mp3"
          }
          RowLayout {
            Layout.fillWidth: true
            spacing: 4
            C.InputField {
              id: stCountry
              Layout.fillWidth: true
              placeholder: "Country, e.g. Ghana or GH"
            }
            C.InputField {
              id: stTags
              Layout.fillWidth: true
              placeholder: "Tags: news, jazz"
            }
          }
          RowLayout {
            Layout.fillWidth: true
            spacing: 4
            C.InputField {
              id: stLogo
              Layout.fillWidth: true
              placeholder: "Logo address (optional)"
            }
            C.Button {
              text: "Add station"
              icon: Model.ICON.plus
              primary: true
              enabled: stUrl.text.trim() !== ""
              onClicked: {
                root.command("custom", { op: "add", kind: "station", fields: {
                  name: stName.text, url: stUrl.text, country: stCountry.text,
                  tags: stTags.text, favicon: stLogo.text } })
                stName.text = ""; stUrl.text = ""; stCountry.text = ""
                stTags.text = ""; stLogo.text = ""
              }
            }
          }
        }
        ColumnLayout {
          Layout.fillWidth: true
          Layout.topMargin: 10
          spacing: 4
          Text {
            text: "Import stations"
            color: root.fg
            font.pixelSize: 12
            font.bold: true
          }
          Text {
            Layout.fillWidth: true
            text: "Paste an M3U or PLS playlist, a JSON or CSV export from the AuroraPulse app, "
                  + "or a list of stream addresses - or give a web address or a file path."
            color: root.dim; font.pixelSize: 10; wrapMode: Text.WordWrap
          }
          C.TextBox {
            id: importText
            Layout.fillWidth: true
            placeholder: "#EXTM3U ..."
          }
          RowLayout {
            Layout.fillWidth: true
            spacing: 4
            C.InputField {
              id: importSource
              Layout.fillWidth: true
              placeholder: "…or https://… or ~/stations.m3u"
            }
            C.Button {
              text: "Import"
              icon: Model.ICON.importFile
              primary: true
              enabled: importText.text.trim() !== "" || importSource.text.trim() !== ""
              onClicked: {
                root.command("stations_import", { text: importText.text, source: importSource.text })
                importText.text = ""
                importSource.text = ""
              }
            }
          }
        }
        RowLayout {
          Layout.fillWidth: true
          Layout.topMargin: 10
          spacing: 4
          Text {
            Layout.fillWidth: true
            text: "Export to Downloads"
            color: root.fg
            font.pixelSize: 12
            font.bold: true
          }
          C.Button {
            text: "mine, M3U"
            onClicked: root.command("stations_export", { format: "m3u", mine: true })
          }
          C.Button {
            text: "all, JSON"
            onClicked: root.command("stations_export", { format: "json", mine: false })
          }
          C.Button {
            text: "all, CSV"
            onClicked: root.command("stations_export", { format: "csv", mine: false })
          }
        }
        Text {
          Layout.fillWidth: true
          Layout.topMargin: 10
          text: (root.custom.radioCount || 0) === 0 ? "You have not added any stations yet."
                : Model.count(root.custom.radioCount, "station", "stations") + " of yours"
          color: root.dim
          font.pixelSize: 11
        }
        Repeater {
          model: root.custom.radio || []
          delegate: C.ItemRow {
            required property var modelData
            title: modelData.name
            subtitle: [modelData.country, modelData.tags, modelData.url]
                      .filter(function (x) { return !!x }).join("  ·  ")
            onRemoved: root.command("custom", { op: "remove", kind: "station", itemId: modelData.id })
          }
        }
        C.ActionRow {
          visible: (root.custom.radioCount || 0) > 1
          label: "Remove all of your stations"
          property bool armed: false
          action: armed ? "really remove all?" : "remove all"
          onRun: {
            if (!armed) { armed = true; return }
            armed = false
            root.command("custom", { op: "clear", kind: "station" })
          }
        }

        C.SectionHeader { title: "Server & data" }
        C.ChoiceRow {
          label: "Radio Browser server"
          help: "Auto picks one that answers. A pinned server is tried first; the others stay as a fallback."
          options: ["", "https://de1.api.radio-browser.info", "https://de2.api.radio-browser.info",
                    "https://fr1.api.radio-browser.info", "https://at1.api.radio-browser.info",
                    "https://nl1.api.radio-browser.info"]
          value: root.value("radioServer", "")
          format: function (v) { return v === "" ? "auto" : v.replace("https://", "").split(".")[0].toUpperCase() }
          onPicked: function (v) { root.settingChanged("radioServer", v) }
        }
        C.ToggleRow {
          label: "Update the station list automatically"
          checked: root.value("radioAutoSync", true)
          onToggled: function (v) { root.settingChanged("radioAutoSync", v) }
        }
        C.SliderRow {
          visible: root.value("radioAutoSync", true)
          label: "Every"
          from: 1; to: 168; stepSize: 1
          value: root.value("radioSyncHours", 24)
          format: function (v) { return v + " h" }
          onMoved: function (v) { root.settingChanged("radioSyncHours", v) }
        }
        C.ActionRow {
          label: "Update the station list now"
          help: root.syncText("radio") || "Downloads the whole directory again; about a minute."
          action: root.syncing("radio") ? "updating…" : "update"
          onRun: if (!root.syncing("radio")) root.command("catalogue", { sync: true, full: true, source: "radio" })
        }
        C.ActionRow {
          label: "Clear the downloaded stations"
          help: "Your own stations are kept. The list can be downloaded again."
          property bool armed: false
          action: armed ? "really clear?" : "clear"
          onRun: {
            if (!armed) { armed = true; return }
            armed = false
            root.command("clear_catalogue", { source: "radio" })
          }
        }

        C.SectionHeader { title: "Statistics" }
        C.InfoRow {
          label: "Stations"
          value: Model.count(root.catalogue.radio || 0, "station", "stations")
        }
        C.InfoRow {
          label: "Of yours"
          value: String(root.catalogue.radio_custom || 0)
        }
        C.InfoRow {
          label: "Last updated"
          value: root.age(root.catalogue.radio_age)
        }
      }

      // ---- tv -----------------------------------------------------------

      ColumnLayout {
        Layout.fillWidth: true
        visible: root.page === "tv"
        spacing: 2

        C.SectionHeader { title: "Preferences" }
        C.ToggleRow {
          label: "Start TV muted"
          help: "Live TV is often mixed much louder than music."
          checked: root.value("tvOpenDefaultMuted", false)
          onToggled: function (v) { root.settingChanged("tvOpenDefaultMuted", v) }
        }
        C.ToggleRow {
          label: "Hide geo-blocked channels"
          checked: root.value("tvHideGeoBlocked", true)
          onToggled: function (v) { root.settingChanged("tvHideGeoBlocked", v) }
        }
        C.ToggleRow {
          label: "Hide channels that are not 24/7"
          checked: root.value("tvHideNot247", true)
          onToggled: function (v) { root.settingChanged("tvHideNot247", v) }
        }
        C.ToggleRow {
          label: "Hide adult channels"
          checked: root.value("tvHideNsfw", true)
          onToggled: function (v) { root.settingChanged("tvHideNsfw", v) }
        }

        C.SectionHeader { title: "Advanced playback" }
        C.SliderRow {
          label: "Stream buffer"
          help: "Seconds buffered ahead. More is steadier on a poor connection."
          from: 5; to: 120; stepSize: 5
          value: root.value("tvBufferSec", 20)
          format: function (v) { return v + " s" }
          onMoved: function (v) { root.settingChanged("tvBufferSec", v) }
        }
        ColumnLayout {
          Layout.fillWidth: true
          spacing: 4
          Text {
            text: "User-Agent (Enter to save)"
            color: root.fg
            font.pixelSize: 12
          }
          C.InputField {
            Layout.fillWidth: true
            text: root.value("tvUserAgent", "")
            placeholder: "AuroraPulse/0.1 (Omarchy)"
            onAccepted: root.settingChanged("tvUserAgent", text.trim())
          }
          Text {
            Layout.topMargin: 6
            text: "Custom TV guide (XMLTV) address"
            color: root.fg
            font.pixelSize: 12
          }
          Text {
            Layout.fillWidth: true
            text: "Leave empty for the automatic guides: one per country, matched to your "
                  + "channels and shown in the TV tab's guide view. Enter to save."
            color: root.dim
            font.pixelSize: 10
            wrapMode: Text.WordWrap
          }
          C.InputField {
            Layout.fillWidth: true
            text: root.value("epgUrl", "")
            placeholder: "https://example.com/guide.xml.gz"
            onAccepted: root.settingChanged("epgUrl", text.trim())
          }
        }

        C.SectionHeader { title: "Playlists" }
        C.InfoRow {
          label: "Built-in source"
          value: "iptv-org/iptv (global)"
        }
        Text {
          Layout.fillWidth: true
          text: "Add your own M3U playlists. Their channels appear under ★ My channels on the "
                + "TV tab and are refreshed with every update; untick one to hide it."
          color: root.dim; font.pixelSize: 10; wrapMode: Text.WordWrap
        }
        Repeater {
          model: root.custom.playlists || []
          delegate: C.ItemRow {
            required property var modelData
            title: modelData.name + "  ·  " + Model.count(modelData.count || 0, "channel", "channels")
            subtitle: modelData.error ? "failed: " + modelData.error : modelData.url
            checkable: true
            checked: modelData.enabled !== false
            confirmRemove: true
            onToggled: function (v) {
              root.command("custom", { op: "enable", kind: "playlist", itemId: modelData.id, enabled: v })
            }
            onRemoved: root.command("custom", { op: "remove", kind: "playlist", itemId: modelData.id })
          }
        }
        RowLayout {
          Layout.fillWidth: true
          spacing: 4
          C.InputField {
            id: plUrl
            Layout.fillWidth: true
            placeholder: "Playlist address, e.g. https://example.com/playlist.m3u"
          }
          C.Button {
            text: "Add"
            icon: Model.ICON.plus
            primary: true
            enabled: plUrl.text.trim() !== ""
            onClicked: {
              root.command("custom", { op: "add", kind: "playlist", fields: { url: plUrl.text.trim() } })
              plUrl.text = ""
            }
          }
        }
        C.TextBox {
          id: plText
          Layout.fillWidth: true
          placeholder: "…or paste M3U text here"
        }
        RowLayout {
          Layout.fillWidth: true
          spacing: 4
          Item { Layout.fillWidth: true }
          C.Button {
            text: "Refresh all"
            icon: Model.ICON.sync
            visible: (root.custom.playlists || []).length > 0
            onClicked: root.command("custom", { op: "refresh", kind: "playlist" })
          }
          C.Button {
            text: "Add pasted playlist"
            icon: Model.ICON.paste
            enabled: plText.text.trim() !== ""
            onClicked: {
              root.command("custom", { op: "add", kind: "playlist", fields: { text: plText.text } })
              plText.text = ""
            }
          }
        }

        C.SectionHeader { title: "Your channels" }
        ColumnLayout {
          Layout.fillWidth: true
          spacing: 4
          C.InputField {
            id: chName
            Layout.fillWidth: true
            placeholder: "Channel name"
          }
          C.InputField {
            id: chUrl
            Layout.fillWidth: true
            placeholder: "Stream address, e.g. https://example.com/live.m3u8"
          }
          RowLayout {
            Layout.fillWidth: true
            spacing: 4
            C.InputField {
              id: chCountry
              Layout.fillWidth: true
              placeholder: "Country"
            }
            C.InputField {
              id: chCategory
              Layout.fillWidth: true
              placeholder: "Category: News, Sports"
            }
          }
          RowLayout {
            Layout.fillWidth: true
            spacing: 4
            C.InputField {
              id: chLogo
              Layout.fillWidth: true
              placeholder: "Logo address (optional)"
            }
            C.Button {
              text: "Add channel"
              icon: Model.ICON.plus
              primary: true
              enabled: chUrl.text.trim() !== ""
              onClicked: {
                root.command("custom", { op: "add", kind: "channel", fields: {
                  name: chName.text, url: chUrl.text, country: chCountry.text,
                  category: chCategory.text, logo: chLogo.text } })
                chName.text = ""; chUrl.text = ""; chCountry.text = ""
                chCategory.text = ""; chLogo.text = ""
              }
            }
          }
        }
        Repeater {
          model: root.custom.tv || []
          delegate: C.ItemRow {
            required property var modelData
            title: modelData.name
            subtitle: [modelData.category, modelData.country, modelData.url]
                      .filter(function (x) { return !!x }).join("  ·  ")
            onRemoved: root.command("custom", { op: "remove", kind: "channel", itemId: modelData.id })
          }
        }

        C.SectionHeader { title: "Server & data" }
        C.ToggleRow {
          label: "Update the channel list automatically"
          checked: root.value("tvAutoSync", true)
          onToggled: function (v) { root.settingChanged("tvAutoSync", v) }
        }
        C.SliderRow {
          visible: root.value("tvAutoSync", true)
          label: "Every"
          from: 1; to: 168; stepSize: 1
          value: root.value("tvSyncHours", 24)
          format: function (v) { return v + " h" }
          onMoved: function (v) { root.settingChanged("tvSyncHours", v) }
        }
        C.ActionRow {
          label: "Update the channel list now"
          help: root.syncText("tv")
          action: root.syncing("tv") ? "updating…" : "update"
          onRun: if (!root.syncing("tv")) root.command("catalogue", { sync: true, full: true, source: "tv" })
        }
        C.ActionRow {
          label: "Clear the downloaded channels"
          help: "Your own channels and playlists are kept."
          property bool armed: false
          action: armed ? "really clear?" : "clear"
          onRun: {
            if (!armed) { armed = true; return }
            armed = false
            root.command("clear_catalogue", { source: "tv" })
          }
        }

        C.SectionHeader { title: "Statistics" }
        C.InfoRow {
          label: "Channels"
          value: Model.count(root.catalogue.tv || 0, "channel", "channels")
        }
        C.InfoRow {
          label: "Of yours"
          value: String(root.catalogue.tv_custom || 0)
        }
        C.InfoRow {
          label: "Last updated"
          value: root.age(root.catalogue.tv_age)
        }
      }

      // ---- media & library ----------------------------------------------

      ColumnLayout {
        Layout.fillWidth: true
        visible: root.page === "podcasts"
        spacing: 2

        C.SectionHeader {
          title: "Subscriptions"
        }
        Text {
          Layout.fillWidth: true
          text: (root.custom.podcasts || []).length
                ? "Your shows are listed first on the Podcasts tab; open one to see its episodes."
                : "Search on the Podcasts tab and press Subscribe on a show, or add one here "
                  + "by its RSS feed address."
          color: root.dim
          font.pixelSize: 10
          wrapMode: Text.WordWrap
        }
        Repeater {
          model: root.custom.podcasts || []
          delegate: C.ItemRow {
            required property var modelData
            title: modelData.title || modelData.feed
            subtitle: [modelData.author, modelData.feed].filter(function (x) { return !!x }).join("  ·  ")
            confirmRemove: true
            onRemoved: root.command("custom", { op: "remove", kind: "podcast",
                                                fields: { feed: modelData.feed } })
          }
        }
        RowLayout {
          Layout.fillWidth: true
          Layout.topMargin: 6
          spacing: 4
          C.InputField {
            id: feedField
            Layout.fillWidth: true
            placeholder: "Feed address, e.g. https://feeds.example.com/show.xml"
            onAccepted: addFeed.clicked()
          }
          C.Button {
            id: addFeed
            text: "Subscribe"
            icon: Model.ICON.rss
            primary: true
            enabled: feedField.text.trim() !== ""
            onClicked: {
              root.command("custom", { op: "add", kind: "podcast",
                                       fields: { feed: feedField.text.trim() } })
              feedField.text = ""
            }
          }
        }
      }

      ColumnLayout {
        Layout.fillWidth: true
        visible: root.page === "library"
        spacing: 2

        C.SectionHeader { title: "Library scanner" }
        Text {
          Layout.fillWidth: true
          text: root.roots().length ? "Folders that are scanned for music:"
                                    : "No folders set, so ~/Music is scanned."
          color: root.dim; font.pixelSize: 10; wrapMode: Text.WordWrap
        }
        Repeater {
          model: root.roots()
          delegate: C.ItemRow {
            required property var modelData
            required property int index
            title: modelData
            onRemoved: {
              var list = root.roots()
              list.splice(index, 1)
              root.setRoots(list)
            }
          }
        }
        RowLayout {
          Layout.fillWidth: true
          spacing: 4
          C.InputField {
            id: rootField
            Layout.fillWidth: true
            placeholder: "Add a folder, e.g. ~/Music or /mnt/media/music"
            onAccepted: addRoot.clicked()
          }
          C.Button {
            id: addRoot
            text: "Add folder"
            icon: Model.ICON.folderAdd
            primary: true
            enabled: rootField.text.trim() !== ""
            onClicked: {
              var path = rootField.text.trim()
              if (!path) return
              var list = root.roots()
              if (list.indexOf(path) < 0) list.push(path)
              root.setRoots(list)
              rootField.text = ""
            }
          }
        }
        ColumnLayout {
          Layout.fillWidth: true
          Layout.topMargin: 8
          spacing: 4
          Text {
            text: "Audio file types (comma separated, Enter to save)"
            color: root.fg
            font.pixelSize: 12
          }
          C.InputField {
            Layout.fillWidth: true
            text: root.value("audioExtensions", "")
            placeholder: "mp3, flac, ogg, opus, m4a, wav"
            onAccepted: root.settingChanged("audioExtensions", text)
          }
        }
        C.ToggleRow {
          label: "Follow links to other folders"
          help: "Scan folders reached through symbolic links. Loops are detected and skipped."
          checked: root.value("localFollowSymlinks", false)
          onToggled: function (v) {
            root.settingChanged("localFollowSymlinks", v)
            root.command("scan", {})
          }
        }
        C.ActionRow {
          label: "Rescan the music folders"
          help: Model.count(root.libraryStats.tracks || 0, "track", "tracks") + " in "
                + Model.count(root.libraryStats.folders || 0, "folder", "folders")
          action: "rescan"
          onRun: root.command("scan", {})
        }
        C.SectionHeader {
          title: "Downloads"
        }
        C.ChoiceRow {
          label: "Audio format"
          help: "MP3 plays everywhere; FLAC is lossless; Original keeps the stream untouched."
          options: ["mp3", "m4a", "opus", "flac", "original"]
          value: root.value("downloadAudioFormat", "mp3")
          format: function (v) { return v === "original" ? "original" : v.toUpperCase() }
          onPicked: function (v) { root.settingChanged("downloadAudioFormat", v) }
        }
        C.ChoiceRow {
          label: "Video format"
          help: "MP4 (H.264) plays on any device. The download button can always pick another."
          options: ["mp4", "mkv", "webm", "original"]
          value: root.value("downloadVideoFormat", "mp4")
          format: function (v) { return v === "original" ? "original" : v.toUpperCase() }
          onPicked: function (v) { root.settingChanged("downloadVideoFormat", v) }
        }
        C.ChoiceRow {
          label: "Highest video quality"
          options: ["480p", "720p", "1080p", "1440p", "2160p", "best"]
          value: root.value("downloadVideoQuality", "1080p")
          onPicked: function (v) { root.settingChanged("downloadVideoQuality", v) }
        }
        C.ToggleRow {
          label: "Include the artwork"
          help: "Embedded in MP3, M4A, MP4 and MKV; saved beside the file for the others."
          checked: root.value("downloadArtwork", true)
          onToggled: function (v) { root.settingChanged("downloadArtwork", v) }
        }
        C.InfoRow {
          label: "Saved to"
          value: "Music › AuroraPulse, Videos › AuroraPulse"
        }

        C.SectionHeader { title: "Queue" }
        C.ToggleRow {
          label: "Don't allow duplicates in the queue"
          checked: root.value("preventDuplicates", true)
          onToggled: function (v) { root.settingChanged("preventDuplicates", v) }
        }

        C.SectionHeader { title: "Lyrics" }
        C.ChoiceRow {
          label: "Lyrics sources"
          help: "Tried in order. A .lrc file next to the track always wins."
          options: ["lrclib,azlyrics,sidecar", "lrclib", "lrclib,azlyrics", "off"]
          value: root.value("lyricsProviders", "lrclib,azlyrics,sidecar")
          format: function (v) { return v === "off" ? "off" : v.replace(/,/g, " → ") }
          onPicked: function (v) { root.settingChanged("lyricsProviders", v) }
        }
        C.ToggleRow {
          label: "Use YouTube Music's own lyrics"
          checked: root.value("useYtLyrics", true)
          onToggled: function (v) { root.settingChanged("useYtLyrics", v) }
        }
      }
    }
  }
}
