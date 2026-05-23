# AlphaLive Roadmap

## Current State

The **AlphaLab → AlphaLive** pipeline is fully operational:

- Strategies are developed and backtested in [AlphaLab](https://github.com/bernardoguterres/AlphaLab), then exported as a versioned JSON config
- AlphaLive loads the config, connects to Alpaca Markets, and runs the strategy 24/7 on Railway
- Signal logic is verified against AlphaLab backtests via a dedicated signal parity test suite (zero-mismatch requirement before every deploy)
- 8 strategies supported across daily, intraday (1H/15Min), and weekly timeframes
- Telegram notifications for every trade, daily P&L summaries, and a live web dashboard

## Planned

### AlphaSignal Integration

[AlphaSignal](https://github.com/bernardoguterres/AlphaSignal) is a financial RAG system that extracts sentiment signals from SEC EDGAR filings and financial news. The planned integration adds a pre-trade sentiment filter to AlphaLive:

- Before entering a position, query AlphaSignal's `/sentiment/{ticker}` endpoint
- If the 5-day or 20-day sentiment trend is strongly negative, suppress the entry signal
- Configurable per-strategy via a new `sentiment_filter` block in the strategy JSON schema
- Falls back gracefully if AlphaSignal is unavailable — trading continues unaffected

This is an enrichment layer, not a dependency. AlphaLive runs correctly without it.

### Other Planned Work

- WebSocket feed for intraday strategies (currently polling Alpaca REST API every 15 minutes)
- Portfolio-level position sizing (Kelly Criterion or volatility-weighted) across multi-strategy deployments
- Automated monthly re-deploy when AlphaLab detects strategy drift vs live performance

## Notes

AlphaSignal is production-ready as a standalone RAG project — it ingests filings, serves a FastAPI sentiment endpoint, and has 74 passing tests. The integration with AlphaLive is the next logical step once live trading results are available to measure signal quality against.
