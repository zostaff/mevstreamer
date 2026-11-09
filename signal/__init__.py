"""Signal: vision + audio aggregation, scoring, dedup, IPC → Rust executor.

Run:  python signal/orchestrator.py

WARNING: this folder's name collides with the stdlib `signal` module. Hence:
  * `import signal.scorer` from the project root does not work (and must not);
    modules in this folder import each other directly (`from scorer import ...`),
    since running a script puts signal/ at sys.path[0].
  * never put the project root at the START of sys.path, or `import signal`
    (asyncio, ingestion/capture.py) picks up this folder instead of the stdlib.
    orchestrator.py appends the root to the END of sys.path.
"""

try:
    from .aggregator import AudioObservation, SignalAggregator, VisionObservation
    from .dedup import SignalDedup
    from .ipc import IPC_SOCKET_PATH, ExecutorClient
    from .resolver import TokenResolver
    from .scorer import Signal, SignalScorer
except ImportError:  # package import unavailable (see above) or aiohttp missing
    pass
