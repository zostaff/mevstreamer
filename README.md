<!-- BANNER IMAGE HERE -->

# mev-streamer

![Python](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)
![Rust](https://img.shields.io/badge/rust-2021-000000?logo=rust&logoColor=white)
![Solana](https://img.shields.io/badge/solana-mainnet-9945FF?logo=solana&logoColor=white)
![Jito](https://img.shields.io/badge/Jito%20%2B%20Helius%20Sender-fan--out-1DB954)
![GPU](https://img.shields.io/badge/GPU-optional-lightgrey)

**Смотрит livestream, по экрану и голосу ловит момент, когда стример заходит в токен, и покупает его на pump.fun — на bonding curve или, если токен уже мигрировал, на PumpSwap — одной подписанной транзакцией, разосланной сразу в Helius Sender и Jito.**

```
 Stream ──▶ Capture ──▶ OCR + Whisper ──▶ Signal ──▶ pump.fun v3 tx ──▶ Sender ‖ Jito ‖ RPC
 (Twitch/    (ffmpeg      (mint, $TICKER,   (score,     (fee-aware quote,    (same signature,
  Kick/YT)    3fps + PCM)  platform, phrase) guards)     risk limits)         lands once)
                                                                    │
                                     journal ◀── TP / SL / trailing / timeout on real value
```

---

## How it works

1. **Capture.** `streamlink`/`yt-dlp` достают прямой URL стрима, два процесса `ffmpeg` выдают кадры 1280×720 (3 fps) и PCM 16 kHz кусками по 3 с. Обрыв → переподключение с backoff 1→30 с; watchdog убивает зависший ffmpeg (HLS без EOF) через 15 с без данных, оркестратор перезапускает умерший захват. Анализируется всегда самый свежий кадр, устаревшее аудио пропускается — задержка не копится.
2. **Vision.** OCR кадра: на macOS — Apple Vision (Neural Engine, ~0.1 с/кадр), иначе EasyOCR. Детектор ищет Solana-адреса и `$TICKER`, UI-распознаватель — платформу и действие стримера. **Опознание без адреса**: с экрана читаются market cap / ликвидность / холдеры (`vision/metrics.py`); если адреса нет, но есть `$TICKER` (или заголовок страницы, напр. «JIMOTHY») и капитализация, резолвер берёт все одноимённые токены и выбирает тот, чья текущая капитализация в ±25% от экранной — только при единственном совпадении (на живом эфире: `$JIMOTHY` из 18 одноимённых за 0.7 с). Такой mint весит 0.40 вместо 0.50. **Восстановление адресов**: если OCR испортил pump-mint (`1→I`, `2→Z`, `o→0`…), генерируются исправления и проверяются на mainnet одним `getMultipleAccounts` (кеш по строке).
3. **Audio.** faster-whisper с VAD транскрибирует речь, keyword spotter ловит торговые фразы и тикеры.
4. **Signal.** Скользящие окна (vision 3 с, audio 10 с), скоринг confidence 0…1, cooldown по токену. Защита от ложных входов: mint должен быть виден минимум на 2 кадрах; если на экране несколько разных mint (чат-оверлей) — BUY не отправляется; buy-фраза привязывается только к mint, видимому ±2 с от момента речи; отрицания и «rug/scam/stay away» гасят сигнал. Сигналы только по тикеру (без mint на экране) по умолчанию не отправляются (`ALLOW_TICKER_ONLY`).
5. **Quote & build.** Executor за один RPC round-trip берёт кривую + минт + канонический PumpSwap-пул и его хранилища ‖ priority fee ‖ blockhash (кеш) ‖ баланс. Кривая не завершена → `buy_exact_quote_in_v3` с комиссиями из pump-fees `FeeConfig`; токен мигрировал → PumpSwap `buy_exact_quote_in_v2` (wrap SOL → buy → unwrap в той же tx) с комиссиями LP/protocol/creator по тирам капитализации. Обе квоты — порт официального `pump-rust-client`, сверены симуляцией до лампорта.
6. **Land.** Одна подписанная tx параллельно уходит во все релеи (Helius Sender, Jito-регионы, RPC) — одна подпись исполняется не более одного раза. Подтверждение — `getSignatureStatuses` каждые 200 мс, засчитывается только `confirmed`. «Не попала» объявляется только когда **высота блока перевалила `lastValidBlockHeight`** — не по таймеру; ошибка релея не считается «не отправлено». Так tx, приземлившаяся позже таймаута, не превращается в неучтённые токены и повторную покупку.
7. **Manage.** Себестоимость — реальная дельта баланса из приземлившейся tx (комиссии, tip'ы, rent). Выход по **стоимости позиции** (сколько вернёт продажа всего сейчас, с комиссиями) против себестоимости: take-profit, stop-loss, trailing stop от пика, timeout; опрос каждые 400 мс. Продажа — весь баланс + закрытие ATA (возврат ~0.002 SOL rent). Для stop-loss/timeout/повторов slippage растёт до `EXIT_SLIPPAGE_PCT`, чтобы обвал не запер позицию.
8. **Journal.** Каждое изменение позиции пишется в `logs/positions.json` (атомарно), события — в `logs/trades.jsonl`. После рестарта executor сверяет журнал с on-chain балансами кошелька и продолжает вести позиции; незавершённые покупки дожидается.

## Dual detection: Visual + Audio

| Признак | Канал | Вес |
|---|---|---|
| mint-адрес на экране, ≥2 кадров (× 0.8–1.0 по OCR-confidence; восстановленный ≤ 0.9) | visual | +0.50 |
| `$TICKER` на экране | visual | +0.20 |
| распознана торговая платформа | visual | +0.15 |
| стример смотрит токен / на кнопке Buy / купил | visual | +0.10 / +0.30 / +0.40 |
| buy-фраза в речи | audio | +0.30 |
| тикер в речи | audio | +0.15 |
| оба канала подтверждают один токен | both | +0.20 |
| тикер без mint (риск копиката) | — | −0.15 |

Сумма обрезается до 1.0; порог исполнения по умолчанию 0.7. Mint на экране + платформа + buy-фраза (0.95) — проходит; только buy-фраза с тикером в речи (0.30) — нет. Короткие бытовые фразы («let's go», «I'm in», «going on») — не buy-фразы; SELL — только явные («sold it», «taking profits», «I'm out»).

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
| Executor: состояние кривой + квота + сборка | ~130–150 мс (1 RPC round-trip) | dry-run на mainnet |
| Round-trip стоимость сделки (buy → sell сразу) | **−2.5%** кривая / **−0.6…−2%** PumpSwap | комиссии; `--selfcheck` / `--selfcheck-amm` / paper |
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
        ├── main.rs         Executor: buy/sell, сверка с on-chain, risk-проверки, восстановление
        ├── journal.rs      positions.json (снапшот) + trades.jsonl (события)
        ├── bonding.rs      BondingCurve/Global/FeeConfig + fee-aware v3 квоты
        ├── tx_builder.rs   buy_exact_quote_in_v3 / sell_v3 (17 аккаунтов), compute budget, tips
        ├── pumpswap.rs     PumpSwap v2: Pool/GlobalConfig/FeeConfig, квоты, buy/sell с wrap/unwrap SOL
        ├── sender.rs       fan-out: Helius Sender ‖ Jito ‖ RPC, подтверждение по подписи
        ├── jito.rs         Jito sendTransaction (bundleOnly), live tip_floor
        ├── rpc.rs          JSON-RPC: аккаунты, blockhash, priority fee, simulate
        ├── selfcheck.rs    --selfcheck, --selfcheck-amm, --inspect, --probe-relays
        ├── position.rs     TP / SL / trailing / timeout по стоимости, лимиты сессии
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

- Квоты — порт референсной математики `pump-rust-client 0.5.0` (MIT). `--selfcheck` на mainnet: **квота buy совпала с симуляцией до единицы токена**; полный выход одной симулированной tx (buy → sell всего → close ATA) — **дельта баланса до лампорта** (проверено на 3 mayhem-коинах, 2026-11-09).
- Mayhem-коины (а это большинство свежих токенов в выборке 2026-11) поддерживаются; у них нет synthetic migration — слишком большая покупка просто отклоняется.
- **Мигрировавшие токены (PumpSwap)** — `buy_exact_quote_in_v2` / `sell_v2`. `--selfcheck-amm` на 3 пулах (в т.ч. с ненулевыми virtual reserves и fee buckets): покупка и полный выход **до лампорта**, buy 81–84k CU, round trip 139–143k CU. Позиция на кривой, которая мигрировала во время удержания, автоматически переезжает на пул. Не поддерживаются cashback-пулы (v2 их отклоняет) и пулы не в wSOL.
- Старые «короткие» аккаунты кривой читаются с нулевым добором (как в официальном декодере).
- Отказ с понятной причиной: кривая завершена (graduated), кривая квотирована не в SOL, cashback-коин (v3 их не принимает), покупка, пересекающая остаток кривой (synthetic migration), продажа больше реальных резервов кривой.

**Кривые не в SOL.** Официальный путь — PumpSwap `multi_hop_swap` (SOL → A → B одной инструкцией). Но все встреченные на mainnet не-SOL цепочки заканчиваются **xStock**'ами (`Xs…`, токенизированные акции, Token-2022 с pausable/transfer-hook): например `GgN3…pump` квотирован в `Xsc9…` (TSLAx-подобный). Ни один pump-маршрут не ведёт к ним из SOL, поэтому multi-hop не помог бы ни в одном наблюдённом случае — отложено. `--inspect <MINT>` показывает цепочку квот.

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
executor/target/release/mev-streamer-executor --new-wallet ~/mevs-key.txt   # отдельный hot-кошелёк для бота
executor/target/release/mev-streamer-executor --selfcheck <любой живой pump-mint на SOL-кривой>
executor/target/release/mev-streamer-executor --probe-relays     # endpoint'ы и формат, ничего не тратит
```

Дальше: `dry-run.sh` на нескольких реальных стримах и разбор `logs/trades.dry.jsonl` — PnL бумажных сделок покажет, есть ли у стратегии край, до того как рисковать деньгами.

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
| `WHISPER_MODEL` / `WHISPER_DEVICE` | `base.en` / auto | audio | CUDA если есть, иначе CPU int8; `small.en` — только с GPU |
| `CAPTURE_STALL_TIMEOUT_SEC` | 15 | ingestion | перезапуск ffmpeg без данных |
| `SIGNAL_THRESHOLD` / `SIGNAL_COOLDOWN_SEC` | 0.7 / 60 | signal | |
| `ALLOW_TICKER_ONLY` | false | signal | отправлять BUY без mint на экране (резолв по тикеру — риск копиката) |
| `HELIUS_API_KEY` | из `RPC_URL` | signal | проверка mint при резолве тикера |
| `BUY_SOL` | — | executor | бюджет входа (комиссии внутри него) |
| `RELAYS` | `sender,jito` | executor | `sender` (Helius Sender), `jito`, `rpc` — через запятую |
| `JITO_TIP` / `JITO_TIP_MAX` | `auto` / 0.001 | executor | auto = p75 из Jito `tip_floor` (обновление раз в 2 с), не выше MAX |
| `JITO_URLS` | global engine | executor | несколько регионов через запятую |
| `SENDER_TIP` / `HELIUS_SENDER_URL` | 0.000005 / global `/fast` | executor | ≥ 0.001 → Sender Max; меньше → автоматически `?swqos_only=true` |
| `PRIORITY_FEE` | `auto` | executor | auto = p75 недавних комиссий по этой кривой, в `[PRIORITY_FEE_MIN, PRIORITY_FEE_MAX]` µL/CU |
| `CU_LIMIT_BUY` / `CU_LIMIT_SELL` | 90000 / 60000 | executor | кривая; замерено: buy 70–75k, sell 46k CU |
| `CU_LIMIT_AMM_BUY` / `CU_LIMIT_AMM_SELL` | 100000 / 75000 | executor | PumpSwap; замерено: buy 81–84k, sell ~58k CU |
| `TAKE_PROFIT` / `STOP_LOSS` / `TIMEOUT_SEC` | 2.0 / 0.6 / 120 | executor | множители **стоимости позиции** к реальной себестоимости |
| `TRAILING_STOP` | 0.25 | executor | продать на 25% ниже пика, когда пик ≥ 1.2×; 0 = выкл |
| `SLIPPAGE_PCT` / `EXIT_SLIPPAGE_PCT` | 15 / 40 | executor | вход и TP / потолок для SL, timeout и повторов |
| `MAX_POSITIONS` | 3 | executor | одновременно |
| `MAX_SESSION_LOSS` | 3 × `BUY_SOL` | executor | реализованный убыток сессии → покупки остановлены; 0 = выкл |
| `MAX_CONSECUTIVE_LOSSES` | 5 | executor | подряд убыточных → покупки остановлены |
| `MIN_SOL_RESERVE` | 0.01 | executor | остаток на кошельке после покупки (на выход) |
| `MAX_SIGNAL_AGE_MS` | 6000 | executor | сигнал старше (от захвата кадра/аудио) — отброшен |
| `MIN_CONFIDENCE` / `MAX_MCAP_SOL` / `REBUY` | 0 / выкл / false | executor | доп. фильтры; повторная покупка того же mint за сессию |
| `MONITOR_INTERVAL_MS` / `STATE_DIR` | 400 / `logs` | executor | опрос правил выхода / журнал |
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
./scripts/check.sh              # cargo test (43), compileall, keyword spotter, mint repair, unit checks (56), smoke test
./scripts/check.sh --network    # + smoke test с настоящим executor'ом
executor/target/release/mev-streamer-executor --selfcheck <MINT>   # buy + полный выход против mainnet-симуляции
executor/target/release/mev-streamer-executor --selfcheck-amm <MINT>  # то же для мигрировавшего токена (PumpSwap)
executor/target/release/mev-streamer-executor --inspect <MINT>     # цепочка квот, где торгуется каждый уровень
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
- вести позиции с TP / SL / trailing / timeout по реальной стоимости, в т.ч. бумажные (с учётом собственной покупки на кривой);
- переживать рестарт: журнал позиций сверяется с on-chain балансами, незавершённые покупки дожидаются;
- не купить дважды из-за «таймаута», который на деле приземлился; не запуститься вторым экземпляром на том же кошельке;
- останавливать покупки по лимитам сессии (убыток, серия потерь, резерв SOL, возраст сигнала);
- проверять себя против mainnet (`--selfcheck`, `--probe-relays`) после апгрейдов pump.fun.

**Не может:**
- покупать на кривых, квотированных не в SOL (наблюдались только цепочки в xStock'и — из SOL недостижимы);
- торговать cashback-коины (v3/v2 их отклоняют; новых не создают);
- покупку, которая выбирает остаток кривой (synthetic migration) — отклоняется;
- обогнать задержку самой платформы стрима;
- надёжно отличить оригинал от копиката по одному тикеру — без адреса покупает только если тикер + экранная капитализация однозначно указывают на один токен;
- понимать сарказм (явные отрицания и «rug/scam» — понимает);
- проверять «чистоту» токена (dev-кошелёк, бандлы, концентрацию холдеров) — только market cap (`MAX_MCAP_SOL`).

**Статус live:** транзакции собираются по актуальному IDL, buy и полный выход сходятся с mainnet-симуляцией до лампорта; релеи принимают формат; восстановление из журнала и защита от второго экземпляра проверены на mainnet с пустым кошельком. Реальная сделка с деньгами в рамках разработки не проводилась, и прибыльность стратегии не измерена — сначала paper-прогоны, потом минимальный `BUY_SOL`.

## Disclaimer

Экспериментальный проект для исследований и обучения; не финансовый совет. Мемкоины крайне волатильны, многие — скам, а сигналы в стримах можно подделать специально под такие боты. Автоматическая торговля по чужому контенту может нарушать правила площадок. Используйте только те средства, которые готовы потерять, держите на торговом кошельке минимум и не коммитьте `.env`. Авторы не несут ответственности за убытки.
