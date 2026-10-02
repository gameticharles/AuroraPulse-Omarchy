#!/usr/bin/env bash
# dev-sync.sh: copy this checkout into the live plugin directory and reload the
# shell, so what you are testing is what is in this tree.
#
#   tools/dev-sync.sh          sync and reload
#   tools/dev-sync.sh --check  report what would change, copy nothing
#   tools/dev-sync.sh --no-reload  sync only
#
# The plugin directory is the manifest id, and the shell loads plugins from
# disk, so a sync followed by a reload is the whole dev loop.
set -euo pipefail

PLUGIN_ID="aurora-pulse"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Third-party plugins are read from the *user* config tree, never from
# OMARCHY_PATH, which points at the system-wide shell install and is not
# writable. Getting this wrong is a permission error at the worst moment.
DEST="${AURORA_PULSE_DEST:-$HOME/.config/omarchy/plugins/$PLUGIN_ID}"

mode="sync"
case "${1:-}" in
  --check) mode="check" ;;
  --no-reload) mode="no-reload" ;;
  "") ;;
  *) echo "usage: $0 [--check|--no-reload]" >&2; exit 64 ;;
esac

# Never copy build detritus or VCS metadata into a directory the shell reads.
mapfile -t FILES < <(cd "$HERE" && find . \
  -type f \
  -not -path "./.git/*" \
  -not -path "*/__pycache__/*" \
  -not -name "*.pyc" \
  -not -name ".DS_Store" \
  -not -name "*.pyz" \
  | sort)

echo "source: $HERE"
echo "target: $DEST"

if [[ "$mode" == "check" ]]; then
  changed=0
  for rel in "${FILES[@]}"; do
    src="$HERE/$rel"; dst="$DEST/$rel"
    if [[ ! -f "$dst" ]] || ! cmp -s "$src" "$dst"; then
      echo "  would sync  ${rel#./}"
      changed=$((changed+1))
    fi
  done
  echo "$changed file(s) differ"
  exit 0
fi

mkdir -p "$DEST"
for rel in "${FILES[@]}"; do
  src="$HERE/$rel"; dst="$DEST/$rel"
  mkdir -p "$(dirname "$dst")"
  # Copy to a temp name and rename, so a half-written file is never read by a
  # shell that reloads mid-copy.
  tmp="$dst.tmp.$$"
  cp "$src" "$tmp"
  mv "$tmp" "$dst"
  # Keep the entry point executable; the shell spawns it directly.
  [[ "$rel" == "./ap-ctl" ]] && chmod 0755 "$dst"
done
echo "synced ${#FILES[@]} file(s)"

# Drop anything in the destination that is no longer in the source, or a
# removed file would linger and a stale QML would keep being loaded. The
# destination's own .git is never touched: the plugin directory may be a clone
# of its own, and this sweep once deleted a file out of it.
while IFS= read -r old; do
  rel="${old#$DEST/}"
  if [[ -n "$rel" && ! -f "$HERE/$rel" ]]; then
    rm -f "$old" && echo "  removed  ${rel}"
  fi
done < <(find "$DEST" -type f -not -path "*/__pycache__/*" \
            -not -path "$DEST/.git/*" 2>/dev/null)

# Remove leftover __pycache__ directories. ap-ctl sets dont_write_bytecode so it
# never creates one, but anything written before that - or by a hand-run python
# in the tree - would sit in the plugin directory, which the shell watches, and
# make it tear itself down and reload on the next start. The stale-file sweep
# above skips these paths on purpose, so they need clearing explicitly.
find "$DEST" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true

if [[ "$mode" == "no-reload" ]]; then
  echo "skipped shell reload"
  exit 0
fi

# Re-create the plugin so the shell actually reads the new files.
#
# This used to call `omarchy-shell -q shell reload`, which does not exist, and
# then fall back to `shell ping`, which always succeeds - so it printed
# "shell reloaded" every single time and nothing had been reloaded. The symptom
# was a plugin that looked synced on disk and stayed visibly stale in the bar,
# for hours, while every dev-sync claimed success.
#
# The shell does notice changed files ("Local plugin changed, reloading"), but
# noticing is not the same as re-instantiating a component that is already
# alive, and the bar widget is always alive. Disable and enable is the
# documented way to make it rebuild, so that is what happens, and the result
# is checked rather than assumed.
if ! command -v omarchy-shell >/dev/null 2>&1 || ! command -v omarchy >/dev/null 2>&1; then
  echo "omarchy-shell not on PATH; the shell will pick this up on next start" >&2
  exit 0
fi

# `omarchy shell ping` is not a command - the IPC client is omarchy-shell -
# so asking omarchy whether the shell is alive printed a usage message and
# this branch then reported "not running" on a live desktop.
if ! omarchy-shell -q shell ping >/dev/null 2>&1; then
  echo "shell not running; it will pick this up on next start" >&2
  exit 0
fi

before="$(pgrep -f "$PLUGIN_ID/ap-ctl daemon" | head -1 || true)"
omarchy plugin disable "$PLUGIN_ID" >/dev/null 2>&1 || true
sleep 1
omarchy plugin enable "$PLUGIN_ID" >/dev/null 2>&1 || true
sleep 2
after="$(pgrep -f "$PLUGIN_ID/ap-ctl daemon" | head -1 || true)"

# Whether the widget LOADED is a different question from whether a daemon is
# running, and only the second one was being asked. The widget failed to load
# for hours - a duplicate property the shell reported on every reload - while
# dev-sync printed "plugin re-created" and the daemon from an earlier session
# sat there satisfying the pid check the whole time. Audio kept playing and
# there was no icon anywhere, which read exactly like "synced but stale".
#
# The shell is the only thing that knows, and it says so in its log. Quickshell
# caches a component that failed to compile, keyed by URL, so it replays the
# original error no matter what is on disk now - which is also why the line
# number in that message stops matching the file.
load_error=""
if command -v journalctl >/dev/null 2>&1; then
  load_error="$(journalctl --user --since '-20s' --no-pager 2>/dev/null \
    | grep -i "Plugin widget $PLUGIN_ID failed" | tail -1 || true)"
fi
if [[ -n "$load_error" ]]; then
  echo "WARNING: the shell could not load the widget:" >&2
  echo "  ${load_error#*omarchy-shell}" >&2
  echo "  A stale Quickshell component cache replays the old error even after" >&2
  echo "  a fix; run 'omarchy-restart-shell' if the file on disk looks correct." >&2
fi

if [[ -n "$after" && "$after" != "$before" ]]; then
  echo "plugin re-created (daemon $before -> $after)"
elif [[ -n "$after" ]]; then
  # Same pid is fine if the widget was already running and the shell reloaded
  # it in place, but say so rather than implying a restart happened.
  echo "plugin re-enabled (daemon $after already running)"
else
  echo "WARNING: plugin enabled but no daemon is running; open the widget" >&2
fi
