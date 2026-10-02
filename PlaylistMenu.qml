import QtQuick
import QtQuick.Controls
import QtQml

// "Add to playlist": one entry per playlist of yours, then "New playlist".
// Shared by the rows' right-click menu and the button beside the title.
//
// An Instantiator, not a Repeater: a Repeater inside a Menu does not reliably
// add its items (see DownloadMenu), and the playlists change while it lives.
Menu {
  id: root

  property var playlists: []

  // A playlist id, or "" for a new one.
  signal chosen(string playlist)

  readonly property var mine: (playlists || []).filter(function (p) { return !p.smart })

  Instantiator {
    model: root.mine
    delegate: MenuItem {
      required property var modelData
      text: modelData.name + "   " + (modelData.count || 0)
      onTriggered: root.chosen(modelData.id)
    }
    onObjectAdded: function (index, object) { root.insertItem(index, object) }
    onObjectRemoved: function (index, object) { root.removeItem(object) }
  }

  MenuSeparator {
    visible: root.mine.length > 0
    height: visible ? implicitHeight : 0
  }

  MenuItem {
    text: "New playlist"
    onTriggered: root.chosen("")
  }
}
