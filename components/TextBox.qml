import QtQuick
import QtQuick.Controls
import qs.Commons
import qs.Commons as Commons

// A multi-line box for pasting a playlist or an exported station list.
Rectangle {
  id: root
  property alias text: area.text
  property string placeholder: ""
  readonly property color fg: Commons.Color.popups.text

  implicitHeight: 110
  radius: 6
  color: Qt.rgba(fg.r, fg.g, fg.b, 0.05)
  border.width: 1
  border.color: area.activeFocus ? Qt.rgba(Commons.Color.accent.r, Commons.Color.accent.g, Commons.Color.accent.b, 0.7)
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
