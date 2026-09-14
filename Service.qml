import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import "Model.js" as Model

// All pCloud state and every subprocess the panel needs. The panel binds to
// the properties here and calls the verbs; nothing in Panel.qml spawns a
// process of its own.
//
// Secrets: sign-in is an OAuth browser flow run by the helper, so no password
// memory the moment it is sent, and the auth token only ever exists inside
// the helper and the login keyring. Neither is ever placed in a command line,
// because argv is readable by any process on the machine.
Item {
  id: root

  property var settings: ({})

  // Resolved from this file's own URL so the plugin works from wherever it is
  // installed, symlinked or copied.
  readonly property string pluginDir: String(Qt.resolvedUrl(".")).replace(/^file:\/\//, "")
  readonly property string helperPath: pluginDir + "pcloud.py"

  // ------------------------------------------------------------ status
  property bool keyringAvailable: true
  property bool rcloneInstalled: true
  property bool authenticated: false
  property string account: ""
  property string email: ""
  property string region: ""
  property string mountPoint: ""
  property bool mounted: false
  property double quota: 0
  property double usedquota: 0
  property double usagePercent: 0
  property bool premium: false
  property int fileCount: 0
  property int folderCount: 0
  property double indexedAt: 0
  property var recent: []
  property string statusText: "Checking…"

  // ------------------------------------------------------------ transient UI state
  property bool refreshing: false
  property string actionStatus: ""
  property string lastError: ""

  property string query: ""
  property var results: []
  property int resultTotal: 0
  // Results arrive a page at a time rather than all at once: a broad query
  // can match thousands of rows, and building that many delegates is far
  // slower than the search itself.
  // Folder listings for this session, keyed by folder id. Browsing is a live
  // API call per folder, so without this every step back through a tree shows
  // the previous folder's contents for the length of a round trip.
  property var folderCache: ({})
  // Set once a live status has landed, so the cached one read at startup can
  // never overwrite fresher data.
  property bool everLoaded: false

  readonly property int searchPageSize: 50
  property int searchLimit: searchPageSize
  property bool searching: false

  property var browseEntries: []
  property string browsePath: "/"
  property int browseFolderId: 0
  property int browseParentId: 0
  property bool browsing: false
  // Breadcrumb of {id, name} for the folder browser's back navigation.
  property var browseStack: []

  property string shareLink: ""
  property string shareName: ""

  property var uploadQueue: []
  property int uploadDone: 0
  property string uploadCurrent: ""

  readonly property int refreshIntervalSec: intSetting("refreshIntervalSec", 90, 15, 3600)
  readonly property int recentLimit: intSetting("recentLimit", 20, 5, 100)
  readonly property bool autoMount: boolSetting("autoMount", true)
  readonly property bool busy: statusProcess.running || connectProcess.running
    || mountProcess.running || unmountProcess.running || logoutProcess.running
  readonly property bool uploading: uploadProcess.running
  readonly property bool indexing: reindexProcess.running

  // A mount is only attempted once per login, so a genuine failure (no fuse,
  // bad token) doesn't turn into a retry loop against every status poll.
  property bool _autoMountTried: false

  signal connectSucceeded()
  signal connectFailed(string message)
  signal shareLinkReady(string link, string name)
  signal uploadFinished(int count)

  function setting(name, fallback) {
    var value = settings ? settings[name] : undefined
    return value === undefined || value === null ? fallback : value
  }

  function intSetting(name, fallback, min, max) {
    var n = parseInt(String(setting(name, fallback)), 10)
    if (!isFinite(n)) n = fallback
    return Math.max(min, Math.min(max, n))
  }

  function boolSetting(name, fallback) {
    var value = setting(name, fallback)
    if (typeof value === "boolean") return value
    var s = String(value).toLowerCase()
    if (s === "true" || s === "1" || s === "yes") return true
    if (s === "false" || s === "0" || s === "no") return false
    return fallback
  }

  function note(message) {
    actionStatus = Model.elide(message, 160)
    noteTimer.restart()
  }

  function reportError(message) {
    lastError = Model.elide(message, 200)
    actionStatus = ""
  }

  // ------------------------------------------------------------ verbs

  function refresh() {
    if (statusProcess.running) return
    refreshing = true
    statusProcess.command = ["python3", helperPath, "status", String(recentLimit)]
    statusProcess.running = true
  }

  // Sign-in is `rclone authorize pcloud`: the helper opens the browser and
  // blocks until the user approves, so this process can be long-lived. No
  // credential passes through QML at all.
  // True only while the browser sign-in is outstanding. `busy` also covers
  // status polls, so the sign-in button needs its own flag to decide whether
  // it is offering to start or to cancel.
  readonly property bool connecting: connectProcess.running

  function connect() {
    if (connectProcess.running) return
    lastError = ""
    note("Waiting for the browser…")
    connectProcess.command = ["python3", helperPath, "connect"]
    connectProcess.running = true
  }

  // rclone waits up to ten minutes for the browser. Without this the panel
  // would sit there disabled for the whole timeout if the page is dismissed.
  function cancelConnect() {
    if (!connectProcess.running) return
    connectProcess.running = false
    lastError = ""
    note("Sign-in cancelled")
  }

  function logout() {
    if (logoutProcess.running) return
    note("Signing out…")
    logoutProcess.command = ["python3", helperPath, "logout"]
    logoutProcess.running = true
  }

  function mount() {
    if (mountProcess.running) return
    if (!rcloneInstalled) { reportError("rclone is not installed"); return }
    note("Mounting drive…")
    mountProcess.command = ["python3", helperPath, "mount"]
    mountProcess.running = true
  }

  function unmount() {
    if (unmountProcess.running) return
    note("Unmounting…")
    unmountProcess.command = ["python3", helperPath, "unmount"]
    unmountProcess.running = true
  }

  function toggleMount() {
    if (mounted) unmount()
    else mount()
  }

  function reindex() {
    if (reindexProcess.running || !authenticated) return
    note("Rebuilding search index…")
    reindexProcess.command = ["python3", helperPath, "reindex"]
    reindexProcess.running = true
  }

  function showMoreResults() {
    if (searching || results.length >= resultTotal) return
    searchLimit += searchPageSize
    runSearch()
  }

  function runSearch() {
    if (!authenticated) return
    var text = String(query || "").trim()
    if (text === "") { results = []; resultTotal = 0; searching = false; return }
    if (searchProcess.running) { searchPending = true; return }
    searching = true
    searchProcess.command = ["python3", helperPath, "search", text,
                             String(searchLimit)]
    searchProcess.running = true
  }
  property bool searchPending: false

  function browse(folderId, folderName, isDrillDown) {
    if (browseProcess.running || !authenticated) return
    if (isDrillDown) {
      var stack = browseStack.slice()
      stack.push({ id: browseFolderId, name: browsePath })
      browseStack = stack
    }
    // Paint what was there last time straight away, then revalidate. A folder
    // already seen this session appears instantly instead of after a call.
    var hit = folderCache[String(folderId || 0)]
    if (hit) {
      browseEntries = hit.entries
      browsePath = hit.path
      browseFolderId = hit.folderId
      browseParentId = hit.parentId
    }
    browsing = !hit
    browseProcess.pendingName = String(folderName || "")
    browseProcess.command = ["python3", helperPath, "list", String(folderId || 0)]
    browseProcess.running = true
  }

  function browseUp() {
    if (browseStack.length === 0) {
      if (browseFolderId !== 0) browse(0, "/", false)
      return
    }
    var stack = browseStack.slice()
    var previous = stack.pop()
    browseStack = stack
    browse(previous.id, previous.name, false)
  }

  function makeLink(entry) {
    if (!entry || linkProcess.running) return
    shareLink = ""
    shareName = String(entry.name || "")
    note("Creating share link…")
    linkProcess.command = ["python3", helperPath, "link",
                           entry.isFolder ? "folder" : "file", String(entry.id || 0)]
    linkProcess.running = true
  }

  function copyText(text) {
    if (String(text || "") === "") return
    // wl-copy reads stdin, so the link never becomes a command-line argument
    // that shows up in another user's process list.
    clipboardProcess.payload = String(text)
    clipboardProcess.running = true
  }

  function upload(paths, folderId) {
    if (uploadProcess.running) return
    var list = []
    for (var i = 0; i < paths.length; i++) {
      var value = String(paths[i] || "").trim()
      if (value !== "") list.push(value)
    }
    if (list.length === 0) return
    uploadQueue = list
    uploadDone = 0
    uploadCurrent = list.length === 1 ? baseName(list[0]) : (list.length + " files")
    note("Uploading " + uploadCurrent + "…")
    uploadProcess.command = ["python3", helperPath, "upload",
                             String(folderId === undefined ? browseFolderId : folderId)].concat(list)
    uploadProcess.running = true
  }

  function baseName(path) {
    var parts = String(path || "").replace(/\/+$/, "").split("/")
    return decodeURIComponent(parts[parts.length - 1] || "")
  }

  // Prefer the mounted copy so the file opens in the user's own apps; fall
  // back to the pCloud web UI when the drive isn't mounted.
  function openEntry(entry) {
    if (!entry) return
    if (mounted && mountPoint !== "") {
      var local = Model.localPath(mountPoint, entry.path)
      Quickshell.execDetached(["xdg-open", local])
      note("Opening " + entry.name)
      return
    }
    Qt.openUrlExternally("https://my.pcloud.com/")
    note("Drive not mounted — opened pCloud on the web")
  }

  function revealEntry(entry) {
    if (!entry || !mounted || mountPoint === "") { openEntry(entry); return }
    var local = Model.localPath(mountPoint, entry.path)
    Quickshell.execDetached(["xdg-open", entry.isFolder ? local : Model.parentPath(local)])
  }

  function openMountPoint() {
    if (mountPoint === "") return
    Quickshell.execDetached(["xdg-open", mountPoint])
  }

  // ------------------------------------------------------------ result handling

  function applyStatus(raw) {
    var data = Model.parseJson(raw, "Could not read pCloud status")
    if (!data.ok) { reportError(data.error); return }
    keyringAvailable = data.keyringAvailable === true
    rcloneInstalled = data.rcloneInstalled === true
    var wasAuthenticated = authenticated
    authenticated = data.authenticated === true
    account = String(data.account || "")
    email = String(data.email || account)
    region = String(data.region || "")
    mountPoint = String(data.mountPoint || "")
    mounted = data.mounted === true
    quota = Number(data.quota || 0)
    usedquota = Number(data.usedquota || 0)
    usagePercent = Number(data.usagePercent || 0)
    premium = data.premium === true
    fileCount = Number(data.fileCount || 0)
    folderCount = Number(data.folderCount || 0)
    indexedAt = Number(data.indexedAt || 0)
    recent = data.recent || []
    statusText = String(data.statusText || "")
    if (data.lastError) lastError = Model.elide(data.lastError, 200)
    else if (authenticated) lastError = ""

    if (!authenticated) {
      _autoMountTried = false
      if (wasAuthenticated) { results = []; browseEntries = []; recent = [] }
      return
    }
    // A fresh index makes search instant and gives the panel its file counts.
    if (indexedAt === 0 && !reindexProcess.running) reindex()
    if (autoMount && rcloneInstalled && !mounted && !_autoMountTried) {
      _autoMountTried = true
      mount()
    }
  }

  Timer {
    id: refreshTimer
    interval: root.refreshIntervalSec * 1000
    repeat: true
    running: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  Timer {
    id: noteTimer
    interval: 3000
    onTriggered: root.actionStatus = ""
  }

  Timer {
    id: delayedRefresh
    interval: 900
    onTriggered: root.refresh()
  }

  // Typing shouldn't fire a subprocess per keystroke, but the wait only needs
  // to cover the cost of one: a query is ~37ms against the SQLite index, so a
  // long debounce would now be the slowest part of a keystroke rather than a
  // saving. runSearch() already coalesces anything that arrives mid-flight.
  Timer {
    id: searchDebounce
    interval: 60
    onTriggered: root.runSearch()
  }

  onQueryChanged: {
    // A new query starts from the first page again.
    searchLimit = searchPageSize
    if (String(query || "").trim() === "") {
      results = []
      resultTotal = 0
      searchDebounce.stop()
      return
    }
    searchDebounce.restart()
  }

  // Read once at startup: the panel can be drawn from the last known state
  // while the live status is still in flight.
  Process {
    id: cachedStatusProcess
    running: true
    command: ["python3", root.helperPath, "status", String(root.recentLimit), "cached"]
    stdout: StdioCollector { id: cachedStatusOut; waitForEnd: true }
    onExited: function(exitCode) {
      if (root.everLoaded || exitCode !== 0) return
      root.applyStatus(cachedStatusOut.text)
    }
  }

  Process {
    id: statusProcess
    stdout: StdioCollector { id: statusOut; waitForEnd: true }
    stderr: StdioCollector { id: statusErr; waitForEnd: true }
    onExited: function(exitCode) {
      root.refreshing = false
      if (exitCode === 0) {
        root.everLoaded = true
        root.applyStatus(statusOut.text)
      } else {
        root.reportError(String(statusErr.text || "pCloud helper failed"))
      }
    }
  }

  Process {
    id: connectProcess
    stdout: StdioCollector { id: connectOut; waitForEnd: true }
    stderr: StdioCollector { id: connectErr; waitForEnd: true }
    onExited: function(exitCode) {
      // A cancelled run is the user's own doing, not a failure to report.
      if (root.actionStatus === "Sign-in cancelled" && exitCode !== 0) return
      var data = Model.parseJson(connectOut.text, String(connectErr.text || "Sign-in failed"))
      if (!data.ok) {
        root.reportError(data.error)
        root.connectFailed(String(data.error))
        return
      }
      root.lastError = ""
      root.note("Signed in")
      root.connectSucceeded()
      root.refresh()
    }
  }

  Process {
    id: logoutProcess
    stdout: StdioCollector { id: logoutOut; waitForEnd: true }
    onExited: {
      root.folderCache = ({})
      root.everLoaded = false
      root.query = ""
      root.results = []
      root.browseEntries = []
      root.browseStack = []
      root.browseFolderId = 0
      root.shareLink = ""
      root.note("Signed out")
      root.refresh()
    }
  }

  Process {
    id: mountProcess
    stdout: StdioCollector { id: mountOut; waitForEnd: true }
    stderr: StdioCollector { id: mountErr; waitForEnd: true }
    onExited: {
      var data = Model.parseJson(mountOut.text, String(mountErr.text || "Mount failed"))
      if (!data.ok) root.reportError(data.error)
      else root.note("Drive mounted at " + String(data.mountPoint || ""))
      delayedRefresh.restart()
    }
  }

  Process {
    id: unmountProcess
    stdout: StdioCollector { id: unmountOut; waitForEnd: true }
    onExited: {
      var data = Model.parseJson(unmountOut.text, "Unmount failed")
      if (!data.ok) root.reportError(data.error)
      else { root._autoMountTried = true; root.note("Drive unmounted") }
      delayedRefresh.restart()
    }
  }

  Process {
    id: reindexProcess
    stdout: StdioCollector { id: reindexOut; waitForEnd: true }
    onExited: {
      var data = Model.parseJson(reindexOut.text, "Could not index your files")
      if (!data.ok) { root.reportError(data.error); return }
      root.fileCount = Number(data.fileCount || 0)
      root.folderCount = Number(data.folderCount || 0)
      root.indexedAt = Number(data.builtAt || 0)
      root.note("Indexed " + Model.formatCount(data.fileCount) + " files")
      if (String(root.query || "").trim() !== "") root.runSearch()
      delayedRefresh.restart()
    }
  }

  Process {
    id: searchProcess
    stdout: StdioCollector { id: searchOut; waitForEnd: true }
    onExited: {
      root.searching = false
      var data = Model.parseJson(searchOut.text, "Search failed")
      if (data.ok) {
        root.results = data.results || []
        root.resultTotal = Number(data.total || 0)
      }
      // Keystrokes that arrived mid-run get one coalesced follow-up.
      if (root.searchPending) {
        root.searchPending = false
        root.runSearch()
      }
    }
  }

  Process {
    id: browseProcess
    property string pendingName: ""
    stdout: StdioCollector { id: browseOut; waitForEnd: true }
    onExited: {
      root.browsing = false
      var data = Model.parseJson(browseOut.text, "Could not open that folder")
      if (!data.ok) { root.reportError(data.error); return }
      root.browseEntries = data.entries || []
      root.browsePath = String(data.path || "/")
      root.browseFolderId = Number(data.folderId || 0)
      root.browseParentId = Number(data.parentId || 0)
      root.folderCache[String(root.browseFolderId)] = {
        entries: root.browseEntries,
        path: root.browsePath,
        folderId: root.browseFolderId,
        parentId: root.browseParentId
      }
    }
  }

  Process {
    id: linkProcess
    stdout: StdioCollector { id: linkOut; waitForEnd: true }
    onExited: {
      var data = Model.parseJson(linkOut.text, "Could not create a share link")
      if (!data.ok) { root.reportError(data.error); return }
      root.shareLink = String(data.link || "")
      root.copyText(root.shareLink)
      root.note("Link copied for " + root.shareName)
      root.shareLinkReady(root.shareLink, root.shareName)
    }
  }

  Process {
    id: uploadProcess
    stdout: StdioCollector { id: uploadOut; waitForEnd: true }
    stderr: StdioCollector { id: uploadErr; waitForEnd: true }
    onExited: {
      var data = Model.parseJson(uploadOut.text, String(uploadErr.text || "Upload failed"))
      // The destination folder now has a file the cached listing lacks.
      root.folderCache = ({})
      root.uploadQueue = []
      root.uploadCurrent = ""
      if (!data.ok) { root.reportError(data.error); return }
      var count = Number(data.count || 0)
      root.uploadDone = count
      root.note("Uploaded " + count + (count === 1 ? " file" : " files"))
      root.uploadFinished(count)
      if (root.browseFolderId !== undefined) root.browse(root.browseFolderId, "", false)
      delayedRefresh.restart()
    }
  }

  Process {
    id: clipboardProcess
    property string payload: ""
    command: ["wl-copy"]
    stdinEnabled: true
    onStarted: {
      write(payload)
      payload = ""
      stdinEnabled = false
    }
  }
}
