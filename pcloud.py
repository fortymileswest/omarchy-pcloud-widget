#!/usr/bin/env python3
"""Backend helper for the Omarchy pCloud bar widget.

Every subcommand prints a single JSON object on stdout and exits 0 when the
call itself completed. Failures are reported as {"ok": false, "error": ...}
rather than tracebacks, because the QML side only ever reads JSON.

Secrets never touch disk and never touch argv (/proc/<pid>/cmdline is world
readable). Sign-in goes through `rclone authorize pcloud`, an OAuth browser
flow, so no password ever reaches this process; pCloud no longer issues tokens
for the older username/password digest login. The resulting access token lives
in the login keyring via secret-tool, and rclone is handed it through the
environment, which is owner-only on Linux, so no plaintext credential is
written to rclone.conf either.
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import time

# urllib.request costs ~22ms to import and uuid is only needed for uploads;
# neither is touched by a search, which is the command run on every keystroke.
# They are imported inside the functions that need them instead.

# --------------------------------------------------------------------------
# paths and constants

HOME = os.path.expanduser("~")
CONFIG_DIR = os.path.join(HOME, ".config", "omarchy", "pcloud")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
CACHE_DIR = os.path.join(HOME, ".cache", "omarchy", "pcloud")
INDEX_PATH = os.path.join(CACHE_DIR, "index.json")   # legacy, removed on sight
INDEX_DB = os.path.join(CACHE_DIR, "index.db")
LOG_PATH = os.path.join(CACHE_DIR, "mount.log")
# Last good status payload, so a freshly started shell can paint the panel
# before the round-trip to pCloud comes back.
STATUS_CACHE = os.path.join(CACHE_DIR, "status.json")

# Non-secret account metadata lives in config.json; the token lives in the
# keyring under this schema. Keep the attribute set stable: changing it
# orphans the stored secret.
KEYRING_ATTRS = ["service", "omarchy-pcloud", "key", "token"]

# pCloud runs two independent regions with separate API hosts. An account
# exists in exactly one of them, so login probes both.
HOSTS = ["api.pcloud.com", "eapi.pcloud.com"]

DEFAULT_MOUNT = os.path.join(HOME, "pCloudDrive")
TIMEOUT = 25

# Fixed trusted executable paths to prevent PATH injection attacks.
# Validate file/parent ownership and write permissions before use.
TRUSTED_COMMANDS = {
    "secret-tool": ["/usr/bin/secret-tool", "/bin/secret-tool"],
    "rclone": ["/usr/bin/rclone", "/usr/local/bin/rclone", "/opt/homebrew/bin/rclone"],
    "fusermount3": ["/usr/bin/fusermount3", "/bin/fusermount3"],
    "fusermount": ["/usr/bin/fusermount", "/bin/fusermount"],
    "umount": ["/usr/bin/umount", "/bin/umount"],
}

# Maximum response size in bytes to prevent memory exhaustion attacks.
MAX_RESPONSE_SIZE = 50 * 1024 * 1024  # 50 MB


def find_trusted_command(name):
    """Find the first trusted executable path that exists and is safe."""
    current_uid = os.getuid()
    for path in TRUSTED_COMMANDS.get(name, []):
        if not os.path.exists(path):
            continue
        try:
            stat = os.stat(path)
            # Reject if world-writable or group-writable
            if stat.st_mode & 0o022:
                continue
            # Accept if owned by root or current user
            if stat.st_uid not in (0, current_uid):
                continue
            # Check parent directory: reject if world or group writable
            parent = os.path.dirname(path)
            parent_stat = os.stat(parent)
            if parent_stat.st_mode & 0o022:
                continue
            return path
        except (OSError, AttributeError):
            continue
    return None


def minimal_env():
    """Create a minimal environment to prevent credential leakage."""
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": HOME,
        "USER": os.environ.get("USER", ""),
    }
    # Session locators, not secrets: secret-tool needs the D-Bus session
    # address to reach the keyring daemon, and rclone's FUSE mount needs
    # XDG_RUNTIME_DIR. Passing these through does not weaken the credential
    # -leakage protection this minimal environment otherwise provides.
    for key in ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env

# pCloud result codes we branch on.
ERR_INVALID_REQUEST = 1101
ERR_TOKEN_INVALID = 1000
ERR_TOKEN_EXPIRED = 2094


class PcloudError(Exception):
    def __init__(self, message, code=0, extra=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.extra = extra or {}


# --------------------------------------------------------------------------
# small utilities


def emit(payload):
    payload.setdefault("ok", True)
    sys.stdout.write(json.dumps(payload))
    sys.stdout.write("\n")


def fail(message, **extra):
    payload = {"ok": False, "error": str(message)}
    payload.update(extra)
    sys.stdout.write(json.dumps(payload))
    sys.stdout.write("\n")
    sys.exit(0)


def read_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_config(data):
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_PATH)


def config_get(key, fallback=None):
    value = read_config().get(key)
    return fallback if value is None else value


def config_set(**pairs):
    data = read_config()
    data.update(pairs)
    write_config(data)
    return data


# --------------------------------------------------------------------------
# keyring


def keyring_available():
    return find_trusted_command("secret-tool") is not None


def token_load():
    cmd = find_trusted_command("secret-tool")
    if not cmd:
        return ""
    try:
        done = subprocess.run(
            [cmd, "lookup"] + KEYRING_ATTRS,
            capture_output=True, text=True, timeout=10, env=minimal_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    # secret-tool exits non-zero when the item simply isn't there.
    return done.stdout.strip() if done.returncode == 0 else ""


def token_store(token):
    cmd = find_trusted_command("secret-tool")
    if not cmd:
        raise PcloudError("secret-tool is not installed, cannot store the token securely")
    done = subprocess.run(
        [cmd, "store", "--label=Omarchy pCloud auth token"] + KEYRING_ATTRS,
        input=token, capture_output=True, text=True, timeout=15, env=minimal_env(),
    )
    if done.returncode != 0:
        raise PcloudError((done.stderr or "could not write to the keyring").strip())


def token_clear():
    cmd = find_trusted_command("secret-tool")
    if not cmd:
        return
    subprocess.run(
        [cmd, "clear"] + KEYRING_ATTRS,
        capture_output=True, text=True, timeout=10, env=minimal_env(),
    )


# --------------------------------------------------------------------------
# pCloud API


def api_host():
    host = config_get("host", "")
    return host if host in HOSTS else HOSTS[0]


def api_call(method, params=None, host=None, token=None):
    """GET a pCloud API method and return its decoded payload.

    The auth token goes in the Authorization header rather than the query
    string so it stays out of any intermediary's request log.
    Responses are capped at MAX_RESPONSE_SIZE to prevent memory exhaustion.
    """
    import urllib.parse
    import urllib.request

    host = host or api_host()
    query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v not in (None, "")})
    url = "https://%s/%s%s" % (host, method, ("?" + query) if query else "")
    request = urllib.request.Request(url, headers={"User-Agent": "omarchy-pcloud-widget/1.0"})
    if token:
        request.add_header("Authorization", "Bearer " + token)
    # Building the index is hundreds of sequential calls, so one dropped
    # connection must not throw the whole walk away.
    body = None
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                # Read incrementally with a size limit to prevent memory exhaustion
                chunks = []
                total = 0
                while True:
                    chunk = response.read(65536)  # 64 KB chunks
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_RESPONSE_SIZE:
                        raise PcloudError("Response exceeds maximum allowed size (%d bytes)" % MAX_RESPONSE_SIZE)
                    chunks.append(chunk)
                body = b"".join(chunks).decode("utf-8", "replace")
            break
        except PcloudError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            last = exc
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    if body is None:
        raise PcloudError("Could not reach %s (%s)" % (host, last.__class__.__name__))

    try:
        data = json.loads(body)
    except ValueError:
        raise PcloudError("pCloud returned a response that was not JSON")

    code = int(data.get("result", 0) or 0)
    if code != 0:
        raise PcloudError(data.get("error") or ("pCloud error %d" % code), code, data)
    return data


def api_upload(host, token, folderid, paths, progress=None):
    """multipart/form-data upload; urllib has no multipart helper, so build it.

    Upload responses are capped at MAX_RESPONSE_SIZE to prevent memory exhaustion.
    """
    import urllib.parse
    import urllib.request
    import uuid

    boundary = "----omarchy" + uuid.uuid4().hex
    chunks = []
    for path in paths:
        name = os.path.basename(path)
        with open(path, "rb") as handle:
            content = handle.read()
        chunks.append(
            ("--%s\r\n" % boundary).encode()
            + ('Content-Disposition: form-data; name="file"; filename="%s"\r\n'
               % name.replace('"', "")).encode()
            + b"Content-Type: application/octet-stream\r\n\r\n"
            + content
            + b"\r\n"
        )
        if progress:
            progress(name)
    body = b"".join(chunks) + ("--%s--\r\n" % boundary).encode()

    url = "https://%s/uploadfile?%s" % (
        host, urllib.parse.urlencode({"folderid": folderid, "nopartial": 1}))
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "multipart/form-data; boundary=" + boundary)
    request.add_header("Authorization", "Bearer " + token)
    request.add_header("User-Agent", "omarchy-pcloud-widget/1.0")
    try:
        with urllib.request.urlopen(request, timeout=max(TIMEOUT, 120)) as response:
            # Read incrementally with a size limit to prevent memory exhaustion
            resp_chunks = []
            total = 0
            while True:
                chunk = response.read(65536)  # 64 KB chunks
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_RESPONSE_SIZE:
                    raise PcloudError("Upload response exceeds maximum allowed size (%d bytes)" % MAX_RESPONSE_SIZE)
                resp_chunks.append(chunk)
            resp_body = b"".join(resp_chunks).decode("utf-8", "replace")
            data = json.loads(resp_body)
    except PcloudError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PcloudError("Upload failed (%s)" % exc.__class__.__name__)
    code = int(data.get("result", 0) or 0)
    if code != 0:
        raise PcloudError(data.get("error") or ("pCloud error %d" % code), code, data)
    return data


def token_blob():
    """The stored OAuth token as a dict, however it was written.

    Older versions stored the bare access token, so a value that is not JSON
    is treated as one.
    """
    raw = token_load().strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {"access_token": raw, "token_type": "bearer"}


def require_token():
    token = str(token_blob().get("access_token") or "")
    if not token:
        raise PcloudError("Not logged in")
    return token


# --------------------------------------------------------------------------
# login


def rclone_authorize():
    """Run `rclone authorize pcloud` and return the OAuth token it prints.

    rclone ships its own pCloud client registration, opens the consent page in
    the browser, and prints the resulting token between paste markers. Using it
    means the widget never handles a password and never needs its own client
    secret. pCloud stopped issuing tokens for the older digest login, which is
    why that path is gone.
    """
    cmd = find_trusted_command("rclone")
    if not cmd:
        raise PcloudError("rclone is not installed")
    try:
        done = subprocess.run(
            [cmd, "authorize", "pcloud"],
            capture_output=True, text=True, timeout=600, env=minimal_env(),
        )
    except subprocess.TimeoutExpired:
        raise PcloudError("Timed out waiting for the browser sign-in")
    except OSError as exc:
        raise PcloudError("Could not run rclone (%s)" % exc.__class__.__name__)

    blob = ""
    for stream in (done.stdout or "", done.stderr or ""):
        match = re.search(r'\{[^{}]*"access_token"[^{}]*\}', stream)
        if match:
            blob = match.group(0)
            break
    if not blob:
        detail = (done.stderr or done.stdout or "").strip()
        if "Failed to get token" in detail or done.returncode != 0:
            raise PcloudError((detail or "Browser sign-in did not complete")[:300])
        raise PcloudError("rclone did not return a token")

    try:
        token = json.loads(blob)
    except ValueError:
        raise PcloudError("rclone returned a token that was not JSON")
    if not str(token.get("access_token") or ""):
        raise PcloudError("rclone returned an empty token")
    return token


def cmd_connect(_argv):
    """Authorise through the browser and store the resulting token."""
    if not find_trusted_command("rclone"):
        fail("rclone is not installed", needsRclone=True)
    try:
        token = rclone_authorize()
    except PcloudError as exc:
        fail(str(exc))
    access = str(token.get("access_token") or "")

    # pCloud runs two independent regions and the OAuth consent page does not
    # say which one the account lives in, so ask both which accepts the token.
    last = None
    for host in HOSTS:
        try:
            info = api_call("userinfo", host=host, token=access)
        except PcloudError as exc:
            last = exc
            continue
        try:
            token_store(json.dumps(token))
        except PcloudError as exc:
            fail(str(exc))
        config_set(host=host, account=info.get("email", ""),
                   userid=info.get("userid", 0))
        emit({
            "loggedIn": True,
            "host": host,
            "email": info.get("email", ""),
            "quota": info.get("quota", 0),
            "usedquota": info.get("usedquota", 0),
        })
        return
    fail("Signed in, but neither pCloud region accepted the token%s"
         % (": %s" % last if last else ""))


def cmd_logout(_argv):
    token = token_load()
    if token:
        try:
            api_call("logout", token=token)
        except PcloudError:
            # The local token is being discarded regardless; a failed remote
            # revoke should not block that.
            pass
    token_clear()
    config_set(account="", userid=0)
    for path in (INDEX_PATH, INDEX_DB, STATUS_CACHE):
        try:
            os.remove(path)
        except OSError:
            pass
    emit({"loggedIn": False})


# --------------------------------------------------------------------------
# mount (rclone)


def mount_point():
    return os.path.expanduser(str(config_get("mountPoint", DEFAULT_MOUNT)))


def is_mounted(path=None):
    path = path or mount_point()
    real = os.path.realpath(path)
    try:
        with open("/proc/self/mounts", "r", encoding="utf-8") as handle:
            for line in handle:
                fields = line.split()
                if len(fields) > 1:
                    # /proc/mounts octal-escapes space, tab, newline and
                    # backslash. Decoding those escapes only leaves the rest of
                    # the field alone; round-tripping through unicode_escape
                    # instead would reinterpret UTF-8 as latin-1 and mangle any
                    # non-ASCII mount point.
                    target = re.sub(r"\\([0-7]{3})",
                                    lambda m: chr(int(m.group(1), 8)), fields[1])
                    if os.path.realpath(target) == real:
                        return True
    except OSError:
        pass
    return False


def rclone_env():
    """Define the remote entirely through the environment.

    RCLONE_CONFIG_<REMOTE>_<KEY> creates an ad-hoc remote, so the token never
    lands in rclone.conf. /proc/<pid>/environ is 0400 owner-only, unlike argv.

    The whole token document is handed over, refresh token and expiry
    included, so rclone can renew the grant itself. Passing only the access
    token would leave the mount unable to recover once it expired.

    Uses a minimal, controlled environment to prevent credential leakage.
    """
    blob = token_blob()
    token = dict(blob) if blob else {}
    token.setdefault("token_type", "bearer")
    # A zero expiry tells rclone the token does not expire on its own; only
    # claim that when the authorisation really carried no expiry.
    token.setdefault("expiry", "0001-01-01T00:00:00Z")
    # Start with minimal environment to prevent credential leakage through inherited variables
    env = dict(minimal_env())
    env["RCLONE_CONFIG_PCLOUD_TYPE"] = "pcloud"
    env["RCLONE_CONFIG_PCLOUD_HOSTNAME"] = api_host()
    env["RCLONE_CONFIG_PCLOUD_TOKEN"] = json.dumps(token)
    return env


def cmd_mount(_argv):
    rclone_cmd = find_trusted_command("rclone")
    if not rclone_cmd:
        fail("rclone is not installed", needsRclone=True)
    token = require_token()
    target = mount_point()
    if is_mounted(target):
        emit({"mounted": True, "mountPoint": target})
        return

    os.makedirs(target, exist_ok=True)
    if os.listdir(target):
        fail("%s is not empty, refusing to mount over it" % target)
    os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)

    command = [
        rclone_cmd, "mount", "pcloud:", target,
        "--vfs-cache-mode", "full",
        "--vfs-cache-max-size", str(config_get("cacheMaxSize", "5G")),
        "--vfs-cache-max-age", str(config_get("cacheMaxAge", "24h")),
        "--vfs-read-chunk-size", "32M",
        "--dir-cache-time", "1h",
        "--poll-interval", "1m",
        "--cache-dir", os.path.join(CACHE_DIR, "vfs"),
        "--umask", "022",
        "--log-level", "NOTICE",
        "--log-file", LOG_PATH,
        "--daemon",
    ]
    try:
        done = subprocess.run(command, env=rclone_env(), capture_output=True,
                              text=True, timeout=45)
    except (OSError, subprocess.TimeoutExpired) as exc:
        fail("Could not start rclone (%s)" % exc.__class__.__name__)

    if done.returncode != 0:
        fail((done.stderr or done.stdout or "rclone mount failed").strip()[:400])

    # --daemon returns as soon as the child forks; the mount appears a moment
    # later. Poll briefly so the caller gets a truthful answer.
    for _ in range(40):
        if is_mounted(target):
            emit({"mounted": True, "mountPoint": target})
            return
        time.sleep(0.25)
    fail("rclone started but %s never became a mount point" % target, logPath=LOG_PATH)


def cmd_unmount(_argv):
    target = mount_point()
    if not is_mounted(target):
        emit({"mounted": False, "mountPoint": target})
        return
    for cmd_name in ("fusermount3", "fusermount", "umount"):
        cmd = find_trusted_command(cmd_name)
        if not cmd:
            continue
        # fusermount/fusermount3 use -u flag, umount does not
        args = [cmd, "-u", target] if cmd_name.startswith("fuser") else [cmd, target]
        done = subprocess.run(args, capture_output=True, text=True, timeout=20, env=minimal_env())
        if done.returncode == 0:
            emit({"mounted": False, "mountPoint": target})
            return
    fail("Could not unmount %s" % target)


# --------------------------------------------------------------------------
# search index


def index_open(readonly=True):
    """Open the search index, or return None when it has not been built.

    The index is SQLite rather than one JSON blob: a large account produces
    hundreds of thousands of rows, and re-parsing that on every keystroke cost
    over a second per query. SQLite scans the small `names` table in C and
    touches the wide `entries` row only for the handful of hits it returns.
    """
    if not os.path.exists(INDEX_DB):
        return None
    try:
        if readonly:
            connection = sqlite3.connect("file:%s?mode=ro" % INDEX_DB, uri=True)
        else:
            connection = sqlite3.connect(INDEX_DB)
        connection.row_factory = sqlite3.Row
        # Read straight out of the page cache on repeat queries.
        connection.execute("PRAGMA mmap_size = 268435456")
        return connection
    except sqlite3.Error:
        return None


def index_meta():
    """Counters for the status payload; {} when there is no index yet."""
    connection = index_open()
    if connection is None:
        return {}
    try:
        rows = connection.execute("SELECT key, value FROM meta").fetchall()
        return {row["key"]: row["value"] for row in rows}
    except sqlite3.Error:
        return {}
    finally:
        connection.close()


INDEX_SCHEMA = """
    PRAGMA journal_mode = OFF;
    PRAGMA synchronous = OFF;
    CREATE TABLE meta(key TEXT PRIMARY KEY, value INTEGER);
    CREATE TABLE entries(
        rid INTEGER PRIMARY KEY, name TEXT, path TEXT, folder TEXT,
        isFolder INTEGER, id INTEGER, parentId INTEGER, size INTEGER,
        modified INTEGER, contentType TEXT);
    -- Matching scans these narrow tables instead of the wide one, which is
    -- what keeps a query in the low tens of milliseconds. Paths are kept
    -- apart from names because most queries never scan them, and a path is
    -- several times longer than a name.
    CREATE TABLE names(rid INTEGER PRIMARY KEY, n TEXT);
    -- Trigram indexes turn "contains X" from a scan of every row into an
    -- index probe: measured on a 583k-row account, the name lookup drops from
    -- ~60ms to under 3ms. They cost roughly 4x the text in disk, which is the
    -- trade this makes deliberately. The plain `names` table stays only
    -- because subsequence matching ("rprt24" -> "report-2024.pdf") puts
    -- wildcards between every character and so cannot use an index at all.
    CREATE VIRTUAL TABLE names_fts USING fts5(n, tokenize='trigram');
    CREATE VIRTUAL TABLE paths_fts USING fts5(p, tokenize='trigram');
