<!-- BANNER IMAGE HERE -->

# mev-streamer

![Python](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)
![Rust](https://img.shields.io/badge/rust-2021-000000?logo=rust&logoColor=white)
![Solana](https://img.shields.io/badge/solana-mainnet-9945FF?logo=solana&logoColor=white)
![Jito](https://img.shields.io/badge/Jito%20%2B%20Helius%20Sender-fan--out-1DB954)
![GPU](https://img.shields.io/badge/GPU-optional-lightgrey)

**Смотрит livestream, по экрану и голосу ловит момент, когда стример заходит в токен, и покупает его на pump.fun bonding curve — одной подписанной транзакцией, разосланной сразу в Helius Sender и Jito.**

```
 Stream ──▶ Capture ──▶ OCR + Whisper ──▶ Signal ──▶ pump.fun v3 tx ──▶ Sender ‖ Jito ‖ RPC
 (Twitch/    (ffmpeg      (mint, $TICKER,   (score,     (fee-aware quote,    (same signature,
  Kick/YT)    3fps + PCM)  platform, phrase) dedup)      TP / SL / timeout)   lands once)
```

---

## How it works

1. **Capture.** `streamlink`/`yt-dlp` достают прямой URL стрима, два процесса `ffmpeg` выдают кадры 1280×720 (3 fps) и PCM 16 kHz кусками по 3 с. Обрыв → переподключение с backoff 1→30 с. Анализируется всегда самый свежий кадр: медленный OCR снижает частоту, но не копит задержку.
2. **Vision.** OCR кадра: на macOS — Apple Vision (Neural Engine, ~0.1 с/кадр), иначе EasyOCR. Детектор ищет Solana-адреса и `$TICKER`, UI-распознаватель — платформу и действие стримера. **Восстановление адресов**: если OCR испортил pump-mint (`1→I`, `2→Z`, `o→0`…), генерируются исправления и проверяются на mainnet одним `getMultipleAccounts` (кеш по строке).
3. **Audio.** faster-whisper с VAD транскрибирует речь, keyword spotter ловит торговые фразы и тикеры.
4. **Signal.** Скользящие окна (vision 3 с, audio 10 с), скоринг confidence 0…1, cooldown по токену, резолв mint по тикеру (Jupiter → DexScreener → Helius).
5. **Quote & build.** Executor за один RPC round-trip берёт кривую + минт ‖ priority fee ‖ blockhash (кеш), квотирует `buy_exact_quote_in_v3` с комиссиями из pump-fees `FeeConfig` (тиры по market cap, creator fee, buyback-доля) и собирает v0-транзакцию: compute budget под замеренный расход, idempotent ATA, buy v3, tip'ы.
6. **Land & manage.** Одна подписанная tx параллельно уходит во все релеи (Helius Sender, Jito-регионы, RPC) — одна подпись исполняется не более одного раза. Подтверждение — `getSignatureStatuses` каждые 400 мс. Позиция ведётся до take-profit / stop-loss / timeout, оценка выхода тоже с учётом комиссий.

## Dual detection: Visual + Audio

| Признак | Канал | Вес |
|---|---|---|
| mint-адрес на экране (в т.ч. восстановленный) | visual | +0.50 |
| `$TICKER` на экране | visual | +0.20 |
| распознана торговая платформа | visual | +0.15 |
| стример смотрит токен / на кнопке Buy / купил | visual | +0.10 / +0.30 / +0.40 |
| buy-фраза в речи | audio | +0.30 |
| тикер в речи | audio | +0.15 |
| оба канала подтверждают один токен | both | +0.20 |
| тикер без mint (риск копиката) | — | −0.15 |

Сумма обрезается до 1.0; порог исполнения по умолчанию 0.7. Mint на экране + платформа + buy-фраза (0.95) — проходит; только buy-фраза с тикером в речи (0.30) — нет.

## Supported platforms

Photon · BullX · GMGN · pump.fun · Dexscreener — по URL/тексту на экране и палитре (`vision/ui_recognizer.py`). Стримы: всё, что открывает `streamlink` или `yt-dlp` (Twitch, Kick, YouTube Live).

## Latency breakdown

Замеры 2026-11-09, Apple M1, без GPU NVIDIA, публичный RPC. На своём железе смотрите `SESSION STATS` (Python) и логи executor'а.

| Этап | Замер | Как получено |
|---|---|---|
| Задержка самой платформы стрима | секунды (вне нашего контроля) | — |
| Ожидание кадра / аудио-чанка | ≤ 333 мс / ≤ 3 с | `STREAM_FPS=3`, `STREAM_AUDIO_CHUNK_SEC=3` |
| OCR кадра, Apple Vision | **118 мс avg / 179 мс p95** | живой эфир, 201 кадр |
| OCR кадра, EasyOCR на MPS / на CPU | 748 мс avg / 7–9 с | живой эфир / синтетический кадр |
| Whisper `base.en` (CPU int8) на 3 с аудио | **398 мс avg / 522 мс p95** | живой эфир (с Apple Vision рядом) |
| Восстановление испорченного mint | 0.4–1.1 с первый раз, ~0 мс из кеша | публичный RPC |
| Агрегация + скоринг | ~0.1 мс | smoke test |
| IPC Python → Rust | < 1 мс | smoke test |
| Executor: состояние кривой + квота + сборка | ~140–150 мс (1 RPC round-trip) | dry-run на mainnet |
| Отправка: принятие релеем | 115 мс (RPC) … 0.5–0.9 с (Sender / Jito из EU-дома) | `--probe-relays` |

Слоты в mainnet с октября 2026 — 200 мс, blockhash живёт ~30 с; executor обновляет его каждые 2 с и никогда не подписывает хешем старше 10 с.

## Architecture

```
mev-streamer/
├── .env.example            конфиг (cp → .env)
├── requirements.txt        все Python-зависимости (pyobjc только на macOS)
├── pyproject.toml          метаданные + extras [ml]
├── scripts/
│   ├── build.sh            .venv + pip + cargo build --release + .env
│   ├── monitor.sh          только сигналы
│   ├── dry-run.sh          полный пайплайн, executor в DRY_RUN
│   ├── run.sh              live
│   ├── _launch.sh          общий запуск/остановка процессов
│   ├── check.sh            интеграционные проверки
│   └── smoke_test.py       e2e без стрима и моделей
├── ingestion/              Module 1: захват стрима
├── vision/                 Module 2: анализ экрана
│   ├── ocr.py              Apple Vision / EasyOCR / Tesseract, ROI
│   ├── detector.py         Solana-адреса, тикеры, «почти-адреса»
│   ├── mint_repair.py      исправление OCR-ошибок в mint + on-chain проверка
│   ├── ui_recognizer.py    платформа + действие
│   └── pipeline.py         VisionPipeline
├── audio/                  Module 3: речь (faster-whisper + keyword spotter)
├── signal/                 Module 4: решение (точка входа orchestrator.py)
└── executor/               Module 5: исполнение (Rust)
    └── src/
        ├── main.rs         Executor: buy/sell, кеш Global/FeeConfig, dry-run симуляция
        ├── bonding.rs      BondingCurve/Global/FeeConfig + fee-aware v3 квоты
        ├── tx_builder.rs   buy_exact_quote_in_v3 / sell_v3 (17 аккаунтов), compute budget, tips
        ├── sender.rs       fan-out: Helius Sender ‖ Jito ‖ RPC, подтверждение по подписи
        ├── jito.rs         Jito sendTransaction (bundleOnly), live tip_floor
        ├── rpc.rs          JSON-RPC: аккаунты, blockhash, priority fee, simulate
        ├── selfcheck.rs    --selfcheck <MINT>, --probe-relays
        ├── position.rs     TP / SL / timeout
        ├── pda.rs          адреса и PDA (проверяются тестом против seed'ов)
        ├── config.rs       .env → lamports/bps без f64
        ├── ipc.rs          UDS-сервер
        └── types.rs        IPC-схема
```

IPC — NDJSON по Unix-сокету (`/tmp/mev-streamer.sock`, права 0600):

```
→ {"mint":"…pump","ticker":"X","action":"buy","confidence":0.85,"source":"both","context":"…","timestamp":1699999999.1}
← {"status":"ok","bundle_id":"<signature>"}    ("dry-run" в DRY_RUN)
← {"status":"error","message":"…"}
```

## pump.fun: что поддерживается

Источник истины — [pump-fun/pump-public-docs](https://github.com/pump-fun/pump-public-docs) (IDL от 2026-10-13). Executor использует **v3-трейды** (`buy_exact_quote_in_v3`, `sell_v3`): 17 аккаунтов вместо 26–27 у v2, комиссии протокола и создателя копятся на кривой, в трейде платится только buyback-доля.

- Квоты — порт референсной математики `pump-rust-client 0.5.0` (MIT); `--selfcheck` на mainnet: **квота buy совпала с симуляцией до единицы токена, sell — до лампорта**.
- Старые «короткие» аккаунты кривой читаются с нулевым добором (как в официальном декодере).
- Отказ с понятной причиной: кривая завершена (graduated), **кривая квотирована не в SOL**, cashback-коин (v3 их не принимает), покупка, пересекающая остаток кривой (synthetic migration).

Наблюдение с mainnet: из 7 свежих токенов 4 были квотированы не в SOL (в других pump-коинах и `Xs…`-минтах). Поддержка таких кривых через `multi_hop_curve_swap` — следующий шаг.

## Quick start

Нужны: Python 3.11+ (лучше 3.13), Rust, `ffmpeg`.

```bash
./scripts/build.sh                                  # .venv, зависимости, cargo build, .env
$EDITOR .env                                        # RPC_URL, STREAM_URL; WALLET_PRIVATE_KEY только для live
./scripts/check.sh                                  # связка модулей без стрима и GPU

./scripts/monitor.sh --stream "twitch.tv/username"  # 1. только смотреть сигналы
./scripts/dry-run.sh --stream "twitch.tv/username"  # 2. полный пайплайн без сделок (с симуляцией на mainnet)
./scripts/run.sh     --stream "twitch.tv/username"  # 3. live
```

Перед первым live:

```bash
executor/target/release/mev-streamer-executor --selfcheck <любой живой pump-mint на SOL-кривой>
executor/target/release/mev-streamer-executor --probe-relays     # endpoint'ы и формат, ничего не тратит
```

## Configuration

Все модули читают `.env` из корня; переменные окружения его перекрывают.

| Переменная | По умолч. | Модуль | Описание |
|---|---|---|---|
| `RPC_URL` | — | все | Solana RPC (свой/Helius — сильно быстрее публичного) |
| `WALLET_PRIVATE_KEY` | пусто | executor | base58, 64 байта; пусто → эфемерный ключ, только DRY_RUN |
| `STREAM_URL` | — | ingestion | или `--stream` |
| `STREAM_FPS` / `STREAM_AUDIO_CHUNK_SEC` | 3 / 3 | ingestion | с Apple Vision можно поднять FPS до 5 |
| `STREAM_RESOLUTION` | `1280:720` | ingestion | |
| `OCR_ENGINE` | `auto` | vision | `auto` (Apple Vision на macOS, иначе EasyOCR) / `applevision` / `easyocr` / `tesseract` |
| `OCR_GPU` / `OCR_MIN_CONFIDENCE` | true / 0.4 | vision | для EasyOCR |
| `VISION_ROI_MODE` | `auto` | vision | `auto` = весь кадр для Apple Vision, зоны для остальных |
| `VISION_MINT_REPAIR` | true | vision | исправление OCR-ошибок в mint с проверкой через `RPC_URL` |
| `UI_DETECTION` | true | vision | платформа и действие |
| `WHISPER_MODEL` / `WHISPER_DEVICE` | `base.en` / `cuda` | audio | без CUDA сам уходит на CPU int8; `small.en` — только с GPU |
| `SIGNAL_THRESHOLD` / `SIGNAL_COOLDOWN_SEC` | 0.7 / 60 | signal | |
| `HELIUS_API_KEY` | из `RPC_URL` | signal | проверка mint при резолве тикера |
| `BUY_SOL` | — | executor | бюджет входа (комиссии внутри него) |
| `RELAYS` | `sender,jito` | executor | `sender` (Helius Sender), `jito`, `rpc` — через запятую |
| `JITO_TIP` / `JITO_TIP_MAX` | `auto` / 0.001 | executor | auto = p75 из Jito `tip_floor` (обновление раз в 2 с), не выше MAX |
| `JITO_URLS` | global engine | executor | несколько регионов через запятую |
| `SENDER_TIP` / `HELIUS_SENDER_URL` | 0.000005 / global `/fast` | executor | ≥ 0.001 → Sender Max; меньше → автоматически `?swqos_only=true` |
| `PRIORITY_FEE` | `auto` | executor | auto = p75 недавних комиссий по этой кривой, в `[PRIORITY_FEE_MIN, PRIORITY_FEE_MAX]` µL/CU |
| `CU_LIMIT_BUY` / `CU_LIMIT_SELL` | 90000 / 60000 | executor | замерено: buy 70–74k, sell 46k CU |
| `TAKE_PROFIT` / `STOP_LOSS` / `TIMEOUT_SEC` | 2.0 / 0.5 / 120 | executor | |
| `MAX_POSITIONS` / `SLIPPAGE_PCT` | 3 / 5 | executor | |
| `SIM_PAYER` | богатый системный кошелёк | executor | от чьего имени симулировать в DRY_RUN без ключа |
| `DRY_RUN` | true | оба | см. режимы |
| `IPC_SOCKET_PATH` / `SOCKET_PATH` | `/tmp/mev-streamer.sock` | signal / executor | скрипты синхронизируют обе |

## Modes

| Скрипт | Python | Executor | Что происходит |
|---|---|---|---|
| `monitor.sh` | `--monitor-only` | не запускается | сигналы печатаются, никуда не уходят |
| `dry-run.sh` | шлёт сигналы | `DRY_RUN=true` | настоящая v3-транзакция симулируется на mainnet (лог: CU и сколько токенов придёт), позиция бумажная |
| `run.sh` | шлёт сигналы | `DRY_RUN=false` | **реальные сделки**: нужен `DRY_RUN=false` в `.env`, ключ и ввод `LIVE` |

Python и executor читают `DRY_RUN` из одного `.env`, а Python при `DRY_RUN=true` к executor'у не обращается — поэтому `dry-run.sh` ставит его каждому процессу отдельно. Ещё: `_launch.sh dry` (только Python, `WOULD EXECUTE`), `_launch.sh executor` (только executor).

## Hardware: CPU-only vs GPU

| | Mac (Apple Silicon) | Linux/Windows + NVIDIA | CPU-only без Apple Vision |
|---|---|---|---|
| OCR | Apple Vision ~0.1 с/кадр (ANE) | EasyOCR на CUDA | EasyOCR 7–9 с/кадр — непригодно; Tesseract или `STREAM_FPS=1` |
| Whisper | `base.en` int8 ~0.4 с/чанк | `small.en` float16 | `tiny.en` / `base.en` |
| Перегрузка видна по | `audio отстаёт на Ns`, рост `skipped stale frames` | | |

Executor лёгкий всегда: время уходит в RPC и сеть. Свой RPC и регион релея рядом с вами важнее железа.

## Development & debugging

```bash
./scripts/check.sh              # cargo test (27), compileall, keyword spotter, mint repair, smoke test
./scripts/check.sh --network    # + smoke test с настоящим executor'ом
executor/target/release/mev-streamer-executor --selfcheck <MINT>   # v3 buy/sell против mainnet-симуляции
executor/target/release/mev-streamer-executor --probe-relays       # релеи принимают формат?
```

Модули по отдельности — как скрипты (не `python -m` из корня: папка `signal/` затеняет stdlib `signal`):

```bash
.venv/bin/python ingestion/capture.py twitch.tv/name --seconds 10 --save-frame /tmp/f.png
.venv/bin/python vision/pipeline.py screenshot.png --resize --text --engine applevision
.venv/bin/python audio/pipeline.py --text "aping into PEPE right now"
.venv/bin/python signal/ipc.py --mock
```

## CAN / CANNOT

**Может:**
- читать с экрана mint, тикеры, платформу и действие стримера, исправлять типичные OCR-ошибки в адресе;
- слышать торговые фразы и тикеры;
- собирать pump.fun v3 buy/sell по актуальному IDL с точным (сверенным симуляцией) учётом комиссий;
- рассылать одну транзакцию в Helius Sender, несколько регионов Jito и RPC, с live-tip и live-priority fee;
- вести позиции с TP/SL/timeout, в т.ч. бумажные с реальной симуляцией в DRY_RUN;
- проверять себя против mainnet (`--selfcheck`, `--probe-relays`) после апгрейдов pump.fun.

**Не может:**
- **покупать на кривых, квотированных не в SOL** (USDC, другие pump-коины) — частый случай с октября 2026; нужен multi-hop;
- торговать cashback-коины (v3 их отклоняет; новых cashback-коинов не создают) и токены после миграции (PumpSwap) — позиция закрывается как `GRADUATED`, продавать вручную;
- покупку, которая выбирает остаток кривой (synthetic migration) — отклоняется;
- обогнать задержку самой платформы стрима;
- надёжно отличить оригинал от копиката по одному тикеру (за это штраф −0.15);
- понимать сарказм и «не покупайте это».

**Статус live:** транзакции собираются по актуальному IDL и проходят mainnet-симуляцию; релеи принимают формат. Реальная сделка с деньгами в рамках разработки не проводилась — начните с минимального `BUY_SOL`.

## Disclaimer

Экспериментальный проект для исследований и обучения; не финансовый совет. Мемкоины крайне волатильны, многие — скам, а сигналы в стримах можно подделать специально под такие боты. Автоматическая торговля по чужому контенту может нарушать правила площадок. Используйте только те средства, которые готовы потерять, держите на торговом кошельке минимум и не коммитьте `.env`. Авторы не несут ответственности за убытки.
