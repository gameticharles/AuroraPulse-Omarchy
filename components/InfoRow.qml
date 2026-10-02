import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// A read-only label/value line. Deliberately not a control: these rows report
// what is already on disk, so giving them something that looks pressable would
// promise an action that does not exist.
RowBase {
  id: root
  property string value: ""
  controlWidth: Math.min(valueText.implicitWidth, 220)

  Text {
    id: valueText
    anchors.top: parent.top
    anchors.topMargin: 5
    anchors.right: parent.right
    width: root.controlWidth
    text: root.value
    color: root.dim
    font.pixelSize: 11
    horizontalAlignment: Text.AlignRight
    elide: Text.ElideRight
  }
}
