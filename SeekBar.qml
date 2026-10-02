import QtQuick
import "Model.js" as Model

// The progress bar.
//
// While the pointer is down the bar shows where it is being dragged to, and
// the seek is sent once, on release. It used to send a seek for every pixel
// of movement and draw the player's reported position in the meantime, so the
// knob jumped back and forth between the two while the player stuttered
// through dozens of seeks. Leaving the strip mid-drag no longer cancels it.
Item {
  id: root

  property double position: 0
  property double duration: 0
  property color accent: "#5b8cff"
  property bool dragging: false
  property double dragFraction: 0

  signal seek(double fraction)

  readonly property bool seekable: duration > 0
  readonly property double fraction: dragging ? dragFraction
                                              : Model.progress(position, duration)
  implicitHeight: 16

  Rectangle {
    id: groove
    anchors.verticalCenter: parent.verticalCenter
    anchors.left: parent.left
    anchors.right: parent.right
    height: area.containsMouse || root.dragging ? 6 : 4
    radius: height / 2
    color: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.18)
    Behavior on height { NumberAnimation { duration: 90 } }

    Rectangle {
      width: root.seekable ? parent.width * root.fraction : 0
      height: parent.height
      radius: parent.radius
      color: root.accent
    }

    Rectangle {
      visible: root.seekable
      width: 12
      height: 12
      radius: 6
      color: root.accent
      x: Math.max(0, Math.min(groove.width - width,
                              groove.width * root.fraction - width / 2))
      y: (groove.height - height) / 2
      scale: root.dragging || area.containsMouse ? 1.2 : 0.9
      Behavior on scale { NumberAnimation { duration: 90 } }
    }
  }

  // Where a click would land, while hovering.
  Text {
    visible: area.containsMouse && !root.dragging && root.seekable
    x: Math.max(0, Math.min(root.width - width, area.mouseX - width / 2))
    anchors.bottom: groove.top
    anchors.bottomMargin: 4
    text: Model.duration(root.track(area.mouseX) * root.duration) || "0:00"
    color: root.accent
    font.pixelSize: 9
  }

  MouseArea {
    id: area
    anchors.fill: parent
    anchors.topMargin: -4
    anchors.bottomMargin: -4
    hoverEnabled: true
    enabled: root.seekable
    cursorShape: root.seekable ? Qt.PointingHandCursor : Qt.ArrowCursor
    preventStealing: true
    onPressed: function (event) {
      root.dragFraction = root.track(event.x)
      root.dragging = true
    }
    onPositionChanged: function (event) {
      if (root.dragging) root.dragFraction = root.track(event.x)
    }
    onReleased: function (event) {
      if (!root.dragging) return
      root.dragFraction = root.track(event.x)
      root.seek(root.dragFraction)
      root.dragging = false
    }
    onCanceled: root.dragging = false
  }

  function track(x) {
    var width = Math.max(1, groove.width)
    return Math.max(0, Math.min(1, (x - groove.x) / width))
  }
}
