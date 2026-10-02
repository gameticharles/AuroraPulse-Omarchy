import QtQuick
import QtQuick.Controls
import qs.Commons

// A multi-line box for pasting a playlist or an exported station list.
Rectangle {
  id: root
  property alias text: area.text
  property string placeholder: ""
  readonly property color fg: Color.popups.text

  implicitHeight: 110
  radius: 6
  color: Qt.rgba(fg.r, fg.g, fg.b, 0.05)
  border.width: 1
  border.color: area.activeFocus ? Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.7)
                                 : Qt.rgba(fg.r, fg.g, fg.b, 0.14)

  ScrollView {
    anchors.fill: parent
    anchors.margins: 4
    TextArea {
      id: area
      placeholderText: root.placeholder
      placeholderTextColor: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.35)
      color: root.fg
      font.pixelSize: 10
      font.family: Style.font.family
      wrapMode: TextEdit.WrapAnywhere
      selectByMouse: true
      background: null
    }
  }
}
