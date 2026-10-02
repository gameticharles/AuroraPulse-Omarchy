import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons

// Shared shape for a settings row: a label, an optional help line, and a
// control on the right.
//
// Colours come from the shell's popup theme rather than hard-coded white, so
// the settings stay readable under a light theme.
Item {
  id: root
  property string label: ""
  property string help: ""
  readonly property color fg: Color.popups.text
  readonly property color dim: Qt.rgba(fg.r, fg.g, fg.b, 0.55)
  readonly property color faint: Qt.rgba(fg.r, fg.g, fg.b, 0.12)
  readonly property color accent: Color.accent
  // Width kept free on the right for the row's control.
  property real controlWidth: 170

  Layout.fillWidth: true
  // Where a control placed under the label (ChoiceRow) can start.
  readonly property real labelHeight: column.height
  implicitHeight: column.height + 10

  // A plain Column with explicit widths, not a ColumnLayout: wrapped text
  // inside a layout whose height feeds back into the outer layout made the
  // settings page re-layout forever and draw only its first row.
  Column {
    id: column
    x: 0
    y: 5
    width: Math.max(0, root.width - root.controlWidth - (root.controlWidth > 0 ? 10 : 0))
    spacing: 1

    Text {
      width: parent.width
      height: Math.max(24, implicitHeight)
      verticalAlignment: Text.AlignVCenter
      text: root.label
      color: root.fg
      font.pixelSize: 12
      elide: Text.ElideRight
    }

    // Help is always shown, small: help that only appears on hover is help
    // nobody finds.
    Text {
      width: parent.width
      text: root.help
      color: root.dim
      font.pixelSize: 10
      wrapMode: Text.WordWrap
      visible: root.help !== ""
    }
  }
}
