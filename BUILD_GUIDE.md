# MEV STREAMER — Build Guide

## Шаг 1: Открой 5 терминалов

В каждом запусти Claude Code и зайди в папку модуля.

---

### Терминал 1 — ingestion (модель: Sonnet 5.5)

```bash
cd "/Volumes/DEXP W500C/mev-streamer/ingestion"
```

Промпт:

```
Ты в папке ingestion/ проекта mev-streamer. Построй модуль захвата видеостримов для real-time анализа.

Что делает: принимает URL стрима (Twitch/Kick/YouTube), извлекает видео фреймы и аудио чанки, передаёт через multiprocessing.Queue.

Файлы:
  capture.py       — StreamCapture class + MultiStreamCapture
  config.py        — загрузка из .env (STREAM_URL, STREAM_FPS=3, STREAM_AUDIO_CHUNK_SEC=3)
  requirements.txt — numpy, python-dotenv, Pillow
  __init__.py

capture.py:
- video_worker: streamlink/yt-dlp для получения прямого URL, затем ffmpeg subprocess:
  ffmpeg -i <url> -vf "fps=FPS,scale=1280:720" -f rawvideo -pix_fmt rgb24 pipe:1
  Читать stdout по фреймам (1280*720*3 bytes), класть в video_queue как (timestamp, frame_bytes)
- audio_worker: отдельный ffmpeg процесс:
  ffmpeg -i <url> -vn -f s16le -ar 16000 -ac 1 pipe:1
  Читать по чанкам (16000*2*CHUNK_SEC bytes), класть в audio_queue
- Auto-reconnect с exponential backoff (1s, 2s, 4s, max 30s)
- Queue maxsize=30, если полная — drop oldest frame но НЕ drop audio
- MultiStreamCapture: запуск нескольких стримов, каждый в своём процессе
- Standalone test: python capture.py <url> — захватить 10 сек, показать FPS
- SIGINT: kill ffmpeg, drain queues

НЕ создавай .env. .env.example в корне проекта (../).
```

---

### Терминал 2 — vision (модель: Sonnet 5.5)

```bash
cd "/Volumes/DEXP W500C/mev-streamer/vision"
```

Промпт:

```
Ты в папке vision/ проекта mev-streamer. Построй модуль визуального анализа видеофреймов — OCR, детекция Solana адресов и распознавание торговых платформ.

Файлы:
  ocr.py            — OCREngine: EasyOCR (GPU) или Tesseract (CPU fallback)
  detector.py       — TokenDetector: Solana address regex [1-9A-HJ-NP-Za-km-z]{32,44}, pump.fun mint ends with "pump"
  ui_recognizer.py  — UIRecognizer: определяет платформу (Photon/BullX/GMGN/pump.fun/Dexscreener) по цветовой палитре и layout, детектирует действия (viewing_token, about_to_buy, bought, sold)
  pipeline.py       — VisionPipeline: OCR → detection → UI recognition → VisionResult
  requirements.txt  — easyocr, opencv-python, Pillow, numpy, python-dotenv
  __init__.py

Ключевые детали:
- OCR reader создавать ОДИН РАЗ (тяжёлая инициализация)
- Для скорости OCR не на весь фрейм, а на 3 ROI: URL bar (top 5%), token header (top 15%), buttons (right 30%)
- UI recognizer: heuristics на цветовых гистограммах, НЕ тяжёлый ML
- detector.py: валидация base58, склейка разбитых OCR блоков
- Tickers: паттерн $TICKER (2-10 uppercase)
- Actions: OCR находит "Buy"/"Sell"/"Confirm"/"Success" → соотв. action
- Standalone test: python pipeline.py <image.jpg>
```

---

### Терминал 3 — audio (модель: Sonnet 5.5)

```bash
cd "/Volumes/DEXP W500C/mev-streamer/audio"
```

Промпт:

