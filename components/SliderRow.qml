import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

RowBase {
  id: root
  property real from: 0
  property real to: 100
  property real stepSize: 1
  property real value: 0
  property var format: function (v) { return String(v) }
  signal moved(real value)
  controlWidth: 160

  function quantise(raw) {
    var steps = Math.max(1, Math.round((root.to - root.from) / root.stepSize))
    var stepped = root.from + Math.round((raw - root.from) / root.stepSize) * root.stepSize
    return Math.max(root.from, Math.min(root.to, stepped))
  }

  Row {
    id: controls
    anchors.top: parent.top
    anchors.topMargin: 5
    anchors.right: parent.right
    spacing: 6

    Text {
      width: 46
      text: root.format(root.value)
      color: root.dim
      font.pixelSize: 11
      horizontalAlignment: Text.AlignRight
    }

    Slider {
      id: slider
      width: 108
      height: 18
      from: root.from
      to: root.to
      stepSize: root.stepSize
      value: root.value
      live: true
      onMoved: { root.moved(root.quantise(value)) }

      // The stock handle is a rounded rect that fights the flat look of
      // everything else here, so it is restyled to match the panel.
      handle: Rectangle {
        x: slider.visualPosition * (slider.availableWidth - width) + slider.leftPadding
        y: slider.topPadding + slider.availableHeight / 2 - height / 2
        width: 13
        height: 13
        radius: 6.5
        color: slider.pressed ? root.fg : Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.8)
      }
      background: Rectangle {
        x: slider.leftPadding
        y: slider.topPadding + slider.availableHeight / 2 - height / 2
        width: slider.availableWidth
        height: 3
        radius: 1.5
        color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.12)
        Rectangle {
          width: slider.visualPosition * parent.width
          height: parent.height
          radius: parent.radius
          color: root.accent
        }
      }
    }
  }
}
