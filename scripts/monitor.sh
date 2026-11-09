#!/usr/bin/env bash
# Orchestrator only, --monitor-only: show signals, no executor, nothing sent.
#   scripts/monitor.sh [--stream URL] [--threshold 0.75] [--log-level DEBUG]
exec "$(dirname "${BASH_SOURCE[0]}")/_launch.sh" monitor "$@"
