import QtQuick
import qs.Commons

// A small text button. `danger` is for the ones that delete things.
Rectangle {
  id: root
  property string text: ""
  property string icon: ""
  property bool danger: false
  property bool primary: false
  property bool enabled: true
  readonly property color tint: danger ? "#ff6b6b" : Color.accent
  signal clicked()

  implicitWidth: label.implicitWidth + 18
  implicitHeight: 26
  radius: 6
  opacity: enabled ? 1 : 0.45
  color: area.containsMouse && enabled
         ? Qt.rgba(tint.r, tint.g, tint.b, 0.25)
         : (primary ? Qt.rgba(tint.r, tint.g, tint.b, 0.16) : "transparent")
  border.width: 1
  border.color: Qt.rgba(tint.r, tint.g, tint.b, 0.5)

  Text {
    id: label
    anchors.centerIn: parent
    text: (root.icon ? root.icon + "  " : "") + root.text
    color: root.tint
    font.family: Style.font.family
    font.pixelSize: 11
  }

  MouseArea {
    id: area
    anchors.fill: parent
    hoverEnabled: true
    enabled: root.enabled
    cursorShape: Qt.PointingHandCursor
    onClicked: root.clicked()
  }
}
