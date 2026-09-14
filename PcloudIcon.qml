import QtQuick
import qs.Commons

// A cloud mark: three discs of differing size sharing one flat baseline, with
// a slab bridging them into a single silhouette.
//
// Deliberately plain: rounded Rectangles, no QtQuick.Shapes, no inline
// component. A ShapePath/PathAngleArc version drew nothing on this Quickshell
// build, and an inline `component Disc:` declared inside the nested Item was
// not on the document root, where QML requires it. Both failed silently.
//
// Geometry is written as fractions of the icon box so the mark scales with
// iconSize: centre x as a fraction of width, centre y and radius as fractions
// of height. The outer discs sit one radius above the baseline so they meet it
// exactly, and the slab's top edge sits below both their tops, so no square
// corner breaks the outline.
//
// layer.enabled flattens the union before the parent's opacity applies, so the
// overlaps do not show as seams when the icon is dimmed for a disconnected
// state.
Item {
  id: root

  property real iconSize: Style.font.icon
  property color color: Color.foreground

  implicitWidth: iconSize * 1.3
  implicitHeight: iconSize
  width: implicitWidth
  height: implicitHeight

  Item {
    anchors.fill: parent
    layer.enabled: true
    layer.samples: 4

    // Left shoulder: centre (0.22w, 0.60h), radius 0.22h.
    Rectangle {
      color: root.color
      antialiasing: true
      width: root.height * 0.44
      height: width
      radius: width / 2
      x: root.width * 0.22 - width / 2
      y: root.height * 0.60 - height / 2
    }

    // Main puff, tallest: centre (0.50w, 0.36h), radius 0.28h.
    Rectangle {
      color: root.color
      antialiasing: true
      width: root.height * 0.56
      height: width
      radius: width / 2
      x: root.width * 0.50 - width / 2
      y: root.height * 0.36 - height / 2
    }

    // Right shoulder: centre (0.79w, 0.60h), radius 0.22h.
    Rectangle {
      color: root.color
      antialiasing: true
      width: root.height * 0.44
      height: width
      radius: width / 2
      x: root.width * 0.79 - width / 2
      y: root.height * 0.60 - height / 2
    }

    // Bridges the three discs down to the common baseline at 0.82. No
    // antialiasing: the slab is axis-aligned, and its soft edge would blend
    // with the discs underneath and show as a seam across the silhouette.
    Rectangle {
      color: root.color
      antialiasing: false
      x: root.width * 0.22
      y: root.height * 0.46
      width: root.width * 0.57
      height: root.height * 0.36
    }
  }
}
