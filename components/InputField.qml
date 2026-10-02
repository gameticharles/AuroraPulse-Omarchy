import QtQuick
import qs.Commons

// A single-line text box in the panel's style. `accepted` fires on Enter.
Rectangle {
  id: root
  property alias text: input.text
  property string placeholder: ""
  property bool monospace: false
  // Tokens and passwords are shown as dots.
  property bool secret: false
  readonly property color fg: Color.popups.text
  signal accepted()

  implicitHeight: 28
  implicitWidth: 200
  radius: 6
  color: Qt.rgba(fg.r, fg.g, fg.b, 0.05)
  border.width: 1
  border.color: input.activeFocus ? Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.7)
                                  : Qt.rgba(fg.r, fg.g, fg.b, 0.14)

  Text {
    anchors.left: parent.left
    anchors.leftMargin: 8
    anchors.right: parent.right
    anchors.rightMargin: 8
    anchors.verticalCenter: parent.verticalCenter
    visible: input.text === ""
    text: root.placeholder
    color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.35)
    font.pixelSize: 11
    elide: Text.ElideRight
  }

  TextInput {
    id: input
    anchors.fill: parent
    anchors.leftMargin: 8
    anchors.rightMargin: 8
    verticalAlignment: TextInput.AlignVCenter
    color: root.fg
    font.pixelSize: 11
    font.family: root.monospace ? Style.font.family : ""
    selectByMouse: true
    clip: true
    echoMode: root.secret ? TextInput.Password : TextInput.Normal
    Keys.onReturnPressed: root.accepted()
    Keys.onEnterPressed: root.accepted()
  }
}
