#!/usr/bin/env bash
# Installs the bambu add-in into a multiclaude checkout, or takes it out again.
#
#   multiclaude/install.sh [--root DIR] [--user NAME]    copy the add-in in
#   multiclaude/install.sh [--root DIR] --uninstall      turn it off and remove it
#
# DIR is the multiclaude checkout: $ASSISTANT_ROOT, or ~/assistant. NAME is the multiclaude user
# whose assistant drives the printer (default: the owner in users.json). install adds one line to
# multiclaude's tracked lib/multiclaude/addons/__init__.py (the Mini App tab): before a `git pull` in
# that checkout, run --uninstall, pull, then install again (as local-gpu's install.sh asks too). The service itself runs
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
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

[ -f "$ROOT/bin/assistant" ] && [ -d "$ROOT/addons" ] || { echo "error: no multiclaude checkout at $ROOT (--root DIR)" >&2; exit 1; }
ROOT="$(cd "$ROOT" && pwd)"
FOLDER="$ROOT/addons/$NAME"
# The Mini App's Print tab: the router module goes into multiclaude's add-in package, and its name is
# added to the add-in list by one line at the end of that file (never by a patch: local-gpu's patch
# edits the list's own line, and both must apply in any order).
ADDONS_INIT="$ROOT/lib/multiclaude/addons/__init__.py"
MODULE="$ROOT/lib/multiclaude/addons/bambu"
ALL_LINE='ALL += ("bambu",)  # bambu-mcp install.sh'
is_on() { jq -e --arg n "$NAME" '.addons // [] | any(. == $n)' "$ROOT/config.json" >/dev/null 2>&1; }

drop_all_line() {
  [ -f "$ADDONS_INIT" ] || return 0
  local tmp
  tmp=$(mktemp "$ADDONS_INIT.XXXXXX")
  grep -vxF "$ALL_LINE" "$ADDONS_INIT" > "$tmp" || true
  # the line went in after one newline of its own: take that off too, so the file is as it was
  python3 -I -c 'import sys; p = sys.argv[1]; t = open(p).read(); open(p, "w").write(t[:-1] if t.endswith("\n\n") else t)' "$tmp"
  cat "$tmp" > "$ADDONS_INIT" && rm -f "$tmp"
}

if [ "$action" = uninstall ]; then
  if is_on; then "$ROOT/bin/assistant" addon disable "$NAME"; fi
  drop_all_line
  rm -rf "$MODULE"
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
# The Print tab's page module: `assistant relay deploy` uploads relay-public/, which is public.
rm -rf "$FOLDER/relay-public" && cp -R "$HERE/addon/relay-public" "$FOLDER/relay-public"

# The Print tab's router module, where multiclaude has add-in modules (the chat Mini App).
if [ -f "$ADDONS_INIT" ]; then
  rm -rf "$MODULE" && mkdir -p "$MODULE"
  install -m 644 "$HERE"/router/bambu/*.py "$MODULE/"
  if ! grep -qxF "$ALL_LINE" "$ADDONS_INIT"; then
    printf '\n%s\n' "$ALL_LINE" >> "$ADDONS_INIT"
  fi
else
  echo "This multiclaude has no add-in modules: the Mini App's Print tab is left out."
fi

# Keep the copied folder out of the checkout's `git status` and out of the way of `git pull`.
if [ -d "$ROOT/.git" ]; then
  for path in "/addons/$NAME/" /lib/multiclaude/addons/bambu/; do
    grep -qxF "$path" "$ROOT/.git/info/exclude" 2>/dev/null || echo "$path" >> "$ROOT/.git/info/exclude"
  done
fi

echo "$NAME is in $ROOT: the service from $BAMBU_HOME, for $user ($display)."
if is_on; then
  echo "It was already on. To use these rules and check the service again: assistant addon enable $NAME"
else
  echo "Turn it on: assistant addon enable $NAME"
fi
