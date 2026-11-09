"""Signal: агрегация vision + audio, scoring, dedup, IPC → Rust executor.

Запуск:  python signal/orchestrator.py

ВНИМАНИЕ: имя папки совпадает со stdlib-модулем `signal`. Поэтому:
  * `import signal.scorer` из корня проекта не работает (и не должен);
    модули этой папки импортируют друг друга напрямую (`from scorer import ...`),
    т.к. при запуске скрипта signal/ оказывается в sys.path[0].
  * корень проекта нельзя ставить В НАЧАЛО sys.path — иначе `import signal`
    (asyncio, ingestion/capture.py) подхватит эту папку вместо stdlib.
    orchestrator.py добавляет корень в КОНЕЦ sys.path.
"""

try:
    from .aggregator import AudioObservation, SignalAggregator, VisionObservation
    from .dedup import SignalDedup
    from .ipc import IPC_SOCKET_PATH, ExecutorClient
    from .resolver import TokenResolver
    from .scorer import Signal, SignalScorer
except ImportError:  # пакетный импорт недоступен (см. выше) или нет aiohttp
    pass
