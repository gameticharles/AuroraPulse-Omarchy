import QtQuick
import QtQuick.Controls
import qs.Commons

// A square icon button. Deliberately tiny: there are eleven of these on
// screen at once and each one that grew by two pixels would cost more
// legibility than it bought.
Rectangle {
  id: root

  property string icon: ""
  property string label: ""
  property bool active: false
  property bool enabled: true
  property real size: 26
  // Said on hover. An icon-only control has to be able to say what it does.
  property string tip: ""
  // Show colorAccent while hovered, not only while active: a warning colour
  // on the way in for buttons that end something, like power.
  property bool accentOnHover: false

  signal clicked()

  implicitWidth: size
  implicitHeight: size
  radius: 5
  color: area.containsMouse && enabled
         ? Qt.rgba(accentColor.r, accentColor.g, accentColor.b, 0.14)
         : (active ? Qt.rgba(accentColor.r, accentColor.g, accentColor.b, 0.10)
                   : "transparent")

  readonly property color accentColor: root.active
                                       || (root.accentOnHover && area.containsMouse && root.enabled)
                                       ? colorAccent : colorFg
  property color colorFg: "#cccccc"
  property color colorAccent: "#5b8cff"

  opacity: enabled ? 1 : 0.35

  Text {
    anchors.centerIn: parent
    text: root.icon
    color: root.accentColor
    font.family: Style.font.family
    font.pixelSize: Math.round(root.size * 0.5)
  }

  Text {
    anchors.centerIn: parent
    visible: root.label !== ""
    text: root.label
    color: root.accentColor
    font.family: root.fontFamilyOverride
    font.pixelSize: Math.round(root.size * 0.4)
    font.bold: true
  }

  // Defaults to the shell font so the Nerd Font glyphs actually exist. This
  // used to be the literal string "Nerd Font", which this system does not
  // have: fc-match answers Liberation Sans, which contains none of the icon
  // glyphs, so the icon silently fell back to nothing.
  property string fontFamilyOverride: Style.font.family

  MouseArea {
    id: area
    anchors.fill: parent
    hoverEnabled: true
    enabled: root.enabled
    cursorShape: Qt.PointingHandCursor
    onClicked: root.clicked()
  }

  ThemedToolTip {
    visible: root.tip !== "" && area.containsMouse
    text: root.tip
  }
}
