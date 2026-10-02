#!/usr/bin/env bash
# preview.sh: render preview.png (the marketplace image) from docs/preview/.
#
# The screenshots beside preview.html are 500 px crops of the open panel;
# replace them to refresh the image, then run this.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../docs/preview"
out=$(mktemp --suffix=.png)
chromium --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=1 \
  --window-size=2000,1250 --screenshot="$out" "file://$PWD/preview.html" 2>/dev/null
magick "$out" -strip -define png:compression-level=9 ../../preview.png
rm -f "$out"
echo "wrote preview.png"
