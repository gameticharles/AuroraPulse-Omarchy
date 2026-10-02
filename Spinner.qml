import QtQuick
import qs.Commons

// A loading spinner, and the glyph shown in its place when nothing is loading.
//
// The spinner is drawn, not taken from the icon font. The font's "loading"
// glyph is an arc that does not sit at the centre of its own box, so rotating
// the text turned the arc around the wrong point - it wobbled round a corner
// instead of spinning in place. A square canvas with the arc drawn about its
// middle rotates exactly about the arc's centre.
//
// The resting glyph is a separate item that is never rotated, so a spin that
// stops part way can no longer leave the play or pause icon tilted.
Item {
  id: root
  property bool running: false
  property string idleText: ""
  property color color: Color.popups.text
  property real size: 16

  implicitWidth: size
  implicitHeight: size

  Text {
    anchors.centerIn: parent
    visible: !root.running && root.idleText !== ""
    text: root.idleText
    color: root.color
    font.family: Style.font.family
    font.pixelSize: root.size
  }

  Canvas {
    id: arc
    anchors.centerIn: parent
    visible: root.running
    width: Math.round(root.size * 0.86)
    height: width
    antialiasing: true
    onPaint: {
      var ctx = getContext("2d")
      ctx.reset()
      var line = Math.max(1.5, width * 0.12)
      var r = (width - line) / 2
      // A faint full ring, and the bright quarter that travels round it.
      ctx.lineWidth = line
      ctx.lineCap = "round"
      ctx.strokeStyle = Qt.rgba(root.color.r, root.color.g, root.color.b, 0.18)
      ctx.beginPath()
      ctx.arc(width / 2, height / 2, r, 0, Math.PI * 2)
      ctx.stroke()
      ctx.strokeStyle = root.color
      ctx.beginPath()
      ctx.arc(width / 2, height / 2, r, -Math.PI / 2, Math.PI * 0.25)
      ctx.stroke()
    }
    onWidthChanged: requestPaint()
    Connections {
      target: root
      function onColorChanged() { arc.requestPaint() }
    }

    RotationAnimator on rotation {
      running: root.running && arc.visible
      from: 0
      to: 360
      duration: 900
      loops: Animation.Infinite
    }
  }
}