```
Ты в папке audio/ проекта mev-streamer. Построй модуль real-time распознавания речи и детекции торговых ключевых фраз.

Файлы:
  transcriber.py      — RealtimeTranscriber: faster-whisper (CTranslate2), модели tiny.en/small.en/medium.en
  keyword_spotter.py  — KeywordSpotter: buy/watch/sell фразы + тикер extraction
  pipeline.py         — AudioPipeline: transcribe → spot → AudioResult
  requirements.txt    — faster-whisper, numpy, python-dotenv
  __init__.py

transcriber.py:
- Вход: PCM bytes (16kHz, 16bit, mono)
- np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
- VAD: RMS threshold, пропускать тишину
- beam_size=1, language="en" для скорости
- Модель создавать ОДИН РАЗ

keyword_spotter.py:
- BUY: "buying this", "aping in", "sending it", "going in", "i'm in", "sniping this", "full send", "yolo"
- WATCH: "looking at", "watching this", "check this out", "could be big", "might buy"
- SELL: "selling", "taking profit", "i'm out", "dump it", "cutting losses"
- Fuzzy matching: exact=0.9, 1 edit=0.7, 2 edits=0.5
- Ticker extraction: слово после buy-фразы, filter stopwords, 2-15 chars

Standalone test: python pipeline.py <audio.wav>
```

---

### Терминал 4 — signal (модель: Sonnet 5.5)

```bash
cd "/Volumes/DEXP W500C/mev-streamer/signal"
```

Промпт:

```
Ты в папке signal/ проекта mev-streamer. Построй модуль агрегации сигналов и IPC.

Этот модуль — центральный хаб. Импортирует из соседних папок:
  from ingestion.capture import StreamCapture
  from vision.pipeline import VisionPipeline
  from audio.pipeline import AudioPipeline

Файлы:
  scorer.py       — SignalScorer: веса сигналов (visual_mint=0.5, audio_buy=0.3, platform=0.15, both_confirm=+0.2)
  dedup.py        — SignalDedup: cooldown per mint (default 60s)
  resolver.py     — TokenResolver: ticker → mint через Jupiter API (GET https://token.jup.ag/strict) + cache
  ipc.py          — ExecutorClient: Unix socket /tmp/mev-streamer.sock, JSON + newline
  orchestrator.py — ГЛАВНЫЙ ФАЙЛ: запуск capture → vision → audio → scoring → dedup → resolve → execute
  requirements.txt — aiohttp, python-dotenv
  __init__.py

orchestrator.py (точка входа всего Python стека):
- python signal/orchestrator.py [--monitor-only] [--dry-run] [--stream URL] [--threshold 0.7]
- Main loop: grab frame/audio from queues → process → score → threshold check → dedup → resolve ticker → send to executor
- Цветной лог: 🟢 BUY SIGNAL [0.87] mint=... src=both / 🟡 WATCH [0.45] ticker=... src=audio
- SIGINT: session stats (signals detected, buy signals, unique tokens, signals sent)
- sys.path.insert для импорта из соседних папок
- async для IPC и resolver

IPC JSON формат (одна строка):
  {"mint":"...","ticker":"...","action":"buy","confidence":0.85,"source":"both","context":"...","timestamp":1699999999.1}
Ответ от executor:
  {"status":"ok","bundle_id":"..."} или {"status":"error","message":"..."}
```

---

### Терминал 5 — executor (модель: Opus 4.6)

```bash
cd "/Volumes/DEXP W500C/mev-streamer/executor"
```

Промпт:

