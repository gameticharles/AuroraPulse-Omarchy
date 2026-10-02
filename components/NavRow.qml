import QtQuick
import QtQuick.Layouts
import qs.Commons

// A row that opens a settings page, like the original app's "Radio
// Settings", "TV & Streaming Settings" and "Media & Library Settings".
Rectangle {
  id: root
  property string icon: ""
  property string label: ""
  property string help: ""
  property color tint: Color.accent
  readonly property color fg: Color.popups.text
  signal clicked()

  Layout.fillWidth: true
  implicitHeight: 52
  radius: 8
  color: area.containsMouse ? Qt.rgba(fg.r, fg.g, fg.b, 0.08) : Qt.rgba(fg.r, fg.g, fg.b, 0.035)

  Rectangle {
    id: badge
    anchors.left: parent.left
    anchors.leftMargin: 10
    anchors.verticalCenter: parent.verticalCenter
    width: 32
    height: 32
    radius: 16
    color: Qt.rgba(root.tint.r, root.tint.g, root.tint.b, 0.18)
    Text {
      anchors.centerIn: parent
      text: root.icon
      color: root.tint
      font.family: Style.font.family
      font.pixelSize: 16
    }
  }

  Column {
    anchors.left: badge.right
    anchors.leftMargin: 10
    anchors.right: chevron.left
    anchors.rightMargin: 8
    anchors.verticalCenter: parent.verticalCenter
    spacing: 2
    Text {
      width: parent.width
      text: root.label
      color: root.fg
      font.pixelSize: 13
      font.bold: true
      elide: Text.ElideRight
    }
    Text {
      width: parent.width
      visible: root.help !== ""
      text: root.help
      color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.55)
      font.pixelSize: 10
      elide: Text.ElideRight
    }
  }

  Text {
    id: chevron
    anchors.right: parent.right
    anchors.rightMargin: 12
    anchors.verticalCenter: parent.verticalCenter
    text: String.fromCodePoint(0xF0142)
    color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.45)
    font.family: Style.font.family
    font.pixelSize: 16
  }

  MouseArea {
    id: area
    anchors.fill: parent
    hoverEnabled: true
    cursorShape: Qt.PointingHandCursor
    onClicked: root.clicked()
  }
}
