# AlphaLive Roadmap

## Current State

The **AlphaLab → AlphaLive** pipeline is fully operational:

- Strategies are developed and backtested in [AlphaLab](https://github.com/bernardoguterres/AlphaLab), then exported as a versioned JSON config
- AlphaLive loads the config, connects to Alpaca Markets, and runs the strategy 24/7 on Railway
- Signal logic is verified against AlphaLab backtests via a dedicated signal parity test suite (zero-mismatch requirement before every deploy)
- 8 strategies supported across daily, intraday (1H/15Min), and weekly timeframes
- Telegram notifications for every trade, daily P&L summaries, and a live web dashboard

## Done

### AlphaSignal Integration (2026-05-25)

[AlphaSignal](https://github.com/bernardoguterres/AlphaSignal) is now wired into AlphaLive's execution gate as a pre-trade sentiment filter.

- `alphalive/services/alphasignal_client.py` — async `httpx` client for `GET /sentiment/{ticker}`
- `run_pre_execution_checks()` — concurrent gate using `asyncio.gather`; AlphaSignal and DeepLOB (placeholder) run in parallel before every order
- Strongly negative sentiment (`score < -0.3`) blocks long entries; strongly positive (`score > 0.3`) blocks shorts
- Fail-open on timeout or any error — trading never blocked by inference unavailability
- Fully configurable via env vars (`ALPHASIGNAL_URL`, `ALPHASIGNAL_ENABLED`, `ALPHASIGNAL_SENTIMENT_THRESHOLD`, etc.)
- 7 new tests; 255 total passing
- Note: AlphaSignal's FAISS index is still empty — filter is live but returns neutral scores until real filings are ingested

## Planned

### DeepLOB Integration (Integration D)

DeepLOB is a deep learning model trained on Level 2 order book (LOB) data that predicts short-term mid-price direction. The planned integration adds it as a second concurrent filter in the existing execution gate alongside AlphaSignal:

- `alphalive/services/deeplob_client.py` — async client for the DeepLOB inference server
- `deeplob/serve.py` — minimal FastAPI inference server that loads the trained model
- The `deeplob_client=None` placeholder in `run_pre_execution_checks()` is already reserved for this
- Only executes if predicted direction matches signal direction with confidence ≥ 0.6
- Fail-open when LOB snapshot unavailable (Alpaca free tier has no L2 feed) or on timeout
- Blocked by: model still in training; no L2 data feed configured

### Other Planned Work

- WebSocket feed for intraday strategies (currently polling Alpaca REST API every 15 minutes)
- Portfolio-level position sizing (Kelly Criterion or volatility-weighted) across multi-strategy deployments
- Automated monthly re-deploy when AlphaLab detects strategy drift vs live performance
