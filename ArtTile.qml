import QtQuick
import qs.Commons
import "Model.js" as Model

// Artwork with a legible fallback.
//
// The path is always a local file the daemon already verified with ffprobe;
// there is no network fetch here, so a hostile "cover.jpg" cannot become a
// rendering problem. If the file is missing the tile falls back to initials
// rather than a broken-image glyph.
Item {
  id: root

  property var item: null
  property real radius: 6
  property bool circular: false
  readonly property bool crop: !!item && (item.source === "youtube"
                                          || item.source === "music"
                                          || item.source === "local")

  readonly property string path: item && item.art ? String(item.art.path || "") : ""
  readonly property color tint: Model.sourceOf(item ? item.source : "").color
  readonly property bool showing: path !== "" && image.status === Image.Ready

  Rectangle {
    anchors.fill: parent
    radius: root.circular ? width / 2 : root.radius
    color: Qt.rgba(root.tint.r, root.tint.g, root.tint.b, 0.16)

    Text {
      anchors.centerIn: parent
      visible: !root.showing
      text: Model.initials(root.item ? (root.item.artist || root.item.title) : "",
                           "AP")
      color: root.tint
      font.family: Style.font.family
      font.pixelSize: Math.max(9, Math.round(Math.min(root.width, root.height) * 0.36))
      font.bold: true
    }

    Image {
      id: image
      anchors.fill: parent
      anchors.margins: root.crop ? 1 : Math.max(2, Math.round(root.width * 0.06))
      visible: root.showing
      source: root.path
      asynchronous: true
      cache: true
      // Station and channel logos are fitted, not cropped: most of them are
      // words on a transparent square, and cropping cut the words off.
      // Video thumbnails are pictures, so they fill the tile.
      fillMode: root.crop ? Image.PreserveAspectCrop : Image.PreserveAspectFit
      smooth: true
      mipmap: true
      sourceSize.width: Math.max(1, Math.round(root.width * 2))
      sourceSize.height: Math.max(1, Math.round(root.height * 2))
    }
  }
}
