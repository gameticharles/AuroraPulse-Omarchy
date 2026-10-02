import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// A row of chips rather than a ComboBox: every option is visible at once, so
// there is no second click to find out what the choices are. The chips sit on
// their own line under the label and wrap, so a long list of options never
// squeezes the label to nothing.
RowBase {
  id: root
  property var options: []
  property var value: ""
  property var format: function (v) { return String(v) }
  signal picked(var value)

  controlWidth: 0
  implicitHeight: labelHeight + strip.height + 16

  Flow {
    id: strip
    x: 0
    y: root.labelHeight + 9
    width: root.width
    spacing: 4

    Repeater {
      model: root.options
      delegate: Rectangle {
        required property var modelData
        readonly property bool selected: String(root.value) === String(modelData)
        height: 24
        width: chipText.implicitWidth + 16
        radius: 5
        color: selected ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.22)
                        : (chipArea.containsMouse
                           ? Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.08) : "transparent")
        border.width: 1
        border.color: selected ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.6)
                               : Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.12)

        Text {
          id: chipText
          anchors.centerIn: parent
          text: root.format(modelData)
          color: parent.selected ? root.fg : root.dim
          font.pixelSize: 10
          font.bold: parent.selected
        }

        MouseArea {
          id: chipArea
          anchors.fill: parent
          hoverEnabled: true
          cursorShape: Qt.PointingHandCursor
          onClicked: root.picked(modelData)
        }
      }
    }
  }
}
