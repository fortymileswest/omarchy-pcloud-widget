// Pure formatting + parsing helpers for the pCloud widget. Kept free of QML
// types so the panel and the service can share them and so they stay testable
// with a plain JS runtime.

var IMAGE_EXTENSIONS = {
  jpg: true, jpeg: true, png: true, gif: true, webp: true, avif: true, heic: true,
  svg: true, bmp: true, tif: true, tiff: true, raw: true, cr2: true, nef: true
}

var VIDEO_EXTENSIONS = {
  mp4: true, mov: true, mkv: true, webm: true, avi: true, m4v: true, mpg: true,
  mpeg: true, wmv: true, flv: true
}

var AUDIO_EXTENSIONS = {
  mp3: true, flac: true, wav: true, m4a: true, ogg: true, opus: true, aac: true,
  wma: true, aiff: true
}

var DOCUMENT_EXTENSIONS = {
  pdf: true, txt: true, md: true, doc: true, docx: true, xls: true, xlsx: true,
  ppt: true, pptx: true, odt: true, ods: true, odp: true, rtf: true, csv: true,
  pages: true, numbers: true, key: true, epub: true
}

var ARCHIVE_EXTENSIONS = {
  zip: true, tar: true, gz: true, bz2: true, xz: true, "7z": true, rar: true,
  zst: true, iso: true
}

var CODE_EXTENSIONS = {
  js: true, ts: true, jsx: true, tsx: true, py: true, rb: true, go: true,
  rs: true, c: true, h: true, cpp: true, hpp: true, java: true, kt: true,
  swift: true, sh: true, json: true, yaml: true, yml: true, toml: true,
  html: true, css: true, qml: true, sql: true
}

function defaultStatus() {
  return {
    ok: true,
    keyringAvailable: false,
    rcloneInstalled: false,
    authenticated: false,
    account: "",
    email: "",
    host: "api.pcloud.com",
    region: "US",
    mountPoint: "",
    mounted: false,
    quota: 0,
    usedquota: 0,
    usagePercent: 0,
    premium: false,
    premiumExpires: "",
    publicLinkQuota: 0,
    recent: [],
    indexedAt: 0,
    fileCount: 0,
    folderCount: 0,
    statusText: "Checking…"
  }
}

// Every helper subcommand answers with one JSON object. Anything else means
// the process died in a way it was supposed to catch, so surface that rather
// than silently showing stale state.
function parseJson(raw, whatFailed) {
  var text = String(raw || "").trim()
  if (text === "") return { ok: false, error: whatFailed || "No response" }
  try {
    var parsed = JSON.parse(text)
    if (!parsed || typeof parsed !== "object") throw new Error("not an object")
    return parsed
  } catch (e) {
    return { ok: false, error: whatFailed || "Unreadable response" }
  }
}

function fileExtension(name) {
  var value = String(name || "").toLowerCase()
  var index = value.lastIndexOf(".")
  return index > 0 ? value.substring(index + 1) : ""
}

function fileKind(entry) {
  if (entry && entry.isFolder) return "folder"
  var ext = fileExtension(entry ? entry.name : entry)
  if (IMAGE_EXTENSIONS[ext]) return "image"
  if (VIDEO_EXTENSIONS[ext]) return "video"
  if (AUDIO_EXTENSIONS[ext]) return "audio"
  if (DOCUMENT_EXTENSIONS[ext]) return "document"
  if (ARCHIVE_EXTENSIONS[ext]) return "archive"
  if (CODE_EXTENSIONS[ext]) return "code"
  return "misc"
}

function fileGlyph(entry) {
  var kind = fileKind(entry)
  if (kind === "folder") return "󰉋"
  if (kind === "image") return "󰋩"
  if (kind === "video") return "󰈫"
  if (kind === "audio") return "󰎆"
  if (kind === "document") return "󰈙"
  if (kind === "archive") return "󰀼"
  if (kind === "code") return "󰅴"
  return "󰈔"
}

