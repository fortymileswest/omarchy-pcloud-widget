#!/usr/bin/env python3
"""A minimal MCP server exposing pCloud to Claude Code over stdio.

It is a thin front end for pcloud.py, so it inherits that helper's auth: the
token lives in the login keyring and is never passed on a command line or
placed in an environment variable. There are no third-party dependencies —
MCP is JSON-RPC 2.0 over stdio, which the standard library covers.

Deliberately read-mostly. It can search, browse, share and upload, but there
is no delete, move, or rename tool, so a stray model call cannot destroy
anything in the account. The mounted drive covers editing in place.
"""

import json
import os
import subprocess
import sys

HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pcloud.py")
PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "pcloud", "version": "1.0.0"}

TOOLS = [
    {
        "name": "pcloud_status",
        "description": (
            "Account and connection status for pCloud: storage quota and usage, "
            "email, region, whether the rclone drive is mounted and where, index "
            "freshness, and the most recently modified files."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "recent_limit": {
                    "type": "integer",
                    "description": "How many recent files to include (default 20).",
                    "minimum": 1, "maximum": 100,
                },
            },
        },
    },
    {
        "name": "pcloud_search",
        "description": (
            "Search the whole pCloud account for files and folders by name. Matches "
            "substrings anywhere in the name or path and falls back to subsequence "
            "matching, so 'rprt24' finds 'report-2024.pdf'. Served from a local "
            "index; call pcloud_reindex if results look stale."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Text to look for."},
                "limit": {
                    "type": "integer",
                    "description": "Maximum results (default 40).",
                    "minimum": 1, "maximum": 200,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "pcloud_list",
        "description": (
            "List the direct contents of one pCloud folder. Folder id 0 is the "
            "account root. Use the folder ids returned here to walk deeper."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "folder_id": {
                    "type": "integer",
                    "description": "Folder id; 0 is the root (default 0).",
                },
            },
        },
    },
    {
        "name": "pcloud_share_link",
        "description": (
            "Create a public share link for a file or folder and return the URL. "
            "Anyone with the link can access the item, so confirm with the user "
            "before sharing anything sensitive."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string", "enum": ["file", "folder"],
                    "description": "Whether the id refers to a file or a folder.",
                },
                "id": {"type": "integer", "description": "The file id or folder id."},
            },
            "required": ["kind", "id"],
        },
    },
    {
        "name": "pcloud_upload",
        "description": (
            "Upload one or more local files into a pCloud folder. Paths must be "
            "absolute. Folder id 0 is the account root."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "folder_id": {
                    "type": "integer",
                    "description": "Destination folder id; 0 is the root (default 0).",
                },
                "paths": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Absolute paths of local files to upload.",
                    "minItems": 1,
                },
            },
            "required": ["paths"],
        },
    },
    {
        "name": "pcloud_reindex",
        "description": (
            "Rebuild the local search index from pCloud. Run this after files "
            "change outside this session, or when pcloud_search misses something "
            "you know exists."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def run_helper(args, timeout=180):
    try:
        done = subprocess.run(
            [sys.executable, HELPER] + [str(a) for a in args],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "The pCloud helper timed out"}
    except OSError as exc:
        return {"ok": False, "error": "Could not run the pCloud helper: %s" % exc}
    try:
        return json.loads(done.stdout or "{}")
    except ValueError:
        return {"ok": False,
                "error": (done.stderr or "The pCloud helper returned no JSON").strip()[:500]}


def call_tool(name, arguments):
    args = arguments or {}
    if name == "pcloud_status":
        return run_helper(["status", int(args.get("recent_limit", 20))])
    if name == "pcloud_search":
        query = str(args.get("query", "")).strip()
        if not query:
            return {"ok": False, "error": "query is required"}
        return run_helper(["search", query, int(args.get("limit", 40))])
    if name == "pcloud_list":
        return run_helper(["list", int(args.get("folder_id", 0))])
    if name == "pcloud_share_link":
        kind = str(args.get("kind", "file"))
        if kind not in ("file", "folder"):
            return {"ok": False, "error": "kind must be 'file' or 'folder'"}
        return run_helper(["link", kind, int(args.get("id", 0))])
    if name == "pcloud_upload":
        paths = args.get("paths") or []
        if not isinstance(paths, list) or not paths:
            return {"ok": False, "error": "paths must be a non-empty array"}
        return run_helper(["upload", int(args.get("folder_id", 0))] + list(paths))
    if name == "pcloud_reindex":
        return run_helper(["reindex"], timeout=600)
    return {"ok": False, "error": "Unknown tool: %s" % name}


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def reply(request_id, result):
    send({"jsonrpc": "2.0", "id": request_id, "result": result})


def reply_error(request_id, code, message):
    send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def handle(message):
    method = message.get("method")
    request_id = message.get("id")
    # Notifications carry no id and must never be answered.
    is_notification = request_id is None

    if method == "initialize":
        reply(request_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        })
        return
    if method in ("notifications/initialized", "notifications/cancelled"):
        return
    if method == "ping":
        if not is_notification:
            reply(request_id, {})
        return
    if method == "tools/list":
        reply(request_id, {"tools": TOOLS})
        return
    if method == "tools/call":
        params = message.get("params") or {}
        result = call_tool(params.get("name", ""), params.get("arguments") or {})
        failed = result.get("ok") is False
        reply(request_id, {
            "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
            "isError": failed,
        })
        return
    if not is_notification:
        reply_error(request_id, -32601, "Method not found: %s" % method)


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        try:
            handle(message)
        except Exception as exc:  # noqa: BLE001 - a bad call must not kill the server
            if message.get("id") is not None:
                reply_error(message["id"], -32603, "%s: %s" % (exc.__class__.__name__, exc))


if __name__ == "__main__":
    main()
