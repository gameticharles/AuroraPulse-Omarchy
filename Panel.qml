import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window
import QtQml
import Quickshell
import Quickshell.Io
import qs.Ui
import qs.Ui as Ui
import qs.Commons
import "Model.js" as Model

// AuroraPulse: one bar widget, one daemon, one player.
//
// The widget owns ./ap-ctl, which is the only thing that touches the network
// or spawns a player. This file renders the state the daemon pushes and sends
// one-line commands back; it never opens a socket and never polls. The daemon
// pushes a state event whenever anything changes and about once a second
// while playing, and the position in between is interpolated here.
Panel {
  id: root
  moduleName: "aurora-pulse"
  ipcTarget: "aurora-pulse"
  // Our own IpcHandler below adds the transport verbs (play-pause, stop, ...)
  // next to open/close, so the base class must not register a second one.
  manageIpc: false

  // ------------------------------------------------------------- settings

  function setting(key, fallback) {
    var s = root.settings
    if (s && s[key] !== undefined && s[key] !== null) return s[key]
    return fallback
  }

  // ------------------------------------------------------------- power

  // Off means off: no daemon, no player, nothing on the network. The power
  // button sets this, and anything that needs the daemon turns it back on.
  property bool poweredOff: false
  property bool daemonWanted: true
  // True for the pause between a crash and the restart. `running` is a
  // binding on these two, never assigned: assigning it once (as the restart
  // used to) silently cut it loose, and then power on could not start it.
  property bool restarting: false
  property bool connected: false
  // Something asked for while the daemon was off or still starting, sent as
  // soon as it says hello.
  property var pendingCommands: []

  // ------------------------------------------------------------- playback

  property var item: null
  property string mode: "off"           // off | loading | playing | paused | error
  property bool paused: true
  property bool buffering: false
  property bool muted: false
  property int volume: 70
  property double position: 0
  property double duration: 0
  property double positionBase: 0
  property double positionStamp: 0
  property double speed: 1.0
  property bool live: false
  property bool hasVideo: false
  property double alarmAt: 0
  property string casting: ""          // the device name while casting
  property bool castPaused: false
  property var castDevices: []
  property bool castSearching: false
  property var subtitleChoices: []
  property string subtitleSelected: ""
  // Renditions of an adaptive stream (TV): [{key, label}], best first.
  property var qualityChoices: []
  property string qualitySelected: ""
  property string qualityShort: ""
  property string qualityPreference: "best"
  property var playlists: []
  property var playlistView: null      // {id, name, smart, items} when one is open
  property bool hasNext: false
  property bool hasPrev: false
  property int index: -1
  property int queueLength: 0
  property var queueUids: ({})
  property string repeat: "off"
  property bool shuffle: false
  property string streamTitle: ""
  property string playError: ""
  property string notice: ""
  property bool pipFloating: false
  property bool favorite: false
  property var queueItems: []
  property var favorites: []
  property var history: []
  // browse | queue | saved: what the browser area shows under the tabs.
  property string browserView: "browse"

  // A local change wins over an echo for a moment. The daemon answers every
  // volume step with a state event, and one carrying the value from two steps
  // ago would yank the slider backwards mid-drag.
  property bool volumeDragging: false
  property double volumeHeldUntil: 0

  readonly property bool hasItem: !!item && typeof item === "object" && !!item.uid
  readonly property bool isLive: live || (hasItem && !!item.is_live)
  readonly property bool isPlaying: hasItem && mode === "playing"
  readonly property var nowSource: Model.sourceOf(hasItem ? item.source : source)

  // -------------------------------------------------------------- browser

  property string source: "radio"
  property string browseMode: "browse"
  property string searchText: ""
  property string country: ""
  property string genre: ""
  property string group: ""
  property string folder: ""
  property string sort: "title"
  // Local: "" for everything, "audio" or "video".
  property string localMedia: ""
  // Local views: "" (songs) | albums | artists | genres | folders, and the
  // one opened from those, e.g. { album: "Blue" }.
  property string localGroup: ""
  property var localFilter: null
  readonly property string localDrillName: localFilter
      ? String(localFilter.album || localFilter.artist || localFilter.genre
               || localFilter.folderName || "") : ""
  property var items: []
  property var genres: []
  property var countries: []
  property bool loading: false
  property bool scanning: false
  property string listError: ""
  property int listTotal: -1
  property int listLimit: 60
  // Another page exists; the daemon says so with every list.
  property bool listMore: false
  // Set by the `play <query>` IPC verb: play the first row of the next list.
  property bool playFirstResult: false
  // Podcasts: the show whose episodes are open.
  property var podcastShow: null
  property string podcastFeed: ""
  // The TV guide grid.
  property var guideRows: ({})
  property double guideStart: 0
  property double guideEnd: 0
  property double guideNow: 0
  property bool guideLoading: false
  property int guideRequestId: 0
  // Downloads, recording, video window, scrobbling.
  property var downloads: []
  property double recordingSince: 0
  property bool pipPinned: false
  property string pipSize: "m"
  property string pipCorner: "br"
  property var scrobbleStatus: ({})
  property var health: ({})
  property var healthPrompt: null       // {missing, installs, updates, required} at launch
  property var audioOutputs: ({ outputs: [], bluetooth: [], chosen: "" })
  readonly property string chosenOutputKind: {
    var outs = audioOutputs.outputs || []
    for (var i = 0; i < outs.length; i++) if (outs[i].current) return outs[i].kind
    return ""
  }
  // The "Play on" menu, as rows: this computer's outputs, paired Bluetooth
  // devices that can be connected, then what is on the network.
  readonly property var playOnRows: {
    var rows = [{ text: "This computer" }]
    var outs = audioOutputs.outputs || []
    for (var i = 0; i < outs.length; i++) {
      var o = outs[i]
      rows.push({ text: (o.current && casting === "" ? "● " : "    ") + o.description
                        + (o.kind === "bluetooth" ? "   Bluetooth" : o.kind === "hdmi" ? "   HDMI" : ""),
                  run: { cmd: "output", args: { action: "select", target: "sink:" + o.name } } })
    }
    var bt = audioOutputs.bluetooth || []
    for (var b = 0; b < bt.length; b++) {
      rows.push({ text: "    " + bt[b].description + "   connect over Bluetooth",
                  run: { cmd: "output", args: { action: "select", target: "bt:" + bt[b].mac,
                                                label: bt[b].description } } })
    }
    if (audioOutputs.chosen)
      rows.push({ text: "    Follow the system's default output",
                  run: { cmd: "output", args: { action: "select", target: "" } } })
    rows.push({ text: "On the network" })
    if (casting !== "") {
      rows.push({ text: "● " + casting })
    } else if (castSearching && !castDevices.length) {
      rows.push({ text: "    Looking for TVs and speakers…" })
    } else if (!castDevices.length) {
      rows.push({ text: "    No TV or network speaker answered" })
    }
    if (casting === "") {
      for (var d = 0; d < castDevices.length; d++) {
        var dev = castDevices[d]
        rows.push({ text: "    " + dev.name + (dev.model ? "  ·  " + dev.model : ""),
                    run: { cmd: "cast", args: { action: "start", device: dev.id } } })
      }
    }
    return rows
  }
  property var alarmInfo: ({})
  property var pipMonitors: []
  property double clockNow: Date.now()
  readonly property string recordingClock: {
    if (recordingSince <= 0) return ""
    var s = Math.max(0, Math.floor(clockNow / 1000 - recordingSince))
    function pad(n) { return n < 10 ? "0" + n : "" + n }
    return "\u25cf " + Math.floor(s / 60) + ":" + pad(s % 60)
  }
  Timer {
    interval: 1000
    repeat: true
    running: root.recordingSince > 0
    onTriggered: root.clockNow = Date.now()
  }

  // Each tab keeps its own list, filters and search, so switching tabs and
  // back shows exactly what was there - instantly - while a refresh runs.
  property var tabState: ({})
  property bool userPickedTab: false
  property bool preferredApplied: false
  // Start-up picks a tab once, when both the settings and the first state
  // have arrived: what is playing if anything is, else the default tab.
  property bool gotSettings: false
  property bool gotState: false
  property bool startupTabDone: false
  property bool startupWaited: false
  // A player that survived a shell restart can take a moment to report in;
  // the default tab waits this long for it before applying.
  Timer {
    id: startupWait
    interval: 1500
    onTriggered: { root.startupWaited = true; root.startupTab() }
  }
  // A row to bring into view once the list it is in has loaded.
  property string revealUid: ""

  // --------------------------------------------------------------- extras

  property var lyrics: null
  property int lyricIndex: -1
  property bool lyricsOpen: false
  property var epg: null
  property bool guideOpen: false
  property var settings2: ({})
  property bool settingsOpen: false
  property var catalogue: ({})
  property var syncProgress: ({})
  property var artByKey: ({})
  property string scanText: ""
  property var customData: ({})
  property var libraryStats: ({})
  property double sleepAt: 0

  // ---------------------------------------------------------------- theme

  readonly property color fg: bar ? bar.barForeground : Color.foreground
  readonly property color accent: Color.accent
  readonly property string fontFamily: Style.font.family
  readonly property color surface: Qt.rgba(Color.popups.background.r,
                                           Color.popups.background.g,
                                           Color.popups.background.b, 1)
  readonly property color panelFg: Color.popups.text
  readonly property color panelDim: Qt.rgba(panelFg.r, panelFg.g, panelFg.b, 0.55)
  readonly property color panelFaint: Qt.rgba(panelFg.r, panelFg.g, panelFg.b, 0.30)
  readonly property color panelPaper: Model.mix(surface, panelFg, 0.07)

  // ------------------------------------------------------------- backend

  readonly property string ctlPath: decodeURIComponent(
    String(Qt.resolvedUrl("ap-ctl")).replace(/^file:\/\//, ""))

  property int nextId: 0
  // The id of the newest request for the list on screen. Replies arrive out
  // of order from a pool, so only this one is the list under the cursor.
  property int listRequestId: 0
  // Facet requests (genres, countries) by id -> which tab and which facet, so
  // a reply lands in the right tab even after the user has moved on.
  property var facetRequests: ({})

  function send(message) {
    if (!backend.running || !connected) {
      // Queued rather than dropped: a click while the daemon starts is still
      // a click. Lists are re-requested on hello anyway, so only commands
      // that change something are worth keeping.
      if (message.cmd !== "source" && message.cmd !== "state") {
        pendingCommands = pendingCommands.concat([message])
      }
      if (poweredOff) powerOn()
      return
    }
    backend.write(JSON.stringify(message) + "\n")
  }

  function request(cmd, extra) {
    var message = { cmd: cmd }
    if (extra) for (var key in extra) message[key] = extra[key]
    message.id = ++nextId
    send(message)
    return message.id
  }

  // ------------------------------------------------------------ browsing

  function listArgs() {
    var args = { source: source, mode: browseMode,
                 limit: (source === "youtube" || source === "music") ? 30 : 60 }
    if (browseMode === "search") args.query = Model.query(searchText)
    if (source === "radio") {
      if (browseMode === "browse") args.section = "popular"
      if (country) args.country = country
      if (genre) args.genre = genre
    } else if (source === "tv") {
      if (country) args.country = country
      if (group) args.group = group
    } else if (source === "local") {
      if (localGroup && !localFilter) {
        // The albums, artists, genres or folders themselves.
        args.mode = localGroup
        if (browseMode === "search") args.query = Model.query(searchText)
      } else {
        if (folder) args.folder = folder
        if (localFilter) {
          if (localFilter.album !== undefined) args.album = localFilter.album
          if (localFilter.artist !== undefined) args.artist = localFilter.artist
          if (localFilter.genre !== undefined) args.genre = localFilter.genre
        }
        if (localMedia && !localGroup) args.media = localMedia
      }
      args.sort = sort
    } else if (source === "podcast" && browseMode === "episodes") {
      args.feed = podcastFeed
    }
    return args
  }

  function loadSource() {
    if (!connected) return
    // YouTube and Music only search; with no query there is nothing to ask.
    if ((source === "youtube" || source === "music") && browseMode !== "search") {
      loading = false
      return
    }
    loading = true
    listError = ""
    listRequestId = request("source", listArgs())
  }

  // The next page, appended. Pages are asked for by offset, so every row the
  // count promises can actually be reached - the old way re-fetched one ever
  // longer list and stopped as soon as a page came back short.
  function loadMore() {
    if (!connected || loading || !listMore) return
    var args = listArgs()
    args.offset = items.length
    loading = true
    listError = ""
    listRequestId = request("source", args)
  }

  function loadFacets() {
    if (!connected || (source !== "radio" && source !== "tv")) return
    var wanted = {}
    if (!genres.length) {
      var gid = request("source", { source: source,
                                    mode: source === "tv" ? "groups" : "genres",
                                    limit: 60 })
      wanted[gid] = { source: source, facet: "genres" }
    }
    if (!countries.length) {
      var cid = request("source", { source: source, mode: "countries", limit: 300 })
      wanted[cid] = { source: source, facet: "countries" }
    }
    facetRequests = Object.assign({}, facetRequests, wanted)
  }

  function saveTab() {
    var next = Object.assign({}, tabState)
    next[source] = {
      browseMode: browseMode, searchText: searchText, country: country,
      genre: genre, group: group, folder: folder, sort: sort, items: items,
      genres: genres, countries: countries, listTotal: listTotal,
      listLimit: listLimit, listMore: listMore,
      top: browser ? browser.topIndex() : 0
    }
    tabState = next
  }

  function selectSource(name, fromUser) {
    if (fromUser) userPickedTab = true
    browserView = "browse"
    if (name === source && items.length) return
    saveTab()
    var saved = tabState[name]
    source = name
    if (saved) {
      browseMode = saved.browseMode
      searchText = saved.searchText
      country = saved.country
      genre = saved.genre
      group = saved.group
      folder = saved.folder
      sort = saved.sort
      items = saved.items
      genres = saved.genres
      countries = saved.countries
      listTotal = saved.listTotal
      listLimit = saved.listLimit
      listMore = !!saved.listMore
      var top = saved.top || 0
      if (top > 0) Qt.callLater(function () { if (browser) browser.showIndex(top, false) })
    } else {
      browseMode = (name === "youtube" || name === "music") ? "search" : "browse"
      searchText = ""
      country = ""
      genre = ""
      group = ""
      folder = ""
      items = []
      genres = []
      countries = []
      listTotal = -1
      listLimit = 60
      listMore = false
    }
    listError = ""
    loading = false
    // The local catalogues are cheap to re-read, so they are refreshed under
    // the cached rows; a YouTube search is several seconds of yt-dlp, so a
    // tab that already has results keeps them until the user searches again.
    if (name === "youtube" || name === "music") {
      if (!items.length && searchText) loadSource()
    } else {
      loadSource()
    }
    loadFacets()
  }

  function submitSearch() {
    var text = Model.query(searchText)
    if (!text && (source === "radio" || source === "tv" || source === "local"
                  || source === "podcast")) {
      browseMode = "browse"
      podcastShow = null
    } else if (!text) {
      items = []
      loading = false
      return
    } else {
      browseMode = "search"
    }
    listLimit = 60
    loadSource()
  }

  function moreRequested() { loadMore() }

  // ---- podcasts

  function openShow(entry) {
    var extra = entry.extra || {}
    podcastFeed = extra.feed || entry.url || ""
    podcastShow = { title: entry.title, author: entry.artist, art: entry.art || {},
                    artUrl: "", show: extra.show || "", feed: podcastFeed,
                    subscribed: false }
    browseMode = "episodes"
    items = []
    listTotal = -1
    listMore = false
    loadSource()
  }

  function closeShow() {
    podcastShow = null
    podcastFeed = ""
    browseMode = Model.query(searchText) ? "search" : "browse"
    items = []
    loadSource()
  }

  function subscribe(show, on) {
    if (!show) return
    request("custom", { op: on ? "add" : "remove", kind: "podcast",
                        fields: { feed: show.feed, title: show.title,
                                  author: show.author || "", art: show.artUrl || "" } })
    podcastShow = Object.assign({}, show, { subscribed: on })
  }

  function requestGuide(channels) {
    guideLoading = true
    guideRequestId = request("epg_grid", { channels: channels, hours: 4 })
  }

  // ------------------------------------------------------------ playback

  function ensureOn() {
    if (poweredOff) powerOn()
  }

  // Play `entry` with the list it came from as the queue, so next and
  // previous step through the stations or tracks on screen. A window around
  // the row keeps the message small for a long list.
  function playEntry(entry, rowIndex) {
    if (!entry) return
    if (entry.kind === "podcast") {
      openShow(entry)
      return
    }
    if (entry.source === "local" && ["album", "artist", "genre", "folder"].indexOf(entry.kind) >= 0) {
      openLocalGroup(entry)
      return
    }
    ensureOn()
    var list = items || []
    var at = (rowIndex !== undefined && rowIndex >= 0 && list[rowIndex]
              && list[rowIndex].uid === entry.uid) ? rowIndex : list.indexOf(entry)
    playError = ""
    if (at < 0) {
      request("play", { item: entry })
      return
    }
    var low = Math.max(0, at - 50)
    var high = Math.min(list.length, at + 150)
    request("play", { items: list.slice(low, high), start: at - low })
    rememberBrowse()
    // Shown at once; the daemon confirms with a state event a moment later.
    item = entry
    mode = "loading"
    buffering = true
    paused = false
  }

  // An album, artist, genre or folder: list what is in it.
  function openLocalGroup(entry) {
    var extra = entry.extra || {}
    if (entry.kind === "folder") {
      folder = extra.path || ""
      localFilter = { folderName: entry.title }
    } else {
      var f = {}
      f[entry.kind] = extra[entry.kind] !== undefined ? extra[entry.kind] : entry.title
      localFilter = f
      folder = ""
    }
    browseMode = "browse"
    items = []
    listTotal = -1
    loadSource()
  }

  function chooseLocalView(choice) {
    if (["albums", "artists", "genres", "folders"].indexOf(choice) >= 0) {
      localGroup = choice
    } else {
      localGroup = ""
      localMedia = choice
    }
    localFilter = null
    folder = ""
    items = []
    listTotal = -1
    loadSource()
  }

  function closeLocalGroup() {
    localFilter = null
    folder = ""
    items = []
    listTotal = -1
    loadSource()
  }

  function playAll(entries) {
    if (!entries || !entries.length) return
    ensureOn()
    request("play", { items: entries.slice(0, 500), start: 0 })
  }

  function enqueue(entry) {
    if (!entry) return
    request("enqueue", { item: entry })
  }

  function playPause() {
    if (!hasItem && !queueLength) return
    ensureOn()
    if (hasItem && mode !== "loading") paused = !paused
    request("toggle")
  }

  function stop() {
    request("stop")
    item = null
    mode = "off"
    paused = true
    buffering = false
  }

  function next() { if (hasNext) request("next") }
  function previous() { request("previous") }

  function seekFraction(fraction) {
    var f = Math.max(0, Math.min(1, fraction))
    if (duration > 0) {
      positionBase = f * duration
      positionStamp = Date.now()
      position = positionBase
    }
    request("position", { fraction: f })
  }

  function seekSeconds(seconds) { request("seek", { seconds: seconds }) }

  function setVolume(value) {
    var v = Math.max(0, Math.min(100, Math.round(value)))
    if (v === volume && !muted) return
    volume = v
    if (v > 0) muted = false
    volumeHeldUntil = Date.now() + 800
    request("volume", { set: v })
  }

  function toggleMute() {
    muted = !muted
    volumeHeldUntil = Date.now() + 800
    request("mute", { muted: muted })
  }

  function cycleRepeat() { request("repeat") }
  function toggleShuffle() { request("shuffle") }
  function pip(action) { request("pip", { action: action }) }

  function powerOff() {
    request("shutdown")
    poweredOff = true
    item = null
    mode = "off"
    paused = true
    buffering = false
    playError = ""
    pendingCommands = []
    // Give the daemon a moment to stop the player and exit by itself; the
    // process is only stopped from here if it has not.
    powerOffTimer.restart()
  }

  function powerOn() {
    if (!poweredOff && daemonWanted) return
    poweredOff = false
    daemonWanted = true
  }

  function openLyrics() {
    lyricsOpen = !lyricsOpen
    guideOpen = false
    settingsOpen = false
    if (lyricsOpen) {
      lyrics = null
      request("lyrics")
    }
  }

  function openGuide() {
    guideOpen = !guideOpen
    lyricsOpen = false
    settingsOpen = false
    if (guideOpen) {
      epg = null
      request("epg", { channel: hasItem ? item.title : "" })
    }
  }

  function openSettings() {
    settingsOpen = !settingsOpen
    lyricsOpen = false
    guideOpen = false
    if (settingsOpen) {
      request("catalogue")
      request("custom", { op: "list" })
      request("library_stats")
      request("scrobble_status")
    }
  }

  function changeSetting(key, value) {
    // Optimistic, so a toggle moves the moment it is clicked; the daemon's
    // settings event confirms or corrects it.
    var next = Object.assign({}, settings2)
    next[key] = value
    settings2 = next
    var patch = {}
    patch[key] = value
    request("settings_set", { settings: patch })
  }

  // -------------------------------------------------------------- events

  function handle(line) {
    var msg
    try { msg = JSON.parse(line) } catch (e) { return }

    switch (msg.type) {
    case "hello":
      connected = true
      var queued = pendingCommands
      pendingCommands = []
      request("settings_get")
      request("catalogue")
      request("state")
      request("saved")
      request("downloads")
      request("output", { action: "list" })
      loadSource()
      loadFacets()
      for (var i = 0; i < queued.length; i++) send(queued[i])
      break

    case "settings":
      settings2 = msg.settings || {}
      // The default tab applies once, at start-up. It used to be applied on
      // every settings reply - and the panel asks for settings each time it
      // opens - so the browser jumped back to Radio while showing another
      // tab's list, and the tabs looked like they were not responding.
      var cc = String(settings2.preferredCountry || "")
      if (cc && !userPickedTab && source === "radio" && !country
          && !tabState.radio && !preferredApplied) {
        preferredApplied = true
        country = cc
        loadSource()
      }
      gotSettings = true
      startupTab()
      break

    case "state":
      applyState(msg)
      break

    case "queue":
      var uids = {}
      var list = msg.items || []
      for (var q = 0; q < list.length; q++) uids[list[q].uid] = true
      queueUids = uids
      queueLength = list.length
      queueItems = list
      break

    case "saved":
      favorites = msg.favorites || []
      history = msg.history || []
      playlists = msg.playlists || []
      if (playlistView && !playlists.some(function (p) { return p.id === playlistView.id }))
        playlistView = null
      break

    case "playlist":
      playlistView = { id: msg.playlist, name: msg.name, smart: !!msg.smart,
                       items: msg.items || [] }
      break

    case "cast":
      castDevices = msg.devices || []
      castSearching = !!msg.searching
      break

    case "subtitles":
      subtitleChoices = msg.choices || []
      subtitleSelected = msg.selected || ""
      break

    case "quality":
      qualityChoices = msg.choices || []
      qualitySelected = msg.selected || ""
      qualityShort = msg.short || ""
      qualityPreference = msg.preference || "best"
      break

    case "raise":
      root.open()
      break

    case "epg_grid":
      if (msg.id === guideRequestId) {
        var byUid = {}
        var rowsIn = msg.rows || []
        for (var g = 0; g < rowsIn.length; g++) byUid[rowsIn[g].uid] = rowsIn[g].programmes || []
        guideRows = byUid
        guideStart = msg.start || 0
        guideEnd = msg.end || 0
        guideNow = msg.now || 0
        guideLoading = false
      }
      break

    case "downloads":
      downloads = msg.jobs || []
      break

    case "recorded":
      showNotice("Recording saved to Music › AuroraPulse › Recordings")
      break

    case "scrobble":
      scrobbleStatus = msg.status || {}
      break

    case "health":
      health = msg
      break

    case "health_prompt":
      healthPrompt = msg
      break

    case "outputs":
      audioOutputs = { outputs: msg.outputs || [], bluetooth: msg.bluetooth || [],
                       chosen: msg.chosen || "" }
      break

    case "alarm":
      alarmInfo = msg.alarm || {}
      break

    case "list":
      handleList(msg)
      break

    case "art":
      var nextArt = Object.assign({}, artByKey)
      nextArt[msg.key] = msg.path
      artByKey = nextArt
      break

    case "catalogue": {
      var fresh = msg.catalogue || {}
      var before = catalogue || {}
      var changed = (source === "radio" && fresh.radio !== undefined
                     && before.radio !== undefined && fresh.radio !== before.radio)
                    || (source === "tv" && fresh.tv !== undefined
                        && before.tv !== undefined && fresh.tv !== before.tv)
      catalogue = fresh
      if (fresh.progress) {
        var merged = Object.assign({}, syncProgress)
        for (var key in fresh.progress) {
          if (fresh.progress[key]) merged[key] = fresh.progress[key]
        }
        syncProgress = merged
      }
      if (changed) {
        // A new mirror landed: the facets may have changed too.
        genres = []
        countries = []
        loadSource()
        loadFacets()
      }
      break
    }

    case "sync_progress": {
      var bySource = Object.assign({}, syncProgress)
      bySource[msg.source] = msg.progress || {}
      syncProgress = bySource
      break
    }

    case "lyrics": {
      var found = msg.lyrics ? Object.assign({}, msg.lyrics) : { lines: [], none: true }
      found.uid = msg.uid || ""
      lyrics = found
      lyricIndex = -1
      syncLyric()
      break
    }

    case "epg":
      epg = { now: msg.now || null, upcoming: msg.upcoming || [] }
      break

    case "custom":
      customData = msg.custom || {}
      break

    case "library_stats":
      libraryStats = msg
      break

    case "exported":
      showNotice("Saved to " + msg.path)
      break

    case "pip":
      pipFloating = !!msg.floating
      pipPinned = !!msg.pinned
      if (msg.size) pipSize = msg.size
      if (msg.corner) pipCorner = msg.corner
      if (msg.monitors) pipMonitors = msg.monitors
      break

    case "notice":
      showNotice(msg.title || "")
      break

    case "library":
      scanning = false
      scanText = ""
      if (source === "local") loadSource()
      break

    case "progress":
      if (msg.task === "scan") {
        scanning = true
        scanText = "scanning, " + (msg.done || 0) + " files"
      }
      break

    case "error":
      if (msg.id !== undefined && msg.id === listRequestId) {
        loading = false
        listError = msg.message || "Could not load this list"
      } else if (msg.id !== undefined && msg.id === guideRequestId) {
        guideLoading = false
      } else if (msg.id !== undefined && facetRequests[msg.id]) {
        // A missing facet list is not worth a red banner.
      } else {
        playError = msg.message || Model.reasonText(msg.reason, "")
        errorTimer.restart()
      }
      break

    case "bye":
      connected = false
      break

    default:
      break
    }
  }

  function handleList(msg) {
    if (msg.id === undefined) return
    if (msg.show && msg.id === listRequestId) {
      podcastShow = Object.assign({}, podcastShow || {}, msg.show)
    }
    var facet = facetRequests[msg.id]
    if (facet) {
      var remaining = Object.assign({}, facetRequests)
      delete remaining[msg.id]
      facetRequests = remaining
      var values
      if (facet.facet === "genres") {
        values = (msg.items || []).map(function (g) { return g.title })
      } else {
        values = (msg.items || []).map(function (c) {
          return { code: (c.extra || {}).code || "", title: c.title,
                   count: (c.extra || {}).count || 0 }
        })
      }
      if (facet.source === source) {
        if (facet.facet === "genres") genres = values
        else countries = values
      } else if (tabState[facet.source]) {
        var next = Object.assign({}, tabState)
        next[facet.source] = Object.assign({}, next[facet.source])
        next[facet.source][facet.facet] = values
        tabState = next
      }
      return
    }

    if (msg.source !== source || msg.id !== listRequestId) return
    loading = false
    listError = ""
    var page = msg.items || []
    if ((msg.offset || 0) > 0) {
      // A later page can repeat a row from an earlier one (a YouTube search
      // shifts a little between requests); a list must not show it twice.
      var have = {}
      for (var h = 0; h < items.length; h++) have[items[h].uid] = true
      items = items.concat(page.filter(function (e) { return !have[e.uid] }))
      listMore = !!msg.more
    } else if (page.length && page.length < items.length && samePrefix(page, items)) {
      // The same list asked for again - a tab opened again refreshes its
      // first page. The rows below it stay, and so does the scroll position.
      items = page.concat(items.slice(page.length))
    } else {
      items = page
      listMore = !!msg.more
    }
    listTotal = (msg.total === undefined || msg.total === null) ? -1 : msg.total
    revealPending()
    if (playFirstResult && items.length) {
      playFirstResult = false
      playEntry(items[0], 0)
    }
    if (msg.source === "local") scanning = !!msg.scanning
  }

  function samePrefix(page, list) {
    for (var i = 0; i < page.length; i++)
      if (!list[i] || !page[i] || list[i].uid !== page[i].uid) return false
    return true
  }

  // Once, at start-up: open on the tab of what is playing, the way it was
  // left when it was played (search, filters, the row), so a shell restart
  // or a reboot does not land on Radio while TV plays. With nothing playing
  // the default tab from Settings applies, as before.
  function startupTab() {
    if (startupTabDone || !gotSettings || !gotState) return
    if (!item && !startupWaited) {
      startupWait.start()
      return
    }
    startupTabDone = true
    if (userPickedTab) return
    var tab = item ? String(item.source || "") : ""
    if (["radio", "tv", "youtube", "music", "podcast", "local"].indexOf(tab) < 0) {
      var fallback = String(settings2.defaultSource || "")
      if (fallback && fallback !== source && !items.length) selectSource(fallback, false)
      return
    }
    var saved = null
    try { saved = JSON.parse(String(settings2.lastBrowse || "null")) } catch (e) { saved = null }
    if (saved && saved.source === tab) {
      var next = Object.assign({}, tabState)
      next[tab] = {
        browseMode: saved.browseMode || ((tab === "youtube" || tab === "music") ? "search" : "browse"),
        searchText: saved.searchText || "", country: saved.country || "",
        genre: saved.genre || "", group: saved.group || "", folder: saved.folder || "",
        sort: saved.sort || "title", items: [], genres: [], countries: [],
        listTotal: -1, listLimit: 60, listMore: false, top: 0
      }
      tabState = next
      if (tab === "local") {
        localMedia = saved.localMedia || ""
        localGroup = saved.localGroup || ""
        localFilter = saved.localFilter || null
      }
      revealUid = item.uid
    }
    if (tab === source && items.length) {
      revealUid = item.uid
      revealPending()
    } else {
      selectSource(tab, false)
    }
  }

  // The browsing that led to what is playing, kept in Settings so the next
  // start-up can open on it. Written only when it changes.
  function rememberBrowse() {
    var state = {
      source: source, browseMode: browseMode === "episodes" ? "browse" : browseMode,
      searchText: String(searchText || "").slice(0, 120), country: country, genre: genre,
      group: group, folder: folder, sort: sort
    }
    if (source === "local") {
      state.localMedia = localMedia
      state.localGroup = localGroup
      if (localFilter && JSON.stringify(localFilter).length < 160) state.localFilter = localFilter
    }
    // Angle brackets escaped: the daemon strips anything that looks like
    // markup from a setting, and JSON.parse reads \u003c back as "<".
    var text = JSON.stringify(state).replace(/</g, "\\u003c").replace(/>/g, "\\u003e")
    if (text.length > 500) return
    if (text !== String(settings2.lastBrowse || "")) changeSetting("lastBrowse", text)
  }

  // Bring the row being waited for into view: once it is in the list, or
  // after a few more pages when it is further down.
  function revealPending() {
    if (!revealUid) return
    for (var i = 0; i < items.length; i++) {
      if (items[i].uid === revealUid) {
        revealUid = ""
        var at = i
        Qt.callLater(function () { if (browser) browser.showIndex(at, true) })
        return
      }
    }
    if (listMore && items.length < 360) Qt.callLater(loadMore)
    else revealUid = ""
  }

  function applyState(msg) {
    var next = (msg.item && msg.item.uid) ? msg.item : null
    // A new stream has its own renditions; the daemon lists them once it plays.
    if (!next || !item || next.uid !== item.uid) qualityChoices = []
    item = next
    gotState = true
    if (!startupTabDone) Qt.callLater(startupTab)
    mode = msg.mode || (item ? (msg.paused ? "paused" : "playing") : "off")
    paused = msg.paused === undefined ? !item : !!msg.paused
    buffering = !!msg.buffering
    live = !!msg.live
    if (!volumeDragging && Date.now() > volumeHeldUntil) {
      if (msg.volume !== undefined) volume = msg.volume
      muted = !!msg.muted
    }
    duration = msg.duration || 0
    positionBase = msg.position || 0
    positionStamp = Date.now()
    position = positionBase
    speed = msg.speed || 1.0
    index = msg.index === undefined ? -1 : msg.index
    if (msg.queueLength !== undefined) queueLength = msg.queueLength
    hasNext = !!msg.hasNext
    hasPrev = !!msg.hasPrev
    repeat = msg.repeat || "off"
    shuffle = !!msg.shuffle
    hasVideo = !!msg.has_video
    streamTitle = msg.streamTitle || ""
    favorite = !!msg.favorite
    recordingSince = msg.recording || 0
    sleepAt = msg.sleepAt || 0
    alarmAt = msg.alarmAt || 0
    casting = msg.casting || ""
    castPaused = !!msg.castPaused
    if (msg.error) {
      playError = msg.error
      errorTimer.restart()
    }
    // A new item invalidates the lyrics on screen.
    if (lyricsOpen && item && lyrics && lyrics.uid !== undefined
        && lyrics.uid !== item.uid) {
      lyrics = null
      request("lyrics")
    }
  }

  function showNotice(text) {
    notice = text
    noticeTimer.restart()
  }

  function syncLyric() {
    if (!lyrics || !lyrics.lines || !lyrics.lines.length) {
      lyricIndex = -1
      return
    }
    lyricIndex = Model.activeLyricIndex(lyrics.lines, position)
  }

  onPositionChanged: if (lyricsOpen) syncLyric()

  // ============================================================== backend

  Process {
    id: backend
    command: ["python3", root.ctlPath, "daemon"]
    running: root.daemonWanted && !root.restarting
    stdinEnabled: true
    stdout: SplitParser {
      splitMarker: "\n"
      onRead: function (data) { root.handle(data) }
    }
    stderr: SplitParser {
      onRead: function (data) { console.warn("ap-ctl:", data) }
    }
    onRunningChanged: {
      if (!running) {
        root.connected = false
        root.item = null
        root.mode = "off"
      }
    }
    onExited: {
      root.connected = false
      if (root.daemonWanted) {
        root.restarting = true
        restartTimer.start()
      }
    }
  }

  Timer {
    id: restartTimer
    interval: 2000
    onTriggered: root.restarting = false
  }

  Timer {
    id: powerOffTimer
    interval: 1500
    onTriggered: root.daemonWanted = false
  }

  Timer {
    id: searchDebounce
    interval: 280
    onTriggered: root.submitSearch()
  }

  Timer {
    id: errorTimer
    interval: 9000
    onTriggered: root.playError = ""
  }

  Timer {
    id: noticeTimer
    interval: 2600
    onTriggered: root.notice = ""
  }

  // The playhead between the daemon's once-a-second pushes. Interpolated, not
  // polled: asking the daemon ten times a second is what used to saturate it.
  Timer {
    interval: 250
    repeat: true
    running: root.hasItem && root.mode === "playing" && !root.isLive
             && root.duration > 0
    onTriggered: {
      var elapsed = (Date.now() - root.positionStamp) / 1000 * root.speed
      root.position = Math.min(root.duration, root.positionBase + elapsed)
    }
  }

  // ============================================================== bar view

  readonly property bool verticalBar: bar ? bar.vertical : false
  readonly property string barTitle: {
    if (!hasItem) return ""
    var clean = Model.cleanName(item.title || "")
    if (isLive && streamTitle) return clean + " — " + streamTitle
    var artist = item.artist || ""
    if (artist && artist !== clean && !isLive) return artist + " — " + clean
    return clean
  }
  readonly property bool showBarTitle: setting("showTitle", true)
                                       && !verticalBar && barTitle !== ""
  readonly property real maxTitleWidth: Style.space(setting("maxTitleWidth", 150))

  readonly property string barHelp: "click: open  ·  middle-click: play/pause  ·  "
                                    + "right-click: stop  ·  scroll: volume"

  implicitWidth: button.implicitWidth + (showBarTitle ? titleClip.width + Style.space(4) : 0)
  implicitHeight: button.implicitHeight

  function barPress(b) {
    if (b === Qt.MiddleButton) root.playPause()
    else if (b === Qt.RightButton) {
      if (root.hasItem) root.stop()
      else root.toggle()
    } else root.toggle()
  }

  BarIconButton {
    id: button
    anchors.left: parent.left
    anchors.top: parent.top
    anchors.bottom: parent.bottom
    bar: root.bar
    tooltipText: {
      if (root.poweredOff) return "AuroraPulse is off\n" + root.barHelp
      if (!root.hasItem) return "AuroraPulse\n" + root.barHelp
      var name = Model.cleanName(root.item.title || "")
      var state = root.mode === "loading" ? "connecting"
                : (root.paused ? "paused" : (root.isLive ? "live" : "playing"))
      return name + " · " + state + "\n" + root.barHelp
    }
    iconComponent: Component {
      Item {
        width: 22
        height: 22
        Text {
          anchors.centerIn: parent
          text: root.poweredOff ? Model.ICON.power
                : (root.hasItem
                   ? (root.paused ? Model.ICON.pause
                      : (root.settings2.showSourceBadge !== false ? root.nowSource.icon
                                                                   : Model.ICON.music))
                   : Model.ICON.play)
          color: root.hasItem && !root.paused
                 ? root.nowSource.color
                 : (root.bar ? root.bar.barForeground : Color.foreground)
          opacity: root.poweredOff ? 0.45 : 1
          font.family: Style.font.family
          font.pixelSize: 15
        }
        // Casting, or a video window open: a small mark in the corner.
        Text {
          visible: root.hasItem && (root.casting !== ""
                                    || (root.hasVideo && root.settings2.showPipBadge !== false))
          anchors.right: parent.right
          anchors.top: parent.top
          anchors.rightMargin: -3
          anchors.topMargin: -2
          text: root.casting !== "" ? Model.ICON.cast : Model.ICON.pip
          color: root.nowSource.color
          font.family: Style.font.family
          font.pixelSize: 8
        }
        // A breathing dot on a live stream, a faster one while connecting.
        Rectangle {
          visible: root.hasItem && (root.mode === "loading"
                                    || (root.isLive && root.mode === "playing"))
          anchors.right: parent.right
          anchors.bottom: parent.bottom
          width: 5; height: 5; radius: 2.5
          color: root.nowSource.color
          SequentialAnimation on opacity {
            running: root.hasItem && root.mode !== "paused"
            loops: Animation.Infinite
            NumberAnimation {
              to: 0.25
              duration: root.mode === "loading" ? 350 : 900
            }
            NumberAnimation {
              to: 1.0
              duration: root.mode === "loading" ? 350 : 900
            }
          }
        }
      }
    }
    onPressed: function (b) { root.barPress(b) }
    onWheelMoved: function (delta) {
      root.setVolume(root.volume + (delta > 0 ? 5 : -5))
    }
  }

  Item {
    id: titleClip
    visible: root.showBarTitle
    anchors.right: parent.right
    anchors.verticalCenter: parent.verticalCenter
    width: Math.min(root.maxTitleWidth, titleImplicit.implicitWidth)
    height: parent.height
    clip: true

    Text {
      id: titleImplicit
      visible: false
      text: root.barTitle
      font.family: root.fontFamily
      font.pixelSize: Style.font.body
    }

    Item {
      id: ticker
      width: titleImplicit.implicitWidth
      height: titleImplicit.height
      anchors.verticalCenter: parent.verticalCenter
      readonly property real overflow: Math.max(0, titleImplicit.implicitWidth - titleClip.width)
      readonly property real loopDistance: titleImplicit.implicitWidth + Style.space(6)

      Text {
        text: root.barTitle
        color: root.bar ? root.bar.barForeground : Color.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        opacity: root.paused ? 0.55 : 1
        width: ticker.overflow > 1 && marquee.running ? titleImplicit.implicitWidth : titleClip.width
        elide: marquee.running ? Text.ElideNone : Text.ElideRight
      }

      SequentialAnimation {
        id: marquee
        loops: Animation.Infinite
        running: root.isPlaying && ticker.overflow > 1
        PauseAnimation { duration: 2500 }
        NumberAnimation {
          target: ticker
          property: "x"
          from: 0
          to: -ticker.overflow
          duration: Math.max(2000, ticker.overflow * 1000 / 30)
        }
        PauseAnimation { duration: 1500 }
        NumberAnimation {
          target: ticker
          property: "x"
          to: 0
          duration: 300
        }
      }
      Connections {
        target: marquee
        function onRunningChanged() { if (!marquee.running) ticker.x = 0 }
      }
    }
  }

  // The title does what the icon does, so the whole widget behaves as one.
  MouseArea {
    anchors.fill: titleClip
    visible: root.showBarTitle
    hoverEnabled: true
    acceptedButtons: Qt.LeftButton | Qt.MiddleButton | Qt.RightButton
    cursorShape: Qt.PointingHandCursor
    onClicked: function (mouse) { root.barPress(mouse.button) }
    onWheel: function (wheel) {
      root.setVolume(root.volume + (wheel.angleDelta.y > 0 ? 5 : -5))
    }
    onEntered: if (root.bar && root.bar.showTooltip) root.bar.showTooltip(root, button.tooltipText)
    onExited: if (root.bar && root.bar.hideTooltip) root.bar.hideTooltip(root)
  }

  // ============================================================== the panel

  readonly property real panelHeight: Math.max(
    Style.space(400),
    Math.min(Screen.height * 0.66, Screen.height - Style.gapsOut * 4))

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    contentWidth: panel.fittedContentWidth(Style.space(500))
    contentHeight: panel.fittedContentHeight(root.panelHeight)
    focusTarget: keyCatcher

    // Keyboard control while the panel is open and no text field has focus:
    //   ↑/↓ move through the list · Enter play it · Shift+Enter or A queue it
    //   Space play/pause · ←/→ seek 10 s · +/− or Shift+↑/↓ volume
    //   N/P next/previous · M mute · S stop · F favourite · Q queue
    //   / search · 1-5 tabs · Esc close
    Item {
      id: keyCatcher
      focus: true
      Keys.onPressed: function (event) {
        var k = event.key
        var handled = true
        if (k === Qt.Key_Space) root.playPause()
        else if (k === Qt.Key_Right) root.request("seek", { by: 10 })
        else if (k === Qt.Key_Left) root.request("seek", { by: -10 })
        else if ((k === Qt.Key_Up || k === Qt.Key_Down) && (event.modifiers & Qt.ShiftModifier))
          root.setVolume(root.volume + (k === Qt.Key_Up ? 5 : -5))
        else if (k === Qt.Key_Up) { if (!browser.moveCursor(-1)) root.setVolume(root.volume + 5) }
        else if (k === Qt.Key_Down) { if (!browser.moveCursor(1)) root.setVolume(root.volume - 5) }
        else if (k === Qt.Key_PageUp) browser.moveCursor(-8)
        else if (k === Qt.Key_PageDown) browser.moveCursor(8)
        else if (k === Qt.Key_Home) browser.moveCursor(-100000)
        else if (k === Qt.Key_End) browser.moveCursor(100000)
        else if ((k === Qt.Key_Return || k === Qt.Key_Enter) && (event.modifiers & Qt.ShiftModifier))
          browser.enqueueCursor()
        else if (k === Qt.Key_Return || k === Qt.Key_Enter) browser.activateCursor()
        else if (k === Qt.Key_A) browser.enqueueCursor()
        else if (k === Qt.Key_Plus || k === Qt.Key_Equal) root.setVolume(root.volume + 5)
        else if (k === Qt.Key_Minus || k === Qt.Key_Underscore) root.setVolume(root.volume - 5)
        else if (k === Qt.Key_N) root.next()
        else if (k === Qt.Key_P) root.previous()
        else if (k === Qt.Key_M) root.toggleMute()
        else if (k === Qt.Key_S) root.stop()
        else if (k === Qt.Key_F && root.hasItem) {
          root.favorite = !root.favorite
          root.request("favorite", { on: root.favorite })
        }
        else if (k === Qt.Key_Q) root.browserView = root.browserView === "queue" ? "browse" : "queue"
        else if (k === Qt.Key_Slash || (k === Qt.Key_F && (event.modifiers & Qt.ControlModifier))) browser.focusSearch()
        else if (k >= Qt.Key_1 && k <= Qt.Key_5) root.selectSource(["radio", "tv", "youtube", "music", "local"][k - Qt.Key_1], true)
        else if (k === Qt.Key_Escape) root.close()
        else handled = false
        event.accepted = handled
      }
    }

    // ---- off -----------------------------------------------------------

    ColumnLayout {
      anchors.centerIn: parent
      visible: root.poweredOff
      spacing: Style.space(10)

      Text {
        Layout.alignment: Qt.AlignHCenter
        text: Model.ICON.power
        color: root.panelFaint
        font.family: Style.font.family
        font.pixelSize: 40
      }
      Text {
        Layout.alignment: Qt.AlignHCenter
        text: "AuroraPulse is off"
        color: root.panelFg
        font.pixelSize: Style.font.title
        font.bold: true
      }
      Text {
        Layout.alignment: Qt.AlignHCenter
        text: "Nothing is playing and nothing is running in the background."
        color: root.panelDim
        font.pixelSize: Style.font.bodySmall
      }
      Rectangle {
        Layout.alignment: Qt.AlignHCenter
        Layout.topMargin: Style.space(6)
        implicitWidth: onLabel.implicitWidth + Style.space(32)
        implicitHeight: Style.space(34)
        radius: Style.space(17)
        color: onArea.containsMouse
               ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.30)
               : Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.18)
        border.width: 1
        border.color: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.6)
        Text {
          id: onLabel
          anchors.centerIn: parent
          text: Model.ICON.power + "  Turn on"
          color: root.accent
          font.family: Style.font.family
          font.pixelSize: Style.font.body
          font.bold: true
        }
        MouseArea {
          id: onArea
          anchors.fill: parent
          hoverEnabled: true
          cursorShape: Qt.PointingHandCursor
          onClicked: root.powerOn()
        }
      }
    }

    ColumnLayout {
      id: column
      anchors.fill: parent
      spacing: Style.space(8)
      visible: !root.poweredOff

      // ---- now playing ------------------------------------------------

      Item {
        id: headerRow
        Layout.fillWidth: true
        Layout.preferredHeight: Style.space(96)

        ArtTile {
          id: hdrArt
          anchors.left: parent.left
          anchors.top: parent.top
          width: Style.space(96)
          height: Style.space(96)
          item: {
            if (!root.hasItem) return root.item
            var key = root.item.art ? root.item.art.key : ""
            var path = key ? root.artByKey[key] : ""
            if (!path) return root.item
            var copy = Object.assign({}, root.item)
            copy.art = { url: "", path: path, key: key }
            return copy
          }
          radius: Style.space(8)
        }

        // Two columns: the artwork, and everything else. The quick tools sit
        // on the first row of the details, at the right, so the title below
        // them runs the full width instead of stopping short of a column of
        // buttons - which also left an empty gap under those buttons.
        Column {
          id: hdrText
          anchors.left: hdrArt.right
          anchors.leftMargin: Style.space(12)
          anchors.right: parent.right
          anchors.top: parent.top
          spacing: Style.space(1)

          Item {
            width: parent.width
            height: hdrButtons.height

            Row {
              id: hdrButtons
              anchors.right: parent.right
              spacing: Style.space(2)

              Text {
                visible: root.recordingSince > 0
                anchors.verticalCenter: parent.verticalCenter
                text: root.recordingClock
                color: "#ff5c5c"
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                rightPadding: Style.space(2)
              }
              IconButton {
                visible: root.hasItem && root.isLive
                icon: Model.ICON.record
                size: 28
                colorFg: root.recordingSince > 0 ? "#ff5c5c" : root.panelDim
                colorAccent: "#ff5c5c"
                active: root.recordingSince > 0
                tip: root.recordingSince > 0 ? "Stop recording (saved to Music › AuroraPulse › Recordings)"
                                             : "Record this stream"
                onClicked: root.request("record", { on: root.recordingSince <= 0 })
              }
              IconButton {
                id: downloadButton
                visible: root.hasItem && (root.item.source === "youtube"
                                          || root.item.source === "music"
                                          || root.item.kind === "episode")
                icon: Model.ICON.downloadOutline
                size: 28
                colorFg: root.panelDim
                colorAccent: root.accent
                tip: "Download for offline"
                onClicked: downloadMenu.popup()
                DownloadMenu {
                  id: downloadMenu
                  allowVideo: root.hasItem && root.item.source === "youtube"
                  defaultVideo: String(root.settings2.downloadVideoFormat || "mp4")
                  defaultAudio: String(root.settings2.downloadAudioFormat || "mp3")
                  onChosen: function (kind, format) {
                    root.request("download", { kind: kind, format: format })
                  }
                }
              }
              IconButton {
                visible: root.hasItem
                icon: root.favorite ? Model.ICON.heart : Model.ICON.heartOutline
                size: 28
                colorFg: root.favorite ? "#ff6b8a" : root.panelDim
                colorAccent: "#ff6b8a"
                tip: root.favorite ? "Remove from favourites" : "Add to favourites"
                onClicked: {
                  root.favorite = !root.favorite
                  root.request("favorite", { on: root.favorite })
                }
              }
              IconButton {
                visible: root.hasItem
                icon: Model.ICON.playlist
                size: 28
                colorFg: root.panelDim
                colorAccent: root.accent
                tip: "Add to a playlist"
                onClicked: playlistMenu.popup()
                PlaylistMenu {
                  id: playlistMenu
                  playlists: root.playlists
                  onChosen: function (id) {
                    if (id) root.request("playlist", { action: "add", playlist: id })
                    else root.request("playlist", {
                      action: "create", items: [root.item],
                      name: "Playlist " + (root.playlists.filter(function (p) { return !p.smart }).length + 1) })
                  }
                }
              }
              IconButton {
                // Only when the stream offers more than one rendition.
                visible: root.hasItem && root.hasVideo && root.qualityChoices.length > 1
                label: root.qualityShort || "Q"
                size: 28
                colorFg: root.panelDim
                colorAccent: root.accent
                tip: "Picture quality"
                onClicked: {
                  root.request("quality", { action: "list" })
                  qualityMenu.popup()
                }
                Menu {
                  id: qualityMenu
                  MenuItem {
                    text: (root.qualityPreference === "best" ? "● " : "    ") + "Best available"
                    onTriggered: root.request("quality", { action: "best" })
                  }
                  MenuSeparator {}
                  Instantiator {
                    model: root.qualityChoices
                    delegate: MenuItem {
                      required property var modelData
                      text: (modelData.key === root.qualitySelected ? "● " : "    ") + modelData.label
                      onTriggered: root.request("quality", { action: "select", key: modelData.key })
                    }
                    onObjectAdded: function (index, object) { qualityMenu.insertItem(index + 2, object) }
                    onObjectRemoved: function (index, object) { qualityMenu.removeItem(object) }
                  }
                }
              }
              IconButton {
                visible: root.hasItem && root.hasVideo
                icon: Model.ICON.subtitles
                size: 28
                colorFg: root.subtitleSelected !== "" ? root.accent : root.panelDim
                colorAccent: root.accent
                active: root.subtitleSelected !== ""
                tip: "Subtitles"
                onClicked: {
                  root.request("subtitle", { action: "list" })
                  subtitleMenu.popup()
                }
                Menu {
                  id: subtitleMenu
                  MenuItem {
                    text: (root.subtitleSelected === "" ? "● " : "    ") + "Off"
                    onTriggered: root.request("subtitle", { action: "off" })
                  }
                  Instantiator {
                    model: root.subtitleChoices
                    delegate: MenuItem {
                      required property var modelData
                      text: (modelData.key === root.subtitleSelected ? "● " : "    ") + modelData.label
                      onTriggered: root.request("subtitle", { action: "select", key: modelData.key })
                    }
                    onObjectAdded: function (index, object) { subtitleMenu.insertItem(index + 1, object) }
                    onObjectRemoved: function (index, object) { subtitleMenu.removeItem(object) }
                  }
                  MenuSeparator {}
                  MenuItem {
                    text: "Larger text"
                    onTriggered: root.request("subtitle", { action: "size",
                      size: Math.min(3, (Number(root.settings2.subtitleSize) || 1) + 0.2) })
                  }
                  MenuItem {
                    text: "Smaller text"
                    onTriggered: root.request("subtitle", { action: "size",
                      size: Math.max(0.5, (Number(root.settings2.subtitleSize) || 1) - 0.2) })
                  }
                }
              }
              IconButton {
                visible: root.hasItem || root.casting !== ""
                icon: root.casting !== "" ? Model.ICON.cast
                      : (root.chosenOutputKind === "bluetooth" ? Model.ICON.bluetooth
                         : root.chosenOutputKind === "headphones" ? Model.ICON.headphones
                         : Model.ICON.speaker)
                size: 28
                colorFg: root.casting !== "" || root.audioOutputs.chosen !== "" ? root.accent
                                                                                : root.panelDim
                colorAccent: root.accent
                active: root.casting !== ""
                tip: root.casting !== "" ? "Casting to " + root.casting
                     : "Play on: speakers, headphones, Bluetooth, a TV"
                onClicked: {
                  root.request("output", { action: "list" })
                  if (root.casting === "") root.request("cast", { action: "discover" })
                  playOnMenu.popup()
                }
                Menu {
                  id: playOnMenu
                  Instantiator {
                    model: root.playOnRows
                    delegate: MenuItem {
                      required property var modelData
                      text: modelData.text
                      enabled: !!modelData.run
                      onTriggered: if (modelData.run) root.request(modelData.run.cmd, modelData.run.args)
                    }
                    onObjectAdded: function (index, object) { playOnMenu.insertItem(index, object) }
                    onObjectRemoved: function (index, object) { playOnMenu.removeItem(object) }
                  }
                  MenuSeparator {}
                  MenuItem {
                    visible: root.casting !== ""
                    height: visible ? implicitHeight : 0
                    text: root.castPaused ? "Resume on the device" : "Pause on the device"
                    onTriggered: root.request("cast", { action: root.castPaused ? "resume" : "pause" })
                  }
                  MenuItem {
                    visible: root.casting !== ""
                    height: visible ? implicitHeight : 0
                    text: "Stop casting, play here"
                    onTriggered: root.request("cast", { action: "stop" })
                  }
                  MenuItem {
                    visible: root.casting === ""
                    height: visible ? implicitHeight : 0
                    text: "Search the network again"
                    onTriggered: {
                      root.request("cast", { action: "discover" })
                      playOnMenu.popup()
                    }
                  }
                }
              }
              IconButton {
                visible: root.hasItem && !root.isLive
                icon: Model.ICON.lyrics
                size: 28
                colorFg: root.panelDim
                colorAccent: root.accent
                active: root.lyricsOpen
                tip: "Lyrics"
                onClicked: root.openLyrics()
              }
              IconButton {
                visible: root.hasItem && root.item.source === "tv"
                icon: Model.ICON.guide
                size: 28
                colorFg: root.panelDim
                colorAccent: root.accent
                active: root.guideOpen
                tip: "TV guide"
                onClicked: root.openGuide()
              }
              IconButton {
                readonly property bool offline: root.settings2.offlineMode === true
                icon: offline ? Model.ICON.cloudOff : Model.ICON.cloud
                size: 28
                colorFg: offline ? "#e5c07b" : root.panelDim
                colorAccent: "#e5c07b"
                active: offline
                tip: offline ? "Offline: only your own music and downloads. Click to go online."
                             : "Go offline: no network at all"
                onClicked: root.changeSetting("offlineMode", !offline)
              }
              IconButton {
                icon: Model.ICON.settings
                size: 28
                colorFg: root.panelDim
                colorAccent: root.accent
                active: root.settingsOpen
                tip: "Settings"
                onClicked: root.openSettings()
              }
              IconButton {
                icon: Model.ICON.power
                size: 28
                colorFg: root.panelDim
                colorAccent: "#ff6b6b"
                accentOnHover: true
                tip: "Turn AuroraPulse off"
                onClicked: root.powerOff()
              }
            }
          }

          Text {
            width: parent.width
            text: root.hasItem ? Model.cleanName(root.item.title || "")
                               : (root.connected ? "Nothing playing" : "Starting…")
            color: root.panelFg
            font.pixelSize: Style.font.title
            font.bold: true
            elide: Text.ElideRight
          }
          Text {
            width: parent.width
            text: {
              if (!root.hasItem) return "Pick a station, channel or track below"
              if (root.mode === "loading") return "Connecting…"
              if (root.isLive && root.streamTitle) return root.streamTitle
              return root.item.artist || root.nowSource.label
            }
            color: root.mode === "loading" ? root.accent : root.panelDim
            font.pixelSize: Style.font.bodySmall
            elide: Text.ElideRight
          }
          Row {
            width: parent.width
            spacing: Style.space(6)
            visible: root.hasItem
            topPadding: Style.space(2)

            Rectangle {
              visible: root.isLive
              width: liveText.implicitWidth + 10
              height: liveText.implicitHeight + 4
              radius: 3
              color: Qt.rgba(root.nowSource.color.r, root.nowSource.color.g,
                             root.nowSource.color.b, 0.2)
              Text {
                id: liveText
                anchors.centerIn: parent
                text: "LIVE"
                color: root.nowSource.color
                font.pixelSize: 9
                font.bold: true
              }
            }
            Text {
              text: (root.hasItem ? root.nowSource.label : "")
                    + (root.hasItem && Model.quality(root.item) !== ""
                       ? "  •  " + Model.quality(root.item) : "")
                    + (root.queueLength > 1
                       ? "  •  " + (root.index + 1) + " of " + root.queueLength : "")
              color: root.panelFaint
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }
        }
      }

      // ---- seek ---------------------------------------------------------

      RowLayout {
        Layout.fillWidth: true
        Layout.fillHeight: false
        spacing: Style.space(8)
        visible: root.hasItem && !root.isLive && root.duration > 0

        Text {
          text: Model.duration(root.position) || "0:00"
          color: root.panelFaint
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }
        SeekBar {
          Layout.fillWidth: true
          position: root.position
          duration: root.duration
          accent: root.nowSource.color
          onSeek: function (fraction) { root.seekFraction(fraction) }
        }
        Text {
          text: Model.duration(root.duration)
          color: root.panelFaint
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }
      }

      // ---- transport ----------------------------------------------------

      RowLayout {
        Layout.fillWidth: true
        // Layouts fill by default in Qt 6; only the browser below should.
        Layout.fillHeight: false
        spacing: Style.space(2)

        IconButton {
          icon: Model.ICON.shuffle
          active: root.shuffle
          enabled: root.queueLength > 1
          colorFg: root.panelDim
          colorAccent: root.accent
          tip: root.shuffle ? "Shuffle is on" : "Shuffle"
          onClicked: root.toggleShuffle()
        }
        IconButton {
          icon: Model.ICON.previous
          enabled: root.hasPrev
          colorFg: root.panelFg
          colorAccent: root.accent
          tip: "Previous"
          onClicked: root.previous()
        }

        Rectangle {
          Layout.preferredWidth: Style.space(42)
          Layout.preferredHeight: Style.space(42)
          radius: Style.space(21)
          color: playArea.containsMouse
                 ? Qt.rgba(root.panelFg.r, root.panelFg.g, root.panelFg.b, 0.16)
                 : root.panelPaper
          opacity: root.hasItem || root.queueLength ? 1 : 0.5
          Spinner {
            anchors.centerIn: parent
            running: root.mode === "loading"
            idleText: root.paused ? Model.ICON.play : Model.ICON.pause
            color: root.panelFg
            size: Style.font.title
          }
          MouseArea {
            id: playArea
            anchors.fill: parent
            hoverEnabled: true
            enabled: root.hasItem || root.queueLength > 0
            cursorShape: Qt.PointingHandCursor
            onClicked: root.playPause()
          }
        }

        IconButton {
          icon: Model.ICON.next
          enabled: root.hasNext
          colorFg: root.panelFg
          colorAccent: root.accent
          tip: "Next"
          onClicked: root.next()
        }
        IconButton {
          icon: root.repeat === "one" ? Model.ICON.repeatOne : Model.ICON.repeat
          active: root.repeat !== "off"
          colorFg: root.panelDim
          colorAccent: root.accent
          tip: root.repeat === "one" ? "Repeat this one"
               : (root.repeat === "all" ? "Repeat the list" : "Repeat")
          onClicked: root.cycleRepeat()
        }
        IconButton {
          icon: Model.ICON.stop
          enabled: root.hasItem
          colorFg: root.panelFg
          colorAccent: root.accent
          tip: "Stop"
          onClicked: root.stop()
        }

        Item { Layout.fillWidth: true }

        VolumeControl {
          Layout.preferredWidth: Style.space(150)
          Layout.minimumWidth: Style.space(110)
          Layout.preferredHeight: Style.space(26)
          volume: root.volume
          muted: root.muted
          icon: Model.volumeIcon(root.volume, root.muted)
          accent: root.accent
          textColor: root.panelFaint
          onVolumeSet: function (v) { root.setVolume(v) }
          onToggleMute: root.toggleMute()
          onDraggingChanged: root.volumeDragging = dragging
        }
      }

      // ---- video window -------------------------------------------------
      //
      // Only while a video plays. The window itself has mpv's own on-screen
      // controls; these place it: size, corner, pin, fullscreen, close.

      RowLayout {
        Layout.fillWidth: true
        Layout.fillHeight: false
        visible: root.hasVideo
        spacing: Style.space(3)

        Text {
          text: Model.ICON.pip + "  Video"
          color: root.panelDim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          rightPadding: Style.space(4)
        }
        Repeater {
          model: ["s", "m", "l", "xl"]
          delegate: Ui.Button {
            required property string modelData
            text: modelData.toUpperCase()
            bordered: true
            foreground: root.pipSize === modelData && root.pipFloating ? root.accent : root.panelDim
            background: root.pipSize === modelData && root.pipFloating
                        ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.14) : "transparent"
            accent: root.accent
            fontSize: Style.font.caption
            verticalPadding: Style.space(1)
            horizontalPadding: Style.space(6)
            tooltipText: "Float the window at this size"
            onClicked: root.request("pip", { action: "size", size: modelData })
          }
        }
        Item { width: Style.space(4) }
        Repeater {
          model: [["tl", Model.ICON.cornerTL], ["tr", Model.ICON.cornerTR],
                  ["bl", Model.ICON.cornerBL], ["br", Model.ICON.cornerBR]]
          delegate: IconButton {
            required property var modelData
            icon: modelData[1]
            size: 24
            active: root.pipCorner === modelData[0] && root.pipFloating
            colorFg: root.panelDim
            colorAccent: root.accent
            tip: "Move to this corner"
            onClicked: root.request("pip", { action: "corner", corner: modelData[0] })
          }
        }
        Item { Layout.fillWidth: true }
        IconButton {
          icon: root.pipPinned ? Model.ICON.unpin : Model.ICON.pin
          size: 24
          active: root.pipPinned
          colorFg: root.panelDim
          colorAccent: root.accent
          tip: root.pipPinned ? "Unpin" : "Pin on top of every workspace"
          onClicked: root.request("pip", { action: "pin" })
        }
        IconButton {
          icon: Model.ICON.list
          size: 24
          active: !root.pipFloating
          colorFg: root.panelDim
          colorAccent: root.accent
          tip: root.pipFloating ? "Tile the window with the others" : "Float the window"
          onClicked: root.request("pip", { action: root.pipFloating ? "unfloat" : "float" })
        }
        IconButton {
          icon: Model.ICON.fullscreen
          size: 24
          colorFg: root.panelDim
          colorAccent: root.accent
          tip: "Fullscreen (press f in the window to leave)"
          onClicked: root.request("pip", { action: "fullscreen" })
        }
        IconButton {
          icon: Model.ICON.windowClose
          size: 24
          colorFg: root.panelDim
          colorAccent: "#ff6b6b"
          tip: "Close the video"
          onClicked: root.stop()
        }
      }

      // ---- messages -----------------------------------------------------

      Rectangle {
        Layout.fillWidth: true
        visible: root.playError !== ""
        Layout.preferredHeight: errorText.implicitHeight + Style.space(14)
        radius: Style.space(6)
        color: Qt.rgba(0.85, 0.25, 0.25, 0.16)
        Text {
          id: errorText
          anchors.left: parent.left
          anchors.right: errorClose.left
          anchors.verticalCenter: parent.verticalCenter
          anchors.margins: Style.space(7)
          text: root.playError
          color: Qt.rgba(1, 0.72, 0.72, 1)
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          wrapMode: Text.WordWrap
        }
        Text {
          id: errorClose
          anchors.right: parent.right
          anchors.rightMargin: Style.space(8)
          anchors.verticalCenter: parent.verticalCenter
          text: "✕"
          color: Qt.rgba(1, 0.72, 0.72, 0.8)
          font.pixelSize: 11
        }
        MouseArea {
          anchors.fill: parent
          cursorShape: Qt.PointingHandCursor
          onClicked: root.playError = ""
        }
      }

      // Something AuroraPulse needs is missing (at launch, and on first run
      // anything at all): say what, and offer to install it.
      Rectangle {
        id: healthBanner
        Layout.fillWidth: true
        visible: !!root.healthPrompt && !root.poweredOff
        Layout.preferredHeight: visible ? bannerColumn.implicitHeight + Style.space(16) : 0
        radius: Style.space(6)
        readonly property bool serious: !!(root.healthPrompt && root.healthPrompt.required)
        color: serious ? Qt.rgba(0.85, 0.25, 0.25, 0.16) : Qt.rgba(root.accent.r, root.accent.g,
                                                                    root.accent.b, 0.12)
        readonly property var installs: root.healthPrompt ? (root.healthPrompt.installs || []) : []
        readonly property var updates: root.healthPrompt ? (root.healthPrompt.updates || []) : []

        Column {
          id: bannerColumn
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          anchors.margins: Style.space(8)
          spacing: Style.space(6)
          Text {
            width: parent.width
            text: (healthBanner.serious ? "AuroraPulse cannot play without: "
                   : (root.healthPrompt && root.healthPrompt.first_run
                      ? "Welcome. A few things would make AuroraPulse work fully: "
                      : "Some things AuroraPulse uses are missing or out of date: "))
                  + (root.healthPrompt ? (root.healthPrompt.missing || []).join(", ") : "")
            color: root.panelFg
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }
          Row {
            spacing: Style.space(5)
            Ui.Button {
              visible: healthBanner.installs.length > 0 || healthBanner.updates.length > 0
              text: healthBanner.installs.length ? "Install all" : "Update"
              iconText: Model.ICON.download
              bordered: true
              selected: true
              foreground: root.panelFg
              accent: root.accent
              fontSize: Style.font.caption
              iconSize: Style.font.caption
              verticalPadding: Style.space(3)
              onClicked: {
                if (healthBanner.installs.length) root.request("health_install", {})
                if (healthBanner.updates.length) root.request("health_install", { update: true })
                root.healthPrompt = null
              }
            }
            Ui.Button {
              text: "Review in Health"
              iconText: Model.ICON.health
              bordered: true
              foreground: root.panelFg
              accent: root.accent
              fontSize: Style.font.caption
              iconSize: Style.font.caption
              verticalPadding: Style.space(3)
              onClicked: {
                root.openSettings()
                settingsView.page = "health"
                root.healthPrompt = null
              }
            }
            Ui.Button {
              text: "Not now"
              bordered: true
              foreground: root.panelDim
              accent: root.accent
              fontSize: Style.font.caption
              verticalPadding: Style.space(3)
              onClicked: {
                root.request("health_dismiss", { names: root.healthPrompt.missing || [] })
                root.healthPrompt = null
              }
            }
          }
        }
      }

      Text {
        Layout.fillWidth: true
        visible: root.notice !== ""
        text: root.notice
        color: root.accent
        font.pixelSize: Style.font.caption
        horizontalAlignment: Text.AlignHCenter
        elide: Text.ElideRight
      }

      // ---- working area -------------------------------------------------

      // The player above, the library below: one quiet line between them.
      PanelSeparator {
        Layout.fillWidth: true
        Layout.topMargin: Style.space(2)
        Layout.bottomMargin: Style.space(2)
        foreground: root.panelFg
      }

      StackLayout {
        Layout.fillWidth: true
        Layout.fillHeight: true
        currentIndex: root.lyricsOpen ? 1 : (root.guideOpen ? 2 : (root.settingsOpen ? 3 : 0))

        Browser {
          id: browser
          source: root.source
          mode: root.browseMode
          searchText: root.searchText
          items: root.items
          genres: root.genres
          countries: root.countries
          genreFilter: root.source === "tv" ? root.group : root.genre
          country: root.country
          loading: root.loading
          connected: root.connected
          scanning: root.scanning
          scanText: root.scanText
          errorText: root.listError
          currentUid: root.hasItem ? String(root.item.uid || "") : ""
          currentLoading: root.mode === "loading"
          currentPlaying: root.mode === "playing"
          more: root.listMore
          view: root.browserView
          queueItems: root.queueItems
          queueIndex: root.hasItem ? root.index : -1
          favorites: root.favorites
          history: root.history
          onSelectView: function (name) {
            root.browserView = name
            if (name === "saved") root.request("saved")
          }
          onQueueJump: function (i) { root.request("jump", { index: i }) }
          onQueueRemove: function (i) { root.request("remove", { index: i }) }
          onQueueMove: function (a, b) { root.request("move", { from: a, to: b }) }
          onQueueClear: root.request("clear")
          onPlayFrom: function (entry, list, i) {
            root.ensureOn()
            root.request("play", { items: list.slice(0, 500), start: i })
          }
          onFavorite: function (entry, on) { root.request("favorite", { item: entry, on: on }) }
          onForget: function (uid) { root.request("forget", { uid: uid }) }
          playlists: root.playlists
          playlistView: root.playlistView
          onPlaylistCommand: function (args) {
            if (args.action === "close") { root.playlistView = null; return }
            root.request("playlist", args)
          }
          onSearchDone: keyCatcher.forceActiveFocus()
          show: root.podcastShow
          guideRows: root.guideRows
          guideStart: root.guideStart
          guideEnd: root.guideEnd
          guideNow: root.guideNow
          guideLoading: root.guideLoading
          downloads: root.downloads
          onWantGuide: function (channels) { root.requestGuide(channels) }
          onOpenShow: function (entry) { root.openShow(entry) }
          onCloseShow: root.closeShow()
          onSubscribe: function (show, on) { root.subscribe(show, on) }
          onDownload: function (entry, kind, format) {
            root.request("download", { item: entry, kind: kind, format: format })
          }
          showBitrateMeta: root.settings2.showBitrate !== false
          showCountryMeta: root.settings2.showCountry !== false
          downloadVideoFormat: String(root.settings2.downloadVideoFormat || "mp4")
          downloadAudioFormat: String(root.settings2.downloadAudioFormat || "mp3")
          onCancelDownload: function (id) { root.request("download_cancel", { jobId: id }) }
          queueUids: root.queueUids
          artByKey: root.artByKey
          catalogue: root.catalogue
          syncProgress: root.syncProgress
          total: root.listTotal
          limit: root.listLimit
          viewMode: String(root.settings2.viewMode || "list")
          customCount: (root.catalogue || {})[root.source + "_custom"] || 0
          onViewModeSet: function (m) { root.changeSetting("viewMode", m) }
          fg: root.panelFg
          dim: root.panelDim
          faint: root.panelFaint
          accent: root.accent
          onUpdateCatalogue: function (src) {
            root.request("catalogue", { sync: true, full: true, source: src })
          }
          onSelectSource: function (name) { root.selectSource(name, true) }
          onSearchEdited: function (text) {
            if (text === root.searchText) return
            root.searchText = text
            // The local catalogues answer in milliseconds, so they filter as
            // you type. YouTube is several seconds of yt-dlp per query, so it
            // waits for Enter instead of firing a search per pause in typing.
            if (root.source === "youtube" || root.source === "music") {
              searchDebounce.stop()
            } else if (text === "") {
              searchDebounce.stop()
              root.submitSearch()
            } else {
              searchDebounce.restart()
            }
          }
          onSubmitted: {
            searchDebounce.stop()
            root.submitSearch()
          }
          onGenreChosen: function (g) {
            if (root.source === "tv") root.group = g || ""
            else root.genre = g || ""
            root.listLimit = 60
            root.loadSource()
          }
          onCountryChosen: function (code) {
            root.country = code || ""
            root.listLimit = 60
            root.loadSource()
          }
          onPlay: function (entry, row) { root.playEntry(entry, row) }
          onEnqueue: function (entry) { root.enqueue(entry) }
          onPlayAll: function (list) { root.playAll(list) }
          onLoadGenres: root.loadFacets()
          localMedia: root.localGroup || root.localMedia
          localDrill: root.localDrillName
          onLocalMediaChosen: function (media) {
            if (media === "__back__") root.closeLocalGroup()
            else root.chooseLocalView(media)
          }
          onWantMore: root.moreRequested()
          onRetry: root.loadSource()
        }

        LyricsView {
          lyrics: root.lyrics
          activeIndex: root.lyricIndex
          accent: root.nowSource.color
          fg: root.panelFg
          dim: root.panelDim
          faint: root.panelFaint
          size: String(root.settings2.lyricsSize || "m")
          onSeek: function (seconds) { root.seekSeconds(seconds) }
          onSizePicked: function (value) { root.changeSetting("lyricsSize", value) }
          onSave: root.request("lyrics_save", {})
        }

        GuideView {
          epg: root.epg
          accent: root.nowSource.color
          fg: root.panelFg
          dim: root.panelDim
          faint: root.panelFaint
        }

        SettingsView {
          id: settingsView
          settings: root.settings2
          catalogue: root.catalogue
          custom: root.customData
          libraryStats: root.libraryStats
          syncProgress: root.syncProgress
          sleepAt: root.sleepAt
          scrobbleStatus: root.scrobbleStatus
          health: root.health
          alarm: root.alarmInfo
          monitors: root.pipMonitors
          onSettingChanged: function (key, value) {
            root.changeSetting(key, value)
            if (key === "scanRoots" || key === "audioExtensions") root.request("library_stats")
          }
          onCommand: function (cmd, args) {
            root.request(cmd, args)
            if (cmd === "scan") root.scanning = true
          }
        }
      }
    }
  }

  // Every open shows the current truth. State is pushed on change, but the
  // list may be empty if the daemon was still starting when the panel last
  // looked.
  onHasVideoChanged: if (hasVideo) videoInfo.restart()
  Timer {
    id: videoInfo
    interval: 1500
    onTriggered: root.request("video_window")
  }

  onOpenedChanged: {
    if (!opened || poweredOff) return
    if (connected) {
      request("state")
      if (!items.length) loadSource()
      if (root.hasVideo) request("video_window")
    }
  }

  // ================================================================== IPC

  // omarchy-shell aurora-pulse <verb>, e.g. for a keybinding:
  //   omarchy-shell aurora-pulse playPause
  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function playPause(): void { root.playPause() }
    function stop(): void { root.stop() }
    function next(): void { root.next() }
    function previous(): void { root.previous() }
    function volumeUp(): void { root.setVolume(root.volume + 5) }
    function volumeDown(): void { root.setVolume(root.volume - 5) }
    function mute(): void { root.toggleMute() }
    function powerOff(): void { root.powerOff() }
    function powerOn(): void { root.powerOn() }
    // omarchy-shell aurora-pulse settings radio   (home, general, radio, tv, library)
    function settings(page: string): void {
      root.open()
      root.lyricsOpen = false
      root.guideOpen = false
      root.settingsOpen = true
      settingsView.page = page || "home"
      root.request("catalogue")
      root.request("custom", { op: "list" })
      root.request("library_stats")
    }
    // omarchy-shell aurora-pulse tab youtube
    function tab(name: string): void {
      root.open()
      root.settingsOpen = false
      root.lyricsOpen = false
      root.guideOpen = false
      if (name === "queue" || name === "saved") {
        root.browserView = name
        if (name === "saved") root.request("saved")
      } else if (name.indexOf("local:") === 0) {
        // omarchy-shell aurora-pulse tab local:albums   (artists, genres, folders, video)
        root.selectSource("local", true)
        root.chooseLocalView(name.slice(6))
      } else {
        root.selectSource(name, true)
      }
    }
    // omarchy-shell aurora-pulse search "daft punk"   (in the open tab)
    function search(query: string): void {
      root.open()
      root.settingsOpen = false
      root.searchText = query
      root.submitSearch()
    }
    // omarchy-shell aurora-pulse play "groove salad"   (first match in the open tab)
    function play(query: string): void {
      root.ensureOn()
      root.browserView = "browse"
      root.playFirstResult = true
      root.searchText = query
      root.submitSearch()
    }
    // omarchy-shell aurora-pulse view grid   (or list)
    function view(mode: string): void {
      root.changeSetting("viewMode", ["grid", "guide"].indexOf(mode) >= 0 ? mode : "list")
    }
    function status(): string {
      if (root.poweredOff) return "off"
      if (!root.hasItem) return "idle"
      return root.mode + ": " + (root.item.title || "")
    }
  }
}