// pCloud quotes storage in decimal units, so divide by 1000 to match what the
// web UI and the invoice say.
function formatBytes(bytes) {
  var value = Number(bytes || 0)
  if (!isFinite(value) || value <= 0) return "0 B"
  var units = ["B", "KB", "MB", "GB", "TB", "PB"]
  var index = 0
  while (value >= 1000 && index < units.length - 1) {
    value = value / 1000
    index++
  }
  var decimals = value >= 100 || index === 0 ? 0 : (value >= 10 ? 1 : 2)
  return value.toFixed(decimals).replace(/\.0+$/, "").replace(/(\.\d)0$/, "$1") + " " + units[index]
}

function formatPercent(value) {
  var number = Number(value || 0)
  if (!isFinite(number) || number <= 0) return "0%"
  if (number >= 10) return Math.round(number) + "%"
  return number.toFixed(1).replace(/\.0$/, "") + "%"
}

function usageText(used, quota) {
  if (Number(quota || 0) > 0) return formatBytes(used) + " of " + formatBytes(quota)
  return formatBytes(used)
}

function formatCount(value) {
  var number = Number(value || 0)
  if (!isFinite(number) || number <= 0) return "0"
  if (number < 1000) return String(Math.round(number))
  if (number < 1000000) return (number / 1000).toFixed(number < 10000 ? 1 : 0).replace(/\.0$/, "") + "k"
  return (number / 1000000).toFixed(1).replace(/\.0$/, "") + "M"
}

function relativeTime(timestampSec, nowMs) {
  var ts = Number(timestampSec || 0)
  if (!isFinite(ts) || ts <= 0) return "Unknown"
  var now = nowMs === undefined ? Date.now() : Number(nowMs)
  var diff = Math.max(0, Math.floor((now - ts * 1000) / 1000))
  if (diff < 60) return "Just now"
  var minutes = Math.floor(diff / 60)
  if (minutes < 60) return minutes + "m ago"
  var hours = Math.floor(minutes / 60)
  if (hours < 24) return hours + "h ago"
  var days = Math.floor(hours / 24)
  if (days < 30) return days + "d ago"
  var months = Math.floor(days / 30)
  if (months < 12) return months + "mo ago"
  return Math.floor(days / 365) + "y ago"
}

// "12.4 MB · 3h ago · /Documents" — trims the parts that have nothing to say.
function entryMeta(entry, nowMs) {
  if (!entry) return ""
  var parts = []
  if (!entry.isFolder && Number(entry.size || 0) > 0) parts.push(formatBytes(entry.size))
  if (Number(entry.modified || 0) > 0) parts.push(relativeTime(entry.modified, nowMs))
  var folder = String(entry.folder || "")
  if (folder !== "" && folder !== "/") parts.push(folder)
  return parts.join(" · ")
}

function parentPath(path) {
  var value = String(path || "/")
  var index = value.lastIndexOf("/")
  if (index <= 0) return "/"
  return value.substring(0, index)
}

// The mount gives every cloud path a local twin, which is what lets the panel
// hand a file to the file manager instead of a browser.
function localPath(mountPoint, cloudPath) {
  var base = String(mountPoint || "")
  var rest = String(cloudPath || "")
  if (base === "") return ""
  if (rest.charAt(0) !== "/") rest = "/" + rest
  return base.replace(/\/+$/, "") + rest
}

function fileUri(path) {
  var parts = String(path || "").split("/")
  for (var i = 0; i < parts.length; i++) parts[i] = encodeURIComponent(parts[i])
  return "file://" + parts.join("/")
}

// Home-relative form of an absolute path. The hero's meta line sits directly
// under the region pill, and a full /home/<user>/... mount point crowds it.
function tildePath(path) {
  return String(path || "").replace(/^\/home\/[^/]+\//, "~/")
}

function elide(text, max) {
  var value = String(text || "").replace(/\s+/g, " ").trim()
  var limit = Number(max || 140)
  return value.length > limit ? value.substring(0, limit - 1) + "…" : value
}

if (typeof module !== "undefined") {
  module.exports = {
    defaultStatus: defaultStatus, parseJson: parseJson, fileExtension: fileExtension,
    fileKind: fileKind, fileGlyph: fileGlyph, formatBytes: formatBytes,
    formatPercent: formatPercent, usageText: usageText, formatCount: formatCount,
    relativeTime: relativeTime, entryMeta: entryMeta, parentPath: parentPath,
    localPath: localPath, fileUri: fileUri, elide: elide,
    tildePath: tildePath
  }
}
