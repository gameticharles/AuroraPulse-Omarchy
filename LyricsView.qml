import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Ui as Ui
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
  // Shift synced lyrics: the new offset in ms (positive shows lines later).
  signal nudge(int offset)
  // Your own lyrics, pasted or typed ("" forgets them).
  signal setText(string text)

  readonly property int offset: (lyrics && lyrics.offset) ? lyrics.offset : 0
  property bool editing: false

  // The lyrics on screen as text to edit: an .lrc when they are timed.
  function asText() {
    var out = []
    for (var i = 0; i < lines.length; i++) {
      var l = lines[i]
      if (synced && l.t >= 0) {
        var m = Math.floor(l.t / 60000), sec = Math.floor(l.t / 1000) % 60,
            cs = Math.floor((l.t % 1000) / 10)
        out.push("[" + (m < 10 ? "0" : "") + m + ":" + (sec < 10 ? "0" : "") + sec + "."
                 + (cs < 10 ? "0" : "") + cs + "]" + (l.text || ""))
      } else {
        out.push(l.text || "")
      }
    }
    return out.join("\n")
  }
  function edit() {
    editor.text = asText()
    editing = true
    editor.forceActiveFocus()
  }

  readonly property var sizes: ["s", "m", "l", "xl"]
  readonly property int basePx: ({ s: 11, m: 13, l: 16, xl: 20 })[size] || 13
  function step(by) {
    var i = Math.max(0, Math.min(sizes.length - 1, sizes.indexOf(size) + by))
    if (sizes[i] !== size) sizePicked(sizes[i])
  }

  readonly property var lines: (lyrics && lyrics.lines) ? lyrics.lines : []
  readonly property bool synced: !!(lyrics && lyrics.synced)

  Column {
    anchors.centerIn: parent
    visible: root.lines.length === 0 && !root.editing
    spacing: 10
    Text {
      anchors.horizontalCenter: parent.horizontalCenter
      text: root.lyrics === null ? "Looking for lyrics…" : "No lyrics found for this track"
      color: root.faint
      font.pixelSize: 12
    }
    Ui.Button {
      anchors.horizontalCenter: parent.horizontalCenter
      visible: root.lyrics !== null
      text: "Add lyrics"
      iconText: Model.ICON.plus
      bordered: true
      foreground: root.fg
      accent: root.accent
      fontSize: Style.font.caption
      iconSize: Style.font.caption
      onClicked: root.edit()
    }
  }

  // Paste or type lyrics: an .lrc with [mm:ss.xx] stamps is timed, anything
  // else is shown as plain text.
  ColumnLayout {
    anchors.fill: parent
    visible: root.editing
    spacing: 6
    Text {
      Layout.fillWidth: true
      text: "Paste or type the lyrics. Lines that start with [mm:ss.xx] are timed."
      color: root.dim
      font.pixelSize: 10
      wrapMode: Text.WordWrap
    }
    ScrollView {
      Layout.fillWidth: true
      Layout.fillHeight: true
      clip: true
      TextArea {
        id: editor
        color: root.fg
        font.pixelSize: 12
        wrapMode: TextEdit.Wrap
        selectByMouse: true
        placeholderText: "[00:12.00]First line\n[00:15.50]Second line"
        placeholderTextColor: root.faint
        background: Rectangle {
          color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.05)
          radius: 6
          border.width: 1
          border.color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, editor.activeFocus ? 0.35 : 0.15)
        }
        Keys.onEscapePressed: root.editing = false
      }
    }
    RowLayout {
      Layout.fillWidth: true
      spacing: 6
      Ui.Button {
        visible: !!(root.lyrics && root.lyrics.source === "yours")
        text: "Remove mine"
        bordered: true
        foreground: root.dim
        accent: root.accent
        fontSize: Style.font.caption
        onClicked: { root.setText(""); root.editing = false }
      }
      Item { Layout.fillWidth: true }
      Ui.Button {
        text: "Cancel"
        bordered: true
        foreground: root.fg
        accent: root.accent
        fontSize: Style.font.caption
        onClicked: root.editing = false
      }
      Ui.Button {
        text: "Save"
        iconText: Model.ICON.save
        bordered: true
        foreground: root.accent
        accent: root.accent
        fontSize: Style.font.caption
        iconSize: Style.font.caption
        enabled: editor.text.trim() !== ""
        onClicked: { root.setText(editor.text); root.editing = false }
      }
    }
  }

  ColumnLayout {
    anchors.fill: parent
    spacing: 0
    visible: root.lines.length > 0 && !root.editing

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
        elide: Text.ElideRight
      }
      // Timing: earlier or later by half a second, remembered per track.
      IconButton {
        visible: root.synced
        icon: "−"
        size: 22
        colorFg: root.dim
        tip: "Lyrics earlier (0.5 s)"
        onClicked: root.nudge(root.offset - 500)
      }
      Text {
        visible: root.synced
        text: (root.offset > 0 ? "+" : "") + (root.offset / 1000).toFixed(1) + " s"
        color: root.offset !== 0 ? root.accent : root.faint
        font.pixelSize: 10
        MouseArea {
          anchors.fill: parent
          enabled: root.offset !== 0
          cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
          onClicked: root.nudge(0)
        }
      }
      IconButton {
        visible: root.synced
        icon: "+"
        size: 22
        colorFg: root.dim
        tip: "Lyrics later (0.5 s)"
        onClicked: root.nudge(root.offset + 500)
      }
      IconButton {
        icon: Model.ICON.tune
        size: 22
        colorFg: root.dim
        tip: "Edit the lyrics"
        onClicked: root.edit()
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
