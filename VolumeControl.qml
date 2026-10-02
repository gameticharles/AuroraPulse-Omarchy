import QtQuick
import qs.Commons
import "Model.js" as Model

// Volume: an icon that toggles mute and a track you drag or scroll.
//
// Deliberately the same shape as SeekBar. Both are "drag means an absolute
// value" controls, and this one used to be built differently: it relied on
// `pressed` staying true for the whole drag, drew its fill as a sibling of the
// groove with no `y` (so the bar sat several pixels above the track it was
// meant to fill), and had no visible knob to aim at. Any of those alone makes
// a working control look broken; together they read as a dead slider.
//
// Scroll is here on purpose. Adjusting volume is the single most repeated
// action in any music player, and on a desktop it is done with the wheel,
// without aiming at anything first.
Item {
  id: root

  property int volume: 70
  property bool muted: false
  property string icon: ""
  property color accent: "#5b8cff"
  property color textColor: Qt.rgba(1, 1, 1, 0.4)

  // Tracked explicitly rather than read off `pressed`, so a drag survives the
  // pointer straying a pixel outside this strip. At 24 high that is easy to
  // do, and `pressed` goes false the moment it happens, which silently ends
  // the drag mid-adjustment.
  property bool dragging: false

  signal volumeSet(int value)
  signal toggleMute()

  // The fill is computed here rather than in three places, so the bar, the
  // knob and the mute state can never disagree about where the level is.
  readonly property double level: muted ? 0 : Math.max(0, Math.min(100, volume)) / 100

  implicitHeight: 24

  Text {
    id: speaker
    anchors.left: parent.left
    anchors.verticalCenter: parent.verticalCenter
    width: 16
    text: root.icon
    color: root.muted ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.5)
                      : root.accent
    font.family: Style.font.family
    font.pixelSize: 14
    MouseArea {
      anchors.fill: parent
      cursorShape: Qt.PointingHandCursor
      onClicked: root.toggleMute()
    }
  }

  Item {
    id: track
    anchors.left: speaker.right
    anchors.leftMargin: 6
    anchors.right: percent.left
    anchors.rightMargin: 6
    anchors.verticalCenter: parent.verticalCenter
    // Taller than the line it draws, because a 3px strip is not a thing a
    // hand can reliably hit. The groove is centred inside it.
    height: 24

    Rectangle {
      id: groove
      anchors.verticalCenter: parent.verticalCenter
      anchors.left: parent.left
      anchors.right: parent.right
      height: 3
      radius: 1.5
      color: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.2)

      Rectangle {
        // Nested in the groove so it tracks it exactly, rather than being a
        // sibling that has to be positioned by hand and gets it wrong.
        width: parent.width * root.level
        height: parent.height
        radius: parent.radius
        color: root.accent
        Behavior on width { NumberAnimation { duration: 80 } }
      }

      Rectangle {
        // Also nested in the groove, and this is the whole point. As a sibling
        // of it the knob was given `y: (groove.height - height) / 2` but
        // placed against the 24px track rather than the 3px groove, which put
        // it about ten pixels too high: a thumb floating above the bar it
        // belonged to, only appearing to line up at one exact volume. Being a
        // child of the groove means both coordinates come from the same parent
        // and cannot disagree.
        id: knob
        width: 11
        height: 11
        radius: 5.5
        color: Qt.rgba(1, 1, 1, 0.9)
        x: Math.max(0, Math.min(parent.width - width,
                                parent.width * root.level - width / 2))
        y: (parent.height - height) / 2
        // Dimmed rather than hidden when muted. A control whose thumb
        // disappears is one you have to guess at.
        opacity: root.muted ? 0.35 : 1
        scale: root.dragging || hover.hovered ? 1.25 : 1
        Behavior on scale { NumberAnimation { duration: 90 } }
        Behavior on opacity { NumberAnimation { duration: 120 } }
      }
    }

    MouseArea {
      id: hover
      anchors.fill: parent
      hoverEnabled: true
      acceptedButtons: Qt.LeftButton | Qt.RightButton
      cursorShape: Qt.PointingHandCursor
      preventStealing: true
      onPressed: function (event) {
        if (event.button !== Qt.LeftButton) return
        root.dragging = true
        root.apply(event.x)
      }
      onPositionChanged: function (event) {
        if (root.dragging) root.apply(event.x)
      }
      onReleased: function (event) {
        if (event.button !== Qt.LeftButton || !root.dragging) return
        root.dragging = false
        root.apply(event.x)
      }
      onClicked: function (event) {
        // Right-click on the track mutes, which is the shell's own slider
        // convention. On a volume control that is also the one thing worth a
        // shortcut to, and it saves reaching for the speaker icon.
        if (event.button === Qt.RightButton) root.toggleMute()
      }
      onExited: {
        // The pointer left the strip, not the button, so keep dragging rather
        // than dropping the adjustment half way.
        if (!(hover.pressed)) root.dragging = false
      }
    }

    WheelHandler {
      target: null
      onWheel: function (event) {
        var step = event.angleDelta.y > 0 ? 5 : -5
        root.volumeSet(Math.max(0, Math.min(100, root.volume + step)))
        event.accepted = true
      }
    }
  }

  // The number, because "about two thirds" is not a volume anyone can return
  // to. Fixed width so the track does not shuffle as it changes.
  Text {
    id: percent
    anchors.right: parent.right
    anchors.verticalCenter: parent.verticalCenter
    width: 24
    horizontalAlignment: Text.AlignRight
    text: root.muted ? "off" : String(root.volume)
    color: root.textColor
    font.pixelSize: 10
  }

  // event.x is measured from the strip, so it is offset by the strip's own x
  // within the track before being measured against the groove.
  function apply(x) {
    var width = Math.max(1, groove.width)
    root.volumeSet(Math.round(Math.max(0, Math.min(1, (x - groove.x) / width)) * 100))
  }
}
