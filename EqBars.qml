import QtQuick

// Three bouncing bars: "this is the one playing". Still while paused, so the
// marker says which row it is without claiming there is sound.
Item {
  id: root
  property color color: "white"
  property bool playing: false

  Row {
    anchors.fill: parent
    spacing: Math.max(1, Math.round(root.width / 8))

    Repeater {
      model: 3
      delegate: Item {
        required property int index
        width: (root.width - 2 * Math.max(1, Math.round(root.width / 8))) / 3
        height: root.height

        Rectangle {
          id: bar
          anchors.bottom: parent.bottom
          width: parent.width
          radius: width / 2
          color: root.color
          height: root.height * (root.playing ? 0.4 : [0.45, 0.8, 0.6][index])

          SequentialAnimation on height {
            running: root.playing
            loops: Animation.Infinite
            NumberAnimation {
              to: root.height * [0.9, 0.55, 1.0][index]
              duration: [420, 360, 480][index]
              easing.type: Easing.InOutSine
            }
            NumberAnimation {
              to: root.height * [0.35, 1.0, 0.45][index]
              duration: [380, 440, 400][index]
              easing.type: Easing.InOutSine
            }
          }
        }
      }
    }
  }
}
