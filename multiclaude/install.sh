#!/usr/bin/env bash
# Installs the bambu add-in into a multiclaude checkout, or takes it out again.
#
#   multiclaude/install.sh [--root DIR] [--user NAME]    copy the add-in in
#   multiclaude/install.sh [--root DIR] --uninstall      turn it off and remove it
#
# DIR is the multiclaude checkout: $ASSISTANT_ROOT, or ~/assistant. NAME is the multiclaude user
# whose assistant drives the printer (default: the owner in users.json). The service itself runs
# from this clone of bambu-mcp. The add-in needs the service-notify add-in (the print and stop
# buttons in Telegram); `setup` stops with a pointer when it is missing.
set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
BAMBU_HOME="$(cd "$HERE/.." && pwd)"
ROOT="${ASSISTANT_ROOT:-$HOME/assistant}"
NAME=bambu
action=install user=
while [ $# -gt 0 ]; do
  case "$1" in
    --root) ROOT="${2:?--root needs a directory}"; shift 2 ;;
    --user) user="${2:?--user needs a name}"; shift 2 ;;
    --uninstall) action=uninstall; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

[ -f "$ROOT/bin/assistant" ] && [ -d "$ROOT/addons" ] || { echo "error: no multiclaude checkout at $ROOT (--root DIR)" >&2; exit 1; }
ROOT="$(cd "$ROOT" && pwd)"
FOLDER="$ROOT/addons/$NAME"
is_on() { jq -e --arg n "$NAME" '.addons // [] | any(. == $n)' "$ROOT/config.json" >/dev/null 2>&1; }

if [ "$action" = uninstall ]; then
  if is_on; then "$ROOT/bin/assistant" addon disable "$NAME"; fi
  rm -rf "$FOLDER"
  echo "$NAME is out of $ROOT. The service's settings and data ($HOME/.config/bambu, $HOME/.local/share/bambu) are kept."
  exit 0
fi

command -v jq >/dev/null || { echo "error: needs jq (multiclaude installs it)" >&2; exit 1; }
if [ -z "$user" ]; then
  user=$(jq -r 'to_entries[] | select(.value.role == "owner") | .key' "$ROOT/users.json" 2>/dev/null | head -1)
  [ -n "$user" ] || { echo "error: no owner in $ROOT/users.json; pass --user NAME" >&2; exit 1; }
fi
jq -e --arg u "$user" 'has($u)' "$ROOT/users.json" >/dev/null 2>&1 ||
  { echo "error: no user '$user' in $ROOT/users.json" >&2; exit 1; }
display=$(jq -r --arg u "$user" '.[$u].display // $u' "$ROOT/users.json")

# The add-in files name this clone, the person and the checkout; multiclaude fills in its own {{…}} later.
fill() {
  BAMBU_HOME="$BAMBU_HOME" BAMBU_USER="$user" BAMBU_DISPLAY="$display" ASSISTANT_ROOT="$ROOT" python3 -I -c '
import os, sys
text = open(sys.argv[1]).read()
for key in ("BAMBU_HOME", "BAMBU_USER", "BAMBU_DISPLAY", "ASSISTANT_ROOT"):
    text = text.replace("{{" + key + "}}", os.environ[key])
sys.stdout.write(text)' "$1"
}
mkdir -p "$FOLDER"
install -m 644 "$HERE/addon/README.md" "$FOLDER/README.md"
fill "$HERE/addon/rules.md" > "$FOLDER/rules.md" && chmod 644 "$FOLDER/rules.md"
fill "$HERE/addon/next-steps" > "$FOLDER/next-steps" && chmod 644 "$FOLDER/next-steps"
fill "$HERE/addon/bambu-service.service" > "$FOLDER/bambu-service.service" && chmod 644 "$FOLDER/bambu-service.service"
for script in setup teardown; do
  fill "$HERE/addon/$script" > "$FOLDER/$script" && chmod 755 "$FOLDER/$script"
done

# Keep the copied folder out of the checkout's `git status` and out of the way of `git pull`.
if [ -d "$ROOT/.git" ]; then
  grep -qxF "/addons/$NAME/" "$ROOT/.git/info/exclude" 2>/dev/null || echo "/addons/$NAME/" >> "$ROOT/.git/info/exclude"
fi

echo "$NAME is in $ROOT: the service from $BAMBU_HOME, for $user ($display)."
if is_on; then
  echo "It was already on. To use these rules and check the service again: assistant addon enable $NAME"
else
  echo "Turn it on: assistant addon enable $NAME"
fi
