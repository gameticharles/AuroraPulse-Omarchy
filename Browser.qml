import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Ui as Ui
import "Model.js" as Model

// The source browser: tabs, a search row, filter chips and the list.
//
// Built from the shell's own controls - its text field, its searchable
// dropdown, its buttons - so it looks and behaves like every other Omarchy
// panel instead of approximating one. The rows are laid out per source,
// because a radio station, a TV channel, a YouTube video and a song do not
// carry the same information: a video wants a 16:9 thumbnail and its length,
// a song its artist and album, a station its genre and bitrate.
Item {
  id: root

  property string source: "radio"
  property string mode: "browse"
  property string searchText: ""
  property var items: []
  property var genres: []
  property var countries: []
  property string genre: ""
  property string country: ""
  // Settings › Browser: what a row says about a station.
  property bool showBitrateMeta: true
  property bool showCountryMeta: true
  // The active category: radio calls it a genre, TV a group.
  property string genreFilter: genre
  property bool loading: false
  property bool connected: true
  property bool scanning: false
  property string scanText: ""
  property bool currentLoading: false
  property bool currentPlaying: false
  property string viewMode: "list"
  property int customCount: 0
  property string errorText: ""
  property string currentUid: ""
  property var queueUids: ({})
  property var artByKey: ({})
  property var catalogue: ({})
  property var syncProgress: ({})
  // Rows the current filter matches, or -1 when the source cannot say.
  property int total: -1
  // Whether another page exists. The daemon decides; the panel used to guess
  // from the page being full, and a filtered page never was.
  property bool more: false
  property int limit: 60
  // browse | queue | saved - what the area under the tabs shows.
  property string view: "browse"
  property var queueItems: []
  property int queueIndex: -1
  property var favorites: []
  property var history: []
  property var playlists: []
  property var playlistView: null
  property var downloads: []
  readonly property bool browsing: view === "browse"
  // TV only: the programme guide grid instead of the channel list.
  readonly property bool guideView: browsing && source === "tv" && viewMode === "guide"
  property var guideRows: ({})
  property double guideStart: 0
  property double guideEnd: 0
  property double guideNow: 0
  property bool guideLoading: false
  // Podcasts: the show whose episodes are listed (mode "episodes"), or null.
  property var show: null
  readonly property bool showOpen: source === "podcast" && mode === "episodes" && !!show

  property color fg: Color.popups.text
  property color dim: Qt.rgba(fg.r, fg.g, fg.b, 0.55)
  property color faint: Qt.rgba(fg.r, fg.g, fg.b, 0.3)
  property color accent: Color.accent

  readonly property string fontFamily: Style.font.family
  readonly property bool gridView: viewMode === "grid"
  readonly property bool mirroredSource: source === "radio" || source === "tv"
  readonly property bool showCountry: mirroredSource && countries.length > 0
  readonly property bool wideArt: source === "youtube"

  readonly property string countryText: {
    if (!country) return ""
    for (var i = 0; i < countries.length; i++) {
      if (countries[i].code === country) return countries[i].title
    }
    return country
  }
  readonly property var countryOptions: {
    var noun = source === "tv" ? " channels" : " stations"
    var out = [{ value: "", label: "All countries" }]
    for (var i = 0; i < countries.length; i++) {
      var c = countries[i]
      out.push({ value: c.code, label: c.title,
                 description: Model.count(c.count || 0, "", "").trim() + noun })
    }
    return out
  }
  // "" is the "All" chip; then the user's own rows, then the genres.
  // Local: one library for music and video, narrowed with these chips.
  property string localMedia: ""
  // The album, artist, genre or folder opened in the Local tab, if any.
  property string localDrill: ""
  signal localMediaChosen(string media)
  readonly property var facetChips: {
    if (source === "local")
      return (localDrill ? ["__back__"] : []).concat(
        ["", "audio", "video", "albums", "artists", "genres", "folders"])
    if (!mirroredSource || (!genres.length && customCount === 0)) return []
    var out = [""]
    if (customCount > 0) out.push(Model.MINE)
    return out.concat(genres)
  }

  // ---- catalogue status (radio and tv) --------------------------------

  readonly property bool mirrored: mirroredSource
                                   && !!(catalogue && catalogue[source + "_mirrored"])
  readonly property int mirroredCount: catalogue ? (catalogue[source] || 0) : 0
  readonly property var updatedAgo: catalogue && catalogue[root.source + "_age"] !== undefined
                                    ? catalogue[root.source + "_age"] : null
  readonly property var myProgress: mirroredSource && syncProgress[source]
                                    ? syncProgress[source] : null
  readonly property bool downloading: !!myProgress && myProgress.stage !== "done"
                                      && myProgress.stage !== "failed"
  readonly property bool syncFailed: !!myProgress && myProgress.stage === "failed"
  readonly property bool queued: !downloading && !!catalogue && catalogue.pending === source
  readonly property var activeFilters: {
    var out = []
    if (mirroredSource && genreFilter) out.push(Model.facetLabel(genreFilter, source))
    if (mirroredSource && countryText) out.push(countryText)
    if (mode === "search" && searchText) out.push("“" + searchText + "”")
    return out
  }
  readonly property bool filtered: activeFilters.length > 0

  signal selectSource(string name)
  signal searchEdited(string text)
  signal submitted()
  signal genreChosen(string genre)
  signal countryChosen(string code)
  signal play(var entry, int row)
  signal enqueue(var entry)
  signal playAll(var entries)
  signal loadGenres()
  signal updateCatalogue(string source)
  signal retry()
  signal viewModeSet(string mode)
  signal wantMore()
  signal selectView(string name)
  // Esc in an empty search field: hand the keyboard back to the shortcuts.
  signal searchDone()
  signal wantGuide(var channels)
  signal openShow(var entry)
  signal closeShow()
  signal subscribe(var show, bool on)
  signal download(var entry, string kind, string format)
  property string downloadVideoFormat: "mp4"
  property string downloadAudioFormat: "mp3"
  signal cancelDownload(string id)
  signal queueJump(int index)
  signal queueRemove(int index)
  signal queueMove(int from, int to)
  signal queueClear()
  signal playFrom(var entry, var list, int index)
  signal favorite(var entry, bool on)
  signal forget(string uid)
  // Everything about playlists goes to the daemon's "playlist" command.
  signal playlistCommand(var args)

  readonly property var tabs: ["radio", "tv", "youtube", "music", "podcast", "local"]

  function requestGuide() {
    if (!root.guideView) return
    root.wantGuide(root.items.slice(0, 60).map(function (c) {
      var extra = c.extra || {}
      return { uid: c.uid, title: c.title, id: extra.id || "", country: extra.country || "" }
    }))
  }
  onGuideViewChanged: requestGuide()
  onItemsChanged: {
    if (root.guideView) Qt.callLater(root.requestGuide)
    if (root.cursor >= root.items.length) root.cursor = -1
  }

  // The keyboard cursor: ↑/↓ move it, Enter plays the row under it. -1 is
  // "no cursor", so a mouse user never sees a stray highlight.
  property int cursor: -1

  function moveCursor(by) {
    var n = root.items.length
    if (!root.browsing || n === 0 || root.guideView) return false
    var step = root.gridView ? by * grid.columns : by
    var next = root.cursor < 0 ? (by > 0 ? 0 : n - 1)
                               : Math.max(0, Math.min(n - 1, root.cursor + step))
    root.cursor = next
    if (root.gridView) grid.positionViewAtIndex(next, GridView.Contain)
    else list.positionViewAtIndex(next, ListView.Contain)
    return true
  }

  function activateCursor() {
    if (!root.browsing || root.cursor < 0 || root.cursor >= root.items.length) return false
    var entry = root.items[root.cursor]
    if (entry.kind === "podcast") root.openShow(entry)
    else root.play(entry, root.cursor)
    return true
  }

  function enqueueCursor() {
    if (!root.browsing || root.cursor < 0 || root.cursor >= root.items.length) return false
    root.enqueue(root.items[root.cursor])
    return true
  }

  onVisibleChanged: if (visible && mirroredSource && !genres.length) loadGenres()
  onSearchTextChanged: if (searchField.text !== root.searchText) searchField.text = root.searchText
  onSourceChanged: {
    cursor = -1
    list.positionViewAtBeginning()
    grid.positionViewAtBeginning()
  }

  function artItem(entry) {
    var key = entry && entry.art ? entry.art.key : ""
    var path = key ? root.artByKey[key] : ""
    if (!path) return entry
    var copy = Object.assign({}, entry)
    copy.art = { url: "", path: path, key: key }
    return copy
  }

  ColumnLayout {
    anchors.fill: parent
    spacing: Style.space(8)

    // ---- tabs ----------------------------------------------------------

    RowLayout {
      Layout.fillWidth: true
      Layout.fillHeight: false
      spacing: Style.space(4)

      Repeater {
        model: root.tabs
        delegate: Ui.Button {
          required property string modelData
          readonly property var meta: Model.sourceOf(modelData)
          readonly property bool active: root.browsing && root.source === modelData
          readonly property color tint: meta.color
          // The open tab says its name; the others are icons with a tooltip,
          // which is what lets six sources and the queue share one row.
          Layout.fillWidth: active
          Layout.preferredWidth: active ? -1 : Style.space(34)
          text: active ? meta.label : ""
          tooltipText: active ? "" : meta.label
          iconText: meta.icon
          // Each source wears its own colour while it is the open tab, and
          // goes back to the panel's ink when it is not. Not `selected`: the
          // shell paints a selected button in the theme's neutral ink, which
          // is exactly what made every active tab grey.
          bordered: active
          foreground: active ? tint : root.dim
          background: active ? Qt.rgba(tint.r, tint.g, tint.b, 0.14) : "transparent"
          accent: tint
          fontFamily: root.fontFamily
          fontSize: Style.font.bodySmall
          iconSize: Style.font.bodySmall
          verticalPadding: Style.space(4)
          onClicked: root.selectSource(modelData)
        }
      }

      Rectangle {
        Layout.preferredWidth: 1
        Layout.preferredHeight: Style.space(18)
        color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.15)
      }

      Ui.Button {
        readonly property bool active: root.view === "queue"
        text: root.queueItems.length > 0 ? String(root.queueItems.length) : ""
        iconText: Model.ICON.queue
        tooltipText: "Queue"
        bordered: active
        foreground: active ? root.accent : root.dim
        background: active ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.14) : "transparent"
        accent: root.accent
        fontFamily: root.fontFamily
        fontSize: Style.font.bodySmall
        iconSize: Style.font.body
        verticalPadding: Style.space(4)
        onClicked: root.selectView(active ? "browse" : "queue")
      }
      Ui.Button {
        readonly property bool active: root.view === "saved"
        readonly property color tint: "#ff6b8a"
        iconText: active ? Model.ICON.heart : Model.ICON.heartOutline
        tooltipText: "Favourites and recently played"
        bordered: active
        foreground: active ? tint : root.dim
        background: active ? Qt.rgba(tint.r, tint.g, tint.b, 0.14) : "transparent"
        accent: tint
        fontFamily: root.fontFamily
        iconSize: Style.font.body
        verticalPadding: Style.space(4)
        onClicked: root.selectView(active ? "browse" : "saved")
      }
    }

    // ---- search row ----------------------------------------------------

    Item {
      Layout.fillWidth: true
      Layout.fillHeight: false
      Layout.preferredHeight: searchField.implicitHeight
      visible: root.browsing && !root.showOpen

      Ui.TextField {
        id: searchField
        objectName: "auroraSearch"
        anchors.left: parent.left
        anchors.right: root.showCountry ? countryPicker.left : guideToggle.left
        anchors.rightMargin: Style.space(6)
        anchors.verticalCenter: parent.verticalCenter
        foreground: root.fg
        accent: root.accent
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        rightPadding: clearSearch.visible ? clearSearch.width + Style.space(12)
                                          : Style.spacing.controlPaddingX
        placeholderText: Model.ICON.search + "  " + (
          root.source === "local" ? "Filter your library"
          : root.source === "radio" ? "Search stations, genres, cities"
          : root.source === "tv" ? "Search channels"
          : root.source === "music" ? "Search songs and artists, then Enter"
          : root.source === "podcast" ? "Search podcasts"
          : "Search YouTube, then Enter")
        Component.onCompleted: text = root.searchText
        onTextChanged: if (text !== root.searchText) root.searchEdited(text)
        Keys.onReturnPressed: root.submitted()
        Keys.onEnterPressed: root.submitted()
        // ↓ from the search box goes into the results.
        Keys.onDownPressed: function (event) {
          if (root.items.length === 0) { event.accepted = false; return }
          root.searchDone()
          root.cursor = -1
          root.moveCursor(1)
        }
        Keys.onEscapePressed: function (event) {
          if (text !== "") text = ""
          else root.searchDone()
          event.accepted = true
        }

        Text {
          id: clearSearch
          visible: searchField.text !== ""
          anchors.right: parent.right
          anchors.rightMargin: Style.space(8)
          anchors.verticalCenter: parent.verticalCenter
          text: Model.ICON.close
          color: clearArea.containsMouse ? root.fg : root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          MouseArea {
            id: clearArea
            anchors.fill: parent
            anchors.margins: -Style.space(5)
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: searchField.text = ""
          }
        }
      }

      // Country: searchable, with how many stations each one has, so the
      // list of two hundred names is a few keystrokes rather than a scroll.
      Ui.SearchableDropdown {
        id: countryPicker
        visible: root.showCountry
        anchors.right: guideToggle.left
        anchors.rightMargin: Style.space(6)
        anchors.verticalCenter: parent.verticalCenter
        width: Style.space(150)
        showLabel: false
        rowHeight: searchField.implicitHeight
        value: root.country
        options: root.countryOptions
        triggerLabel: "All countries"
        placeholderText: "Search countries"
        emptyText: root.countries.length ? "No matching country" : "Loading countries…"
        foreground: root.fg
        accent: root.accent
        fontFamily: root.fontFamily
        onChanged: function (value) { root.countryChosen(value) }
      }

      Ui.Button {
        id: guideToggle
        visible: root.source === "tv"
        width: visible ? implicitWidth : 0
        anchors.right: viewToggle.left
        anchors.rightMargin: visible ? Style.space(2) : 0
        anchors.verticalCenter: parent.verticalCenter
        iconText: Model.ICON.guide
        tooltipText: root.guideView ? "Back to the channel list" : "TV guide"
        foreground: root.guideView ? Model.SOURCE.tv.color : root.dim
        background: root.guideView ? Qt.rgba(Model.SOURCE.tv.color.r, Model.SOURCE.tv.color.g,
                                             Model.SOURCE.tv.color.b, 0.14) : "transparent"
        accent: root.accent
        iconSize: Style.font.body
        onClicked: root.viewModeSet(root.guideView ? "list" : "guide")
      }

      Ui.Button {
        id: viewToggle
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        iconText: root.gridView ? Model.ICON.list : Model.ICON.grid
        tooltipText: root.gridView ? "Show as a list" : "Show as a grid of artwork"
        foreground: root.dim
        accent: root.accent
        iconSize: Style.font.body
        onClicked: root.viewModeSet(root.gridView ? "list" : "grid")
      }
    }

    // ---- an open podcast show ---------------------------------------------

    Item {
      Layout.fillWidth: true
      Layout.fillHeight: false
      Layout.preferredHeight: Style.space(52)
      visible: root.browsing && root.showOpen

      Ui.Button {
        id: backButton
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        iconText: Model.ICON.back
        tooltipText: "Back to podcasts"
        foreground: root.dim
        accent: root.accent
        iconSize: Style.font.body
        onClicked: root.closeShow()
      }
      ArtTile {
        id: showArt
        anchors.left: backButton.right
        anchors.leftMargin: Style.space(6)
        anchors.verticalCenter: parent.verticalCenter
        width: Style.space(46)
        height: Style.space(46)
        radius: Style.space(6)
        item: root.show ? root.artItem({ uid: "podcast:show", source: "podcast",
                                         title: root.show.title,
                                         art: root.show.art || {} })
                        : null
      }
      Column {
        anchors.left: showArt.right
        anchors.leftMargin: Style.space(10)
        anchors.right: subscribeButton.left
        anchors.rightMargin: Style.space(8)
        anchors.verticalCenter: parent.verticalCenter
        spacing: Style.space(2)
        Text {
          width: parent.width
          text: root.show ? root.show.title : ""
          color: root.fg
          font.pixelSize: Style.font.body
          font.bold: true
          elide: Text.ElideRight
        }
        Text {
          width: parent.width
          text: root.show ? [root.show.author, root.total >= 0
                             ? Model.count(root.total, "episode", "episodes") : ""]
                              .filter(function (x) { return !!x }).join("  ·  ") : ""
          color: root.dim
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }
      }
      Ui.Button {
        id: subscribeButton
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        readonly property bool on: !!(root.show && root.show.subscribed)
        text: on ? "Subscribed" : "Subscribe"
        iconText: on ? Model.ICON.ok : Model.ICON.rss
        bordered: true
        foreground: on ? Model.SOURCE.podcast.color : root.fg
        background: on ? Qt.rgba(Model.SOURCE.podcast.color.r, Model.SOURCE.podcast.color.g,
                                 Model.SOURCE.podcast.color.b, 0.14) : "transparent"
        accent: Model.SOURCE.podcast.color
        fontSize: Style.font.caption
        iconSize: Style.font.caption
        onClicked: root.subscribe(root.show, !on)
      }
    }

    // ---- filter chips ---------------------------------------------------

    ListView {
      id: genreStrip
      Layout.fillWidth: true
      Layout.fillHeight: false
      Layout.preferredHeight: visible ? Style.space(26) : 0
      visible: root.browsing && root.facetChips.length > 0
      orientation: ListView.Horizontal
      spacing: Style.space(5)
      clip: true
      boundsBehavior: Flickable.StopAtBounds
      model: root.facetChips

      delegate: Ui.Button {
        required property string modelData
        height: genreStrip.height
        text: root.source === "local"
              ? (modelData === "__back__" ? root.localDrill
                 : ({ "": "All", audio: "Music", video: "Videos", albums: "Albums",
                      artists: "Artists", genres: "Genres", folders: "Folders" })[modelData])
              : (modelData === "" ? "All" : Model.facetLabel(modelData, root.source))
        iconText: modelData === Model.MINE ? Model.ICON.star
                  : root.source !== "local" ? ""
                  : ({ __back__: Model.ICON.back, audio: Model.ICON.music, video: Model.ICON.video,
                       albums: Model.ICON.album, artists: Model.ICON.artist,
                       genres: Model.ICON.genre, folders: Model.ICON.folder })[modelData] || ""
        bordered: true
        selected: root.source === "local"
                  ? (modelData === "__back__" || (root.localMedia === modelData && !root.localDrill)
                     || (root.localMedia === modelData && root.localDrill !== ""
                         && ["albums", "artists", "genres", "folders"].indexOf(modelData) >= 0))
                  : (modelData === "" ? root.genreFilter === ""
                     : modelData.toLowerCase() === root.genreFilter.toLowerCase())
        foreground: root.fg
        accent: root.accent
        fontFamily: root.fontFamily
        fontSize: Style.font.caption
        iconSize: Style.font.caption
        verticalPadding: Style.space(2)
        horizontalPadding: Style.space(9)
        onClicked: {
          if (root.source === "local") root.localMediaChosen(modelData)
          else root.genreChosen(modelData)
        }
      }

      // The wheel scrolls the strip sideways.
      MouseArea {
        anchors.fill: parent
        acceptedButtons: Qt.NoButton
        onWheel: function (wheel) {
          var d = wheel.angleDelta.y !== 0 ? wheel.angleDelta.y : wheel.angleDelta.x
          var maxX = Math.max(0, genreStrip.contentWidth - genreStrip.width)
          genreStrip.contentX = Math.max(0, Math.min(maxX, genreStrip.contentX - d))
        }
      }
    }

    // ---- queue and saved -------------------------------------------------

    QueueView {
      Layout.fillWidth: true
      Layout.fillHeight: true
      visible: root.view === "queue"
      items: root.queueItems
      index: root.queueIndex
      artByKey: root.artByKey
      currentLoading: root.currentLoading
      currentPlaying: root.currentPlaying
      fg: root.fg
      accent: root.accent
      onJump: function (i) { root.queueJump(i) }
      onRemove: function (i) { root.queueRemove(i) }
      onMove: function (a, b) { root.queueMove(a, b) }
      onClear: root.queueClear()
    }

    SavedView {
      Layout.fillWidth: true
      Layout.fillHeight: true
      visible: root.view === "saved"
      favorites: root.favorites
      history: root.history
      playlists: root.playlists
      playlistView: root.playlistView
      downloads: root.downloads
      artByKey: root.artByKey
      currentUid: root.currentUid
      currentPlaying: root.currentPlaying
      fg: root.fg
      accent: root.accent
      onPlay: function (entry, list, i) { root.playFrom(entry, list, i) }
      onUnfavorite: function (entry) { root.favorite(entry, false) }
      onForget: function (uid) { root.forget(uid) }
      onClearHistory: root.forget("")
      onPlaylistCommand: function (args) { root.playlistCommand(args) }
      onCancelDownload: function (id) { root.cancelDownload(id) }
    }

    // ---- results --------------------------------------------------------

    Item {
      Layout.fillWidth: true
      Layout.fillHeight: true
      visible: root.browsing

      // Empty, loading and failure states.
      Column {
        anchors.centerIn: parent
        width: parent.width - Style.space(40)
        spacing: Style.space(8)
        visible: root.items.length === 0

        Spinner {
          anchors.horizontalCenter: parent.horizontalCenter
          running: root.loading
          idleText: root.errorText ? Model.ICON.alert : Model.sourceOf(root.source).icon
          color: root.loading ? root.accent : root.faint
          size: Style.space(28)
        }
        Text {
          width: parent.width
          horizontalAlignment: Text.AlignHCenter
          wrapMode: Text.WordWrap
          text: root.emptyText()
          color: root.errorText ? "#ff9b9b" : root.dim
          font.pixelSize: Style.font.bodySmall
        }
        Ui.Button {
          anchors.horizontalCenter: parent.horizontalCenter
          visible: root.errorText !== "" && !root.loading
          text: "Try again"
          bordered: true
          foreground: root.fg
          accent: root.accent
          fontSize: Style.font.caption
          onClicked: root.retry()
        }
      }

      GuideGrid {
        anchors.fill: parent
        visible: root.guideView && root.items.length > 0
        items: root.items.slice(0, 60)
        rows: root.guideRows
        start: root.guideStart
        end: root.guideEnd
        now: root.guideNow
        loading: root.guideLoading
        artByKey: root.artByKey
        currentUid: root.currentUid
        fg: root.fg
        accent: root.accent
        onPlay: function (entry, i) { root.play(entry, i) }
      }

      ListView {
        id: list
        anchors.fill: parent
        visible: root.items.length > 0 && !root.gridView && !root.guideView
        clip: true
        spacing: Style.space(2)
        model: root.items
        boundsBehavior: Flickable.StopAtBounds
        onContentYChanged: root.maybeLoadMore(list)
        onContentHeightChanged: root.maybeLoadMore(list)
        ScrollBar.vertical: ScrollBar {
          policy: list.contentHeight > list.height ? ScrollBar.AsNeeded : ScrollBar.AlwaysOff
        }

        delegate: Item {
          id: row
          required property var modelData
          required property int index
          width: list.width - Style.space(8)
          // 16:9 for anything that is a picture: YouTube, and local videos.
          readonly property bool wide: root.wideArt
                                       || (row.modelData.extra || {}).media === "video"
          height: wide ? Style.space(66) : Style.space(54)

          readonly property bool isCurrent: root.currentUid !== ""
                                            && modelData.uid === root.currentUid
          readonly property bool hot: rowArea.containsMouse || actionsHover.hovered
          readonly property color ink: isCurrent ? root.accent : root.fg

          Ui.BorderSurface {
            anchors.fill: parent
            radius: Style.cornerRadius
            color: rowArea.pressed ? Style.pressedFillFor(root.fg, root.accent)
                 : (row.hot || row.index === root.cursor) ? Style.hoverFillFor(root.fg, root.accent)
                 : row.isCurrent ? Style.normalFillFor(root.fg, root.accent)
                 : "transparent"
            borderSpec: Border.none()
            Behavior on color { ColorAnimation { duration: 110 } }
          }
          // The keyboard cursor, as a ring: visible over the playing row too.
          Rectangle {
            anchors.fill: parent
            visible: row.index === root.cursor
            radius: Style.cornerRadius
            color: "transparent"
            border.width: 1
            border.color: root.accent
          }

          // Declared before the buttons: siblings take input in reverse
          // order, so a row-wide MouseArea declared after them would sit on
          // top and swallow every click meant for a button.
          MouseArea {
            id: rowArea
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            acceptedButtons: Qt.LeftButton | Qt.RightButton
            onClicked: function (mouse) {
              if (mouse.button === Qt.RightButton) rowMenu.popup()
              else if (row.modelData.kind === "podcast") root.openShow(row.modelData)
              else root.play(row.modelData, row.index)
            }
          }

          ArtTile {
            id: art
            anchors.left: parent.left
            anchors.leftMargin: Style.space(6)
            anchors.verticalCenter: parent.verticalCenter
            height: row.wide ? Style.space(54) : Style.space(42)
            width: row.wide ? Math.round(height * 16 / 9) : height
            item: root.artItem(row.modelData)
            radius: Style.space(5)

            // The length, on the thumbnail, where YouTube puts it.
            Rectangle {
              visible: row.wide && Model.duration(row.modelData.duration) !== ""
              anchors.right: parent.right
              anchors.bottom: parent.bottom
              anchors.margins: Style.space(3)
              width: badge.implicitWidth + Style.space(8)
              height: badge.implicitHeight + Style.space(2)
              radius: Style.space(3)
              color: Qt.rgba(0, 0, 0, 0.75)
              Text {
                id: badge
                anchors.centerIn: parent
                text: Model.duration(row.modelData.duration)
                color: "white"
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption - 1
              }
            }

            // Playing marker: a spinner while it connects, moving bars after.
            Rectangle {
              anchors.fill: parent
              radius: Style.space(5)
              visible: row.isCurrent
              color: Qt.rgba(0, 0, 0, 0.5)
              Spinner {
                anchors.centerIn: parent
                visible: root.currentLoading
                running: row.isCurrent && root.currentLoading
                color: root.accent
                size: Style.space(18)
              }
              EqBars {
                anchors.centerIn: parent
                visible: !root.currentLoading
                width: Style.space(16)
                height: Style.space(14)
                color: root.accent
                playing: row.isCurrent && root.currentPlaying
              }
            }
          }

          Column {
            anchors.left: art.right
            anchors.leftMargin: Style.space(10)
            anchors.right: trailing.left
            anchors.rightMargin: Style.space(8)
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(2)

            Text {
              width: parent.width
              text: root.displayTitle(row.modelData)
              color: row.ink
                            font.pixelSize: Style.font.bodySmall + 1
              font.bold: row.isCurrent
              elide: Text.ElideRight
              wrapMode: row.wide ? Text.Wrap : Text.NoWrap
              maximumLineCount: row.wide ? 2 : 1
            }
            Text {
              width: parent.width
              text: root.displaySub(row.modelData)
              color: root.dim
                            font.pixelSize: Style.font.caption
              elide: Text.ElideRight
              visible: text !== ""
            }
          }

          Item {
            id: trailing
            anchors.right: parent.right
            anchors.rightMargin: Style.space(6)
            anchors.verticalCenter: parent.verticalCenter
            width: Math.max(metaText.implicitWidth, actions.implicitWidth)
            height: parent.height

            HoverHandler { id: actionsHover }

            Text {
              id: metaText
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              visible: !row.hot
              text: root.displayMeta(row.modelData)
              color: root.faint
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            Row {
              id: actions
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              visible: row.hot
              spacing: 0
              IconButton {
                icon: Model.ICON.play
                size: 26
                colorFg: root.fg
                tip: "Play"
                onClicked: root.play(row.modelData, row.index)
              }
              IconButton {
                icon: Model.ICON.playlist
                size: 26
                colorFg: root.dim
                active: !!root.queueUids[row.modelData.uid]
                tip: "Add to the queue"
                onClicked: root.enqueue(row.modelData)
              }
            }
          }

          Menu {
            id: rowMenu
            MenuItem {
              text: "Play"
              onTriggered: root.play(row.modelData, row.index)
            }
            MenuItem {
              text: "Add to queue"
              onTriggered: root.enqueue(row.modelData)
            }
            MenuItem {
              text: "Add to favourites"
              onTriggered: root.favorite(row.modelData, true)
            }
            PlaylistMenu {
              title: "Add to playlist"
              playlists: root.playlists
              onChosen: function (id) {
                if (id) root.playlistCommand({ action: "add", playlist: id, item: row.modelData })
                else root.playlistCommand({ action: "create", name: "Playlist " + (root.playlists.filter(
                  function (p) { return !p.smart }).length + 1), items: [row.modelData] })
              }
            }
            DownloadMenu {
              title: "Download"
              enabled: ["youtube", "music"].indexOf(row.modelData.source) >= 0
                       || row.modelData.kind === "episode"
              allowVideo: row.modelData.source === "youtube"
              defaultVideo: root.downloadVideoFormat
              defaultAudio: root.downloadAudioFormat
              onChosen: function (kind, format) { root.download(row.modelData, kind, format) }
            }
            MenuSeparator {}
            MenuItem {
              text: "Copy title"
              onTriggered: root.copyText(root.displayTitle(row.modelData))
            }
            MenuItem {
              text: "Copy address"
              enabled: !!row.modelData.url
              onTriggered: root.copyText(String(row.modelData.url || ""))
            }
          }
        }

        footer: listFooter
      }

      GridView {
        id: grid
        anchors.fill: parent
        visible: root.items.length > 0 && root.gridView && !root.guideView
        clip: true
        model: root.items
        boundsBehavior: Flickable.StopAtBounds
        readonly property int columns: Math.max(2, Math.floor(width / Style.space(118)))
        cellWidth: Math.floor(width / columns)
        cellHeight: (root.wideArt ? Math.round((cellWidth - 12) * 9 / 16) : cellWidth - 12)
                    + Style.space(44)
        onContentYChanged: root.maybeLoadMore(grid)
        onContentHeightChanged: root.maybeLoadMore(grid)
        ScrollBar.vertical: ScrollBar {}
        footer: listFooter

        delegate: Item {
          id: tile
          required property var modelData
          required property int index
          width: grid.cellWidth
          height: grid.cellHeight
          readonly property bool isCurrent: root.currentUid !== ""
                                            && modelData.uid === root.currentUid

          Ui.BorderSurface {
            anchors.fill: parent
            anchors.margins: 3
            radius: Style.cornerRadius
            color: (tileArea.containsMouse || tile.index === root.cursor)
                 ? Style.hoverFillFor(root.fg, root.accent)
                 : tile.isCurrent ? Style.normalFillFor(root.fg, root.accent) : "transparent"
            borderSpec: Border.none()
          }
          Rectangle {
            anchors.fill: parent
            anchors.margins: 3
            visible: tile.index === root.cursor
            radius: Style.cornerRadius
            color: "transparent"
            border.width: 1
            border.color: root.accent
          }

          ArtTile {
            id: tileArt
            anchors.top: parent.top
            anchors.topMargin: 6
            anchors.horizontalCenter: parent.horizontalCenter
            width: grid.cellWidth - 12
            height: root.wideArt ? Math.round(width * 9 / 16) : width
            item: root.artItem(tile.modelData)
            radius: Style.space(6)

            Rectangle {
              anchors.fill: parent
              radius: Style.space(6)
              visible: tile.isCurrent || tileArea.containsMouse
              color: Qt.rgba(0, 0, 0, tile.isCurrent ? 0.5 : 0.3)
              Spinner {
                anchors.centerIn: parent
                visible: !(tile.isCurrent && !root.currentLoading)
                running: tile.isCurrent && root.currentLoading
                idleText: Model.ICON.play
                color: tile.isCurrent ? root.accent : "white"
                size: Style.space(26)
              }
              EqBars {
                anchors.centerIn: parent
                visible: tile.isCurrent && !root.currentLoading
                width: Style.space(22)
                height: Style.space(20)
                color: root.accent
                playing: root.currentPlaying
              }
            }
          }

          Text {
            anchors.top: tileArt.bottom
            anchors.topMargin: Style.space(4)
            anchors.left: tileArt.left
            anchors.right: tileArt.right
            text: root.displayTitle(tile.modelData)
            color: tile.isCurrent ? root.accent : root.fg
                        font.pixelSize: Style.font.caption + 1
            font.bold: tile.isCurrent
            wrapMode: Text.Wrap
            maximumLineCount: 2
            elide: Text.ElideRight
            horizontalAlignment: Text.AlignHCenter
          }

          MouseArea {
            id: tileArea
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            acceptedButtons: Qt.LeftButton | Qt.RightButton
            onClicked: function (mouse) {
              if (mouse.button === Qt.RightButton) root.enqueue(tile.modelData)
              else if (tile.modelData.kind === "podcast") root.openShow(tile.modelData)
              else root.play(tile.modelData, tile.index)
            }
          }
          ThemedToolTip {
            visible: tileArea.containsMouse
            delay: 700
            text: root.displayTitle(tile.modelData)
                  + (root.displaySub(tile.modelData) ? "\n" + root.displaySub(tile.modelData) : "")
                  + "\nright-click: add to the queue"
          }
        }
      }

      // The end of the list says what it is: more on the way, more to ask
      // for, or genuinely the end.
      Component {
        id: listFooter
        Item {
          width: root.width
          height: root.items.length > 0 ? Style.space(44) : 0

          Row {
            anchors.centerIn: parent
            spacing: Style.space(6)
            visible: root.loading
            Spinner {
              anchors.verticalCenter: parent.verticalCenter
              running: root.loading
              color: root.accent
              size: Style.font.body
            }
            Text {
              text: "Loading more…"
              color: root.dim
              font.pixelSize: Style.font.caption
            }
          }

          Ui.Button {
            anchors.centerIn: parent
            visible: !root.loading && root.more
            text: "Load more  ·  " + Model.count(root.items.length, "", "").trim()
                  + (root.total >= 0 ? " of " + Model.count(root.total, "", "").trim() : "")
            bordered: true
            foreground: root.fg
            accent: root.accent
            fontSize: Style.font.caption
            onClicked: root.wantMore()
          }

          Text {
            anchors.centerIn: parent
            visible: !root.loading && !root.more && root.items.length >= 20
            text: "All " + Model.count(root.items.length, "", "").trim() + " shown"
            color: root.faint
            font.pixelSize: Style.font.caption
          }
        }
      }

      // Off-screen helper for "copy".
      TextEdit {
        id: copyArea
        visible: false
      }
    }

    // ---- status bar -----------------------------------------------------

    RowLayout {
      Layout.fillWidth: true
      Layout.fillHeight: false
      visible: root.browsing
      spacing: Style.space(8)

      Text {
        Layout.fillWidth: true
        text: {
          if (root.source === "local" && root.scanning) return root.scanText || "Scanning your music folders…"
          if (!root.mirroredSource) {
            return root.items.length ? Model.count(root.items.length, "result", "results") : ""
          }
          if (root.downloading) return root.syncLine()
          if (root.queued) return "Queued for download"
          if (root.syncFailed) return "The last update failed; the list on disk is unchanged"
          if (!root.mirrored) return "The " + (root.source === "tv" ? "channel" : "station")
                                     + " list has not been downloaded yet"
          if (root.filtered) return root.filterLine()
          return root.stockLine()
        }
        color: root.downloading || root.queued || root.scanning ? root.accent
               : (root.syncFailed ? "#ff9b9b" : root.faint)
        font.pixelSize: Style.font.caption
        elide: Text.ElideRight
      }

      // A refresh running under rows already on screen says so, quietly.
      Text {
        visible: root.loading && root.items.length > 0
        text: "updating…"
        color: root.accent
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
      }

      Text {
        visible: root.mirroredSource && root.total >= 0 && root.mirrored && !root.downloading
        text: Model.count(root.total, "match", "matches")
        color: root.faint
        font.pixelSize: Style.font.caption
      }

      Text {
        visible: root.mirroredSource && !root.downloading && !root.queued
        text: !root.mirrored ? "download" : (root.syncFailed ? "retry" : "update")
        color: updateArea.containsMouse || !root.mirrored || root.syncFailed
               ? root.accent : root.dim
        font.pixelSize: Style.font.caption
        font.underline: updateArea.containsMouse
        MouseArea {
          id: updateArea
          anchors.fill: parent
          anchors.margins: -Style.space(4)
          hoverEnabled: true
          cursorShape: Qt.PointingHandCursor
          onClicked: root.updateCatalogue(root.source)
        }
      }

      Text {
        visible: root.items.length > 1
        text: "play all"
        color: playAllArea.containsMouse ? root.accent : root.dim
        font.pixelSize: Style.font.caption
        font.underline: playAllArea.containsMouse
        MouseArea {
          id: playAllArea
          anchors.fill: parent
          anchors.margins: -Style.space(4)
          hoverEnabled: true
          cursorShape: Qt.PointingHandCursor
          onClicked: root.playAll(root.items)
        }
      }
    }
  }

  // ------------------------------------------------------------- helpers

  function emptyText() {
    if (root.loading) return root.source === "youtube" || root.source === "music"
                             ? "Searching…" : "Loading…"
    if (root.errorText) return root.errorText
    if (!root.connected) return "Starting…"
    if (root.source === "local") {
      return root.scanning ? "Looking for music in your folders…"
                           : "No music found. Add your music folders in Settings → Media & library."
    }
    if (root.source === "youtube" || root.source === "music") {
      return root.searchText === "" ? "Search for anything and press Enter."
                                    : "Press Enter to search."
    }
    if (root.searchText) return "Nothing matches “" + root.searchText + "”"
                                + (root.countryText ? " in " + root.countryText : "")
    if (root.filtered) return "Nothing matches these filters."
    return "Nothing here yet."
  }

  function focusSearch() {
    if (!root.browsing) root.selectView("browse")
    searchField.forceActiveFocus()
    searchField.selectAll()
  }

  function copyText(value) {
    copyArea.text = value
    copyArea.selectAll()
    copyArea.copy()
  }

  // "downloading 18,231 of 59,828 stations, 24.1 MB, 31%"
  function syncLine() {
    var info = root.myProgress
    if (!info) return ""
    var noun = root.source === "tv" ? "channel" : "station"
    var nounPlural = noun + "s"
    var rows = info.rows || 0
    var total = info.total
    // Radio is counted in stations and television in bytes of one playlist;
    // the numerator has to match the denominator or the percentage lies.
    var byBytes = info.unit === "bytes"
    var done = byBytes ? (info.bytes || 0) : rows
    var head = info.stage === "parse" ? "parsing" : "downloading"
    var parts = []
    if (info.stage === "parse" && !byBytes) {
      parts.push(Model.count(rows, noun, nounPlural))
    } else if (total && !byBytes) {
      parts.push(Model.count(rows, noun, nounPlural) + " of "
                 + Model.count(total, noun, nounPlural))
    } else {
      parts.push(Model.count(rows, noun, nounPlural))
    }
    if (info.bytes) parts.push(Model.bytes(info.bytes))
    if (info.stage !== "parse" && info.page) {
      parts.push("page " + info.page + (info.pages ? "/" + info.pages : ""))
    }
    var line = head + " " + parts.join(", ")
    if (info.stage === "download" && total) {
      line += ", " + Math.min(100, Math.floor((done / total) * 100)) + "%"
    }
    return line
  }

  // "59,826 stations, updated 23 h ago"
  function stockLine() {
    if (!root.mirrored) return "not downloaded yet"
    var noun = root.source === "tv" ? "channel" : "station"
    return Model.count(root.mirroredCount, noun, noun + "s")
           + ", updated " + root.relativeAge(root.updatedAgo)
  }

  // "matching Jazz, United Kingdom"
  function filterLine() {
    return "matching " + root.activeFilters.join(", ")
  }

  property int tick: 0
  Timer {
    interval: 30000
    running: root.mirrored
    repeat: true
    onTriggered: root.tick++
  }

  // Seconds -> the shortest phrase that is still true. Missing is
  // "unknown", not "just now", and every unit floors rather than rounds.
  function relativeAge(seconds) {
    if (seconds === null || seconds === undefined) return "unknown"
    var s = Number(seconds)
    if (!(s >= 0)) return "unknown"
    if (s < 60) return "just now"
    if (s < 3600) return Math.floor(s / 60) + " min ago"
    if (s < 86400) return Math.floor(s / 3600) + " h ago"
    var days = Math.floor(s / 86400)
    if (days < 30) return days + (days === 1 ? " day ago" : " days ago")
    return Math.floor(days / 30) + " mo ago"
  }

  function displayTitle(entry) {
    if (!entry) return ""
    if (entry.kind === "genre" || entry.kind === "group" || entry.kind === "country"
        || entry.kind === "folder" || entry.kind === "album" || entry.kind === "artist") {
      return entry.title
    }
    if (entry.source === "radio" || entry.source === "tv") {
      return Model.cleanName(entry.title)
    }
    // YouTube titles are the whole thing; only songs split "Artist - Song".
    if (entry.source === "youtube") return entry.title
    var split = Model.splitTitle(entry.title)
    return split.song || entry.title
  }

  function displaySub(entry) {
    if (!entry) return ""
    var extra = entry.extra || {}
    var parts = []
    if (entry.source === "local" && ["album", "artist", "genre", "folder"].indexOf(entry.kind) >= 0) {
      if (entry.kind === "album" && entry.artist) parts.push(entry.artist)
      parts.push(Model.count(extra.count || 0, "track", "tracks"))
      return parts.join("  ·  ")
    }
    if (entry.source === "radio") {
      if (entry.artist) parts.push(entry.artist)
      if (root.showCountryMeta) {
        if (extra.country && !root.country) parts.push(extra.country)
        else if (extra.state) parts.push(extra.state)
      }
    } else if (entry.source === "tv") {
      if (extra.group || entry.artist) parts.push(extra.group || entry.artist)
      if (root.showCountryMeta && extra.country && !root.country) parts.push(extra.country)
    } else if (entry.source === "youtube") {
      if (entry.artist) parts.push(entry.artist + (extra.verified ? " ✓" : ""))
      if (extra.views) parts.push(Model.compact(extra.views) + " views")
      if (entry.is_live) parts.push("live now")
    } else if (entry.source === "podcast") {
      if (entry.kind === "podcast") {
        if (entry.artist) parts.push(entry.artist)
        if (extra.genre) parts.push(extra.genre)
      } else {
        if (extra.published) parts.push(Model.shortDate(extra.published))
        if (extra.description) parts.push(extra.description)
      }
    } else if (entry.source === "music") {
      if (entry.artist) parts.push(entry.artist)
      if (entry.album && entry.album !== entry.title) parts.push(entry.album)
      if (extra.plays) parts.push(extra.plays)
    } else {
      if (entry.artist) parts.push(entry.artist)
      if (entry.album) parts.push(entry.album)
    }
    return parts.join("  ·  ")
  }

  function extra_episodes(entry) {
    var n = (entry.extra || {}).episodes || 0
    return n ? Model.count(n, "episode", "episodes") : ""
  }

  function displayMeta(entry) {
    if (!entry) return ""
    if (["folder", "album", "artist", "genre"].indexOf(entry.kind) >= 0) return ""
    // Every radio stream is live, so "live" on every radio row would say
    // nothing and hide the bitrate; on television it does mean something.
    if (entry.is_live && entry.source !== "radio") return "live"
    // YouTube and local videos show their length on the thumbnail.
    if (entry.source === "youtube") return ""
    if ((entry.extra || {}).media === "video") return Model.quality(entry)
    if (entry.kind === "podcast") return extra_episodes(entry)
    var d = Model.duration(entry.duration)
    var q = root.showBitrateMeta ? Model.quality(entry) : ""
    if (d && q) return d + "  " + q
    return d || q
  }

  // Asks for the next page once the end of the list is near. The daemon
  // says whether there is one, so a short filtered page no longer reads as
  // the end of the list.
  function maybeLoadMore(view) {
    if (root.loading || !root.more || root.errorText !== "") return
    if (!view || view.contentHeight <= 0) return
    if (view.contentY + view.height >= view.contentHeight - Style.space(140)) root.wantMore()
  }
}
