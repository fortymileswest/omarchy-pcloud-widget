#!/usr/bin/env bash
# Copy this plugin into the Omarchy shell's plugin directory and reload it.
# Omarchy rejects symlinks inside a plugin folder, so development happens
# here and this script publishes a plain copy.
set -euo pipefail

id="fortymileswest.pcloud"
src="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dest="$HOME/.config/omarchy/plugins/$id"

mkdir -p "$dest"
for file in manifest.json pcloud.py mcp_server.py Model.js Service.qml Panel.qml PcloudIcon.qml README.md; do
  [ -f "$src/$file" ] && install -m 0644 "$src/$file" "$dest/$file"
done
chmod 0755 "$dest/pcloud.py"

# Saving under ~/.config/omarchy/plugins/ hot-reloads plugin code, but ask
# explicitly so a first install is picked up without waiting on the watcher.
omarchy-shell shell rescanPlugins >/dev/null 2>&1 || true
echo "Installed $id to $dest"
