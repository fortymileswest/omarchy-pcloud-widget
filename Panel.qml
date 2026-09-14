import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

Panel {
  id: root
  moduleName: "fortymileswest.pcloud"
  ipcTarget: "pcloud"
  manageIpc: false

  // "recent" and "browse" are the two resting views; typing in the search box
  // temporarily overrides both, and clearing it returns to whichever was up.
  property string mode: "recent"
  property string focusSection: "email"
  property int rowIndex: 0
  property bool cursorActive: false
  property bool dropHover: false

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color accent: Color.accent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  readonly property bool searchActive: String(pcloud.query || "").trim() !== ""
  readonly property var rows: searchActive ? pcloud.results
    : (mode === "browse" ? pcloud.browseEntries : pcloud.recent)
  readonly property bool connected: pcloud.authenticated
  readonly property color barIconColor: !connected ? Qt.darker(barForeground, 1.7)
    : (pcloud.mounted ? barForeground : Qt.darker(barForeground, 1.3))

  readonly property string emptyMessage: {
    if (searchActive) return pcloud.searching ? "Searching…" : "No files match that."
    if (mode === "browse") return pcloud.browsing ? "Opening…" : "This folder is empty."
    if (pcloud.indexing) return "Indexing your files…"
    return "Nothing here yet. Drop a file on this panel to upload it."
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  Service {
    id: pcloud
    settings: root.settings
  }

  // ------------------------------------------------------------ cursor model

  function selectedRow() {
    if (rows.length === 0) return null
    return rows[Math.max(0, Math.min(rowIndex, rows.length - 1))]
  }

  function ensureCursor() {
    if (!connected) {
      focusSection = "connect"
      return
    }
    if (focusSection === "connect") focusSection = "search"
    if (focusSection === "rows") {
      if (rows.length === 0) { focusSection = "search"; rowIndex = 0; return }
      rowIndex = Math.max(0, Math.min(rows.length - 1, rowIndex))
    }
  }

  function moveCursor(dx, dy) {
    cursorActive = true
    ensureCursor()
    if (dy === 0) return
    if (!connected) return
    if (focusSection === "search") {
      if (dy > 0 && rows.length > 0) { focusSection = "rows"; rowIndex = 0; scrollCursorIntoView() }
      return
    }
    if (focusSection === "rows") {
      if (dy < 0 && rowIndex === 0) { focusSection = "search"; panelFlick.contentY = 0; return }
      rowIndex = Math.max(0, Math.min(rows.length - 1, rowIndex + dy))
      scrollCursorIntoView()
    }
  }

  function activateCursor() {
    ensureCursor()
    if (!connected) { connectAccount(); return }
    if (focusSection === "rows") {
      var entry = selectedRow()
      if (entry && entry.isFolder) openFolder(entry)
      else pcloud.openEntry(entry)
    }
  }

  function setRowCursor(index) {
    cursorActive = true
    focusSection = "rows"
    rowIndex = index
  }

  function scrollItemIntoView(item) {
    if (!panelFlick || !item) return
    Qt.callLater(function() {
      if (!item) return
      var margin = Style.space(6)
      var point = item.mapToItem(panelFlick.contentItem, 0, 0)
      var top = point.y
      var bottom = top + item.height
      var maxY = Math.max(0, panelFlick.contentHeight - panelFlick.height)
      if (top < panelFlick.contentY + margin) panelFlick.contentY = Math.max(0, top - margin)
      else if (bottom > panelFlick.contentY + panelFlick.height - margin)
        panelFlick.contentY = Math.min(maxY, bottom + margin - panelFlick.height)
    })
  }

  function scrollCursorIntoView() {
    if (focusSection === "rows" && rowColumn && rowIndex >= 0 && rowIndex < rowColumn.children.length)
      scrollItemIntoView(rowColumn.children[rowIndex])
  }

  // ------------------------------------------------------------ actions

  function connectAccount() {
    pcloud.connect()
  }

  function openFolder(entry) {
    if (!entry) return
    pcloud.query = ""
    mode = "browse"
    pcloud.browse(entry.id, entry.name, true)
    rowIndex = 0
    panelFlick.contentY = 0
  }

  function setMode(next) {
    mode = next
    pcloud.query = ""
    rowIndex = 0
    panelFlick.contentY = 0
    if (next === "browse" && pcloud.browseEntries.length === 0) pcloud.browse(0, "/", false)
  }

  function handleDrop(drop) {
    var urls = drop.urls || []
    var paths = []
    for (var i = 0; i < urls.length; i++) paths.push(String(urls[i]))
    if (paths.length === 0) return
    // Uploads land in whatever folder the browser is showing; recent/search
    // views have no folder context, so they go to the account root.
    pcloud.upload(paths, mode === "browse" ? pcloud.browseFolderId : 0)
  }

  onOpenedChanged: if (opened) {
    cursorActive = false
    panelFlick.contentY = 0
    pcloud.refresh()
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  Connections {
    target: pcloud
    function onAuthenticatedChanged() {
      root.focusSection = pcloud.authenticated ? "search" : "connect"
      root.ensureCursor()
    }
  }

  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): string { pcloud.refresh(); return "ok" }
    function mount(): string { pcloud.mount(); return "ok" }
    function unmount(): string { pcloud.unmount(); return "ok" }
    function reindex(): string { pcloud.reindex(); return "ok" }
    function status(): string { return pcloud.statusText }
  }

  // ------------------------------------------------------------ bar button

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    iconComponent: Component {
      Item {
        PcloudIcon {
          anchors.centerIn: parent
          iconSize: Style.space(12)
          color: root.barIconColor
          opacity: root.connected ? 1.0 : 0.55
        }
      }
    }
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) pcloud.refresh()
      else if (buttonCode === Qt.MiddleButton) pcloud.toggleMount()
      else root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(420))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(620))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onMoveRequested: function(dx, dy) {
        if (!root.cursorActive) { root.cursorActive = true; return }
        root.moveCursor(dx, dy)
      }
      onActivateRequested: if (root.cursorActive) root.activateCursor()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: column
          width: panelFlick.width
          spacing: Style.space(12)

          // ---------------------------------------------------- hero
          Item {
            id: header
            width: parent.width
            implicitHeight: hero.implicitHeight
            readonly property bool ringVisible: false

            PanelHero {
              id: hero
              width: parent.width
              title: "pCloud"
              meta: {
                if (!root.connected) return pcloud.statusText
                if (pcloud.mounted) return "Mounted · " + Model.tildePath(pcloud.mountPoint)
                return pcloud.rcloneInstalled ? "Not mounted" : "rclone not installed"
              }
              // The region badge rides in trailingControl rather than in
              // PanelHero's own `detail` slot: that slot lives on the title
              // line, so it sits above the mount switch, which is centred
              // across both hero lines. Putting them in one Row lines them up.
              detail: ""
              foreground: root.foreground
              fontFamily: root.fontFamily
              iconOpacity: root.connected ? 1.0 : 0.5
              iconComponent: Component {
                PcloudIcon {
                  iconSize: Style.font.display
                  color: root.connected ? root.foreground : root.dim
                }
              }
              trailingControl: Component {
                Row {
                  spacing: Style.space(10)

                  BorderSurface {
                    id: regionPill
                    visible: root.connected && pcloud.region !== ""
                    anchors.verticalCenter: parent.verticalCenter
                    implicitWidth: regionText.implicitWidth + Style.space(10)
                    implicitHeight: regionText.implicitHeight + Style.space(4)
                    color: "transparent"
                    borderSpec: Border.controlSpec("normal", root.foreground, Color.accent)
                    radius: Style.cornerRadius

                    Text {
                      id: regionText
                      anchors.centerIn: parent
                      text: pcloud.region
                      color: root.dim
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.body
                      font.bold: true
                    }
                  }

                  ToggleSwitch {
                    id: mountSwitch
                    anchors.verticalCenter: parent.verticalCenter
                    visible: root.connected && pcloud.rcloneInstalled
                    checked: pcloud.mounted
                    busy: pcloud.busy
                    foreground: hero.foreground
                    onToggled: pcloud.toggleMount()

                    PanelToolTip {
                      visible: mountSwitch.containsMouse
                      text: pcloud.mounted ? "Unmount the drive" : "Mount the drive"
                      fontFamily: hero.fontFamily
                    }
                  }
                }
              }
            }
          }

          // ---------------------------------------------------- notices
          Text {
            visible: text !== ""
            width: parent.width
            text: {
              if (pcloud.actionStatus !== "") return pcloud.actionStatus
              if (pcloud.lastError !== "") return pcloud.lastError
              if (root.connected && !pcloud.keyringAvailable)
                return "secret-tool is missing — the login keyring is unavailable."
              if (root.connected && !pcloud.rcloneInstalled)
                return "Install rclone to mount the drive: omarchy pkg add rclone"
              return ""
            }
            color: pcloud.lastError !== "" && pcloud.actionStatus === "" ? root.urgent : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          // ---------------------------------------------------- login
          LoginForm {
            visible: !root.connected
            width: parent.width
          }

          // ---------------------------------------------------- stats
          Column {
            visible: root.connected
            width: parent.width
            spacing: Style.spacing.labelGap

            UsageBar { width: parent.width }

            InfoPair {
              label: "Stored"
              value: Model.usageText(pcloud.usedquota, pcloud.quota)
                + (pcloud.quota > 0 ? "  (" + Model.formatPercent(pcloud.usagePercent) + ")" : "")
            }
            InfoPair {
              label: "Files"
              value: Model.formatCount(pcloud.fileCount) + " in "
                + Model.formatCount(pcloud.folderCount) + " folders"
            }
            InfoPair {
              label: "Account"
              value: pcloud.email + (pcloud.premium ? "  ·  Premium" : "")
            }
            InfoPair {
              visible: pcloud.indexedAt > 0
              label: "Indexed"
              value: Model.relativeTime(pcloud.indexedAt)
            }
          }

          PanelSeparator { visible: root.connected; foreground: root.foreground }

          // ---------------------------------------------------- search
          TextField {
            id: searchField
            visible: root.connected
            width: parent.width
            foreground: root.foreground
            accent: root.accent
            placeholderText: "Search your pCloud…"
            text: pcloud.query
            hasCursor: root.cursorActive && root.focusSection === "search"
            onTextChanged: if (text !== pcloud.query) pcloud.query = text
            // Typing breaks the `text:` binding above, so mirror programmatic
            // clears (sign out, mode switch) back onto the field.
            Connections {
              target: pcloud
              function onQueryChanged() {
                if (searchField.text !== pcloud.query) searchField.text = pcloud.query
              }
            }
            onActiveFocusChanged: if (activeFocus) {
              root.cursorActive = true
              root.focusSection = "search"
            }
            Keys.onDownPressed: root.moveCursor(0, 1)
            Keys.onEscapePressed: {
              if (text !== "") text = ""
              else root.close()
            }
          }

          // ---------------------------------------------------- mode tabs + breadcrumb
          RowLayout {
            visible: root.connected && !root.searchActive
            width: parent.width
            spacing: Style.space(8)

            ModeTab { label: "Recent"; value: "recent" }
            ModeTab { label: "Browse"; value: "browse" }

            Item { Layout.fillWidth: true }

            PanelActionButton {
              visible: root.mode === "browse" && (pcloud.browseStack.length > 0 || pcloud.browseFolderId !== 0)
              iconText: "󰁍"
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: pcloud.browseUp()
            }
            PanelActionButton {
              iconText: "󰑐"
              foreground: root.foreground
              fontFamily: root.fontFamily
              enabled: !pcloud.indexing
              onClicked: pcloud.reindex()
            }
          }

          Text {
            visible: root.connected && !root.searchActive && root.mode === "browse"
            width: parent.width
            text: pcloud.browsePath === "" ? "/" : pcloud.browsePath
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            elide: Text.ElideMiddle
          }

          PanelSectionHeader {
            visible: root.connected && root.searchActive
            text: pcloud.resultTotal > root.rows.length
              ? "TOP " + root.rows.length + " OF " + pcloud.resultTotal + " MATCHES"
              : root.rows.length + (root.rows.length === 1 ? " MATCH" : " MATCHES")
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          // ---------------------------------------------------- rows
          Text {
            visible: root.connected && root.rows.length === 0
            width: parent.width
            text: root.emptyMessage
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
          }

          Column {
            id: rowColumn
            visible: root.connected && root.rows.length > 0
            width: parent.width
            spacing: Style.space(4)

            Repeater {
              model: root.rows
              EntryRow {
                required property var modelData
                required property int index
                width: rowColumn.width
                entry: modelData
                position: index
              }
            }
          }

          // ---------------------------------------------------- pagination
          Button {
            visible: root.connected && root.searchActive
              && pcloud.results.length < pcloud.resultTotal
            width: parent.width
            text: pcloud.searching
              ? "Loading…"
              : "Show " + Math.min(pcloud.searchPageSize,
                                   pcloud.resultTotal - pcloud.results.length)
                + " more of " + pcloud.resultTotal
            enabled: !pcloud.searching
            onClicked: pcloud.showMoreResults()
          }

          // ---------------------------------------------------- footer
          PanelSeparator { visible: root.connected; foreground: root.foreground }

          RowLayout {
            visible: root.connected
            width: parent.width
            spacing: Style.space(8)

            FooterButton {
              label: "Open drive"
              glyph: "󰉋"
              enabled: pcloud.mounted
              onActivated: pcloud.openMountPoint()
            }
            FooterButton {
              label: "pCloud web"
              glyph: "󰖟"
              onActivated: Qt.openUrlExternally("https://my.pcloud.com/")
            }
            Item { Layout.fillWidth: true }
            FooterButton {
              label: "Sign out"
              glyph: "󰍃"
              onActivated: pcloud.logout()
            }
          }
        }
      }

      // -------------------------------------------------------- drag and drop
      DropArea {
        anchors.fill: parent
        enabled: root.connected && !pcloud.uploading
        keys: ["text/uri-list"]
        onEntered: root.dropHover = true
        onExited: root.dropHover = false
        onDropped: function(drop) {
          root.dropHover = false
          root.handleDrop(drop)
        }
      }

      Rectangle {
        anchors.fill: parent
        visible: root.dropHover || pcloud.uploading
        color: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.14)
        border.color: root.accent
        border.width: Style.space(2)
        radius: Style.cornerRadius

        Column {
          anchors.centerIn: parent
          spacing: Style.space(8)

          Text {
            anchors.horizontalCenter: parent.horizontalCenter
            text: pcloud.uploading ? "󰅧" : "󰈴"
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.displayLarge
          }
          Text {
            anchors.horizontalCenter: parent.horizontalCenter
            text: pcloud.uploading
              ? "Uploading " + pcloud.uploadCurrent + "…"
              : "Drop to upload to " + (root.mode === "browse" ? pcloud.browsePath : "/")
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
          }
        }
      }
    }
  }

  // ============================================================== components

  component UsageBar: Item {
    implicitHeight: Style.space(8)

    Rectangle {
      anchors.fill: parent
      radius: height / 2
      color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.12)
    }

    Rectangle {
      height: parent.height
      // Always paint a sliver once anything is stored, so a nearly empty
      // account still reads as "connected" rather than "broken".
      width: pcloud.quota > 0
        ? Math.max(pcloud.usedquota > 0 ? Style.space(3) : 0,
                   parent.width * Math.min(1, pcloud.usagePercent / 100))
        : 0
      radius: height / 2
      color: pcloud.usagePercent >= 90 ? root.urgent : root.accent
      Behavior on width { NumberAnimation { duration: 320; easing.type: Easing.OutCubic } }
    }
  }

  component ModeTab: CursorSurface {
    property string label: ""
    property string value: ""

    // `current` is CursorSurface's own persistent selected-state flag.
    current: root.mode === value
    foreground: root.foreground
    implicitWidth: tabText.implicitWidth + Style.space(20)
    implicitHeight: tabText.implicitHeight + Style.space(8)

    Text {
      id: tabText
      anchors.centerIn: parent
      text: parent.label
      color: parent.current ? root.foreground : root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      font.bold: parent.current
    }

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: root.setMode(parent.value)
    }
  }

  component FooterButton: CursorSurface {
    id: footerButton
    property string label: ""
    property string glyph: ""
    property bool enabled: true
    signal activated()

    foreground: root.foreground
    opacity: enabled ? 1.0 : 0.45
    implicitWidth: footerRow.implicitWidth + Style.space(16)
    implicitHeight: footerRow.implicitHeight + Style.space(8)

    Row {
      id: footerRow
      anchors.centerIn: parent
      spacing: Style.space(6)

      Text {
        text: footerButton.glyph
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.iconSmall
        anchors.verticalCenter: parent.verticalCenter
      }
      Text {
        text: footerButton.label
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        anchors.verticalCenter: parent.verticalCenter
      }
    }

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      enabled: footerButton.enabled
      cursorShape: Qt.PointingHandCursor
      onEntered: footerButton.hasCursor = true
      onExited: footerButton.hasCursor = false
      onClicked: footerButton.activated()
    }
  }

  component InfoPair: Row {
    property string label: ""
    property string value: ""

    width: parent.width
    spacing: Style.space(8)

    Text {
      id: pairLabel
      text: parent.label
      color: root.foreground
      opacity: 0.6
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }

    Item {
      width: Math.max(0, parent.width - pairLabel.implicitWidth - pairValue.width - parent.spacing * 2)
      height: 1
    }

    Text {
      id: pairValue
      width: Math.min(implicitWidth, parent.width * 0.68)
      text: parent.value
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      horizontalAlignment: Text.AlignRight
      elide: Text.ElideMiddle
    }
  }

  component EntryRow: CursorSurface {
    id: entryRow
    property var entry: null
    property int position: 0
    readonly property string entryName: entry ? String(entry.name || "Untitled") : "Untitled"

    hasCursor: root.cursorActive && root.focusSection === "rows" && root.rowIndex === position
    foreground: root.foreground
    implicitHeight: entryContent.implicitHeight + Style.spacing.rowPaddingX

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      acceptedButtons: Qt.LeftButton | Qt.RightButton
      onEntered: root.setRowCursor(entryRow.position)
      onClicked: function(mouse) {
        if (mouse.button === Qt.RightButton) pcloud.revealEntry(entryRow.entry)
        else if (entryRow.entry && entryRow.entry.isFolder) root.openFolder(entryRow.entry)
        else pcloud.openEntry(entryRow.entry)
      }
    }

    RowLayout {
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      spacing: Style.space(8)

      Text {
        text: Model.fileGlyph(entryRow.entry)
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.icon
        Layout.alignment: Qt.AlignVCenter
      }

      ColumnLayout {
        id: entryContent
        Layout.fillWidth: true
        spacing: Style.space(1)

        Text {
          Layout.fillWidth: true
          text: entryRow.entryName
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          elide: Text.ElideMiddle
        }

        Text {
          Layout.fillWidth: true
          visible: text !== ""
          text: Model.entryMeta(entryRow.entry)
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideMiddle
        }
      }

      PanelActionButton {
        iconText: "󰌷"
        tooltipText: "Copy a share link"
        foreground: root.foreground
        fontFamily: root.fontFamily
        Layout.alignment: Qt.AlignVCenter
        onClicked: pcloud.makeLink(entryRow.entry)
      }
    }
  }

  component LoginForm: Column {
    spacing: Style.space(8)

    Text {
      width: parent.width
      text: "Connect your pCloud account. The browser opens for pCloud's own "
        + "sign-in page, so no password is typed here; only the token it "
        + "returns is kept, in your login keyring."
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      wrapMode: Text.WordWrap
    }

    Button {
      width: parent.width
      text: pcloud.connecting ? "Waiting for the browser — cancel" : "Connect pCloud"
      enabled: pcloud.connecting || (!pcloud.busy && pcloud.rcloneInstalled)
      onClicked: pcloud.connecting ? pcloud.cancelConnect() : root.connectAccount()
    }

    Text {
      width: parent.width
      visible: !pcloud.rcloneInstalled
      text: "rclone is required to sign in. Install it with: omarchy pkg add rclone"
      color: root.urgent
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      wrapMode: Text.WordWrap
    }

    Text {
      width: parent.width
      visible: !pcloud.keyringAvailable
      text: "secret-tool is not installed, so the token cannot be stored securely. Install it with: omarchy pkg add libsecret"
      color: root.urgent
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      wrapMode: Text.WordWrap
    }
  }
}
