import QtQuick
import QtQuick.Layouts
import "Model.js" as Model

// The TV programme guide.
//
// The daemon parses the XMLTV once and caches it, so this is a lookup rather
// than a download. "Now" is the important row and everything after it is
// context, which is why only the current programme gets the accent.
Item {
  id: root

  property var epg: null
  property color fg: "#dddddd"
  property color dim: Qt.rgba(1, 1, 1, 0.55)
  property color faint: Qt.rgba(1, 1, 1, 0.3)
  property color accent: "#5b8cff"

  readonly property var now: (epg && epg.now) ? epg.now : ({})
  readonly property var upcoming: (epg && epg.upcoming) ? epg.upcoming : []
  // Never null, so a binding that reads .title before any guide has arrived
  // evaluates to "" instead of throwing. `visible` does not stop evaluation.
  readonly property bool hasNow: !!(epg && epg.now)

  Text {
    anchors.centerIn: parent
    visible: !root.hasNow && root.upcoming.length === 0
    text: root.epg === null ? "Loading the guide…" : "No guide data for this channel"
    color: root.faint
    font.pixelSize: 12
  }

  Flickable {
    anchors.fill: parent
    anchors.margins: 4
    visible: root.hasNow || root.upcoming.length > 0
    contentHeight: column.height
    clip: true
    boundsBehavior: Flickable.StopAtBounds

    ColumnLayout {
      id: column
      width: parent.width
      spacing: 6

      // A thin marker for the current time, so "what is on now" is a glance
      // rather than a calculation.
      Rectangle {
        Layout.fillWidth: true
        Layout.preferredHeight: 1
        color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.12)
        visible: root.hasNow
      }

      RowLayout {
        Layout.fillWidth: true
        spacing: 12
        visible: root.hasNow

        ColumnLayout {
          Layout.preferredWidth: 62
          spacing: 1
          Text {
            text: root.clock(root.now.start)
            color: root.accent
            font.pixelSize: 12
            font.bold: true
          }
          Text {
            text: root.clock(root.now.stop)
            color: root.dim
            font.pixelSize: 10
          }
        }

        ColumnLayout {
          Layout.fillWidth: true
          spacing: 2
          Text {
            Layout.fillWidth: true
            text: root.now.title || ""
            color: root.fg
            font.pixelSize: 13
            font.bold: true
            elide: Text.ElideRight
          }
          Text {
            Layout.fillWidth: true
            visible: !!root.now.desc
            text: root.now.desc || ""
            color: root.dim
            font.pixelSize: 11
            wrapMode: Text.WordWrap
            maximumLineCount: 3
            elide: Text.ElideRight
          }
          // A progress bar for the current programme: it answers "how much of
          // this is left" without the user doing arithmetic on a clock time.
          Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 2
            Layout.topMargin: 4
            radius: 1
            color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.1)
            visible: root.hasNow && root.now.stop > root.now.start
            Rectangle {
              width: parent.width * Math.max(0, Math.min(1,
                (Date.now() / 1000 - root.now.start) / (root.now.stop - root.now.start)))
              height: parent.height
              radius: parent.radius
              color: root.accent
            }
          }
        }
      }

      Rectangle {
        Layout.fillWidth: true
        Layout.preferredHeight: 1
        color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.1)
      }

      Repeater {
        model: root.upcoming
        delegate: RowLayout {
          required property var modelData
          Layout.fillWidth: true
          spacing: 12

          Text {
            Layout.preferredWidth: 62
            text: root.clock(modelData.start)
            color: root.dim
            font.pixelSize: 11
          }
          Text {
            Layout.fillWidth: true
            text: modelData.title || ""
            color: root.dim
            font.pixelSize: 12
            elide: Text.ElideRight
          }
          Text {
            text: Model.duration(modelData.stop - modelData.start)
            color: root.faint
            font.pixelSize: 10
          }
        }
      }
    }
  }

  // 24-hour time, because a TV guide in 12-hour form is ambiguous twice a
  // day and the ambiguity is always at 1am and 1pm.
  function clock(epochSeconds) {
    if (!epochSeconds) return ""
    var d = new Date(epochSeconds * 1000)
    function pad(n) { return n < 10 ? "0" + n : "" + n }
    return pad(d.getHours()) + ":" + pad(d.getMinutes())
  }
}
