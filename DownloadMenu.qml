import QtQuick
import QtQuick.Controls

// Download, in a chosen format. Shared by the button beside the title and the
// rows' right-click menu, so both offer the same choices.
//
// The formats marked "default" are the ones in Settings › Media & library;
// picking another here is a one-off and does not change them. Written out as
// plain items: a Repeater inside a Menu does not reliably add its items.
Menu {
  id: root

  property bool allowVideo: true
  property string defaultVideo: "mp4"
  property string defaultAudio: "mp3"

  signal chosen(string kind, string format)

  function label(text, value, fallback) {
    return "    " + text + (value === fallback ? "   (default)" : "")
  }

  MenuItem {
    text: "Video"
    enabled: false
    visible: root.allowVideo
    height: visible ? implicitHeight : 0
  }

  MenuItem {
    text: root.label("MP4 · plays everywhere", "mp4", root.defaultVideo)
    visible: root.allowVideo
    height: visible ? implicitHeight : 0
    onTriggered: root.chosen("video", "mp4")
  }

  MenuItem {
    text: root.label("MKV", "mkv", root.defaultVideo)
    visible: root.allowVideo
    height: visible ? implicitHeight : 0
    onTriggered: root.chosen("video", "mkv")
  }

  MenuItem {
    text: root.label("WebM", "webm", root.defaultVideo)
    visible: root.allowVideo
    height: visible ? implicitHeight : 0
    onTriggered: root.chosen("video", "webm")
  }

  MenuItem {
    text: root.label("Original, as YouTube sends it", "original", root.defaultVideo)
    visible: root.allowVideo
    height: visible ? implicitHeight : 0
    onTriggered: root.chosen("video", "original")
  }

  MenuSeparator {
    visible: root.allowVideo
    height: visible ? implicitHeight : 0
  }

  MenuItem {
    text: "Audio"
    enabled: false
  }

  MenuItem {
    text: root.label("MP3", "mp3", root.defaultAudio)
    onTriggered: root.chosen("audio", "mp3")
  }

  MenuItem {
    text: root.label("M4A (AAC)", "m4a", root.defaultAudio)
    onTriggered: root.chosen("audio", "m4a")
  }

  MenuItem {
    text: root.label("Opus", "opus", root.defaultAudio)
    onTriggered: root.chosen("audio", "opus")
  }

  MenuItem {
    text: root.label("FLAC, lossless", "flac", root.defaultAudio)
    onTriggered: root.chosen("audio", "flac")
  }

  MenuItem {
    text: root.label("Original, as it is streamed", "original", root.defaultAudio)
    onTriggered: root.chosen("audio", "original")
  }
}