"""

ENTRY_COLUMNS = ("name, path, folder, isFolder, id, parentId, size, modified, "
                 "contentType")


def entry_values(rid, row):
    return (rid, row["name"], row["path"], row["folder"],
            1 if row["isFolder"] else 0, row["id"], row["parentId"],
            row["size"], row["modified"], row["contentType"])


def index_insert(connection, rid, row):
    connection.execute("INSERT INTO entries VALUES (?,?,?,?,?,?,?,?,?,?)",
                       entry_values(rid, row))
    connection.execute("INSERT INTO names VALUES (?,?)", (rid, row["name"].lower()))
    connection.execute("INSERT INTO names_fts(rowid, n) VALUES (?,?)",
                       (rid, row["name"].lower()))
    connection.execute("INSERT INTO paths_fts(rowid, p) VALUES (?,?)",
                       (rid, row["path"].lower()))


class IndexWriter(object):
    """Streams rows into a fresh index, then swaps it in atomically.

    Rows are written as the walk produces them rather than collected first: a
    large account is hundreds of thousands of rows, and holding them all as
    dicts before writing cost hundreds of megabytes of memory for no gain.
    """

    def __init__(self):
        os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
        self.path = INDEX_DB + ".tmp"
        for leftover in (self.path, self.path + "-journal"):
            try:
                os.remove(leftover)
            except OSError:
                pass
        self.connection = sqlite3.connect(self.path)
        self.connection.executescript(INDEX_SCHEMA)
        self.rid = 0
        self.files = 0
        self.folders = 0
        self.bytes = 0

    def add(self, row):
        index_insert(self.connection, self.rid, row)
        self.rid += 1
        if row["isFolder"]:
            self.folders += 1
        else:
            self.files += 1
            self.bytes += row["size"]

    def finish(self):
        stats = {
            "builtAt": int(time.time()),
            "count": self.rid,
            "fileCount": self.files,
            "folderCount": self.folders,
            "bytes": self.bytes,
        }
        self.connection.executemany("INSERT INTO meta VALUES (?,?)", stats.items())
        # Built after the inserts: creating it up front would pay to keep the
        # tree balanced on every row.
        self.connection.execute(
            "CREATE INDEX entries_recent ON entries(isFolder, modified DESC)")
        self.connection.commit()
        self.connection.close()
        os.chmod(self.path, 0o600)
        os.replace(self.path, INDEX_DB)
        # A stale JSON index from an older version would just waste disk.
        try:
            os.remove(INDEX_PATH)
        except OSError:
            pass
        return stats

    def abort(self):
        try:
            self.connection.close()
        except sqlite3.Error:
            pass
        try:
            os.remove(self.path)
        except OSError:
            pass


def index_add(rows):
    """Fold freshly uploaded files into the existing index.

    Rebuilding from scratch costs a call per top-level folder - about a minute
    on a large account - which is far too much to pay for dropping one file.
    Returns False when there is no index to update, so the caller can decide
    whether a full build is warranted.
    """
    connection = index_open(readonly=False)
    if connection is None:
        return False
    try:
        rid = connection.execute(
            "SELECT COALESCE(MAX(rid), -1) FROM entries").fetchone()[0] + 1
        added_rows = 0
        added_files = 0
        added_bytes = 0
        for row in rows:
            # Re-uploading a file replaces its row rather than duplicating it.
            for stale in connection.execute(
                    "SELECT rid, isFolder, size FROM entries WHERE path = ?",
                    (row["path"],)).fetchall():
                for table in ("entries", "names", "names_fts", "paths_fts"):
                    key = "rid" if table in ("entries", "names") else "rowid"
                    connection.execute("DELETE FROM %s WHERE %s = ?" % (table, key),
                                       (stale["rid"],))
                added_rows -= 1
                if not stale["isFolder"]:
                    added_files -= 1
                    added_bytes -= stale["size"]
            index_insert(connection, rid, row)
            rid += 1
            added_rows += 1
            if not row["isFolder"]:
                added_files += 1
                added_bytes += row["size"]
        connection.execute(
            "UPDATE meta SET value = value + ? WHERE key = 'count'", (added_rows,))
        connection.execute(
            "UPDATE meta SET value = value + ? WHERE key = 'fileCount'", (added_files,))
        connection.execute(
            "UPDATE meta SET value = value + ? WHERE key = 'bytes'", (added_bytes,))
        connection.commit()
        return True
    except sqlite3.Error:
        return False
    finally:
        connection.close()


def like_escape(text):
    for character in ("\\", "%", "_"):
        text = text.replace(character, "\\" + character)
    return text


def flatten(entry, prefix, sink):
    """Walk a recursive listfolder payload, handing each row to `sink`.

    A callback rather than a list so the index build can write rows straight
    out as it walks; callers that do want a list pass `list.append`.
    """
    for child in entry.get("contents") or []:
        name = str(child.get("name", ""))
        path = prefix + "/" + name if prefix else "/" + name
        is_folder = bool(child.get("isfolder"))
        sink({
            "name": name,
            "path": path,
            "folder": prefix or "/",
            "isFolder": is_folder,
            "id": int(child.get("folderid", 0)) if is_folder else int(child.get("fileid", 0)),
            "parentId": int(entry.get("folderid", 0) or 0),
            "size": 0 if is_folder else int(child.get("size", 0) or 0),
            "modified": int(child.get("modified_ts") or parse_ts(child.get("modified"))),
            "contentType": str(child.get("contenttype", "")),
        })
        if is_folder:
            flatten(child, path, sink)


def parse_ts(value):
    """pCloud returns RFC 1123 dates; fall back to 0 when absent."""
    if not value:
        return 0
    try:
        import email.utils
        parsed = email.utils.parsedate_to_datetime(str(value))
        return int(parsed.timestamp())
    except Exception:  # noqa: BLE001
        return 0


def collect_tree(token, folderid, prefix, sink):
    """Flatten a folder subtree into rows, one recursive call where possible.

    pCloud refuses `recursive=1` on a tree as large as a whole account root
    and answers 1101 "Invalid request" - but it accepts the same call on the
    folders one level down. So try recursive first and, when it is refused,
    descend a level and recurse per child. Small accounts cost one call;
    large ones cost one per top-level folder instead of one per folder.
    """
    try:
        data = api_call("listfolder", {"folderid": folderid, "recursive": 1},
                        token=token)
    except PcloudError as exc:
        if exc.code != ERR_INVALID_REQUEST:
            raise
        data = api_call("listfolder", {"folderid": folderid}, token=token)
        meta = data.get("metadata") or {}
        flatten(meta, prefix, sink)
        for child in meta.get("contents") or []:
            if not child.get("isfolder"):
                continue
            name = str(child.get("name", ""))
            path = prefix + "/" + name if prefix else "/" + name
            collect_tree(token, int(child.get("folderid", 0) or 0), path, sink)
        return
    flatten(data.get("metadata") or {}, prefix, sink)


def build_index(token):
    writer = IndexWriter()
    try:
        collect_tree(token, 0, "", writer.add)
    except BaseException:
        # A half-written index must not replace a good one.
        writer.abort()
        raise
    return writer.finish()


def cmd_reindex(_argv):
    index = build_index(require_token())
    emit({
        "builtAt": index["builtAt"],
        "count": index["count"],
        "fileCount": index["fileCount"],
        "folderCount": index["folderCount"],
        "bytes": index["bytes"],
    })


def score(needle, row):
    """Rank a row against a lowercased query. Higher is better, 0 = no match."""
    name = row["name"].lower()
    path = row["path"].lower()
    if needle in name:
        base = 1000 - name.index(needle) * 4 - max(0, len(name) - len(needle))
        if name == needle:
            base += 500
        elif name.startswith(needle):
            base += 250
        return max(1, base)
    if needle in path:
        return max(1, 400 - path.index(needle))
    # Subsequence fallback so "rprt24" still finds "report-2024.pdf".
    position = 0
    for character in needle:
        position = name.find(character, position)
        if position < 0:
            return 0
        position += 1
    return 120


# A single query never scores more than this many candidates. Only `limit`
# rows are ever shown, the stages already hand back their best-scoring kinds
# first, and fetching plus scoring candidates in Python costs far more than
# the indexed lookup that found them - so this cap, not the search itself, is
# what keeps a keystroke fast.
SEARCH_CANDIDATES = 600


def search_index(connection, query, limit):
    """Candidate rows for a lowercased query, best-matching kinds first."""
    # FTS5 only applies its trigram index to a plain two-argument LIKE: adding
    # ESCAPE turns the probe back into a full scan and costs ~150ms on a large
    # account. Queries containing a LIKE wildcard are rare, so escape only
    # those and let everything else take the indexed path.
    wildcard = any(character in query for character in ("%", "_", "\\"))
    if wildcard:
        pattern = like_escape(query)
        clause = " ESCAPE '\\'"
    else:
        pattern = query
        clause = ""
    substring = "%" + pattern + "%"
    # Escape per character, not by splitting the already-escaped string: doing
    # the latter drops a wildcard between a backslash and the character it
    # escapes, so "a_b" ends up asking for a literal "%" and matches nothing.
    subsequence = "%" + "%".join(like_escape(c) for c in query) + "%"

    seen = {}
    # Each stage costs a lookup, so later stages run only when the earlier,
    # better-scoring ones did not turn up enough rows to rank.
    enough = max(limit * 4, 64)

    def gather(table, column, argument):
        if len(seen) >= SEARCH_CANDIDATES:
            return
        sql = ("SELECT rowid FROM %s WHERE %s LIKE ?%s LIMIT ?"
               % (table, column, clause))
        for row in connection.execute(sql, (argument, SEARCH_CANDIDATES - len(seen))):
            seen[row[0]] = True

    # Trigrams need three characters; below that the index cannot help and a
    # plain scan of the names is the cheaper answer.
    if len(query) >= 3:
        gather("names_fts", "n", substring)      # name contains the query
    else:
        gather("names", "n", substring)
    if len(seen) < enough and len(query) >= 3:
        gather("paths_fts", "p", substring)      # somewhere in the path
    if len(seen) < enough:
        # Wildcards between every character, so no index applies: this one is
        # always a scan, and runs only when the indexed stages came up short.
        gather("names", "n", subsequence)        # "rprt24" -> "report-2024.pdf"
    if not seen:
        return []

    rids = list(seen.keys())
    out = []
    # SQLite caps host parameters, so fetch the candidates in blocks.
    for start in range(0, len(rids), 900):
        block = rids[start:start + 900]
        sql = ("SELECT name, path, folder, isFolder, id, parentId, size, "
               "modified, contentType FROM entries WHERE rid IN (%s)"
               % ",".join("?" * len(block)))
        for row in connection.execute(sql, block):
            item = dict(row)
            item["isFolder"] = bool(item["isFolder"])
            out.append(item)
    return out


def cmd_search(argv):
    query = (argv[0] if argv else "").strip().lower()
    limit = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 40

    connection = index_open()
    if connection is None:
        try:
            build_index(require_token())
        except PcloudError as exc:
            fail(str(exc))
        connection = index_open()
        if connection is None:
            fail("The search index could not be built")

    try:
        meta = {}
        for row in connection.execute("SELECT key, value FROM meta"):
            meta[row["key"]] = row["value"]
        if not query:
            emit({"results": [], "builtAt": meta.get("builtAt", 0),
                  "count": meta.get("count", 0)})
            return

        scored = []
        for row in search_index(connection, query, limit):
            value = score(query, row)
            if value:
                # Files rank above folders at equal relevance, newest first.
                scored.append((value, 0 if row["isFolder"] else 1,
                               row["modified"], row))
        scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        emit({
            "results": [item[3] for item in scored[:limit]],
            "total": len(scored),
            "builtAt": meta.get("builtAt", 0),
            "count": meta.get("count", 0),
        })
    finally:
        connection.close()


# --------------------------------------------------------------------------
# browse, links, upload, status


def cmd_list(argv):
    folderid = int(argv[0]) if argv and argv[0].lstrip("-").isdigit() else 0
    data = api_call("listfolder", {"folderid": folderid}, token=require_token())
    metadata = data.get("metadata") or {}
    rows = []
    flatten({"contents": metadata.get("contents") or [], "folderid": folderid},
            str(metadata.get("path", "") or ""), rows.append)
    rows.sort(key=lambda r: (not r["isFolder"], r["name"].lower()))
    emit({
        "folderId": folderid,
        "path": metadata.get("path", "/") or "/",
        "name": metadata.get("name", "/") or "/",
        "parentId": int(metadata.get("parentfolderid", 0) or 0),
        "entries": rows,
    })


def cmd_link(argv):
    if len(argv) < 2:
        fail("usage: link <file|folder> <id>")
    kind, ident = argv[0], argv[1]
    token = require_token()
    if kind == "folder":
        data = api_call("getfolderpublink", {"folderid": int(ident)}, token=token)
    else:
        data = api_call("getfilepublink", {"fileid": int(ident)}, token=token)
    emit({"link": data.get("link", ""), "code": data.get("code", "")})


def cmd_upload(argv):
    if len(argv) < 2:
        fail("usage: upload <folderid> <path> [path...]")
    folderid = int(argv[0])
    paths = []
    for raw in argv[1:]:
        path = uri_to_path(raw)
        if not path:
            continue
        if os.path.isdir(path):
            fail("%s is a folder, only files can be uploaded" % os.path.basename(path))
        if not os.path.isfile(path):
            fail("%s does not exist" % path)
        paths.append(path)
    if not paths:
        fail("Nothing to upload")

    token = require_token()
    uploaded = []
    fresh = []
    folder_path = ""
    for path in paths:
        data = api_upload(api_host(), token, folderid, [path])
        for item in data.get("metadata") or []:
            name = str(item.get("name", os.path.basename(path)))
            uploaded.append({
                "name": name,
                "fileid": int(item.get("fileid", 0) or 0),
                "size": int(item.get("size", 0) or 0),
            })
            if folder_path == "":
                folder_path = upload_folder_path(token, folderid)
            parent = "" if folder_path == "/" else folder_path
            fresh.append({
                "name": name,
                "path": parent + "/" + name,
                "folder": folder_path or "/",
                "isFolder": False,
                "id": int(item.get("fileid", 0) or 0),
                "parentId": int(item.get("parentfolderid", folderid) or folderid),
                "size": int(item.get("size", 0) or 0),
                "modified": int(item.get("modified_ts")
                                or parse_ts(item.get("modified"))),
                "contentType": str(item.get("contenttype", "")),
            })

    # The new files must show up in search straight away. Folding them into
    # the existing index is instant; a full rebuild is the fallback for when
    # there is no index yet.
    if fresh and not index_add(fresh):
        try:
            build_index(token)
        except PcloudError:
            pass
    emit({"uploaded": uploaded, "count": len(uploaded)})


def upload_folder_path(token, folderid):
    """Absolute path of the upload target, for the rows added to the index."""
    if folderid == 0:
        return "/"
    try:
        data = api_call("listfolder", {"folderid": folderid, "nofiles": 1},
                        token=token)
        return str((data.get("metadata") or {}).get("path", "") or "/")
    except PcloudError:
        return "/"


def uri_to_path(raw):
    value = str(raw or "").strip()
    if value.startswith("file://"):
        import urllib.parse
        parsed = urllib.parse.urlparse(value)
        return urllib.parse.unquote(parsed.path)
    return value


def status_cache_write(payload):
    try:
        os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
        tmp = STATUS_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.chmod(tmp, 0o600)
        os.replace(tmp, STATUS_CACHE)
    except OSError:
        pass


def cmd_status(argv):
    limit = int(argv[0]) if argv and argv[0].isdigit() else 20
    # `status <limit> cached` answers from the last good payload without
    # touching the network, so the panel has something to draw immediately.
    # The caller follows it with a live status; this never replaces that.
    if "cached" in argv:
        try:
            with open(STATUS_CACHE, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict):
                payload["cached"] = True
                emit(payload)
                return
        except (OSError, ValueError):
            pass
    config = read_config()
    target = mount_point()
    token = str(token_blob().get("access_token") or "")
    payload = {
        "keyringAvailable": keyring_available(),
        "rcloneInstalled": find_trusted_command("rclone") is not None,
        "authenticated": bool(token),
        "account": config.get("account", ""),
        "host": api_host(),
        "region": "EU" if api_host().startswith("e") else "US",
        "mountPoint": target,
        "mounted": is_mounted(target),
        "quota": 0,
        "usedquota": 0,
        "usagePercent": 0.0,
        "premium": False,
        "premiumExpires": "",
        "email": config.get("account", ""),
        "publicLinkQuota": 0,
        "recent": [],
        "indexedAt": 0,
        "fileCount": 0,
        "folderCount": 0,
        "statusText": "Not logged in",
    }
    if not token:
        emit(payload)
        return

    try:
        info = api_call("userinfo", token=token)
    except PcloudError as exc:
        if exc.code in (ERR_TOKEN_INVALID, ERR_TOKEN_EXPIRED):
            token_clear()
            payload["authenticated"] = False
            payload["statusText"] = "Session expired, log in again"
            emit(payload)
            return
        payload["statusText"] = "Offline"
        payload["lastError"] = str(exc)
        emit(payload)
        return

    quota = int(info.get("quota", 0) or 0)
    used = int(info.get("usedquota", 0) or 0)
    payload.update({
        "quota": quota,
        "usedquota": used,
        "usagePercent": (used / quota * 100.0) if quota else 0.0,
        "premium": bool(info.get("premium")),
        "premiumExpires": str(info.get("premiumexpires", "") or ""),
        "email": str(info.get("email", "") or config.get("account", "")),
        "publicLinkQuota": int(info.get("publiclinkquota", 0) or 0),
        "statusText": "Connected",
    })

    connection = index_open()
    if connection is not None:
        try:
            meta = {}
            for row in connection.execute("SELECT key, value FROM meta"):
                meta[row["key"]] = row["value"]
            payload["indexedAt"] = meta.get("builtAt", 0)
            payload["fileCount"] = meta.get("fileCount", 0)
            payload["folderCount"] = meta.get("folderCount", 0)
            recent = []
            for row in connection.execute(
                    "SELECT name, path, folder, isFolder, id, parentId, size, "
                    "modified, contentType FROM entries WHERE isFolder = 0 "
                    "ORDER BY modified DESC LIMIT ?", (limit,)):
                item = dict(row)
                item["isFolder"] = False
                recent.append(item)
            payload["recent"] = recent
        except sqlite3.Error:
            pass
        finally:
            connection.close()
    status_cache_write(payload)
    emit(payload)


COMMANDS = {
    "status": cmd_status,
    "connect": cmd_connect,
    "logout": cmd_logout,
    "search": cmd_search,
    "reindex": cmd_reindex,
    "list": cmd_list,
    "link": cmd_link,
    "upload": cmd_upload,
    "mount": cmd_mount,
    "unmount": cmd_unmount,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        fail("usage: pcloud.py {%s} [args]" % "|".join(sorted(COMMANDS)))
    try:
        COMMANDS[sys.argv[1]](sys.argv[2:])
    except PcloudError as exc:
        fail(exc.message, code=exc.code)
    except Exception as exc:  # noqa: BLE001 - the QML side must always get JSON
        fail("%s: %s" % (exc.__class__.__name__, exc))


if __name__ == "__main__":
    main()
