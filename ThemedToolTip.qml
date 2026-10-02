import QtQuick
import QtQuick.Controls
import qs.Commons
import qs.Ui as Ui

// The shell's own tooltip look - the one the browser tabs get from Ui.Button:
// the theme's [tooltip] colours and border, square corners, the shell font.
// Qt's stock ToolTip ignores the theme (it came out pale yellow), so every
// tooltip in the plugin uses this instead. Long text wraps rather than
// running off the screen.
//
//   ThemedToolTip { visible: area.containsMouse; text: "Play" }
ToolTip {
  id: root

  property real maxWidth: 360
  readonly property var borderSpec: Border.localOrSurfaceSpec(
      "tooltip", "border", Color.tooltip.border, Color.tooltip.border,
      Math.max(1, Style.normalBorderWidth))

  delay: 400
  padding: 0
  width: Math.min(implicitWidth, maxWidth)

  background: Ui.BorderSurface {
    color: Color.tooltip.background
    borderSpec: root.borderSpec
    radius: 0
  }

  contentItem: Text {
    textFormat: Text.PlainText
    text: root.text
    color: Color.tooltip.text
    wrapMode: Text.Wrap
    font.family: Style.font.family
    font.pixelSize: Style.font.bodySmall
    leftPadding: Border.left(root.borderSpec) + Style.spacing.controlPaddingX
    rightPadding: Border.right(root.borderSpec) + Style.spacing.controlPaddingX
    topPadding: Border.top(root.borderSpec) + Style.spacing.controlPaddingY
    bottomPadding: Border.bottom(root.borderSpec) + Style.spacing.controlPaddingY
  }
}
