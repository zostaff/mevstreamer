#!/usr/bin/env bash
# One-time setup: Python venv + deps, Rust executor build, .env.
#
#   scripts/build.sh              # everything
#   scripts/build.sh --no-ml      # skip easyocr/torch/faster-whisper (enough for smoke_test / monitoring the wiring)
#   scripts/build.sh --no-rust    # skip cargo build
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

NO_ML=0; NO_RUST=0
for a in "$@"; do
  case "$a" in
    --no-ml) NO_ML=1 ;;
    --no-rust) NO_RUST=1 ;;
    -h|--help) sed -n 2,7p "$0"; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[warn]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

# --- system deps -------------------------------------------------------------
for bin in ffmpeg streamlink yt-dlp; do
  command -v "$bin" >/dev/null || warn "$bin not found in PATH (brew install $bin) — stream capture needs it"
done

# --- python ------------------------------------------------------------------
# 3.11+ is required (PYTHONSAFEPATH); torch/easyocr wheels lag behind new
# CPython releases, so prefer 3.13 → 3.12 → 3.11 over a bleeding-edge python3.
PY="${PYTHON:-}"
if [[ -z "$PY" ]]; then
  for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null; then PY="$(command -v "$c")"; break; fi
  done
fi
[[ -n "$PY" ]] || die "python 3.11+ not found (brew install python@3.13)"
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "$PY is older than 3.11"
say "python: $PY ($("$PY" -V 2>&1))"

if [[ ! -x .venv/bin/python ]]; then
  say "creating .venv"
  "$PY" -m venv .venv
fi
VPY=.venv/bin/python
"$VPY" -m pip install -q --upgrade pip

if (( NO_ML )); then
  say "installing core deps (no ML)"
  "$VPY" -m pip install -q -r signal/requirements.txt -r ingestion/requirements.txt numpy
else
  say "installing Python deps (torch/easyocr/whisper — this can take a while)"
  "$VPY" -m pip install -q -r requirements.txt
fi

# --- rust --------------------------------------------------------------------
if (( ! NO_RUST )); then
  command -v cargo >/dev/null || die "cargo not found (https://rustup.rs) — or rerun with --no-rust"
  say "building executor (release)"
  (cd executor && cargo build --release)
fi

# --- .env --------------------------------------------------------------------
if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
  say "created .env from .env.example — fill in RPC_URL and STREAM_URL"
else
  chmod 600 .env 2>/dev/null || true
fi

say "done. next: edit .env → scripts/check.sh → scripts/monitor.sh"
