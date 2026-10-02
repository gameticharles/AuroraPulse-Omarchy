import QtQuick
import QtQuick.Controls
import qs.Commons
import qs.Ui as Ui
import "Model.js" as Model
import "components" as C

// Favourites, playlists, recently played and downloads, across every source.
//
// A favourite is kept with ♥ beside the title of whatever is playing (or from
// a row's right-click menu); the history fills itself as things play.
Item {
  id: root

  property var favorites: []
  property var history: []
  property var playlists: []
  property var playlistView: null      // the open playlist: {id, name, smart, items}
  property var downloads: []
  property var artByKey: ({})
  property string currentUid: ""
  property bool currentPlaying: false
  property color fg: Color.popups.text
  property color dim: Qt.rgba(fg.r, fg.g, fg.b, 0.55)
  property color faint: Qt.rgba(fg.r, fg.g, fg.b, 0.3)
  property color accent: Color.accent

  // favorites | playlists | history | downloads
  property string section: "favorites"
  readonly property bool inPlaylist: section === "playlists" && !!playlistView
  readonly property bool playlistIndex: section === "playlists" && !playlistView
  readonly property bool editable: inPlaylist && !playlistView.smart
  readonly property var rows: section === "favorites" ? favorites
                              : section === "history" ? history
                              : inPlaylist ? (playlistView.items || []) : []
  readonly property int activeDownloads: {
    var n = 0
    for (var i = 0; i < downloads.length; i++) {
      if (downloads[i].state === "queued" || downloads[i].state === "running") n++
    }
    return n
  }

  signal play(var entry, var list, int index)
  signal unfavorite(var entry)
  signal forget(string uid)
  signal clearHistory()
  signal cancelDownload(string id)
  signal playlistCommand(var args)

  function playlistIcon(p) {
    return ({ "smart:most": Model.ICON.star, "smart:recent": Model.ICON.history,
              "smart:added": Model.ICON.plus, "smart:favorites": Model.ICON.heart })[p.id]
           || Model.ICON.playlistTv
  }

  function artItem(entry) {
    var key = entry && entry.art ? entry.art.key : ""
    var path = key ? root.artByKey[key] : ""
    if (!path) return entry
    var copy = Object.assign({}, entry)
    copy.art = { url: "", path: path, key: key }
    return copy
  }

  function ago(stamp) {
    if (!stamp) return ""
    var s = Math.max(0, Date.now() / 1000 - stamp)
    if (s < 60) return "just now"
    if (s < 3600) return Math.floor(s / 60) + " min ago"
    if (s < 86400) return Math.floor(s / 3600) + " h ago"
    return Math.floor(s / 86400) + " d ago"
  }

  Row {
    id: switcher
    anchors.left: parent.left
    anchors.top: parent.top
    spacing: Style.space(5)

    Ui.Button {
      text: "Favorites  " + root.favorites.length
      iconText: Model.ICON.heart
      bordered: true
      selected: root.section === "favorites"
      foreground: root.fg
      accent: "#ff6b8a"
      fontSize: Style.font.caption
      iconSize: Style.font.caption
      verticalPadding: Style.space(2)
      onClicked: root.section = "favorites"
    }
    Ui.Button {
      text: "Playlists"
      iconText: Model.ICON.playlistTv
      bordered: true
      selected: root.section === "playlists"
      foreground: root.fg
      accent: root.accent
      fontSize: Style.font.caption
      iconSize: Style.font.caption
      verticalPadding: Style.space(2)
      onClicked: root.section = "playlists"
    }
    Ui.Button {
      text: "Recently played  " + root.history.length
      iconText: Model.ICON.history
      bordered: true
      selected: root.section === "history"
      foreground: root.fg
      accent: root.accent
      fontSize: Style.font.caption
      iconSize: Style.font.caption
      verticalPadding: Style.space(2)
      onClicked: root.section = "history"
    }
    Ui.Button {
      text: "Downloads" + (root.activeDownloads ? "  " + root.activeDownloads : "")
      iconText: Model.ICON.downloadOutline
      bordered: true
      selected: root.section === "downloads"
      foreground: root.fg
      accent: root.accent
      fontSize: Style.font.caption
      iconSize: Style.font.caption
      verticalPadding: Style.space(2)
      onClicked: root.section = "downloads"
    }
  }

  Ui.Button {
    anchors.right: parent.right
    anchors.verticalCenter: switcher.verticalCenter
    visible: root.section === "history" && root.history.length > 0
    text: "Clear"
    iconText: Model.ICON.clearAll
    bordered: true
    foreground: root.fg
    accent: root.accent
    fontSize: Style.font.caption
    iconSize: Style.font.caption
    verticalPadding: Style.space(2)
    onClicked: root.clearHistory()
  }

  Column {
    anchors.centerIn: parent
    spacing: Style.space(8)
    visible: root.playlistIndex ? false
             : root.section === "downloads" ? root.downloads.length === 0 : root.rows.length === 0
    width: parent.width - Style.space(40)
    Text {
      anchors.horizontalCenter: parent.horizontalCenter
      text: root.section === "favorites" ? Model.ICON.heartOutline
            : root.section === "playlists" ? Model.ICON.playlistTv
            : (root.section === "history" ? Model.ICON.history : Model.ICON.downloadOutline)
      color: root.faint
      font.family: Style.font.family
      font.pixelSize: Style.space(28)
    }
    Text {
      width: parent.width
      horizontalAlignment: Text.AlignHCenter
      wrapMode: Text.WordWrap
      text: root.section === "favorites"
            ? "No favourites yet. Press ♥ beside what is playing, or right-click any row."
            : root.section === "playlists"
            ? (root.editable ? "This playlist is empty. Right-click any row and choose Add to playlist."
                             : "Nothing here yet: this list fills itself as you listen.")
            : (root.section === "history" ? "Nothing played yet."
               : "Nothing downloaded yet. Use the download button beside a YouTube video, "
                 + "a song or a podcast episode, or right-click its row. Files go to "
                 + "Music › AuroraPulse and Videos › AuroraPulse, and appear in Local.")
      color: root.dim
      font.pixelSize: Style.font.bodySmall
    }
  }

  // ---- playlists ---------------------------------------------------------

  // The open playlist's header: back, its name (renamed in place), play all,
  // delete.
  Item {
    id: playlistHeader
    visible: root.inPlaylist
    anchors.left: parent.left
    anchors.right: parent.right
    anchors.top: switcher.bottom
    anchors.topMargin: Style.space(8)
    height: visible ? Style.space(30) : 0
    property bool renaming: false
    property bool armed: false
    onVisibleChanged: { renaming = false; armed = false }

    Row {
      anchors.left: parent.left
      anchors.verticalCenter: parent.verticalCenter
      spacing: Style.space(4)
      IconButton {
        icon: Model.ICON.back
        size: 26
        colorFg: root.dim
        tip: "All playlists"
        onClicked: root.playlistCommand({ action: "close" })
      }
      Text {
        visible: !playlistHeader.renaming
        anchors.verticalCenter: parent.verticalCenter
        text: root.inPlaylist ? root.playlistView.name + "   "
                                + Model.count(root.rows.length, "item", "items") : ""
        color: root.fg
        font.pixelSize: Style.font.bodySmall + 1
        font.bold: true
        elide: Text.ElideRight
        width: Math.min(implicitWidth, playlistHeader.width - Style.space(150))
        MouseArea {
          anchors.fill: parent
          enabled: root.editable
          cursorShape: root.editable ? Qt.IBeamCursor : Qt.ArrowCursor
          onDoubleClicked: playlistHeader.renaming = true
        }
      }
      C.InputField {
        id: renameField
        visible: playlistHeader.renaming
        width: playlistHeader.width - Style.space(150)
        text: root.inPlaylist ? root.playlistView.name : ""
        onAccepted: {
          root.playlistCommand({ action: "rename", playlist: root.playlistView.id, name: text })
          playlistHeader.renaming = false
        }
      }
    }
    Row {
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      spacing: Style.space(2)
      IconButton {
        icon: Model.ICON.play
        size: 26
        colorFg: root.fg
        enabled: root.rows.length > 0
        tip: "Play all"
        onClicked: root.playlistCommand({ action: "play", playlist: root.playlistView.id })
      }
      IconButton {
        visible: root.editable
        icon: Model.ICON.tune
        size: 26
        colorFg: root.dim
        tip: "Rename"
        onClicked: playlistHeader.renaming = !playlistHeader.renaming
      }
      IconButton {
        visible: root.editable
        icon: Model.ICON.remove
        size: 26
        colorFg: playlistHeader.armed ? "#ff9b9b" : root.dim
        tip: playlistHeader.armed ? "Press again to delete this playlist" : "Delete this playlist"
        onClicked: {
          if (!playlistHeader.armed) { playlistHeader.armed = true; return }
          root.playlistCommand({ action: "delete", playlist: root.playlistView.id })
        }
      }
    }
  }

  // All playlists: a new one, yours, then the ones that fill themselves.
  Column {
    id: playlistIndexView
    visible: root.playlistIndex
    anchors.left: parent.left
    anchors.right: parent.right
    anchors.top: switcher.bottom
    anchors.topMargin: Style.space(8)
    spacing: Style.space(2)

    Row {
      width: parent.width
      spacing: Style.space(4)
      C.InputField {
        id: newName
        width: parent.width - createButton.width - Style.space(4)
        placeholder: "New playlist name, then Enter"
        onAccepted: createButton.clicked()
      }
      Ui.Button {
        id: createButton
        text: "Create"
        iconText: Model.ICON.plus
        bordered: true
        foreground: root.fg
        accent: root.accent
        fontSize: Style.font.caption
        iconSize: Style.font.caption
        verticalPadding: Style.space(3)
        enabled: newName.text.trim() !== ""
        onClicked: {
          if (!newName.text.trim()) return
          root.playlistCommand({ action: "create", name: newName.text.trim() })
          newName.text = ""
        }
      }
    }

    Repeater {
      model: root.playlists
      delegate: Item {
        id: pl
        required property var modelData
        width: playlistIndexView.width - Style.space(8)
        height: Style.space(40)
        Ui.BorderSurface {
          anchors.fill: parent
          radius: Style.cornerRadius
          color: plArea.containsMouse ? Style.hoverFillFor(root.fg, root.accent) : "transparent"
          borderSpec: Border.none()
        }
        MouseArea {
          id: plArea
          anchors.fill: parent
          hoverEnabled: true
          cursorShape: Qt.PointingHandCursor
          onClicked: root.playlistCommand({ action: "items", playlist: pl.modelData.id })
        }
        Text {
          id: plIcon
          anchors.left: parent.left
          anchors.leftMargin: Style.space(10)
          anchors.verticalCenter: parent.verticalCenter
          text: root.playlistIcon(pl.modelData)
          color: pl.modelData.smart ? root.dim : root.accent
          font.family: Style.font.family
          font.pixelSize: Style.font.body
        }
        Text {
          anchors.left: plIcon.right
          anchors.leftMargin: Style.space(10)
          anchors.right: plPlay.left
          anchors.verticalCenter: parent.verticalCenter
          text: pl.modelData.name + (pl.modelData.smart ? "   · automatic" : "")
          color: root.fg
          font.pixelSize: Style.font.bodySmall + 1
          elide: Text.ElideRight
        }
        Text {
          anchors.right: plPlay.left
          anchors.rightMargin: Style.space(6)
          anchors.verticalCenter: parent.verticalCenter
          text: String(pl.modelData.count || 0)
          color: root.faint
          font.pixelSize: Style.font.caption
        }
        IconButton {
          id: plPlay
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          icon: Model.ICON.play
          size: 24
          colorFg: root.fg
          enabled: (pl.modelData.count || 0) > 0
          tip: "Play"
          onClicked: root.playlistCommand({ action: "play", playlist: pl.modelData.id })
        }
      }
    }
  }

  // ---- downloads ---------------------------------------------------------

  ListView {
    id: downloadList
    visible: root.section === "downloads"
    anchors.left: parent.left
    anchors.right: parent.right
    anchors.top: switcher.bottom
    anchors.topMargin: Style.space(8)
    anchors.bottom: parent.bottom
    clip: true
    spacing: Style.space(2)
    model: root.downloads
    boundsBehavior: Flickable.StopAtBounds
    ScrollBar.vertical: ScrollBar {}

    delegate: Item {
      id: job
      required property var modelData
      width: downloadList.width - Style.space(8)
      height: Style.space(52)
      readonly property bool busy: modelData.state === "running" || modelData.state === "queued"

      Ui.BorderSurface {
        anchors.fill: parent
        radius: Style.cornerRadius
        color: jobHover.hovered ? Style.hoverFillFor(root.fg, root.accent) : "transparent"
        borderSpec: Border.none()
      }
      HoverHandler { id: jobHover }

      ArtTile {
        id: jobArt
        anchors.left: parent.left
        anchors.leftMargin: Style.space(6)
        anchors.verticalCenter: parent.verticalCenter
        width: Style.space(40)
        height: Style.space(40)
        radius: Style.space(5)
        item: root.artItem({ uid: job.modelData.uid, source: job.modelData.source,
                             title: job.modelData.title, art: job.modelData.art || {} })
      }

      Column {
        anchors.left: jobArt.right
        anchors.leftMargin: Style.space(10)
        anchors.right: jobTools.left
        anchors.rightMargin: Style.space(8)
        anchors.verticalCenter: parent.verticalCenter
        spacing: Style.space(3)
        Text {
          width: parent.width
          text: job.modelData.title
          color: root.fg
          font.pixelSize: Style.font.bodySmall + 1
          elide: Text.ElideRight
        }
        Rectangle {
          visible: job.modelData.state === "running"
          width: parent.width
          height: 3
          radius: 1.5
          color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.12)
          Rectangle {
            width: parent.width * Math.max(0, Math.min(1, (job.modelData.progress || 0) / 100))
            height: parent.height
            radius: parent.radius
            color: root.accent
          }
        }
        Text {
          width: parent.width
          text: {
            var d = job.modelData
            var kind = d.kind === "video" ? "video" : "audio"
            if (d.state === "running") return kind + "  ·  " + Math.round(d.progress || 0) + "%"
                                              + (d.speed ? "  ·  " + d.speed : "")
                                              + (d.eta ? "  ·  " + d.eta + " left" : "")
            if (d.state === "queued") return kind + "  ·  waiting"
            if (d.state === "done") return kind + "  ·  saved"
            if (d.state === "cancelled") return kind + "  ·  cancelled"
            return "failed: " + (d.error || "unknown error")
          }
          color: job.modelData.state === "failed" ? "#ff9b9b"
                 : (job.modelData.state === "done" ? root.dim : root.faint)
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }
      }

      Row {
        id: jobTools
        anchors.right: parent.right
        anchors.rightMargin: Style.space(4)
        anchors.verticalCenter: parent.verticalCenter
        IconButton {
          visible: job.modelData.state === "done" && !!job.modelData.path
          icon: Model.ICON.folderDownload
          size: 24
          colorFg: root.dim
          tip: "Open the file"
          onClicked: Qt.openUrlExternally("file://" + job.modelData.path)
        }
        IconButton {
          icon: Model.ICON.close
          size: 24
          colorFg: root.dim
          tip: job.busy ? "Cancel" : "Remove from this list (the file stays)"
          onClicked: root.cancelDownload(job.modelData.id)
        }
      }
    }
  }

  ListView {
    id: list
    visible: root.section !== "downloads" && !root.playlistIndex
    anchors.left: parent.left
    anchors.right: parent.right
    anchors.top: root.inPlaylist ? playlistHeader.bottom : switcher.bottom
    anchors.topMargin: Style.space(8)
    anchors.bottom: parent.bottom
    clip: true
    spacing: Style.space(2)
    model: root.rows
    boundsBehavior: Flickable.StopAtBounds
    ScrollBar.vertical: ScrollBar {}

    delegate: Item {
      id: row
      required property var modelData
      required property int index
      width: list.width - Style.space(8)
      height: Style.space(52)
      readonly property bool isCurrent: root.currentUid !== "" && modelData.uid === root.currentUid
      readonly property bool hot: area.containsMouse || toolsHover.hovered

      Ui.BorderSurface {
        anchors.fill: parent
        radius: Style.cornerRadius
        color: row.hot ? Style.hoverFillFor(root.fg, root.accent)
             : row.isCurrent ? Style.normalFillFor(root.fg, root.accent) : "transparent"
        borderSpec: Border.none()
      }

      MouseArea {
        id: area
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: root.play(row.modelData, root.rows, row.index)
      }

      ArtTile {
        id: art
        anchors.left: parent.left
        anchors.leftMargin: Style.space(6)
        anchors.verticalCenter: parent.verticalCenter
        width: Style.space(40)
        height: Style.space(40)
        radius: Style.space(5)
        item: root.artItem(row.modelData)
        Rectangle {
          anchors.fill: parent
          radius: Style.space(5)
          visible: row.isCurrent
          color: Qt.rgba(0, 0, 0, 0.5)
          EqBars {
            anchors.centerIn: parent
            width: Style.space(15)
            height: Style.space(13)
            color: root.accent
            playing: root.currentPlaying
          }
        }
      }

      Column {
        anchors.left: art.right
        anchors.leftMargin: Style.space(10)
        anchors.right: tools.left
        anchors.rightMargin: Style.space(6)
        anchors.verticalCenter: parent.verticalCenter
        spacing: Style.space(2)
        Text {
          width: parent.width
          text: Model.cleanName(row.modelData.title || "")
          color: row.isCurrent ? root.accent : root.fg
          font.pixelSize: Style.font.bodySmall + 1
          font.bold: row.isCurrent
          elide: Text.ElideRight
        }
        Text {
          width: parent.width
          text: [Model.sourceOf(row.modelData.source).label, row.modelData.artist,
                 (root.section === "history" || (root.inPlaylist && root.playlistView.id === "smart:recent"))
                 ? root.ago((row.modelData.extra || {}).playedAt) : ""]
                .filter(function (x) { return !!x }).join("  ·  ")
          color: root.dim
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }
      }

      Row {
        id: tools
        anchors.right: parent.right
        anchors.rightMargin: Style.space(4)
        anchors.verticalCenter: parent.verticalCenter
        HoverHandler { id: toolsHover }
        IconButton {
          visible: row.hot
          icon: Model.ICON.play
          size: 24
          colorFg: root.fg
          tip: "Play"
          onClicked: root.play(row.modelData, root.rows, row.index)
        }
        IconButton {
          visible: row.hot && root.editable && row.index > 0
          icon: Model.ICON.up
          size: 24
          colorFg: root.dim
          tip: "Move up"
          onClicked: root.playlistCommand({ action: "move", playlist: root.playlistView.id,
                                            from: row.index, to: row.index - 1 })
        }
        IconButton {
          visible: row.hot && root.editable && row.index < root.rows.length - 1
          icon: Model.ICON.down
          size: 24
          colorFg: root.dim
          tip: "Move down"
          onClicked: root.playlistCommand({ action: "move", playlist: root.playlistView.id,
                                            from: row.index, to: row.index + 1 })
        }
        IconButton {
          visible: root.section === "favorites" || (row.hot && (root.section === "history"
                                                                || root.editable))
          icon: root.section === "favorites" ? Model.ICON.heart : Model.ICON.close
          size: 24
          colorFg: root.section === "favorites" ? "#ff6b8a" : root.dim
          tip: root.section === "favorites" ? "Remove from favourites"
               : root.editable ? "Remove from this playlist" : "Remove from history"
          onClicked: {
            if (root.section === "favorites") root.unfavorite(row.modelData)
            else if (root.editable) root.playlistCommand({ action: "remove",
                                                           playlist: root.playlistView.id,
                                                           uid: row.modelData.uid })
            else root.forget(row.modelData.uid)
          }
        }
      }
    }
  }
}
