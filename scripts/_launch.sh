#!/usr/bin/env bash
# Launcher engine. Normally called through the wrappers:
#
#   scripts/monitor.sh  [args]  → monitor  — show signals only; nothing is sent anywhere
#   scripts/dry-run.sh  [args]  → paper    — full chain: Python → executor in DRY_RUN
#                                            (simulated tx + paper positions with TP/SL/timeout)
#   scripts/run.sh      [args]  → live     — REAL trades. Needs DRY_RUN=false in .env + typed confirmation
#
#   scripts/_launch.sh dry      [args]     — Python only, logs "WOULD EXECUTE", no executor
#   scripts/_launch.sh executor            — executor only (DRY_RUN from .env)
#
# [args] go to signal/orchestrator.py: --stream URL, --threshold 0.75, --log-level DEBUG
#
# Python and the executor both read DRY_RUN from the same .env, and with DRY_RUN=true
# Python never talks to the executor. So paper sets DRY_RUN=false for Python only and
# DRY_RUN=true for the executor. Env vars set here take precedence over .env on both sides.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

MODE="${1:-help}"; [[ $# -gt 0 ]] && shift

say()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[warn]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

# Value of KEY: process env first, then the last KEY= line in .env (no sourcing —
# RPC URLs with ?/& would break a shell `source`).
env_get() {
  local key="$1" val="${!1:-}"
  if [[ -z "$val" && -f .env ]]; then
    val="$(grep -E "^[[:space:]]*${key}[[:space:]]*=" .env | tail -n1 | cut -d= -f2- || true)"
    val="${val%%#*}"; val="$(printf '%s' "$val" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/")"
  fi
  printf '%s' "$val"
}

PY=.venv/bin/python
BIN=executor/target/release/mev-streamer-executor
SOCK="$(env_get IPC_SOCKET_PATH)"; SOCK="${SOCK:-$(env_get SOCKET_PATH)}"; SOCK="${SOCK:-/tmp/mev-streamer.sock}"
# Python reads IPC_SOCKET_PATH, the executor reads SOCKET_PATH — keep them in sync.
export IPC_SOCKET_PATH="$SOCK" SOCKET_PATH="$SOCK"

need_python() {
  [[ -x "$PY" ]] || die ".venv not found — run scripts/build.sh"
  # python.org builds ship without system CA roots → every HTTPS call (ticker
  # resolver: Jupiter / DexScreener / Helius) fails CERTIFICATE_VERIFY_FAILED.
  if [[ -z "${SSL_CERT_FILE:-}" ]]; then
    SSL_CERT_FILE="$(cd /tmp && "$ROOT/$PY" -c 'import certifi; print(certifi.where())' 2>/dev/null || true)"
    [[ -n "$SSL_CERT_FILE" ]] && export SSL_CERT_FILE
  fi
  [[ -f .env ]] || warn ".env not found — using environment only (cp .env.example .env)"
  command -v ffmpeg >/dev/null || die "ffmpeg not found (brew install ffmpeg)"
  if [[ -z "$(env_get STREAM_URL)" && " $* " != *" --stream "* && " $* " != *" --stream="* ]]; then
    die "STREAM_URL is not set (.env or --stream URL)"
  fi
}

need_executor() {
  if [[ ! -x "$BIN" ]]; then
    command -v cargo >/dev/null || die "executor binary missing and cargo not found"
    say "building executor"
    (cd executor && cargo build --release)
  fi
  local rpc key
  rpc="$(env_get RPC_URL)"
  [[ -n "$rpc" ]] || die "RPC_URL is not set"
  [[ "$rpc" != *YOUR_KEY* ]] || die "RPC_URL still has the YOUR_KEY placeholder from .env.example"
  # The root .env.example ships WALLET_PRIVATE_KEY=base58_private_key, which the
  # executor rejects even in DRY_RUN. In paper mode, blank it → ephemeral keypair.
  key="$(env_get WALLET_PRIVATE_KEY)"
  if [[ "$key" == "base58_private_key" ]]; then
    [[ "$MODE" == "live" ]] && die "WALLET_PRIVATE_KEY is still the .env.example placeholder"
    warn "WALLET_PRIVATE_KEY is a placeholder — executor will use an ephemeral keypair"
    export WALLET_PRIVATE_KEY=""
  fi
}

EXEC_PID=""
start_executor() {   # $1 = DRY_RUN value for the executor
  mkdir -p logs
  local log="logs/executor-$(date +%Y%m%d-%H%M%S).log"
  if [[ -S "$SOCK" ]] && pgrep -f "$BIN" >/dev/null; then
    die "an executor is already running on $SOCK (pkill -INT -f mev-streamer-executor)"
  fi
  say "starting executor (DRY_RUN=$1) → $log"
  # cwd=root: dotenvy picks up the root .env
  # the log pipe ignores Ctrl-C so the executor's final stats still get written
  DRY_RUN="$1" "$BIN" > >(trap '' INT; tee -a "$log" | sed -l 's/^/[exec] /') 2>&1 &
  EXEC_PID=$!
  local i
  for i in $(seq 1 300); do       # up to 30s: initial blockhash fetch over RPC
    [[ -S "$SOCK" ]] && { say "executor ready on $SOCK (pid $EXEC_PID)"; return 0; }
    kill -0 "$EXEC_PID" 2>/dev/null || die "executor exited during startup — see $log"
    sleep 0.1
  done
  die "executor did not open $SOCK within 30s — see $log"
}

stop_executor() {
  [[ -n "$EXEC_PID" ]] || return 0
  if kill -0 "$EXEC_PID" 2>/dev/null; then
    kill -INT "$EXEC_PID" 2>/dev/null || true   # executor only handles SIGINT: prints stats, removes socket
    local i; for i in $(seq 1 50); do kill -0 "$EXEC_PID" 2>/dev/null || break; sleep 0.1; done
    kill -0 "$EXEC_PID" 2>/dev/null && kill -9 "$EXEC_PID" 2>/dev/null || true
  fi
  wait "$EXEC_PID" 2>/dev/null || true
  EXEC_PID=""
}
trap stop_executor EXIT

orchestrator() { "$PY" signal/orchestrator.py "$@"; }

case "$MODE" in
  monitor)
    need_python "$@"
    orchestrator --monitor-only "$@"
    ;;
  dry)
    need_python "$@"
    orchestrator --dry-run "$@"
    ;;
  paper)
    need_python "$@"; need_executor
    start_executor true
    DRY_RUN=false orchestrator "$@"
    ;;
  live)
    need_python "$@"; need_executor
    [[ "$(env_get DRY_RUN | tr '[:upper:]' '[:lower:]')" =~ ^(false|0|no|off)$ ]] \
      || die "live mode requires DRY_RUN=false in .env"
    [[ -n "$(env_get WALLET_PRIVATE_KEY)" ]] || die "WALLET_PRIVATE_KEY is not set"
    warn "LIVE TRADING: real SOL, BUY_SOL=$(env_get BUY_SOL), MAX_POSITIONS=$(env_get MAX_POSITIONS)"
    warn "executor/README.md: check the pump.fun instruction layout in tx_builder.rs before going live"
    read -r -p "type LIVE to continue: " ans
    [[ "$ans" == "LIVE" ]] || die "aborted"
    start_executor false
    DRY_RUN=false orchestrator "$@"
    ;;
  executor)
    need_executor
    cd "$ROOT" && exec "$BIN"
    ;;
  -h|--help|help)
    sed -n 2,16p "$0"
    ;;
  *)
    die "unknown mode: $MODE (monitor|dry|paper|live|executor)"
    ;;
esac
