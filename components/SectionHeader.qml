import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons

// A group heading. Sized to sit between rows rather than shout.
Text {
  property string title: ""

  Layout.fillWidth: true
  Layout.topMargin: 14
  Layout.bottomMargin: 2
  text: title
  color: Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.45)
  font.pixelSize: 10
  font.bold: true
  font.capitalization: Font.AllUppercase
  font.letterSpacing: 0.8
  elide: Text.ElideRight
}
