#!/usr/bin/env sh
set -eu

TUV_HOME=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REQ="$TUV_HOME/requirements.txt"
APP="$TUV_HOME/tuv.py"
LAUNCHER_MODE="default"

if [ "${1:-}" = "." ]; then
  LAUNCHER_MODE="cwd"
  shift
fi

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

fail_bootstrap() {
  echo "Tuv runner bootstrap failed: $1" >&2
  echo "Runner Python: ${TUV_RUNNER_PYTHON:-unknown}" >&2
  echo "Runner venv: ${TUV_RUNNER_VENV:-unknown}" >&2
  exit 1
}

find_bootstrap_python() {
  tmp="${TMPDIR:-/tmp}/tuv-bootstrap-python-$$.txt"
  : > "$tmp"
  candidates=""
  cwd=$(pwd)

  for path in "$cwd/python" "$cwd/python3" "$cwd/bin/python" "$cwd/bin/python3"; do
    if [ -x "$path" ]; then
      candidates="$candidates
$path"
    fi
  done
  if [ -f "$cwd/pyvenv.cfg" ] && [ -x "$cwd/bin/python" ]; then
    candidates="$candidates
$cwd/bin/python"
  fi

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

BOOTSTRAP_PYTHON=$(find_bootstrap_python)
if [ -z "$BOOTSTRAP_PYTHON" ]; then
  echo "No usable Python interpreter was found." >&2
  exit 1
fi

export TUV_HOME
if ! PREP_OUTPUT=$("$BOOTSTRAP_PYTHON" "$APP" --prepare-runner --launcher-mode "$LAUNCHER_MODE" 2>&1); then
  echo "$PREP_OUTPUT" >&2
  exit 1
fi
eval "$(
  printf '%s\n' "$PREP_OUTPUT" | while IFS='=' read -r key value; do
    case "$key" in
      TUV_NEWEST_PYTHON|TUV_RUNNER_VENV|TUV_RUNNER_PYTHON)
        printf "%s='%s'\n" "$key" "$(printf '%s' "$value" | sed "s/'/'\\\\''/g")"
        ;;
    esac
  done
)"

if [ -z "${TUV_RUNNER_VENV:-}" ] || [ -z "${TUV_RUNNER_PYTHON:-}" ]; then
  echo "Tuv runner preparation did not return a runner venv." >&2
  exit 1
fi

if ! "$TUV_RUNNER_PYTHON" -m pip --version >/dev/null 2>&1; then
  "$TUV_RUNNER_PYTHON" -m ensurepip --upgrade || fail_bootstrap "runner pip is unavailable and ensurepip could not restore it"
fi

if ! "$TUV_RUNNER_PYTHON" -m pip --version >/dev/null 2>&1; then
  fail_bootstrap "runner pip is unavailable after ensurepip"
fi

if ! "$TUV_RUNNER_PYTHON" -m uv --version >/dev/null 2>&1; then
  "$TUV_RUNNER_PYTHON" -m pip install uv || fail_bootstrap "runner uv is unavailable and could not be installed; network or package index access may be unavailable"
fi

if ! "$TUV_RUNNER_PYTHON" -m uv --version >/dev/null 2>&1; then
  fail_bootstrap "runner uv is unavailable after installation"
fi

REQ_HASH=$(hash_file "$REQ")
STATE="$TUV_RUNNER_VENV/.tuv-requirements-state"
STATE_HASH=""
if [ -f "$STATE" ]; then
  STATE_HASH=$(cat "$STATE")
fi

if [ "$REQ_HASH" != "$STATE_HASH" ]; then
  "$TUV_RUNNER_PYTHON" -m pip install -r "$REQ" || fail_bootstrap "requirements could not be installed; network or package index access may be unavailable"
  printf '%s' "$REQ_HASH" > "$STATE"
fi

if ! "$TUV_RUNNER_PYTHON" -c 'import packaging' >/dev/null 2>&1; then
  "$TUV_RUNNER_PYTHON" -m pip install -r "$REQ" || fail_bootstrap "requirements could not be installed; network or package index access may be unavailable"
  printf '%s' "$REQ_HASH" > "$STATE"
fi

if command -v uv >/dev/null 2>&1 && uv --version >/dev/null 2>&1; then
  TUV_SYSTEM_UV_EXE=$(command -v uv)
  export TUV_SYSTEM_UV_EXE
fi

export TUV_NEWEST_PYTHON
export TUV_RUNNER_VENV
export TUV_RUNNER_PYTHON
exec "$TUV_RUNNER_PYTHON" "$APP" "$@"
