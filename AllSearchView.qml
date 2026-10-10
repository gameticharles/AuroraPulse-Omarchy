import QtQuick
import QtQuick.Controls
import qs.Commons
import qs.Ui as Ui
import "Model.js" as Model

// One search across every source: radio, TV, your library, podcasts, YouTube
// and YouTube Music, grouped by source. Each source answers on its own, so
// the quick ones (the mirrored catalogues and the library) show at once while
// YouTube is still looking. "Show all" opens that source's tab on the same
// words.
Item {
  id: root

  property var results: ({})          // source -> { items, error }
  property string query: ""
  property bool searching: false
  property var artByKey: ({})
  property string currentUid: ""
  property color fg: Color.popups.text
  property color dim: Qt.rgba(fg.r, fg.g, fg.b, 0.55)
  property color faint: Qt.rgba(fg.r, fg.g, fg.b, 0.3)
  property color accent: Color.accent

  signal search(string text)
  signal activate(var entry, var list, int index)
  signal showAll(string source, string text)

  readonly property var order: ["local", "radio", "tv", "music", "youtube", "podcast"]
  readonly property int found: {
    var n = 0
    for (var i = 0; i < order.length; i++) {
      var r = results[order[i]]
      if (r && r.items) n += r.items.length
    }
    return n
  }

  function focusSearch() { field.forceActiveFocus(); field.selectAll() }

  function artItem(entry) {
    var key = entry && entry.art ? entry.art.key : ""
    var path = key ? root.artByKey[key] : ""
    if (!path) return entry
    var copy = Object.assign({}, entry)
    copy.art = { url: "", path: path, key: key }
    return copy
  }

  function subOf(entry) {
    var parts = []
    if (entry.artist) parts.push(entry.artist)
    if (entry.album && entry.album !== entry.title) parts.push(entry.album)
    var x = entry.extra || {}
    if (!parts.length && (x.group || x.country)) parts.push(x.group || x.country)
    var d = Model.duration(entry.duration)
    if (d && !entry.is_live) parts.push(d)
    return parts.join("  ·  ")
  }

  Ui.TextField {
    id: field
    anchors.left: parent.left
    anchors.right: parent.right
    anchors.top: parent.top
    foreground: root.fg
    accent: root.accent
    font.pixelSize: Style.font.bodySmall
    placeholderText: Model.ICON.search + "  Search everything, then Enter"
    text: root.query
    Keys.onReturnPressed: root.search(text)
    Keys.onEnterPressed: root.search(text)
    Keys.onEscapePressed: function (event) { text = ""; event.accepted = true }
  }

  Text {
    anchors.centerIn: parent
    visible: !root.query
    width: parent.width - Style.space(40)
    horizontalAlignment: Text.AlignHCenter
    wrapMode: Text.WordWrap
    text: "Search stations, channels, your library, podcasts, YouTube and YouTube Music at once."
    color: root.dim
    font.pixelSize: Style.font.bodySmall
  }

  Flickable {
    id: flick
    anchors.left: parent.left
    anchors.right: parent.right
    anchors.top: field.bottom
    anchors.topMargin: Style.space(8)
    anchors.bottom: parent.bottom
    visible: !!root.query
    clip: true
    contentHeight: sections.implicitHeight
    boundsBehavior: Flickable.StopAtBounds
    ScrollBar.vertical: ScrollBar {
      policy: flick.contentHeight > flick.height ? ScrollBar.AsNeeded : ScrollBar.AlwaysOff
    }

    Column {
      id: sections
      width: flick.width - Style.space(8)
      spacing: Style.space(10)

      Repeater {
        model: root.order
        delegate: Column {
          id: section
          required property string modelData
          readonly property var meta: Model.sourceOf(modelData)
          readonly property var result: root.results[modelData] || null
          readonly property var rows: result && result.items ? result.items : []
          width: sections.width
          spacing: Style.space(2)
          // A source with nothing for these words keeps out of the way.
          visible: !result || rows.length > 0 || !!result.error

          Item {
            width: parent.width
            height: Style.space(26)
            Text {
              id: sectionIcon
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              text: section.meta.icon
              color: section.meta.color
              font.family: Style.font.family
              font.pixelSize: Style.font.body
            }
            Text {
              anchors.left: sectionIcon.right
              anchors.leftMargin: Style.space(6)
              anchors.verticalCenter: parent.verticalCenter
              text: section.meta.label
                    + (section.rows.length ? "  ·  " + section.rows.length : "")
              color: root.fg
              font.bold: true
              font.pixelSize: Style.font.bodySmall
            }
            Spinner {
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              visible: !section.result && root.searching
              running: visible
              color: section.meta.color
              size: Style.space(14)
            }
            Ui.Button {
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              visible: section.rows.length > 0
              text: "Show all"
              iconText: Model.ICON.chevronRight
              foreground: root.dim
              accent: root.accent
              fontSize: Style.font.caption
              iconSize: Style.font.caption
              verticalPadding: Style.space(1)
              onClicked: root.showAll(section.modelData, root.query)
            }
          }

          Text {
            visible: !!(section.result && section.result.error)
            width: parent.width
            text: section.result ? String(section.result.error || "") : ""
            color: root.faint
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          Repeater {
            model: section.rows
            delegate: Item {
              id: hit
              required property var modelData
              required property int index
              width: section.width
              height: Style.space(44)
              readonly property bool current: root.currentUid !== ""
                                              && modelData.uid === root.currentUid
              Ui.BorderSurface {
                anchors.fill: parent
                radius: Style.cornerRadius
                color: hitArea.containsMouse ? Style.hoverFillFor(root.fg, root.accent)
                     : hit.current ? Style.normalFillFor(root.fg, root.accent) : "transparent"
                borderSpec: Border.none()
              }
              MouseArea {
                id: hitArea
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: root.activate(hit.modelData, section.rows, hit.index)
              }
              ArtTile {
                id: hitArt
                anchors.left: parent.left
                anchors.leftMargin: Style.space(6)
                anchors.verticalCenter: parent.verticalCenter
                width: Style.space(34)
                height: Style.space(34)
                radius: Style.space(4)
                item: root.artItem(hit.modelData)
              }
              Column {
                anchors.left: hitArt.right
                anchors.leftMargin: Style.space(10)
                anchors.right: parent.right
                anchors.rightMargin: Style.space(6)
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.space(1)
                Text {
                  width: parent.width
                  text: hit.modelData.title || ""
                  color: hit.current ? root.accent : root.fg
                  font.pixelSize: Style.font.bodySmall
                  elide: Text.ElideRight
                }
                Text {
                  width: parent.width
                  visible: text !== ""
                  text: root.subOf(hit.modelData)
                  color: root.dim
                  font.pixelSize: Style.font.caption
                  elide: Text.ElideRight
                }
              }
            }
          }
        }
      }

      Text {
        visible: !root.searching && root.found === 0 && !!root.query
        width: parent.width
        horizontalAlignment: Text.AlignHCenter
        text: "Nothing matches “" + root.query + "” anywhere."
        color: root.dim
        font.pixelSize: Style.font.bodySmall
      }
    }
  }
}
