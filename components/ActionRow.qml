import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

RowBase {
  id: root
  property string action: "run"
  signal run()
  controlWidth: buttonText.implicitWidth + 16

  Rectangle {
    anchors.top: parent.top
    anchors.topMargin: 5
    anchors.right: parent.right
    width: buttonText.implicitWidth + 16
    height: 24
    radius: 5
    color: buttonArea.containsMouse ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.22)
                                    : "transparent"
    border.width: 1
    border.color: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.45)

    Text {
      id: buttonText
      anchors.centerIn: parent
      text: root.action
      color: root.accent
      font.pixelSize: 11
    }

    MouseArea {
      id: buttonArea
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: root.run()
    }
  }
}
