# MEV STREAMER — Build Guide

## Step 1: Open 5 terminals

In each one, start Claude Code and cd into the module folder.

---

### Terminal 1 — ingestion (model: Sonnet 5.5)

```bash
cd "/Volumes/DEXP W500C/mev-streamer/ingestion"
```

Prompt:

```
You are in the ingestion/ folder of the mev-streamer project. Build a livestream capture module for real-time analysis.

What it does: takes a stream URL (Twitch/Kick/YouTube), extracts video frames and audio chunks, and passes them through multiprocessing.Queue.

Files:
  capture.py       — StreamCapture class + MultiStreamCapture
  config.py        — loads from .env (STREAM_URL, STREAM_FPS=3, STREAM_AUDIO_CHUNK_SEC=3)
  requirements.txt — numpy, python-dotenv, Pillow
  __init__.py

capture.py:
- video_worker: streamlink/yt-dlp to get the direct URL, then an ffmpeg subprocess:
  ffmpeg -i <url> -vf "fps=FPS,scale=1280:720" -f rawvideo -pix_fmt rgb24 pipe:1
  Read stdout frame by frame (1280*720*3 bytes), put into video_queue as (timestamp, frame_bytes)
- audio_worker: a separate ffmpeg process:
  ffmpeg -i <url> -vn -f s16le -ar 16000 -ac 1 pipe:1
  Read in chunks (16000*2*CHUNK_SEC bytes), put into audio_queue
- Auto-reconnect with exponential backoff (1s, 2s, 4s, max 30s)
- Queue maxsize=30; when full, drop the oldest frame but do NOT drop audio
- MultiStreamCapture: run several streams, each in its own process
- Standalone test: python capture.py <url> — capture 10 s, print FPS
- SIGINT: kill ffmpeg, drain queues

Do NOT create .env. .env.example lives in the project root (../).
```

---

### Terminal 2 — vision (model: Sonnet 5.5)

```bash
cd "/Volumes/DEXP W500C/mev-streamer/vision"
```

Prompt:

```
You are in the vision/ folder of the mev-streamer project. Build a video-frame analysis module — OCR, Solana address detection and trading-platform recognition.

Files:
  ocr.py            — OCREngine: EasyOCR (GPU) or Tesseract (CPU fallback)
  detector.py       — TokenDetector: Solana address regex [1-9A-HJ-NP-Za-km-z]{32,44}, pump.fun mint ends with "pump"
  ui_recognizer.py  — UIRecognizer: identifies the platform (Photon/BullX/GMGN/pump.fun/Dexscreener) by color palette and layout, detects actions (viewing_token, about_to_buy, bought, sold)
  pipeline.py       — VisionPipeline: OCR → detection → UI recognition → VisionResult
  requirements.txt  — easyocr, opencv-python, Pillow, numpy, python-dotenv
  __init__.py

Key details:
- Create the OCR reader ONCE (heavy initialization)
- For speed, run OCR on 3 ROIs instead of the whole frame: URL bar (top 5%), token header (top 15%), buttons (right 30%)
- UI recognizer: color-histogram heuristics, NOT heavy ML
- detector.py: base58 validation, joining OCR blocks that got split
- Tickers: $TICKER pattern (2-10 uppercase)
- Actions: OCR finds "Buy"/"Sell"/"Confirm"/"Success" → corresponding action
- Standalone test: python pipeline.py <image.jpg>
```

---

### Terminal 3 — audio (model: Sonnet 5.5)

```bash
cd "/Volumes/DEXP W500C/mev-streamer/audio"
```

Prompt:

```
You are in the audio/ folder of the mev-streamer project. Build a real-time speech recognition and trading key-phrase detection module.

Files:
  transcriber.py      — RealtimeTranscriber: faster-whisper (CTranslate2), models tiny.en/small.en/medium.en
  keyword_spotter.py  — KeywordSpotter: buy/watch/sell phrases + ticker extraction
  pipeline.py         — AudioPipeline: transcribe → spot → AudioResult
  requirements.txt    — faster-whisper, numpy, python-dotenv
  __init__.py

transcriber.py:
- Input: PCM bytes (16kHz, 16bit, mono)
- np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
- VAD: RMS threshold, skip silence
- beam_size=1, language="en" for speed
- Create the model ONCE

keyword_spotter.py:
- BUY: "buying this", "aping in", "sending it", "going in", "i'm in", "sniping this", "full send", "yolo"
- WATCH: "looking at", "watching this", "check this out", "could be big", "might buy"
- SELL: "selling", "taking profit", "i'm out", "dump it", "cutting losses"
- Fuzzy matching: exact=0.9, 1 edit=0.7, 2 edits=0.5
- Ticker extraction: the word after a buy phrase, filter stopwords, 2-15 chars

Standalone test: python pipeline.py <audio.wav>
```

---

### Terminal 4 — signal (model: Sonnet 5.5)

```bash
cd "/Volumes/DEXP W500C/mev-streamer/signal"
```

Prompt:

