import QtQuick
import QtQuick.Layouts
import "Model.js" as Model

// Synced lyrics.
//
// Three sources are tried in order by the daemon (YouTube's own, LRCLib, a
// sidecar file) and the view does not care which one won. What it does care
// about is that unsynced lyrics are still readable: a plain-text page has no
// timestamps, so it is shown in full rather than pretending to be timed.
Item {
  id: root

  property var lyrics: null
  property int activeIndex: -1
  property color fg: "#dddddd"
  property color dim: Qt.rgba(1, 1, 1, 0.55)
  property color faint: Qt.rgba(1, 1, 1, 0.3)
  property color accent: "#5b8cff"
  // s | m | l | xl, from Settings; the A- and A+ buttons change it.
  property string size: "m"

  signal seek(double seconds)
  signal sizePicked(string size)
  signal save()

  readonly property var sizes: ["s", "m", "l", "xl"]
  readonly property int basePx: ({ s: 11, m: 13, l: 16, xl: 20 })[size] || 13
  function step(by) {
    var i = Math.max(0, Math.min(sizes.length - 1, sizes.indexOf(size) + by))
    if (sizes[i] !== size) sizePicked(sizes[i])
  }

  readonly property var lines: (lyrics && lyrics.lines) ? lyrics.lines : []
  readonly property bool synced: !!(lyrics && lyrics.synced)

  Text {
    anchors.centerIn: parent
    visible: root.lines.length === 0
    text: root.lyrics === null ? "Looking for lyrics…" : "No lyrics found for this track"
    color: root.faint
    font.pixelSize: 12
  }

  ColumnLayout {
    anchors.fill: parent
    spacing: 0
    visible: root.lines.length > 0

    // Provenance, small, once. When the words are only a best guess the user
    // should be able to see that before they trust them. Beside it, the text
    // size and a way to keep the lyrics as an .lrc file.
    RowLayout {
      Layout.fillWidth: true
      Layout.bottomMargin: 6
      spacing: 2
      Text {
        Layout.fillWidth: true
        text: ((root.lyrics && root.lyrics.source) || "unknown")
              + (root.synced ? "  •  synced" : "  •  unsynced")
        color: root.faint
        font.pixelSize: 10
      }
      IconButton {
        icon: "A−"
        size: 22
        colorFg: root.dim
        enabled: root.size !== "s"
        tip: "Smaller text"
        onClicked: root.step(-1)
      }
      IconButton {
        icon: "A+"
        size: 22
        colorFg: root.dim
        enabled: root.size !== "xl"
        tip: "Larger text"
        onClicked: root.step(1)
      }
      IconButton {
        icon: Model.ICON.save
        size: 22
        colorFg: root.dim
        tip: "Save as an .lrc file"
        onClicked: root.save()
      }
    }

    ListView {
      id: list
      Layout.fillWidth: true
      Layout.fillHeight: true
      clip: true
      spacing: 6
      model: root.lines
      currentIndex: root.activeIndex
      // Keep the active line in the middle: the sung line is the one being
      // read, and it should not be at the edge of the window.
      preferredHighlightBegin: height * 0.36
      preferredHighlightEnd: height * 0.40
      highlightRangeMode: ListView.ApplyRange
      highlightMoveDuration: 220
      highlightFollowsCurrentItem: true
      boundsBehavior: Flickable.StopAtBounds
      // An invisible highlight is what the range mode scrolls to keep in
      // view; without one the active line walked off the bottom of the panel.
      highlight: Item {}

      Text {
        anchors.centerIn: parent
        visible: list.count === 0
        text: "…"
        color: Qt.rgba(1, 1, 1, 0.3)
      }

      delegate: Text {
        required property var modelData
        required property int index

        width: list.width - 24
        x: 12
        text: modelData.text || "♪"
        color: index === root.activeIndex ? root.accent
             : (Math.abs(index - root.activeIndex) <= 1 ? root.fg : root.faint)
        font.pixelSize: index === root.activeIndex ? root.basePx + 2 : root.basePx
        font.bold: index === root.activeIndex
        wrapMode: Text.WordWrap
        horizontalAlignment: Text.AlignHCenter

        // Tapping a timed line jumps the player there. That is the one
        // interaction that makes lyrics worth having on a desktop.
        MouseArea {
          anchors.fill: parent
          cursorShape: root.synced ? Qt.PointingHandCursor : Qt.ArrowCursor
          enabled: root.synced && modelData.t >= 0
          onClicked: root.seek(modelData.t / 1000)
        }
      }
    }
  }
}
