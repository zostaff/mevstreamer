#!/usr/bin/env bash
# Integration checks without a stream or GPU.
#
#   scripts/check.sh            # cargo test + Python compile + smoke test (mock executor, offline)
#   scripts/check.sh --network  # also smoke test against the real executor (DRY_RUN, needs RPC)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY=.venv/bin/python
[[ -x "$PY" ]] || { echo "run scripts/build.sh first (or scripts/build.sh --no-ml)" >&2; exit 1; }

step() { printf '\n\033[36m==> %s\033[0m\n' "$*"; }

step "executor: cargo test"
(cd executor && cargo test --release --quiet)

step "python: compile all modules"
"$PY" -m compileall -q -x '/\._' ingestion vision audio signal scripts

step "audio: keyword spotter (no Whisper)"
"$PY" audio/pipeline.py --text "aping into PEPE right now"

step "vision: OCR mint repair (offline)"
(cd /tmp && "$ROOT/$PY" -I - "$ROOT/vision" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[1])
import mint_repair as mr
cases = {  # real EasyOCR misreads → on-screen mint
    "9iPGa52e8Fhxs496SKIHEvtqPMb64J71TkKnzZVnpump": "9iPGa52e8Fhxs496SK1HEvtqPMb64J71TkKnz2Vnpump",
    "GcqdWhz49ELjKjYNe48syb4BC6z0be3a7isA4gmkpump": "GcqdWhz49ELjKjYNe48syb4BC6zobe3a7isA4gmkpump",
}
for raw, want in cases.items():
    assert mr.is_near_mint(raw), raw
    assert want in mr.candidates(raw), f"{want} not among candidates of {raw}"
assert not mr.is_near_mint("9iPGa52e8Fhxs496SK1HEvtqPMb64J71TkKnz2Vnpump")  # valid → untouched
assert not mr.is_near_mint("Holders")
print("mint repair: candidates contain the true mint for", len(cases), "OCR misreads")
PYEOF
)

step "unit checks: false-signal guards (offline)"
(cd /tmp && "$ROOT/$PY" -I "$ROOT/scripts/unit_checks.py")

step "smoke test: orchestrator → mock executor"
LOG_LEVEL=WARNING "$PY" scripts/smoke_test.py

if [[ "${1:-}" == "--network" ]]; then
  step "smoke test: orchestrator → real executor (DRY_RUN)"
  (cd executor && cargo build --release --quiet)
  LOG_LEVEL=WARNING "$PY" scripts/smoke_test.py --executor
  printf '\n(for a live pump.fun mint also run: executor/target/release/mev-streamer-executor --selfcheck <MINT>)\n'
fi

printf '\n\033[32;1mall checks passed\033[0m\n'
