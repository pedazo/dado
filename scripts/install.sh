#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if command -v pipx >/dev/null 2>&1; then
  pipx install --force "$ROOT"
else
  BASE=${XDG_DATA_HOME:-"$HOME/.local/share"}/dado
  python3 -m venv "$BASE/venv"
  "$BASE/venv/bin/python" -m pip install --upgrade "$ROOT"
  mkdir -p "$HOME/.local/bin"
  TARGET="$HOME/.local/bin/dado"
  if [ -e "$TARGET" ] || [ -L "$TARGET" ]; then
    if [ "$(readlink "$TARGET" 2>/dev/null || true)" != "$BASE/venv/bin/dado" ]; then
      echo "Refusing to overwrite existing $TARGET" >&2
      exit 1
    fi
  else
    ln -s "$BASE/venv/bin/dado" "$TARGET"
  fi
  echo "Installed isolated CLI; ensure $HOME/.local/bin is on PATH."
fi
echo "DADO CLI installed. Run 'dado init' separately inside each project."
