import QtQuick
import QtQuick.Controls
import qs.Commons
import qs.Ui as Ui
import "Model.js" as Model

// The TV guide as a grid: channels down the side, time across the top, what
// is on now under the "now" line.
//
// The guide data comes per country from epgshare, matched to our channels by
// name; a channel the guide does not know simply has an empty row rather than
// a wrong programme. Click a channel, or any of its programmes, to tune in.
Item {
  id: root

  property var items: []          // the channels on screen in the TV tab
  property var rows: ({})         // uid -> [{start, stop, title, desc}]
  property double start: 0
  property double end: 0
  property double now: 0
  property bool loading: false
  property var artByKey: ({})
  property string currentUid: ""
  property color fg: Color.popups.text
  property color dim: Qt.rgba(fg.r, fg.g, fg.b, 0.55)
  property color faint: Qt.rgba(fg.r, fg.g, fg.b, 0.3)
  property color accent: Color.accent

  signal play(var entry, int index)

  readonly property real labelWidth: Style.space(118)
  readonly property real rowHeight: Style.space(42)
  readonly property real perHour: Style.space(200)
  readonly property real timelineWidth: Math.max(0, (end - start) / 3600 * perHour)
  readonly property int matched: {
    var n = 0
    for (var i = 0; i < items.length; i++) {
      var r = rows[items[i].uid]
      if (r && r.length) n++
    }
    return n
  }

  function xFor(t) { return (t - root.start) / 3600 * root.perHour }

  function clock(epoch) {
    var d = new Date(epoch * 1000)
    function pad(n) { return n < 10 ? "0" + n : "" + n }
    return pad(d.getHours()) + ":" + pad(d.getMinutes())
  }

  // Half-hour ticks across the window.
  readonly property var ticks: {
    var out = []
    if (end <= start) return out
    var t = Math.ceil(start / 1800) * 1800
    for (; t < end; t += 1800) out.push(t)
    return out
  }

  function artItem(entry) {
    var key = entry && entry.art ? entry.art.key : ""
    var path = key ? root.artByKey[key] : ""
    if (!path) return entry
    var copy = Object.assign({}, entry)
    copy.art = { url: "", path: path, key: key }
    return copy
  }

  // ---- time header ------------------------------------------------------

  Item {
    id: header
    anchors.left: parent.left
    anchors.leftMargin: root.labelWidth
    anchors.right: parent.right
    anchors.top: parent.top
    height: Style.space(22)
    clip: true

    Repeater {
      model: root.ticks
      delegate: Text {
        required property double modelData
        x: root.xFor(modelData) - flick.contentX + Style.space(3)
        anchors.verticalCenter: parent.verticalCenter
        text: root.clock(modelData)
        color: root.dim
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
      }
    }
  }

  Text {
    anchors.left: parent.left
    anchors.verticalCenter: header.verticalCenter
    text: root.loading ? "Loading guides…"
                       : Model.count(root.matched, "channel", "channels") + " with a guide"
    color: root.loading ? root.accent : root.faint
    font.pixelSize: Style.font.caption
  }

  // ---- channel column ---------------------------------------------------

  Item {
    id: channelColumn
    anchors.left: parent.left
    anchors.top: header.bottom
    anchors.bottom: parent.bottom
    width: root.labelWidth
    clip: true

    Column {
      y: -flick.contentY
      width: parent.width
      Repeater {
        model: root.items
        delegate: Item {
          required property var modelData
          required property int index
          width: channelColumn.width
          height: root.rowHeight
          readonly property bool isCurrent: modelData.uid === root.currentUid

          Ui.BorderSurface {
            anchors.fill: parent
            anchors.margins: 1
            radius: Style.cornerRadius
            color: chanArea.containsMouse ? Style.hoverFillFor(root.fg, root.accent)
                 : parent.isCurrent ? Style.normalFillFor(root.fg, root.accent) : "transparent"
            borderSpec: Border.none()
          }
          ArtTile {
            id: logo
            anchors.left: parent.left
            anchors.leftMargin: Style.space(4)
            anchors.verticalCenter: parent.verticalCenter
            width: Style.space(28)
            height: Style.space(28)
            radius: Style.space(4)
            item: root.artItem(modelData)
          }
          Text {
            anchors.left: logo.right
            anchors.leftMargin: Style.space(6)
            anchors.right: parent.right
            anchors.rightMargin: Style.space(4)
            anchors.verticalCenter: parent.verticalCenter
            text: Model.cleanName(modelData.title || "")
            color: parent.isCurrent ? root.accent : root.fg
            font.pixelSize: Style.font.caption + 1
            font.bold: parent.isCurrent
            wrapMode: Text.Wrap
            maximumLineCount: 2
            elide: Text.ElideRight
          }
          MouseArea {
            id: chanArea
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: root.play(modelData, index)
          }
        }
      }
    }
  }

  // ---- programmes ---------------------------------------------------------

  Flickable {
    id: flick
    anchors.left: channelColumn.right
    anchors.right: parent.right
    anchors.top: header.bottom
    anchors.bottom: parent.bottom
    clip: true
    contentWidth: root.timelineWidth
    contentHeight: root.items.length * root.rowHeight
    boundsBehavior: Flickable.StopAtBounds
    ScrollBar.vertical: ScrollBar {}
    ScrollBar.horizontal: ScrollBar {}
    // Open at "now", a little in from the left edge.
    onTimelineReady: contentX = Math.max(0, root.xFor(root.now) - Style.space(40))
    signal timelineReady()

    Repeater {
      model: root.items
      delegate: Item {
        id: channelRow
        required property var modelData
        required property int index
        y: index * root.rowHeight
        width: root.timelineWidth
        height: root.rowHeight
        readonly property var programmes: root.rows[modelData.uid] || []

        Rectangle {
          anchors.bottom: parent.bottom
          width: parent.width
          height: 1
          color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.06)
        }

        Text {
          visible: channelRow.programmes.length === 0 && !root.loading
          x: Math.max(Style.space(6), root.xFor(root.now) - Style.space(30))
          anchors.verticalCenter: parent.verticalCenter
          text: "no guide for this channel"
          color: root.faint
          font.pixelSize: Style.font.caption
        }

        Repeater {
          model: channelRow.programmes
          delegate: Rectangle {
            id: block
            required property var modelData
            readonly property bool onNow: modelData.start <= root.now && root.now < modelData.stop
            x: Math.max(0, root.xFor(modelData.start)) + 1
            width: Math.max(2, root.xFor(Math.min(modelData.stop, root.end))
                               - Math.max(0, root.xFor(modelData.start)) - 2)
            y: 2
            height: root.rowHeight - 4
            radius: Style.space(4)
            color: progArea.containsMouse ? Style.hoverFillFor(root.fg, root.accent)
                 : onNow ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.18)
                 : Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.05)
            border.width: onNow ? 1 : 0
            border.color: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.5)
            clip: true

            Column {
              anchors.left: parent.left
              anchors.leftMargin: Style.space(6)
              anchors.right: parent.right
              anchors.rightMargin: Style.space(4)
              anchors.verticalCenter: parent.verticalCenter
              spacing: 0
              Text {
                width: parent.width
                text: block.modelData.title
                color: block.onNow ? root.fg : root.dim
                font.pixelSize: Style.font.caption + 1
                font.bold: block.onNow
                elide: Text.ElideRight
              }
              Text {
                width: parent.width
                text: root.clock(block.modelData.start) + "–" + root.clock(block.modelData.stop)
                color: root.faint
                font.family: Style.font.family
                font.pixelSize: Style.font.caption - 1
                elide: Text.ElideRight
              }
            }

            MouseArea {
              id: progArea
              anchors.fill: parent
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              onClicked: root.play(channelRow.modelData, channelRow.index)
            }
            ThemedToolTip {
              visible: progArea.containsMouse
              text: block.modelData.title + "\n" + root.clock(block.modelData.start)
                    + "–" + root.clock(block.modelData.stop)
                    + (block.modelData.desc ? "\n\n" + block.modelData.desc : "")
            }
          }
        }
      }
    }

    // The present moment.
    Rectangle {
      visible: root.now > root.start && root.now < root.end
      x: root.xFor(root.now)
      width: 2
      height: Math.max(flick.height, flick.contentHeight)
      color: root.accent
      opacity: 0.85
    }
  }

  onTimelineWidthChanged: if (timelineWidth > 0) flick.timelineReady()

  Text {
    anchors.centerIn: parent
    visible: root.items.length === 0
    text: "No channels to show"
    color: root.dim
    font.pixelSize: Style.font.bodySmall
  }
}
