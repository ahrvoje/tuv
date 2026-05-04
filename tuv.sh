#!/usr/bin/env sh
set -eu

TUV_HOME=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
RUNNER="$TUV_HOME/.tuv-venv"
REQ="$TUV_HOME/requirements.txt"
APP="$TUV_HOME/tuv.py"

if [ ! -f "$APP" ]; then
  echo "tuv.py was not found in $TUV_HOME" >&2
  exit 1
fi

if [ ! -f "$REQ" ]; then
  echo "requirements.txt was not found in $TUV_HOME" >&2
  exit 1
fi

hash_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    cksum "$1" | awk '{print $1 "-" $2}'
  fi
}

find_newest_python() {
  tmp="${TMPDIR:-/tmp}/tuv-python-$$.txt"
  : > "$tmp"
  candidates=""

  for name in python3.15 python3.14 python3.13 python3.12 python3.11 python3.10 python3.9 python3.8 python3.7 python3 python; do
    path=$(command -v "$name" 2>/dev/null || true)
    if [ -n "$path" ]; then
      candidates="$candidates
$path"
    fi
  done

  for dir in /usr/bin /usr/local/bin /opt/homebrew/bin /opt/local/bin; do
    if [ -d "$dir" ]; then
      for path in "$dir"/python3* "$dir"/python; do
        if [ -x "$path" ]; then
          candidates="$candidates
$path"
        fi
      done
    fi
  done

  printf '%s\n' "$candidates" | awk 'NF && !seen[$0]++' | while IFS= read -r py; do
    "$py" -c 'import sys; print(sys.version_info[0], sys.version_info[1], sys.version_info[2], sys.executable)' >> "$tmp" 2>/dev/null || true
  done

  newest=$(sort -k1,1nr -k2,2nr -k3,3nr "$tmp" | head -n 1 | cut -d ' ' -f 4-)
  rm -f "$tmp"
  printf '%s' "$newest"
}

NEWEST_PYTHON=$(find_newest_python)
if [ -z "$NEWEST_PYTHON" ]; then
  echo "No usable Python interpreter was found." >&2
  exit 1
fi

if ! "$NEWEST_PYTHON" -m uv --version >/dev/null 2>&1; then
  printf 'uv is missing from %s. Install uv into this Python? [y/N] ' "$NEWEST_PYTHON"
  read ans
  case "$ans" in
    y|Y|yes|YES)
      if ! "$NEWEST_PYTHON" -m pip --version >/dev/null 2>&1; then
        "$NEWEST_PYTHON" -m ensurepip --upgrade
      fi
      "$NEWEST_PYTHON" -m pip install uv
      ;;
    *)
      echo "uv is required to run Tuv." >&2
      exit 1
      ;;
  esac
fi

if [ ! -x "$RUNNER/bin/python" ]; then
  "$NEWEST_PYTHON" -m uv venv --allow-existing --python "$NEWEST_PYTHON" "$RUNNER"
fi

REQ_HASH=$(hash_file "$REQ")
STATE="$RUNNER/.tuv-requirements-state"
STATE_HASH=""
if [ -f "$STATE" ]; then
  STATE_HASH=$(cat "$STATE")
fi

if [ "$REQ_HASH" != "$STATE_HASH" ]; then
  "$NEWEST_PYTHON" -m uv pip install --python "$RUNNER" -r "$REQ"
  printf '%s' "$REQ_HASH" > "$STATE"
fi

export TUV_HOME
exec "$RUNNER/bin/python" "$APP" "$@"
