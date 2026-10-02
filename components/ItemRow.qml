import QtQuick
import QtQuick.Layouts
import qs.Commons

// One entry in a managed list - a station, a channel, a playlist, a folder -
// with an optional on/off box and a remove button.
Rectangle {
  id: root
  property string title: ""
  property string subtitle: ""
  property bool checkable: false
  property bool checked: true
  property bool removable: true
  property bool confirmRemove: false
  readonly property color fg: Color.popups.text
  signal toggled(bool value)
  signal removed()

  Layout.fillWidth: true
  implicitHeight: 40
  radius: 6
  color: hover.hovered ? Qt.rgba(fg.r, fg.g, fg.b, 0.07) : Qt.rgba(fg.r, fg.g, fg.b, 0.03)
  HoverHandler { id: hover }

  // Two clicks to delete: the first arms, the second removes. A list item
  // that vanishes on one stray click is one the user has to type in again.
  property bool armed: false
  Timer {
    id: disarm
    interval: 2500
    onTriggered: root.armed = false
  }

  Text {
    id: box
    visible: root.checkable
    anchors.left: parent.left
    anchors.leftMargin: 8
    anchors.verticalCenter: parent.verticalCenter
    width: visible ? 18 : 0
    text: root.checked ? String.fromCodePoint(0xF0132) : String.fromCodePoint(0xF0131)
    color: root.checked ? Color.accent : Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.45)
    font.family: Style.font.family
    font.pixelSize: 16
    MouseArea {
      anchors.fill: parent
      anchors.margins: -4
      cursorShape: Qt.PointingHandCursor
      onClicked: root.toggled(!root.checked)
    }
  }

  Column {
    anchors.left: box.right
    anchors.leftMargin: 8
    anchors.right: removeButton.left
    anchors.rightMargin: 6
    anchors.verticalCenter: parent.verticalCenter
    spacing: 1
    Text {
      width: parent.width
      text: root.title
      color: root.checkable && !root.checked ? Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.45)
                                             : root.fg
      font.pixelSize: 12
      font.strikeout: root.checkable && !root.checked
      elide: Text.ElideRight
    }
    Text {
      width: parent.width
      visible: root.subtitle !== ""
      text: root.subtitle
      color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.5)
      font.pixelSize: 10
      elide: Text.ElideMiddle
    }
  }

  Rectangle {
    id: removeButton
    visible: root.removable
    anchors.right: parent.right
    anchors.rightMargin: 6
    anchors.verticalCenter: parent.verticalCenter
    width: root.armed ? armedText.implicitWidth + 14 : 26
    height: 26
    radius: 5
    color: removeArea.containsMouse || root.armed ? Qt.rgba(1, 0.42, 0.42, 0.2) : "transparent"
    Text {
      id: armedText
      anchors.centerIn: parent
      text: root.armed ? "remove?" : String.fromCodePoint(0xF09E7)
      color: "#ff8a8a"
      font.family: Style.font.family
      font.pixelSize: root.armed ? 10 : 15
    }
    MouseArea {
      id: removeArea
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: {
        if (root.confirmRemove && !root.armed) {
          root.armed = true
          disarm.restart()
          return
        }
        root.armed = false
        root.removed()
      }
    }
  }
}
