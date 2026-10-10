import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Ui as Ui
import "Model.js" as Model

// Edit a local file's tags: title, artist, album, album artist, genre, year,
// track number, and optionally a new cover. The daemon rewrites the file
// without re-encoding it (ffmpeg, sandboxed) and reads it back into the
// library. Drawn over the list it was opened from.
Rectangle {
  id: root

  property var entry: null
  // A cover picture chosen with the file chooser, or "".
  property string art: ""
  property color fg: Color.popups.text
  property color dim: Qt.rgba(fg.r, fg.g, fg.b, 0.55)
  property color accent: Color.accent

  signal save(string path, var fields, string art)
  signal pickArt()

  visible: !!entry
  color: Color.popups.background
  radius: Style.cornerRadius

  readonly property var fieldNames: [
    { key: "title", label: "Title" }, { key: "artist", label: "Artist" },
    { key: "album", label: "Album" }, { key: "album_artist", label: "Album artist" },
    { key: "genre", label: "Genre" }, { key: "date", label: "Year" },
    { key: "track", label: "Track number" }]

  function open(e) {
    var x = (e && e.extra) || {}
    art = ""
    var values = { title: e.title || "", artist: e.artist || "", album: e.album || "",
                   album_artist: "", genre: x.genre || "", date: x.year || "", track: "" }
    for (var i = 0; i < inputs.count; i++) {
      var input = inputs.itemAt(i)
      input.text = values[input.key] || ""
      input.original = input.text
    }
    entry = e
    if (inputs.count) inputs.itemAt(0).focusInput()
  }

  function close() { entry = null; art = "" }

  function submit() {
    var fields = {}
    for (var i = 0; i < inputs.count; i++) {
      var input = inputs.itemAt(i)
      // Only what changed, so a field you left alone keeps the file's own
      // value (album artist and track number are not read into the library).
      if (input.text !== input.original) fields[input.key] = input.text
    }
    var path = String(((entry || {}).extra || {}).path || "")
    if (path && (Object.keys(fields).length || art)) root.save(path, fields, art)
    close()
  }

  // Clicks stay in the editor.
  MouseArea { anchors.fill: parent }

  ColumnLayout {
    anchors.fill: parent
    anchors.margins: Style.space(12)
    spacing: Style.space(6)

    RowLayout {
      Layout.fillWidth: true
      spacing: Style.space(8)
      ArtTile {
        Layout.preferredWidth: Style.space(46)
        Layout.preferredHeight: Style.space(46)
        radius: Style.space(6)
        item: root.art ? { uid: "tag-art", source: "local", title: "",
                           art: { url: "", path: root.art } } : root.entry
      }
      Column {
        Layout.fillWidth: true
        spacing: Style.space(2)
        Text {
          width: parent.width
          text: "Edit tags"
          color: root.fg
          font.bold: true
          font.pixelSize: Style.font.body
        }
        Text {
          width: parent.width
          text: String(((root.entry || {}).extra || {}).path || "")
          color: root.dim
          font.pixelSize: Style.font.caption
          elide: Text.ElideMiddle
        }
      }
    }

    Flickable {
      Layout.fillWidth: true
      Layout.fillHeight: true
      clip: true
      contentHeight: form.implicitHeight
      boundsBehavior: Flickable.StopAtBounds

      Column {
        id: form
        width: parent.width
        spacing: Style.space(6)
        Repeater {
          id: inputs
          model: root.fieldNames
          delegate: Column {
            required property var modelData
            readonly property string key: modelData.key
            property alias text: input.text
            property string original: ""
            function focusInput() { input.forceActiveFocus() }
            width: form.width
            spacing: Style.space(2)
            Text {
              text: modelData.label
              color: root.dim
              font.pixelSize: Style.font.caption
            }
            Ui.TextField {
              id: input
              width: parent.width
              foreground: root.fg
              accent: root.accent
              font.pixelSize: Style.font.bodySmall
              Keys.onReturnPressed: root.submit()
              Keys.onEnterPressed: root.submit()
              Keys.onEscapePressed: root.close()
            }
          }
        }
      }
    }

    RowLayout {
      Layout.fillWidth: true
      spacing: Style.space(6)
      Ui.Button {
        text: root.art ? "Cover chosen" : "Cover…"
        iconText: Model.ICON.album
        bordered: true
        foreground: root.art ? root.accent : root.fg
        accent: root.accent
        fontSize: Style.font.caption
        iconSize: Style.font.caption
        tooltipText: root.art ? root.art : "Choose a JPEG or PNG for the cover"
        onClicked: root.pickArt()
      }
      Item { Layout.fillWidth: true }
      Ui.Button {
        text: "Cancel"
        bordered: true
        foreground: root.fg
        accent: root.accent
        fontSize: Style.font.caption
        onClicked: root.close()
      }
      Ui.Button {
        text: "Save"
        iconText: Model.ICON.save
        bordered: true
        foreground: root.accent
        accent: root.accent
        fontSize: Style.font.caption
        iconSize: Style.font.caption
        onClicked: root.submit()
      }
    }
  }
}
