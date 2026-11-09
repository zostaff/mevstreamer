#!/usr/bin/env bash
# LIVE trading: executor (DRY_RUN=false) in background + orchestrator. Ctrl-C stops both.
#   scripts/run.sh [--stream URL] [--threshold 0.75] [--log-level DEBUG]
exec "$(dirname "${BASH_SOURCE[0]}")/_launch.sh" live "$@"
