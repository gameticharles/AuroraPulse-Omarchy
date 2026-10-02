import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

RowBase {
  id: root
  property bool checked: false
  controlWidth: 40
  signal toggled(bool value)

  Rectangle {
    id: track
    objectName: "toggle"
    anchors.top: parent.top
    anchors.topMargin: 5
    anchors.right: parent.right
    width: 34
    height: 19
    radius: 9.5
    color: root.checked ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.55)
                        : Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.12)
    border.width: 1
    border.color: root.checked ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.8)
                               : Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.18)
    Behavior on color { ColorAnimation { duration: 110 } }

    Rectangle {
      width: 15
      height: 15
      radius: 7.5
      y: 2
      x: root.checked ? track.width - width - 2 : 2
      color: root.checked ? root.fg : Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.6)
      Behavior on x { NumberAnimation { duration: 110 } }
    }

    MouseArea {
      anchors.fill: parent
      cursorShape: Qt.PointingHandCursor
      // Emit the wanted value and let the owner update `checked`. Assigning
      // it here cut the binding, so the switch stopped following the setting.
      onClicked: root.toggled(!root.checked)
    }
  }
}
