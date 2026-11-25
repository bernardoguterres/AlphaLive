# Alpha bug tracker — November 2026 audit

Every bug below was **reproduced** (repro scripts in the audit folders noted per section), not guessed.
Status: **FIXED** = changed in code, tested, merged to `main` — AlphaLab `8f16a4b` (F3), `52dbaa5` (F1/F4), `de48507` (F6); AlphaLive `770ffe7` (F5), `2660c21` (F8), `7b76e63` (F1/F2/F4), `8ec636f` (F6), `dfd15cb` (F7), `dc1b95a` (F9). **OPEN** = confirmed, not fixed.

## Summary

| Repo | Fixed | Open |
|---|---|---|
| AlphaLive | 16 | 4 |
| AlphaLab | 10 | 19 |
| AlphaLab ↔ AlphaLive (cross-repo, incl. F5/F6) | 5 | 2 |
| AlphaSignal | 2 | 11 + ops notes |
| **Total** | **33** | **36** |

All four High-severity items (X1, X2, S8, plus B7 as the live-config one) are fixed as of 24 Nov 2026 (AlphaLab#7, AlphaLive#8, AlphaSignal#5).

**Merged to `main`:** batch 1 (AlphaLab#3, AlphaLive#3), batch 2 (AlphaLab#4, AlphaLive#4, AlphaSignal#4), batch 3 (AlphaLive#5), lint (AlphaLive#6), end-to-end findings (AlphaLab#5, AlphaLab#6, AlphaLive#7). F26: AlphaLab#6.

Both audit waves are complete (first wave: one agent per repo; second wave: cross-repo export/import, AlphaLive scripts/configs/deploy, AlphaLab API+UI, AlphaSignal ingestion/ops).

---

## FIXED

| ID | Repo | Bug | Fix |
|---|---|---|---|
| F1 | AlphaLive + AlphaLab | `ma_crossover` only buys on the cross day, so any stop-loss/take-profit exit left it in cash until the next golden cross (production SPY config: OOS Sharpe −0.93 vs −0.15 with re-entry). | New opt-in `reentry_after_protective_exit` + `reentry_wait_bars` in both repos (strategy, backtest engine, schemas, export contract v1.3, UI, replay). Cross-repo test proves both re-enter on the same day. |
| F2 | AlphaLive | (found by audit in F1) The re-entry window only opened if the protective close filled inside the submit response; real Alpaca returns `accepted`, so it would almost never open live. | Window opens when the close is accepted (`pending`/`partial`); ledger sync closes it if the close never completes. Regression test on the simulator fails without the fix. |
| F3 | AlphaLab | Browser tab showed the old Lovable heart favicon (AlphaLab's own `logo.png` was deleted in commit 6f0df5f). | Original logo restored and linked; Lovable `favicon.ico` removed. |
| F4 | AlphaLab ↔ AlphaLive | (found by cross-repo audit in F1) AlphaLive's risk cooldown (`cooldown_bars`, exported from `cooldown_days`) delayed re-entries that AlphaLab took immediately (MSFT: live 2 days late, 3 times). | AlphaLab's engine now applies the same cooldown to re-entries (bars since the last strategy order's decision bar). Cross-repo test runs AlphaLive's real `RiskManager.check_cooldown`; with cooldown 20 the re-entry moves from bar 78 to 81 in both. |
| F5 | AlphaSignal ↔ AlphaLive (was S1) | A rejected API key (401/403) was treated as an outage, so the sentiment gate silently switched off under the default fail-open policy (BUY allowed on a −0.9 score). | 401/403 now `misconfigured_block` (blocks entries whatever the policy, CRITICAL log); 404 (ticker not covered) is `no_data` and follows the fail policy. `tests/test_alphasignal_auth_errors.py`. |
| F6 | AlphaLab + AlphaLive (was B3) | MA crossover's volume/separation filters also dropped death crosses, trapping positions (AAPL held through 2022). | Filters now screen entries only, in both repos; parity fixtures regenerated from AlphaLab (4 previously-dropped exits now SELL). Tests in both repos. |
| F7 | AlphaLive (was L1) | Leftover trailing-stop high / entry time after a manual close sold the next entry on the first exit check. | Startup ledger sync clears them; a BUY fill that opens from flat also starts them fresh. `tests/test_position_lifecycle_fixes.py`. |
| F8 | AlphaLive (was L3) | Max-trades and API-budget halts latched the manual pause; neither the daily reset nor `/resume` cleared it. | Limits now block while breached and lift when their counter resets (hour rollover also checked in `can_trade`); alert once per breach; `/resume` lists them as still active. |
| F9 | AlphaLive (was L2) | A strategy SELL was dropped for the day when the latest-trade price fetch failed. | SELL falls back to the broker's position mark, then the last close (BUY behaviour unchanged). |
| F10 | AlphaLive (was L5) | EOD/shutdown summary crashed (`float * None`) after a same-day round trip. | Win rate from broker-confirmed `fill_events` round trips; order-based fallback skips unpriced orders. AlphaLive#4. |
| F11 | AlphaLive (was L4) | `portfolio_max_positions` ignored entries not yet filled. | In-flight BUY intents count toward the cap. AlphaLive#4. |
| F12 | AlphaLive (was L7) | `release_quarantine.py` edited the state file under a running bot. | Takes the bot's exclusive lock; exits 3 while locked. AlphaLive#4. |
| F13 | AlphaLive (was L12) | No `.dockerignore`: local images baked in `.env`. | `.dockerignore` excludes env files, tooling state, logs, data (not build-verified: no Docker daemon). AlphaLive#4. |
| F14 | AlphaLab (was B1) | Test runs wrote synthetic prices into the real cache. | Session fixture redirects the real cache dir to a temp dir; a full run no longer touches `data/cache`. AlphaLab#4. |
| F15 | AlphaLab (was B11) | Data page unmounted on any per-ticker fetch error. | Errors typed as `{ticker, error}` and rendered as text. AlphaLab#4. |
| F16 | AlphaLab (was B12) | Trade Table P&L contradicted the backend (one-buy-one-sell pairing). | Ported backend `round_trips()` (quantity FIFO per ticker). AlphaLab#4. |
| F17 | AlphaLab (was B13) | Heatmap months lost the month-boundary move; dates shifted west of UTC. | Months chained from previous month-end; UTC getters / formatting. AlphaLab#4. |
| F18 | AlphaSignal (was S2) | `/health` blocked the event loop (40 calls → `/sentiment` 7 s). | Plain `def` (threadpool) + cached verified-vector count. AlphaSignal#4. |
| F19 | AlphaLive (was L8, L9) | Report scripts crashed on every run (`QueryOrderStatus.FILLED`) and mis-paired lots. | Shared `alphalive/utils/fill_matching.py`: CLOSED orders with fills, FIFO by quantity. AlphaLive#5. |
| F20 | AlphaLive (was L11) | Position sizes could add up to over 100% of equity (silent margin). | `--validate-only`/`verify_deployment` fail such sets; startup warns. AlphaLive#5. |
| F21 | AlphaLive (was L10) | `verify_deployment.py` said "Safe to deploy" for configs the bot refuses. | Runs the bot's own offline validation; honours `STRATEGY_CONFIG_DIR`. AlphaLive#5. |
| F22 | AlphaLab | A backtest whose data covered only part of the requested window ran silently on the shorter span; that is how synthetic test entries left in the cache were served as AAPL 2020–2024. | `data_quality_warnings` now says when data starts N days late or ends N days early; the run summary shows it. The 6 poisoned cache entries were quarantined to `backend/data/cache/_quarantine_2026-11-23/`. AlphaLab#5. |
| F23 | AlphaLab | (regression from F-B13) Timezone-less bar dates were parsed as UTC midnight, so west of UTC trades showed the previous day (Sunday exits). | `parseBarDate` keeps the calendar day; tests under London, New York, Tokyo and UTC. AlphaLab#5. |
| F24 | AlphaLab | 422 responses carried Pydantic's debug text ("1 validation error for FetchDataRequest ... errors.pydantic.dev"), shown verbatim in UI toasts. | One `field: message` line per problem. AlphaLab#5. |
| F25 | AlphaLive | A BUY that passed the sentiment gate, or went through on a fail-open bypass, was logged only at DEBUG. | Every gated BUY logs one INFO line with score, gate status and fail policy. AlphaLive#7. |
| F26 | AlphaLab (was B24) | Catch-all handler turned 400/405 into 500 with a traceback; `[]`/`null` bodies crashed; screener `top_n: "abc"` was a 500. | `json_body()` helper; HTTPException codes pass through. AlphaLab#6. |

| F27 | AlphaLab (was X2) | Stops were checked on the daily close and filled at the next open; AlphaLive closes intraday on live prices. 23 of 53 round trips on the SPY/MSFT fixtures exited differently. | Stops checked against each bar's low/high, filled at the level (or the open on a gap); stop assumed when stop and target share a bar; trailing peak ratchets with the high after the check. Same comparison: 4/53 differ (same-day strategy sells at the open). AlphaLab#7. |
| F28 | AlphaLive (was X1) | After a risk exit the stateful strategies were reset to flat and re-bought on the next entry bar; AlphaLab's strategies wait for their own exit (trend_adaptive_rsi SPY: 4 round trips vs 14 live). | Shadow position: the engine keeps the strategy's own state while the ledger is flat; the strategy's own SELL becomes a no-order HOLD. Replay of 12 exports: identical entry dates 12/12. AlphaLive#8. |
| F29 | AlphaSignal (was S8) | Section extraction dropped all text before the first matched heading and took cross-references/TOC lines for headings (JPM 10-Qs kept 0.6-3%, 10-K MD&A stored as item_8). | Cross-references skipped, longest run per item, MD&A opening sentence recognised, front matter kept. 306 cached filings: minimum text kept 0.6% → 100%. Chunker ch8; corpus unchanged until re-ingested (needs OpenAI embeddings). AlphaSignal#5. |
| F30 | AlphaLab (was B7) | A monthly backtest exported as `timeframe: 1Day`. | Export refuses intervals AlphaLive cannot trade (422); mapping no longer defaults. AlphaLab#7. |
| F31 | AlphaLive | (found by the first real paper connection, 24 Nov) `ALPACA_BASE_URL=https://paper-api.alpaca.markets/v2`, as Alpaca's dashboard shows it, made every request 404 (`/v2/v2/...`): the bot exits at startup. The read-only paper check missed it because it pins the URL. | Trailing `/v2` stripped in `BrokerConfig`. AlphaLive#8. |
| F32 | AlphaLive | A revoked Telegram token was retried 3× per message (1+2+4 s) and every 10 minutes forever. | 401/404 is final: one CRITICAL log naming the fix, notifications off for the run, trading unaffected. AlphaLive#8. |

---

## OPEN — AlphaLive (repros: `scratchpad/audit_alphalive/`)

| ID | Severity | Bug | Where |
|---|---|---|---|
| ~~L1~~ FIXED (F7) | Wrong order | Leftover trailing-stop high (and entry timestamp) after a position is closed manually while the bot is down → the next new entry is sold on the first exit check ("Trailing stop hit (high $200, now $150)"). | `main.py` `_sync_position_ledger`, `order_manager.py:573-575` |
| ~~L2~~ FIXED (F9) | Failed exit | Strategy SELL is dropped for the day if `get_current_price()` fails, although a SELL doesn't use the price. For `ma_crossover` that exit exists only on the cross day. | `main.py:823` |
| ~~L3~~ FIXED (F8) | Silent stop | Max-trades and API-budget emergency halts set the manual-pause flag; neither the next day's reset nor `/resume` clears it (`/resume` replies "Trading Resumed" but BUYs stay blocked). Only a restart clears it. | `risk_manager.py:758,775`, `telegram_commands.py:447-450` |
| ~~L4~~ FIXED (F11) | Risk miscount | `portfolio_max_positions` counts only filled positions; two strategies can both enter before either fills (cap 1 → 2 positions). | `main.py:849-863` |
| ~~L5~~ FIXED (F10) | Crash (reporting) | EOD summary crashes (`float * None`) after a same-day round trip when the submit response had no fill price; the SIGTERM summary too. | `main.py:97,105-116`, `order_manager.py:455` |
| L6 | Minor | Telegram `/status` and `/close_all` show `int(qty)`, so fractional small-account positions read "0 shares". | `telegram_commands.py:302,518` |

### AlphaLive scripts / configs / deploy, wave 2 (repros: `scratchpad/wave2_live_ops/`)

| ID | Severity | Bug | Where |
|---|---|---|---|
| ~~L7~~ FIXED (F12) | High | `release_quarantine.py` opens the state file without the exclusive lock, so it runs while the bot is live; the bot's next save reverts it (or the script overwrites fresh fills). | `scripts/release_quarantine.py:33` |
| ~~L8~~ FIXED (F19) | High | `performance_tracker.py`, `generate_trade_journal.py`, `automated_weekly_report.py` crash on every run (`QueryOrderStatus.FILLED` doesn't exist in alpaca-py 0.28). | `:109`, `:69`, `:66` |
| ~~L9~~ FIXED (F19) | High (reports) | Those scripts' P&L pairs one SELL with one whole BUY lot: 10@100 + 5@110, sell 15@120 → reports 200 and 1 open position (truth: 250, none open). | `performance_tracker.py:187`, `generate_trade_journal.py:132`, `automated_weekly_report.py:102` |
| ~~L10~~ FIXED (F21) | Medium | `verify_deployment.py` says "Safe to deploy" for a config folder the bot would refuse (never calls `validate_all`, ignores `STRATEGY_CONFIG_DIR`). | `scripts/verify_deployment.py` |
| ~~L11~~ FIXED (F20) | Medium | No check that position sizes fit the account: the four production `ma_crossover` configs (30% each) size $120k of BUYs on $100k — silent margin use. | sizing / validation |
| ~~L12~~ FIXED (F13) | Medium | No `.dockerignore`: a local `docker build` copies `.env` (and `.claude/settings.local.json`) into the image, and `run.py` loads it (`ALPACA_PAPER=false` from a stray `.env` turns live trading on). Railway's git build is unaffected. | repo root, `Dockerfile` |
| L13 | Med-Low | Dashboard realized P&L uses today's average buy cost instead of FIFO (buy 10@100, sell@110, buy 10@120, sell@125 → shows 150, truth 50); partly-filled-then-cancelled BUYs ignored. | `dashboard/index.html:683-698` |
| L14 | Low | `live_signal_monitor.py` uses 1-minute bars for 15Min strategies and daily bars for 1Week. | `scripts/live_signal_monitor.py:107-112` |
| L15 | Low | Broken paths/data: `analyze_logs.py` matches a log line the bot never writes (always 0 trades, HOLD counted twice); `test_replay_mode.sh` and `Procfile` point at configs that don't exist; `configs/research/vwap_reversion_AAPL_2020-2024.json` contains an error message, not a strategy. | various |

Also known, not code bugs: `.env` `ALPACA_BASE_URL` ends in `/v2` (doubles the path → 404); daily 1Day check only runs 09:35–09:59 ET; `security_audit.sh` "CRITICAL" is a false positive (regex matches the variable name, no real keys in history).

## OPEN — AlphaLab (repros: `scratchpad/audit_alphalab/`)

| ID | Severity | Bug | Where |
|---|---|---|---|
| ~~B1~~ FIXED (F14) | Wrong numbers | Test runs write synthetic prices into the real price cache under real keys (e.g. MSFT 2020–2024); a server started within 24h backtests fake data. 4 poisoned entries currently in `backend/data/cache` (all past expiry). | `tests/test_frontend_integration.py:20-55`, `cache_manager.py:57` |
| B2 | Wrong numbers | Batch backtest silently ignores `max_drawdown_pct`/`drawdown_halt_enabled` (META Greenblatt: single 115% vs batch 25%) and reports raw orders as "trades". | `blueprints/backtest.py:403-433`, `validators.py:321`, `BatchBacktest.tsx:54-63` |
| ~~B3~~ FIXED (F6) | Wrong money (live too) | MA crossover volume filter also blocks EXITS: a death cross on low volume is ignored (AAPL held through the 2022 bear market). Mirrored in AlphaLive. | `moving_average_crossover.py:96`, AlphaLive `signal_engine.py:458` |
| B4 | Wrong numbers | VWAP reversion "exit short" emits BUY → opens real longs in the long-only engine (27/37 SPY buys). | `vwap_reversion.py:122-155` |
| B5 | Misleading | Failed preconditions (missing column, too few bars) return `status: ok` with −100% return. | `engine.py:141-147` |
| B6 | Wrong numbers | Compare / optimize / heatmap ignore the strategy's bar interval (weekly strategy run on daily bars). | `backtest.py:182,222,308` |
| ~~B7~~ FIXED (F30) | Wrong live config | A monthly backtest exports as `timeframe: 1Day`. | `helpers.py:47,164` |
| B8 | Misleading | Open positions at the end of a run are excluded from win rate / profit factor without saying so (META PF 2.0 vs 9.34). | `metrics.py` (`include_open_trades` unused) |
| B9 | Research-only | `PortfolioConstructor._run_dynamic` ranks and fills on the same close. | `portfolio_constructor.py` |
| B10 | Robustness | String/float window params → 500; `monte_carlo_runs` accepts negatives / unbounded. | validators |

### AlphaLab API + UI, wave 2 (repros: `scratchpad/wave2_lab_ui/`)

| ID | Severity | Bug | Where |
|---|---|---|---|
| ~~B11~~ FIXED (F15) | High (crash) | Data page goes blank (whole app unmounts) on any per-ticker fetch error: `errors` items are `{ticker, error}` objects passed to `toast.error`. Same root cause shows "[object Object]" on Backtest. | `DataManager.tsx:55`, `Backtest.tsx:99`, `types/index.ts:259` |
| ~~B12~~ FIXED (F16) | High (wrong numbers) | Trade table pairs one buy with one sell instead of backend FIFO-by-quantity: rsi_simple table sum −$27,319 vs backend +$1,151. | `utils/tradePairing.ts:15-37` |
| ~~B13~~ FIXED (F17) | High (wrong numbers) | Monthly heatmap measures each month first→last point, losing the gap from the previous month-end (2021 shows +1.06% vs actual −7.53%); local-time date getters shift months in US timezones. | `MonthlyReturnsHeatmap.tsx:10-19` |
| B14 | Med-High | One transient yfinance error marks a ticker invalid until the server restarts. | `fetcher.py:193-196` |
| B15 | Medium | Cache hits always report quality 1.0, so Data-page Refresh is a no-op showing 100%; scores keyed by ticker only. | `fetcher.py:78-82,346`, `DataManager.tsx:219` |
| B16 | Medium | Saved-run history (~174 KB each) overflows browser storage at ~32 runs (uncaught QuotaExceededError); no Clear button since the redesign. | `backtestStore.ts:44` |
| B17 | Medium | Batch export labels results with whatever strategy is selected now, not the one that ran. | `BatchBacktest.tsx:72-76` |
| B18 | Medium | The Interval select never reaches the backtest request; the quality check ignores interval. | `Backtest.tsx:102-114,132` |
| B19 | Medium | Optimizer ignores risk settings and the drawdown halt and counts orders as trades (+6.99%/134 vs single run +1.15%/63). | `parameter_optimizer.py:93-119` |
| B20 | Medium (DoS) | Unbounded optimize grid (300k combos accepted); heatmap `np.arange` runs before the cell cap (step=0 → 500); compare accepts 40 duplicates. | `parameter_optimizer.py:46`, `backtest.py:247` |
| B21 | Med-Low (security) | Settings accept `text/plain` JSON, so a cross-site form POST changes settings without a CORS preflight (`paper_trading: false` written). | settings blueprint `get_json(force=True)` |
| B22 | Low-Med | `_results_store` grows without eviction; runs reopened from history 404 on export after a restart. | results store |
| B23 | Low | Data fetch rejects tickers like BRK-B (`isalpha()`), failing the whole batch. | `validators.py:25` |
| ~~B24~~ FIXED (F26) | Low | Catch-all handler turns 400/405 (bad JSON, invalid dates) into 500 "Internal server error". | `routes.py:59` |
| B25 | Low | Data page "Coverage" column shows the requested range, not the dates the data actually covers. | Data page |
| B26 | Low | Strategy parameter summary line omits the re-entry setting. | strategy params summary |

## OPEN — AlphaLab ↔ AlphaLive boundary (repros: `scratchpad/wave2_crossrepo/`)

Real AlphaLab exports loaded through AlphaLive's loader on the SPY/MSFT fixtures. Signal parity itself is clean; these are differences in what happens AROUND the signals, which the parity tests don't cover (they run without stop-loss/take-profit/cooldown).

| ID | Severity | Bug | Where |
|---|---|---|---|
| ~~X1~~ FIXED (F28) | High | After a stop-loss/take-profit exit, AlphaLive marks stateful strategies flat and re-buys on the next oversold bar; AlphaLab's strategy never learns of risk exits and waits for its own exit. `trend_adaptive_rsi` SPY: 4 round trips in AlphaLab vs 14 live; `bollinger_rsi_combo` SPY 10 vs 13. Also affects `greenblatt_weekly`. | AlphaLab `engine.py:150,385`; AlphaLive `main.py:1041` |
| ~~X2~~ FIXED (F27) | High | Stops are checked on the daily close in AlphaLab but on live prices every 5 min in AlphaLive (trailing high too): 23 of 53 round trips exit differently, ~7 change from win to loss. | AlphaLab `portfolio.py:262,288`; AlphaLive `main.py:1000` |
| X3 | Med-Low | `greenblatt_weekly` minimum hold is one week too long live (entry timestamp booked at fill time, elapsed days floored), so optional exits fire a week late or are lost. | AlphaLive `state.py:572,599` |
| X4 | Low | `greenblatt_weekly` can be exported on daily bars via the API; AlphaLive then treats `min_hold_bars` 52 as weeks. | AlphaLab `_resolve_interval`; AlphaLive `main.py:283` |

## OPEN — AlphaSignal (repros: `scratchpad/audit_alphasignal/`)

S9b (Low-Med, found while fixing S8): MD&A is still not labelled in 144/306 cached filings. 128 have no item headings the parser recognises (kept whole as `full_text`); in 16 the MD&A sits inside a neighbouring item. No text is lost; sentiment just can't prefer MD&A chunks there.

S9c (Medium, seen 24 Nov after the ch8 re-ingest): sentiment's `_pick_chunks` takes the earliest chunks of the preferred section, which for JPM's MD&A is the procedural opening ("should be read in conjunction with..."), scored +0.22 as "no substantive information".
S9d (Medium, same run): a news item about Hyperliquid was attributed to JPM (scored −0.68), the known headline-mention proxy limit.

| ID | Severity | Bug | Where |
|---|---|---|---|
| ~~S1~~ FIXED (F5) | High | A wrong/missing API key or unknown ticker makes `/sentiment` 401/404, which AlphaLive treats as an outage → gate silently off (BUY allowed on a −0.9 score). | AlphaLive `alphasignal_client.py:242,436-444`; AlphaSignal `api/dependencies.py:173` |
| ~~S2~~ FIXED (F18) | Med-High | `/health` is unauthenticated and does blocking SQLite work inside `async def`; 40 concurrent calls push `/sentiment` to 7 s → past AlphaLive's 6 s timeout. | `api/routes/health.py:52-93` |
| S3 | Medium | Sentiment prompt and cache key ignore the ticker: "Microsoft surges… Apple slumps" scores +0.8 for both. | `generation/sentiment.py:84-93,139` |
| S4 | Medium | "Verbatim evidence" check passes with no evidence (`all([])` is True; a string splits into characters). | `sentiment.py:203-206` |
| S5 | Med-Low | Valid non-object JSON from the model (`0.7`, `[…]`, `null`) crashes `/sentiment` with 500; out-of-range scores clamped and kept as reliable. | `sentiment.py:186-190` |
| S6 | Low-Med | A zero-confidence document decides the score when alone (plain-mean fallback). | `sentiment.py:324-327` |
| S7 | Low | Hybrid retrieval: all-zero BM25 scores normalise to 1.0 and crowd out relevant hits (`/query` only); `ticker_filter` not uppercased. | `retriever.py:482-533` |

Ops notes: the corpus ends 2026-08-07, so every `/sentiment` call today is `stale` and the gate never evaluates; 60 tests fail only because `tiktoken` can't download `cl100k_base` offline.

### AlphaSignal ingestion + ops, wave 2 (repros: `scratchpad/wave2_signal_ops/`)

**Why data stops at 2026-08-07:** nothing is broken; ingestion was simply never run again. There is no scheduler, cron, Procfile worker or caller of `/ingest`. A rerun works today (the Yahoo feed returned fresh items), but Yahoo only gives ~2 days of news per run, so a **daily scheduled ingest** is needed for the 30-day freshness window.

| ID | Severity | Bug | Where |
|---|---|---|---|
| ~~S8~~ FIXED (F29) | High | `extract_sections` drops all text before the first matched section header: JPM 10-Qs keep only 0.6–3% of their text (MD&A lost); JPM 10-K MD&A stored as `item_8`. JPM has 103 10-Q chunks vs GS 6,377, so JPM sentiment is scored on boilerplate. | `ingestion/edgar.py:374-403` |
| S9 | Medium | Ingestion failures are silent: Reuters feed host doesn't resolve and `feedparser` returns 0 entries with no warning (all news ever stored is Yahoo); an SEC 403/rate-limit returns `[]` and `/ingest` reports "completed". | `edgar.py:188`, `news.py:86-126` |
| S10 | Medium | Two processes ingesting at once (API + `build_corpus.py`) can roll back the vector-store manifest and delete the other's vectors, leaving SQLite chunks with no vectors. | `vector_store.py:181,323,373` |
| S11 | Low-Med | Filings already on disk ignore the date window (a 2015–2019 backfill returns the 2026 filing); every run re-chunks all 306 filings. | `edgar.py:125` |
| S12 | Low | `compute_ndcg` sizes the ideal score by results returned, not k, rewarding fewer results (latent: golden set unannotated). | `evaluator.py` |
| S13 | Low | Sentiment eval fixture's 15 events all fall in the 2020–2023 corpus gap (0% coverage by construction); committed `eval_results_2026-08-07.json` predates the `date_to` fix. | `run_eval` fixture |

---

## End-to-end run (2026-11-23, `testing/e2e-2026-11-23/`)

AlphaLab API export → real `run.py` AlphaLive process (fake clock, Alpaca simulator) → real AlphaSignal API on the real corpus (42,078 vectors), with a local model stub instead of OpenAI (no network, no cost). After AlphaLive#7, all 5 scenarios pass:

| Scenario | Result |
|---|---|
| A. Sentiment −0.9 | BUY blocked, `available_block` |
| B. Sentiment +0.5 | 1 BUY (171.646 sh), INFO `Sentiment gate passed ... available_pass` |
| C. Wrong API key | BUY blocked, `misconfigured_block` |
| D. AlphaSignal down, fail_policy=open | 1 BUY, INFO `... error_bypass fail_policy=open` |
| D2. AlphaSignal down, fail_policy=closed | BUY blocked |
