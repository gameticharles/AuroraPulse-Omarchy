import QtQuick
import QtQuick.Controls
import qs.Commons
import qs.Ui as Ui
import "Model.js" as Model

// The queue: everything lined up, from any source, in play order.
//
// Click an entry to jump to it; hover for move up, move down and remove. The
// entry playing now is marked and cannot be removed from under the player -
// stop it, or skip past it, first.
Item {
  id: root

  property var items: []
  property int index: -1
  property var artByKey: ({})
  property bool currentLoading: false
  property bool currentPlaying: false
  property color fg: Color.popups.text
  property color dim: Qt.rgba(fg.r, fg.g, fg.b, 0.55)
  property color faint: Qt.rgba(fg.r, fg.g, fg.b, 0.3)
  property color accent: Color.accent

  signal jump(int index)
  signal remove(int index)
  signal move(int from, int to)
  signal clear()

  readonly property int totalSeconds: {
    var sum = 0
    for (var i = 0; i < items.length; i++) sum += Number(items[i].duration || 0)
    return sum
  }

  function artItem(entry) {
    var key = entry && entry.art ? entry.art.key : ""
    var path = key ? root.artByKey[key] : ""
    if (!path) return entry
    var copy = Object.assign({}, entry)
    copy.art = { url: "", path: path, key: key }
    return copy
  }

  Item {
    id: header
    anchors.left: parent.left
    anchors.right: parent.right
    anchors.top: parent.top
    height: Style.space(30)

    Text {
      anchors.left: parent.left
      anchors.verticalCenter: parent.verticalCenter
      text: root.items.length === 0 ? "The queue is empty"
            : Model.count(root.items.length, "item", "items")
              + (root.totalSeconds > 0 ? "  ·  " + Model.duration(root.totalSeconds) : "")
              + (root.index >= 0 ? "  ·  playing " + (root.index + 1) : "")
      color: root.dim
      font.pixelSize: Style.font.caption
    }

    Ui.Button {
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      visible: root.items.length > 1
      text: "Clear"
      iconText: Model.ICON.clearAll
      tooltipText: "Remove everything except what is playing"
      bordered: true
      foreground: root.fg
      accent: root.accent
      fontSize: Style.font.caption
      iconSize: Style.font.caption
      verticalPadding: Style.space(2)
      onClicked: root.clear()
    }
  }

  Column {
    anchors.centerIn: parent
    spacing: Style.space(8)
    visible: root.items.length === 0
    width: parent.width - Style.space(40)

    Text {
      anchors.horizontalCenter: parent.horizontalCenter
      text: Model.ICON.queue
      color: root.faint
      font.family: Style.font.family
      font.pixelSize: Style.space(28)
    }
    Text {
      width: parent.width
      horizontalAlignment: Text.AlignHCenter
      wrapMode: Text.WordWrap
      text: "Play something, or add rows with the queue button that appears when you hover them."
      color: root.dim
      font.pixelSize: Style.font.bodySmall
    }
  }

  ListView {
    id: list
    anchors.left: parent.left
    anchors.right: parent.right
    anchors.top: header.bottom
    anchors.topMargin: Style.space(4)
    anchors.bottom: parent.bottom
    clip: true
    spacing: Style.space(2)
    model: root.items
    boundsBehavior: Flickable.StopAtBounds
    ScrollBar.vertical: ScrollBar {}
    Component.onCompleted: if (root.index > 3) positionViewAtIndex(root.index, ListView.Center)

    delegate: Item {
      id: row
      required property var modelData
      required property int index
      width: list.width - Style.space(8)
      height: Style.space(50)
      readonly property bool isCurrent: index === root.index
      readonly property bool hot: area.containsMouse || tools.hovered

      Ui.BorderSurface {
        anchors.fill: parent
        radius: Style.cornerRadius
        color: row.hot ? Style.hoverFillFor(root.fg, root.accent)
             : row.isCurrent ? Style.normalFillFor(root.fg, root.accent) : "transparent"
        borderSpec: Border.none()
      }

      MouseArea {
        id: area
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: root.jump(row.index)
      }

      Text {
        id: number
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        width: Style.space(24)
        horizontalAlignment: Text.AlignHCenter
        text: row.isCurrent ? "" : String(row.index + 1)
        color: root.faint
        font.pixelSize: Style.font.caption
      }

      ArtTile {
        id: art
        anchors.left: number.right
        anchors.leftMargin: Style.space(2)
        anchors.verticalCenter: parent.verticalCenter
        width: Style.space(38)
        height: Style.space(38)
        radius: Style.space(5)
        item: root.artItem(row.modelData)

        Rectangle {
          anchors.fill: parent
          radius: Style.space(5)
          visible: row.isCurrent
          color: Qt.rgba(0, 0, 0, 0.5)
          EqBars {
            anchors.centerIn: parent
            width: Style.space(15)
            height: Style.space(13)
            color: root.accent
            playing: root.currentPlaying
          }
        }
      }

      Column {
        anchors.left: art.right
        anchors.leftMargin: Style.space(10)
        anchors.right: tools.left
        anchors.rightMargin: Style.space(6)
        anchors.verticalCenter: parent.verticalCenter
        spacing: Style.space(2)
        Text {
          width: parent.width
          text: Model.cleanName(row.modelData.title || "")
          color: row.isCurrent ? root.accent : root.fg
          font.pixelSize: Style.font.bodySmall + 1
          font.bold: row.isCurrent
          elide: Text.ElideRight
        }
        Text {
          width: parent.width
          text: [Model.sourceOf(row.modelData.source).label, row.modelData.artist,
                 (row.modelData.extra || {}).country]
                .filter(function (x) { return !!x }).join("  ·  ")
          color: root.dim
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }
      }

      Row {
        id: tools
        anchors.right: parent.right
        anchors.rightMargin: Style.space(4)
        anchors.verticalCenter: parent.verticalCenter
        property bool hovered: toolsHover.hovered
        HoverHandler { id: toolsHover }

        Text {
          anchors.verticalCenter: parent.verticalCenter
          visible: !row.hot
          text: row.modelData.is_live ? "live" : Model.duration(row.modelData.duration)
          color: root.faint
          font.pixelSize: Style.font.caption
          rightPadding: Style.space(4)
        }
        IconButton {
          visible: row.hot
          icon: Model.ICON.up
          size: 24
          colorFg: root.dim
          enabled: row.index > 0
          tip: "Move up"
          onClicked: root.move(row.index, row.index - 1)
        }
        IconButton {
          visible: row.hot
          icon: Model.ICON.down
          size: 24
          colorFg: root.dim
          enabled: row.index < root.items.length - 1
          tip: "Move down"
          onClicked: root.move(row.index, row.index + 1)
        }
        IconButton {
          visible: row.hot && !row.isCurrent
          icon: Model.ICON.close
          size: 24
          colorFg: root.dim
          tip: "Remove from the queue"
          onClicked: root.remove(row.index)
        }
      }
    }
  }
}
