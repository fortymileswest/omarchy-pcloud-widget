# pCloud widget for the Omarchy bar

Manage your pCloud connection and files from the Omarchy top bar: storage
stats, instant search across the whole account, a folder browser,
drag-and-drop upload and one-click share links. Signing in mounts the drive
locally through rclone, with a local cache.

Built for Omarchy 4.x, whose bar is the Quickshell-based `omarchy-shell`.

Made by [fortymileswest](https://fortymileswest.co.uk).

## Install

```bash
omarchy pkg add rclone                                              # required for the mounted drive
omarchy plugin add https://github.com/fortymileswest/omarchy-pcloud-widget --enable
```

Or clone it yourself first:

```bash
git clone https://github.com/fortymileswest/omarchy-pcloud-widget.git
cd omarchy-pcloud-widget
./install.sh                                  # copy into ~/.config/omarchy/plugins/
omarchy pkg add rclone                        # required for the mounted drive
omarchy plugin enable fortymileswest.pcloud   # add it to the bar
```

Then click the cloud icon in the bar and press **Connect pCloud**.

`install.sh` copies rather than symlinks, because Omarchy rejects symlinks
inside a plugin folder. Re-run it after editing anything here, then restart
the shell with `omarchy-restart-shell`. `rescanPlugins` re-reads manifests
but doesn't reload plugin QML, so edits stay invisible until the shell
actually restarts.

## How your credentials are handled

You never type a password into the widget. Signing in runs
`rclone authorize pcloud`, which opens pCloud's own consent page in your
browser using rclone's client registration, and the widget only ever sees
the token that comes back. pCloud no longer issues tokens for the older
username/password digest login, so that path has been removed entirely.

The OAuth token is stored in your login keyring via `secret-tool`
(gnome-keyring), not in a config file. The whole token document is kept,
refresh token included, so the grant can be renewed rather than breaking
when it expires.

Nothing secret is ever passed as a command-line argument, since
`/proc/<pid>/cmdline` is readable by every process on the machine. The token
is read from the keyring inside the helper, and share links reach the
clipboard through `wl-copy`'s stdin rather than the command line.

rclone gets the token through the environment, not `rclone.conf`.
`RCLONE_CONFIG_PCLOUD_*` defines an ad-hoc remote, and `/proc/<pid>/environ`
is owner-only, so no plaintext credential is written to disk at all.

Two-factor authentication and any new-device email check are handled by
pCloud's own sign-in page, not by this widget.

Signing out clears the keyring entry and deletes the local search index. It
also calls pCloud's `logout` method, but that's really meant for the older
digest tokens, so if you want to be certain an OAuth grant is revoked,
remove the rclone entry under Settings in your pCloud account as well.

Non-secret settings (email, region, mount point) live in
`~/.config/omarchy/pcloud/config.json`, mode 0600.

## Using it

| Where | Action |
|---|---|
| Bar icon | left = open panel, right = refresh, middle = mount/unmount |
| Hero switch | mount or unmount the drive |
| Search box | type to search the whole account; Esc clears, then closes |
| Recent / Browse | recent files, or walk folders from the root |
| Row | click to open, right-click to reveal in the file manager, 󰌷 copies a share link |
| Below results | show the next 50 matches |
| Anywhere in the panel | drop files to upload them |
| ↑ ↓ / Enter | move the cursor and activate |

The icon is dim when signed out, half-bright when signed in but unmounted,
and full brightness once the drive is mounted.

Uploads go to the folder you're browsing, or to the account root from the
Recent and search views.

### Search

pCloud has no server-side search endpoint, so the widget pulls the whole
folder tree once into a SQLite index at `~/.cache/omarchy/pcloud/index.db`
and searches that locally instead of doing a round trip per keystroke.
Matching is substring-first, then subsequence, so `rprt24` finds
`report-2024.pdf`. Results come back 50 at a time.

Names and paths carry FTS5 trigram indexes, which turn "contains X" into an
index probe rather than a scan. On a 583k-row account a query takes about
35 ms end to end. The trigram indexes cost roughly four times the text on
disk (that account's index is around 455 MB), which is a trade worth making
for the speed.

Building the index costs one API call per top-level folder, because pCloud
refuses `listfolder?recursive=1` at the account root once the tree gets
large (error 1101), while still accepting it one level down. On a large
account, expect a rebuild to take about a minute; 󰑐 rebuilds on demand.
Uploads are folded into the existing index in milliseconds rather than
triggering a full rebuild.

## Settings

Configure via `omarchy bar set fortymileswest.pcloud <key> <value>`:

| Key | Default | Meaning |
|---|---|---|
| `refreshIntervalSec` | `90` | Status poll interval |
| `autoMount` | `true` | Mount the drive after signing in |
| `mountPoint` | `~/pCloudDrive` | Where the drive appears |
| `cacheMaxSize` | `5G` | rclone VFS cache limit |
| `recentLimit` | `20` | Recent files shown |

The mount uses `--vfs-cache-mode full`, so opened files are cached locally
and writes are written back.

## IPC

```bash
omarchy-shell pcloud status     # connection summary
omarchy-shell pcloud open       # open the panel
omarchy-shell pcloud mount      # mount / unmount
omarchy-shell pcloud reindex    # rebuild the search index
```

## MCP server

`mcp_server.py` exposes the same account to Claude Code over stdio, reusing
the keyring token, so there's no second credential and no third-party code
involved:

```bash
claude mcp add pcloud --scope user -- python3 "$PWD/mcp_server.py"
```

Tools: `pcloud_status`, `pcloud_search`, `pcloud_list`, `pcloud_share_link`,
`pcloud_upload`, `pcloud_reindex`. There's deliberately no delete, move or
rename tool, so a stray call can't destroy anything. Edit through the
mounted drive instead.

## Files

| File | Role |
|---|---|
| `manifest.json` | Plugin declaration and settings schema |
| `pcloud.py` | All API, keyring, SQLite index and rclone work; every subcommand prints one JSON object |
| `Service.qml` | State and subprocesses; the panel spawns nothing itself |
| `Panel.qml` | Bar button, popup UI, keyboard cursor, drop target |
| `PcloudIcon.qml` | The cloud mark, drawn as vector shapes so it scales with the bar |
| `Model.js` | Pure formatting and parsing helpers |
| `mcp_server.py` | Dependency-free MCP front end for `pcloud.py` |

## Troubleshooting

- **"rclone is not installed"**: run `omarchy pkg add rclone`.
- **Mount fails**: check `~/.cache/omarchy/pcloud/mount.log`. The mount
  point has to be empty, since the widget refuses to mount over existing
  files.
- **"Session expired"**: press **Connect pCloud** to authorise again.
- **Nothing in search**: press 󰑐 to rebuild the index.
- **Widget missing or unchanged after an edit**: run `./install.sh` and then
  `omarchy-restart-shell`. `rescanPlugins` alone won't reload plugin QML.
  Check `journalctl --user -e | grep -i pcloud` for load errors.

## Licence

MIT, see [LICENSE](LICENSE).