```
You are in the signal/ folder of the mev-streamer project. Build the signal aggregation and IPC module.

This module is the central hub. It imports from sibling folders:
  from ingestion.capture import StreamCapture
  from vision.pipeline import VisionPipeline
  from audio.pipeline import AudioPipeline

Files:
  scorer.py       — SignalScorer: signal weights (visual_mint=0.5, audio_buy=0.3, platform=0.15, both_confirm=+0.2)
  dedup.py        — SignalDedup: cooldown per mint (default 60s)
  resolver.py     — TokenResolver: ticker → mint via Jupiter API (GET https://token.jup.ag/strict) + cache
  ipc.py          — ExecutorClient: Unix socket /tmp/mev-streamer.sock, JSON + newline
  orchestrator.py — MAIN FILE: runs capture → vision → audio → scoring → dedup → resolve → execute
  requirements.txt — aiohttp, python-dotenv
  __init__.py

orchestrator.py (entry point for the whole Python stack):
- python signal/orchestrator.py [--monitor-only] [--dry-run] [--stream URL] [--threshold 0.7]
- Main loop: grab frame/audio from queues → process → score → threshold check → dedup → resolve ticker → send to executor
- Colored log: 🟢 BUY SIGNAL [0.87] mint=... src=both / 🟡 WATCH [0.45] ticker=... src=audio
- SIGINT: session stats (signals detected, buy signals, unique tokens, signals sent)
- sys.path.insert to import from sibling folders
- async for IPC and resolver

IPC JSON format (one line):
  {"mint":"...","ticker":"...","action":"buy","confidence":0.85,"source":"both","context":"...","timestamp":1699999999.1}
Executor reply:
  {"status":"ok","bundle_id":"..."} or {"status":"error","message":"..."}
```

---

### Terminal 5 — executor (model: Opus 4.6)

```bash
cd "/Volumes/DEXP W500C/mev-streamer/executor"
```

Prompt:

```
You are in the executor/ folder of the mev-streamer project. Build a Rust module for low-latency trade execution on Solana via Jito bundles. It receives signals over a Unix Domain Socket.

Files:
  Cargo.toml
  src/main.rs, config.rs, tx_builder.rs, jito.rs, rpc.rs, pda.rs, bonding.rs, position.rs, ipc.rs, types.rs

Crates: solana-sdk 2.2, solana-client 2.2, spl-token 7, spl-associated-token-account 5, bs58 0.5, tokio (full), serde+serde_json, dotenvy, reqwest (json)

IPC: tokio UnixListener on /tmp/mev-streamer.sock, newline-delimited JSON
Input: {"mint":"...","action":"buy","confidence":0.85,...}
Reply: {"status":"ok","bundle_id":"..."} or {"status":"error","message":"..."}

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

Position: poll every 3s, exit on TP(2.0x)/SL(0.5x)/timeout(120s), sell is also a Jito bundle
Config from .env: RPC_URL, WALLET_PRIVATE_KEY, BUY_SOL, JITO_TIP, TAKE_PROFIT, STOP_LOSS, TIMEOUT_SEC, MAX_POSITIONS, SLIPPAGE_PCT, DRY_RUN
All amounts are u64 lamports. NO floats for money. Cache recent_blockhash for 30s.
SIGINT: close socket, print stats.
```

---

## Step 2: Wait for all 5 modules to finish

Check that every terminal has completed its work.

---

## Step 3: Integration (new terminal, model: Sonnet 5.5)

```bash
cd "/Volumes/DEXP W500C/mev-streamer"
```

Prompt:

```
You are in the root of the mev-streamer project. 5 modules are already built in subfolders (ingestion/, vision/, audio/, signal/, executor/). Do NOT overwrite them.

Tasks:
1. Read each module's code. Check that:
   - signal/orchestrator.py imports correctly from ingestion/, vision/, audio/
   - the IPC socket path matches between signal/ipc.py and executor/src/ipc.rs
   - the signal JSON schema matches Python↔Rust
   - .env variables are the same everywhere
   Fix any mismatches.

2. Create:
   - requirements.txt (root) — merge all subfolder requirements.txt files
   - scripts/build.sh — pip install + cargo build --release
   - scripts/run.sh — start executor in the background + orchestrator, SIGINT cleanup
   - scripts/dry-run.sh — same with DRY_RUN=true
   - scripts/monitor.sh — Python only, --monitor-only, no executor
   - pyproject.toml or setup.py for the Python part

3. README.md — long, no images (the author will add them):
   - Badges: Python 3.10+, Rust, Solana, Jito MEV, GPU optional
   - One line on what it does
   - ASCII flow: Stream → Capture → OCR+Whisper → Signal → Jito Bundle
   - How it works (6 steps)
   - Dual detection: Visual + Audio
   - Supported platforms: Photon, BullX, GMGN, pump.fun, Dexscreener
   - Latency breakdown table
   - Architecture tree
   - Quick start
   - Config table (all .env)
   - Three modes: monitor / dry-run / live
   - Hardware: CPU-only vs GPU
   - CAN / CANNOT
   - Disclaimer
   - <!-- BANNER IMAGE HERE --> at the top

4. Add __init__.py wherever it is missing. Run cargo check for Rust.
```

---

## Step 4: Git + Push

```bash
cd "/Volumes/DEXP W500C/mev-streamer"
git init
git add -A
git commit -m "initial: mev-streamer platform"
```

Create the GitHub repo (zostaff/mev-streamer) and push.

---

## Running the finished bot

```bash
# 1. Install dependencies
./scripts/build.sh

# 2. Copy the config
cp .env.example .env
# Fill in: RPC_URL, WALLET_PRIVATE_KEY, STREAM_URL

# 3. Monitor only (safe)
./scripts/monitor.sh --stream "twitch.tv/username"

# 4. Dry run (full pipeline, no trades)
./scripts/dry-run.sh --stream "twitch.tv/username"

# 5. Live (real trades)
./scripts/run.sh --stream "twitch.tv/username"
```