```
Ты в папке executor/ проекта mev-streamer. Построй Rust модуль для low-latency исполнения сделок на Solana через Jito bundles. Принимает сигналы через Unix Domain Socket.

Файлы:
  Cargo.toml
  src/main.rs, config.rs, tx_builder.rs, jito.rs, rpc.rs, pda.rs, bonding.rs, position.rs, ipc.rs, types.rs

Crates: solana-sdk 2.2, solana-client 2.2, spl-token 7, spl-associated-token-account 5, bs58 0.5, tokio (full), serde+serde_json, dotenvy, reqwest (json)

IPC: tokio UnixListener на /tmp/mev-streamer.sock, newline-delimited JSON
Вход: {"mint":"...","action":"buy","confidence":0.85,...}
Ответ: {"status":"ok","bundle_id":"..."} или {"status":"error","message":"..."}

pump.fun V3:
  Program: 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P
  Global: 4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf
  Fee Recipient: CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbCJ5tGLDMDSRp
  Event Authority: Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1
  Fee Config: 62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV
  Buy disc: d78f037c39e9b3b6, Sell disc: 33e685a4017f83ad
  Buy data: [8 disc][8 amount_in u64][8 min_tokens_out u64]
  12 accounts: Global, FeeRecipient, mint, bondingCurve(PDA seeds ["bonding-curve",mint]), bondingCurveATA, buyerATA, buyer(signer), System, Token, Rent, EventAuth, PumpProgram
  Bonding state: [8 skip][8 virtualTokenReserves][8 virtualSolReserves][8 realTokenReserves][8 realSolReserves][8 tokenTotalSupply][1 complete]
  Math: tokens_out = (sol_in * vtoken) / (vsol + sol_in), slippage: min_out = expected * (100-slippage) / 100

TX: VersionedTransaction v0: SetComputeUnitLimit(200000) + SetComputeUnitPrice(100000) + CreateATA(if needed) + Buy + JitoTip
Jito: POST https://mainnet.block-engine.jito.wtf/api/v1/bundles sendBundle
Tips: 96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5, HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe, Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY, ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49, DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh, ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt, DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL, 3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT

Position: poll каждые 3s, exit on TP(2.0x)/SL(0.5x)/timeout(120s), sell тоже Jito bundle
Config из .env: RPC_URL, WALLET_PRIVATE_KEY, BUY_SOL, JITO_TIP, TAKE_PROFIT, STOP_LOSS, TIMEOUT_SEC, MAX_POSITIONS, SLIPPAGE_PCT, DRY_RUN
Все суммы u64 lamports. НЕ float для финансов. recent_blockhash кешировать 30s.
SIGINT: close socket, print stats.
```

---

## Шаг 2: Дождись завершения всех 5 модулей

Проверь что каждый терминал закончил работу.

---

## Шаг 3: Сборка (новый терминал, модель: Sonnet 5.5)

```bash
cd "/Volumes/DEXP W500C/mev-streamer"
```

Промпт:

```
Ты в корне проекта mev-streamer. 5 модулей уже построены в подпапках (ingestion/, vision/, audio/, signal/, executor/). НЕ перезаписывай их.

Задачи:
1. Прочитай код каждого модуля. Проверь:
   - signal/orchestrator.py корректно импортирует из ingestion/, vision/, audio/
   - IPC socket path совпадает между signal/ipc.py и executor/src/ipc.rs
   - JSON schema сигналов совпадает Python↔Rust
   - .env переменные одинаковые
   Несоответствия — исправь.

2. Создай:
   - requirements.txt (корневой) — объединить все requirements.txt подпапок
   - scripts/build.sh — pip install + cargo build --release
   - scripts/run.sh — запуск executor в фоне + orchestrator, SIGINT cleanup
   - scripts/dry-run.sh — то же с DRY_RUN=true
   - scripts/monitor.sh — только Python, --monitor-only, без executor
   - pyproject.toml или setup.py для Python части

3. README.md — большой, без картинок (автор вставит сам):
   - Badges: Python 3.10+, Rust, Solana, Jito MEV, GPU optional
   - Одна строка что делает
   - ASCII flow: Stream → Capture → OCR+Whisper → Signal → Jito Bundle
   - How it works (6 шагов)
   - Dual detection: Visual + Audio
   - Supported platforms: Photon, BullX, GMGN, pump.fun, Dexscreener
   - Latency breakdown таблица
   - Architecture tree
   - Quick start
   - Config table (все .env)
   - Три режима: monitor / dry-run / live
   - Hardware: CPU-only vs GPU
   - CAN / CANNOT
   - Disclaimer
   - <!-- BANNER IMAGE HERE --> в начале

4. Добавь __init__.py если где-то нет. Проверь cargo check для Rust.
```

---

## Шаг 4: Git + Push

```bash
cd "/Volumes/DEXP W500C/mev-streamer"
git init
git add -A
git commit -m "initial: mev-streamer platform"
```

Создай репо на GitHub (zostaff/mev-streamer) и push.

---

## Порядок запуска готового бота

```bash
# 1. Установить зависимости
./scripts/build.sh

# 2. Скопировать конфиг
cp .env.example .env
# Заполнить: RPC_URL, WALLET_PRIVATE_KEY, STREAM_URL

# 3. Только мониторинг (безопасно)
./scripts/monitor.sh --stream "twitch.tv/username"

# 4. Dry run (полный пайплайн без сделок)
./scripts/dry-run.sh --stream "twitch.tv/username"

# 5. Live (реальные сделки)
./scripts/run.sh --stream "twitch.tv/username"
```
