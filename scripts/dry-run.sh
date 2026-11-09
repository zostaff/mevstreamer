#!/usr/bin/env bash
# Full pipeline without trades: executor in DRY_RUN (simulated tx, paper positions) + orchestrator.
#   scripts/dry-run.sh [--stream URL] [--threshold 0.75] [--log-level DEBUG]
exec "$(dirname "${BASH_SOURCE[0]}")/_launch.sh" paper "$@"
