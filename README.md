# Claude AI Trading Bot

A fully automated multi-symbol perpetual trading bot for **Hyperliquid** with a
web dashboard, structured trade journal, and a self-improving strategy
reviewer:

1. **Sentiment engine** — MiniMax scores broad crypto sentiment every 15 min
   from 6 free RSS feeds + Fear & Greed + on-chain context (BTC dominance,
   funding rates), then tunes position sizing.
2. **Dual-AI trade gate** — every BUY/SELL signal is reviewed by Gemini AND
   MiniMax in parallel; both must approve before the order goes in.
3. **Trade journal** — every ENTRY/EXIT is logged as one JSONL line with the
   full decision context (RSI/EMA/BB readings, AI votes, sentiment, funding,
   config snapshot) so every trade is later attributable.
4. **Flask + Caddy dashboard** — live positions, equity curve, per-symbol PnL,
   editable config, AI suggestions with one-click Apply, journal download.
5. **Daily strategy reviewer** — Gemini 2.5 Pro digests the journal each night
   and proposes RSI/EMA/BB parameter tweaks within hard safety bounds; the
   operator approves them on the dashboard (or `AUTO_STRATEGY_EVOLVE=True`
   applies them automatically).

Trades **SOL, ETH, ADA** simultaneously with independent per-symbol position
limits.

---

## Architecture

```
┌────────────────────────── bot.py (every 60s) ──────────────────────────┐
│                                                                         │
│  kill switch ──► tick():                                                │
│    sample equity                                  ┐                     │
│    log equity (every 5 min) ─► journal/equity-*.jsonl                   │
│    for symbol in [SOL, ETH, ADA]:                                       │
│      1) trailing-stop check on existing position  │                     │
│      2) candles → RSI(14) + EMA(9,21) + BB(20,2)  │                     │
│      3) composite signal (BUY/SELL/HOLD)          │                     │
│      4) Trade Gate (Gemini ‖ MiniMax) — both GO   │                     │
│      5) market_open                               │                     │
│           ENTRY ─► journal/journal-*.jsonl ◄──────┘                     │
│      EXIT (trailing/flip/kill) ─► journal/journal-*.jsonl               │
└─────────────────────────────────────────────────────────────────────────┘
                       ▲                   ▲                ▲
                       │ reads             │ reads          │ reads
                       │                   │                │
              ┌────────┴────────┐  ┌───────┴────────┐  ┌────┴───────────┐
              │  ai_analyst.py  │  │ strategy_      │  │ dashboard.py   │
              │  (every 15 min) │  │  reviewer.py   │  │ (Flask :8080)  │
              │                 │  │  (cron daily)  │  │                │
              │  3-round AI:    │  │                │  │  /api/state    │
              │  RSS + FNG +    │  │  Pairs trades  │  │  /api/config   │
              │  funding +      │  │  → stats →     │  │  /api/equity-* │
              │  BTC dom        │  │  Gemini 2.5    │  │  /api/pnl-*    │
              │                 │  │  Pro           │  │  /api/run-     │
              │  → suggested_   │  │                │  │   reviewer     │
              │    capital      │  │  → suggested_  │  │  /api/apply-*  │
              │    or live      │  │    strategy or │  │  /api/download │
              │    (gated by    │  │    overrides   │  │                │
              │    AUTO_CAPI    │  │  (gated by     │  │  Charts:       │
              │    TAL_TUNE)    │  │   AUTO_STRAT.  │  │   • equity     │
              │                 │  │   EVOLVE)      │  │   • PnL/symbol │
              └────────┬────────┘  └───────┬────────┘  └────────┬───────┘
                       │                   │                    │
                       ▼                   ▼                    ▼
                  ┌──────────────────────────────────────────────────┐
                  │                  config.json                     │
                  │  TRADE_SIZE_MULTIPLIER, DAILY_LOSS_LIMIT,        │
                  │  AUTO_CAPITAL_TUNE, AUTO_STRATEGY_EVOLVE,        │
                  │  TRADE_GATE_ENABLED, strategy_overrides{},       │
                  │  ai_meta{ suggested_capital, suggested_strategy }│
                  └──────────────────────────────────────────────────┘
```

| File | Role |
|---|---|
| `bot.py` | Main 60-second tick loop, multi-symbol orchestration, journal writes, equity sampler |
| `strategy.py` | Composite signal — EMA trend filter + RSI/Bollinger entry. Reads `strategy_overrides` from `config.json` first, falls back to `config.py` |
| `exchange.py` | Hyperliquid SDK wrapper |
| `risk.py` | Account-level drawdown kill switch |
| `ai_analyst.py` | 3-round AI sentiment cycle. Honours `AUTO_CAPITAL_TUNE` (suggestion vs. live apply) |
| `trade_gate.py` | Dual-AI per-trade approval gate (returns structured votes for journal) |
| `journal.py` | Append-only JSONL trade & equity journals; monthly rotation; 6-month retention |
| `strategy_reviewer.py` | Daily reviewer — pairs trades, computes stats, asks Gemini Pro for parameter tweaks within safety bounds |
| `dashboard.py` | Flask app on `127.0.0.1:8080`. JSON API + single-page UI |
| `templates/dashboard.html` | Single-page dashboard. Vanilla JS + Chart.js (CDN) |
| `deploy/Caddyfile` | Public HTTPS reverse proxy with Basic Auth |
| `deploy/bot.service` | systemd unit for the bot |
| `deploy/dashboard.service` | systemd unit for the dashboard |
| `settings.py` | Atomic load/save for `config.json` |
| `config.py` | Static settings (symbols, base sizes, defaults for tunable params) |
| `config.json` | Dynamic settings written by AI / dashboard |
| `compare_ai.py` | A/B testing harness for Gemini vs MiniMax (manual use) |
| `logger.py` | Timestamped console logger |

---

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# Then edit .env with the four keys below.
```

### Required environment variables (`.env`)

| Key | Where to get it |
|---|---|
| `HYPERLIQUID_PRIVATE_KEY` | app.hyperliquid.xyz → API → Generate (use the **agent** key, never the main wallet key) |
| `HYPERLIQUID_ADDRESS` | Your main wallet address (the one holding USDC) |
| `GEMINI_API_KEY` | https://aistudio.google.com/apikey (free tier covers all our usage) |
| `MINIMAX_API_KEY` | https://platform.minimax.io (international) |

---

## Run

### Locally — two terminals

```bash
# Terminal 1: the bot
python bot.py

# Terminal 2: the dashboard
python dashboard.py
# → open http://127.0.0.1:8080
```

### Manual one-off scripts

```bash
# Run the AI sentiment cycle once (writes config.json, no orders)
python ai_analyst.py

# Run the strategy reviewer (needs >=5 closed trades in lookback window)
python strategy_reviewer.py --lookback-days 30 --dry-run    # prints suggestion
python strategy_reviewer.py --lookback-days 30              # writes to config.json

# A/B test Gemini vs MiniMax (5 runs each)
python compare_ai.py 5
```

### Long-running deployment

For production use the systemd units in `deploy/` — see **Deployment to a VM**
below. Don't use tmux/`python bot.py` for live capital, you lose auto-restart.

---

## Configuration

The system has three layers of configuration, in order of how often they change:

1. **`config.py`** — static defaults (symbols, base trade sizes, thresholds).
   Change requires a bot restart.
2. **`config.json`** — dynamic settings written by the AI and the dashboard.
   Bot reads it every tick.
3. **`config.json` → `strategy_overrides`** — per-key overrides for the
   tunable strategy parameters. When a key is present here, it shadows the
   `config.py` default. Empty by default.

### Static — `config.py`

| Field | Default | Meaning |
|---|---|---|
| `SYMBOLS` | `["SOL", "ETH", "ADA"]` | Hyperliquid perp symbols traded in parallel |
| `BASE_TRADE_SIZE_USD` | `{SOL: 40, ETH: 40, ADA: 20}` | Per-symbol base notional. Final size = base × `TRADE_SIZE_MULTIPLIER` |
| `NEW_SYMBOL_DEFAULT_BASE_USD` | `20.0` | Default base size for new dashboard symbols |
| `NEW_SYMBOL_DEFAULT_LEVERAGE` | `20` | Default leverage for new dashboard symbols |
| `LOOP_SECONDS` | `60` | Bot tick interval |
| `CANDLE_INTERVAL` | `"1m"` | Candle resolution feeding RSI |
| `RSI_PERIOD` | `14` | Wilder RSI lookback |
| `RSI_OVERSOLD` | `25.0` | Default; overridable from dashboard |
| `RSI_OVERBOUGHT` | `75.0` | Default; overridable from dashboard |
| `EMA_FAST_PERIOD` | `9` | Default; overridable from dashboard |
| `EMA_SLOW_PERIOD` | `21` | Default; overridable from dashboard |
| `BB_PERIOD` | `20` | Default; overridable from dashboard |
| `BB_STDEV` | `2.0` | Default; overridable from dashboard |
| `TRAILING_TIERS` | `[(15,0),(30,15)]` | `(arm ROE%, floor ROE%)` pairs |
| `USE_TESTNET` | `False` | Switch to Hyperliquid testnet |
| `AI_REFRESH_SECONDS` | `900` | How often `ai_analyst.run_once()` runs (15 min) |
| `GEMINI_MODEL` | `"gemini-2.5-flash"` | Sentiment + trade-gate model |
| `GEMINI_REVIEWER_MODEL` | `"gemini-2.5-pro"` | Higher-tier model used only by daily reviewer |
| `MINIMAX_MODEL` | `"MiniMax-M2.7"` | MiniMax model id |
| `JUDGE_MULTI_SHOT` | `3` | Round 3 takes the median of N MiniMax calls |
| `DEMO_FAKE_LOSS` | `False` | Force kill switch on second tick (testing only) |

### Dynamic — `config.json`

Editable from the dashboard (whitelist enforced server-side); also written by
AI modules.

| Field | Default | Meaning |
|---|---|---|
| `TRADE_SIZE_MULTIPLIER` | `1.0` | Scalar applied to every symbol's base size |
| `DAILY_LOSS_LIMIT` | `0.02` | Drawdown threshold for kill switch (anchored at startup) |
| `TRADE_GATE_ENABLED` | `true` | Toggle the per-trade dual-AI gate (false = pure RSI) |
| `AUTO_CAPITAL_TUNE` | `true` | If false, `ai_analyst` writes `ai_meta.suggested_capital` for manual Apply. If true, it overwrites live `TRADE_SIZE_MULTIPLIER`/`DAILY_LOSS_LIMIT` directly |
| `AUTO_STRATEGY_EVOLVE` | `false` | If false, `strategy_reviewer` writes `ai_meta.suggested_strategy` for manual Apply. If true, validated overrides go straight into `strategy_overrides` |
| `AI_ROUND1_PROMPT` | `(template)` | Editable instruction template for the Round 1 model (Gemini/MiniMax base). Falls back to `config.py` if missing |
| `AI_JUDGE_PROMPT` | `(template)` | Editable instruction template for the Round 3 synthesis judge. Falls back to `config.py` if missing |
| `strategy_overrides` | `{}` | Per-key shadow values for `RSI_OVERSOLD`/`RSI_OVERBOUGHT`/`EMA_FAST_PERIOD`/`EMA_SLOW_PERIOD`/`BB_PERIOD`/`BB_STDEV` |
| `ai_meta.last_sentiment` | `null` | Final 1-10 score from Round 3 |
| `ai_meta.last_confidence` | `null` | Final confidence 0-1 |
| `ai_meta.last_reason` | `null` | One-sentence rationale from the judge |
| `ai_meta.last_fng` | `null` | Latest Fear & Greed Index value + classification |
| `ai_meta.btc_dominance` | `null` | BTC market cap dominance % |
| `ai_meta.funding_rates` | `null` | Per-symbol 8h funding rates |
| `ai_meta.suggested_capital` | `null` | Latest sentiment-driven capital suggestion (when `AUTO_CAPITAL_TUNE=false`) |
| `ai_meta.suggested_strategy` | `null` | Latest strategy reviewer suggestion (diagnosis + per-param overrides + rationale) |

You can hand-edit `config.json` while everything is running — `settings.save()`
is atomic (`os.replace`), and both bot and dashboard guard with a lock for
their own writes.

---

## Trade Journal

Every ENTRY and EXIT is appended to `journal/journal-YYYYMM.jsonl` (one file
per UTC month). Equity samples every 5 minutes go to
`journal/equity-YYYYMM.jsonl`. Files older than 6 months are deleted on bot
startup (`journal.purge_old`).

ENTRY records carry the full decision context that justified the trade:

```jsonc
{
  "ts": "2026-05-07T08:42:11Z",
  "type": "ENTRY",
  "trade_id": "a3f7b1d2c8e9",        // links to the matching EXIT
  "symbol": "SOL",
  "side": "BUY",
  "fill_price": 84.05,
  "size_usd": 40.0,
  "size_units": 0.4759,
  "decision_context": {
    "trigger": "RSI oversold",
    "tech": { "rsi": 19.5, "ema_fast": 83.2, "ema_slow": 81.0,
              "ema_trend": "BULL", "bb_position": "below_lower", ... },
    "ai_gate": {
      "enabled": true,
      "votes": {
        "gemini":  { "decision": "GO", "reason": "..." },
        "minimax": { "decision": "GO", "reason": "..." }
      }
    },
    "sentiment": { "score": 6, "confidence": 0.7, "fng": 47, "reason": "..." },
    "btc_dominance": 58.4,
    "funding_rate": 0.0001,
    "session_pnl_pct": -0.3,
    "config_snapshot": {
      "TRADE_SIZE_MULTIPLIER": 1.0, "DAILY_LOSS_LIMIT": 0.02,
      "RSI_OVERSOLD": 25.0, "RSI_OVERBOUGHT": 75.0,
      "EMA_FAST_PERIOD": 9, "EMA_SLOW_PERIOD": 21,
      "BB_PERIOD": 20, "BB_STDEV": 2.0,
      "TRAILING_TIERS": [[15.0, 0.0], [30.0, 15.0]],
      "BASE_TRADE_SIZE_USD": 40.0
    }
  }
}
```

EXIT records share the `trade_id` and add `exit_context`:

```jsonc
{
  "ts": "2026-05-07T09:24:42Z",
  "type": "EXIT",
  "trade_id": "a3f7b1d2c8e9",
  "symbol": "SOL",
  "side": "LONG",
  "fill_price": 86.40,
  "exit_context": {
    "exit_reason": "trailing_stop",      // | opposite_signal | kill_switch
    "entry_price": 84.05,
    "entry_ts": "2026-05-07T08:42:11Z",
    "hold_seconds": 2531,
    "max_roe_pct": 31.2,
    "trade_max_drawdown_pct": 4.1,
    "final_roe_pct": 15.0,
    "pnl_usd": 0.62,
    "entry_ai_score": 6,
    "entry_fng_value": 47,
    "entry_rsi": 19.5,
    "entry_ema_spread_pct": -2.3
  }
}
```

The `config_snapshot` and `ai_gate.votes` fields make every trade
**attributable**: when `strategy_reviewer` later digests them, it knows which
parameter set produced which outcome.

---

## Dashboard

```bash
python dashboard.py
# → http://127.0.0.1:8080  (loopback only — never expose 8080 publicly)
```

Sections:

- **Open Positions** — live positions for each symbol with size, entry, PnL,
  margin, ROE.
- **Dynamic Config** — read-only static block + editable form for the five
  whitelisted fields (`TRADE_SIZE_MULTIPLIER`, `DAILY_LOSS_LIMIT`,
  `TRADE_GATE_ENABLED`, `AUTO_CAPITAL_TUNE`, `AUTO_STRATEGY_EVOLVE`).
- **AI Capital Suggestion** — the latest `suggested_capital` from `ai_analyst`
  with score, confidence, reason, and an Apply button (disabled if already
  applied or already matches live config).
- **Strategy Params (effective)** — shows each tunable param's current
  effective value, distinguishing default vs. override, plus a Clear
  Overrides button.
- **Daily Strategy Review** — diagnosis, per-change rationale, validation
  notes, Apply button. Two on-demand buttons (`Run Reviewer 7d` / `30d`)
  dispatch the reviewer in a background thread; the UI polls every 3 s while
  it's running.
- **Performance** (full-width) — two Chart.js charts:
  - **Equity Curve** — line chart from `journal/equity-*.jsonl`, time-axis,
    auto-downsampled to ≤2000 points.
  - **Realised PnL by Symbol** — bar chart aggregating EXIT records' `pnl_usd`,
    green for positive bars, red for negative, tooltip with trade count and
    win rate.
  Range selector: 1d / 7d / 30d / 90d.
- **Recent Trades** — latest 50 records, click any row to expand its full
  `decision_context` or `exit_context` as JSON.
- **Download Journal** — header button streams a zip of all retained JSONL
  files (trade + equity).

The whole UI auto-refreshes state every 30 seconds and charts every 5 minutes
(equity log only writes that often anyway).

---

## Strategy Reviewer

`strategy_reviewer.py` is meant to run as a daily cron job. It:

1. Reads journal records for the lookback window (default 30 days).
2. Pairs ENTRY ↔ EXIT by `trade_id`, computes per-symbol / per-trigger /
   per-exit-reason / per-param-snapshot stats (trades, wins, losses, win rate,
   profit factor, avg PnL, avg hold time, avg max/final ROE).
3. Sends those stats + the current effective parameter set to **Gemini 2.5
   Pro** with strict JSON-output constraints.
4. **Validates** the proposed overrides:
   - Each value clamped to the bounds below.
   - Cross-field constraints enforced; violators are dropped wholesale.

| Param | Bound | |
|---|---|---|
| `RSI_OVERSOLD` | 10 – 40 | |
| `RSI_OVERBOUGHT` | 60 – 90 | |
| `EMA_FAST_PERIOD` | 5 – 20 | integer |
| `EMA_SLOW_PERIOD` | 15 – 60 | integer |
| `BB_PERIOD` | 10 – 40 | integer |
| `BB_STDEV` | 1.0 – 3.5 | |
| `RSI_OVERBOUGHT − RSI_OVERSOLD` | ≥ 30 | else both dropped |
| `EMA_SLOW − EMA_FAST` | ≥ 5 | else both dropped |

5. Writes the result (diagnosis, validated overrides, rationale, confidence,
   validation notes) to `config.json → ai_meta.suggested_strategy`.
6. If `AUTO_STRATEGY_EVOLVE=true`, also merges validated overrides into
   `strategy_overrides` so the bot picks them up on the next tick.
7. If `AUTO_STRATEGY_EVOLVE=false`, the operator approves on the dashboard.

Cron line for the VM (UTC 00:05 daily):

```cron
5 0 * * *  /home/rain/claude-ai-trading-bot/.venv/bin/python /home/rain/claude-ai-trading-bot/strategy_reviewer.py >> /home/rain/claude-ai-trading-bot/reviewer.log 2>&1
```

---

## Investment Decision Pipeline

This is the heart of the bot. Decisions happen at two cadences:

### A. Sentiment refresh (every 15 minutes) — `ai_analyst.run_once()`

#### Round 1 — MiniMax sentiment scoring

MiniMax receives:
- 15 deduped headlines, **balanced 5-per-source** across Cointelegraph, CoinDesk,
  TheBlock, CryptoSlate, BitcoinMagazine, Bitcoinist (no API tokens required).
- Latest **Fear & Greed Index** (`alternative.me`).
- Per-symbol market state: price, 24h change, current RSI, position, funding rate.

Returns `SCORE: 1-10`, `CONFIDENCE: 0-1`, `REASON: <one sentence>`.

#### Round 2 — Supplementary data fetch

- **BTC dominance** from CoinGecko `/global` (free, no token).
- **Per-symbol funding rates** (8h) from Hyperliquid `meta_and_asset_ctxs`.

#### Round 3 — MiniMax judge × N (median)

Round 1 result + Round 2 supplements + per-symbol state → MiniMax
`JUDGE_MULTI_SHOT` times (default 3). Median score, median confidence, reason
from the run closest to median. If all judge calls fail, Round 1's result is
used as fallback.

#### Mapping to live parameters

```
confidence < 0.4              → mult=0.83, loss=2%   (low conviction → shrink)
score > 8  (high conf bull)   → mult=1.25, loss=2%
score < 3  (high conf bear)   → mult=0.40, loss=1%   (tightened stop)
otherwise (neutral)           → mult=1.0,  loss=2%
```

When `AUTO_CAPITAL_TUNE=false` (default), this mapping is written to
`ai_meta.suggested_capital` **without** touching live `TRADE_SIZE_MULTIPLIER`
or `DAILY_LOSS_LIMIT`. The dashboard's Apply button promotes it to live.

### B. Composite signal (every tick) — `strategy.decide()`

```
For each symbol on every 60s tick:
  ema_fast = EMA(closes, EMA_FAST_PERIOD)         # default 9
  ema_slow = EMA(closes, EMA_SLOW_PERIOD)         # default 21
  rsi      = RSI(closes, 14)
  upper, mid, lower = Bollinger(closes, BB_PERIOD, BB_STDEV)  # default 20, 2σ
  ema_trend = "BULL" if ema_fast > ema_slow else "BEAR"

  BUY  if ema_trend == "BULL" and (rsi < RSI_OVERSOLD   or  close < lower)
  SELL if ema_trend == "BEAR" and (rsi > RSI_OVERBOUGHT or  close > upper)
  else HOLD
```

All thresholds shown read from `strategy_overrides` first, then `config.py`.
The journal records the **effective** values used for each entry under
`decision_context.config_snapshot`, so reviews aren't confused by mid-period
changes.

### C. Trailing stop (every tick) — runs before signal evaluation

| Max ROE seen | Armed floor | Behaviour |
|---|---|---|
| ≥ +15% | breakeven (0%) | Position closes if ROE drops to 0% |
| ≥ +30% | +15% lock-in | Position closes if ROE drops to +15% |

ROE is `unrealizedPnl / marginUsed × 100` using Hyperliquid's own fields, so
leverage is reflected. Triggers write an EXIT to the journal with
`exit_reason: "trailing_stop"` *before* the close is sent.

### D. Trade gate (every entry signal) — `trade_gate.judge_trade()`

Only runs when `TRADE_GATE_ENABLED=true`.

```
Gemini  ─┐  both called in parallel
MiniMax ─┘
            ↓
  Decision rule:
   - both responded, both GO        → place order
   - both responded, any one SKIP   → skip this tick
   - only one responded             → that one's decision is authoritative
   - both failed                    → SKIP (no AI signal)
```

Both votes are returned structured (`{decision, reason}` per analyst) and
stored in the ENTRY journal under `decision_context.ai_gate.votes`.

> **Gemini billing note**: the free tier of `gemini-2.5-flash` only allows
> 20 requests per day, far below this bot's usage. Enable Tier 1 billing on
> your Google Cloud project (~$0.50/month at default refresh) for the
> consensus rule to actually run. Without billing the gate gracefully degrades
> to MiniMax-only via the single-respondent fallback.

---

## AI quota usage

Per typical hour with default settings:

| Path | Calls/hr | Daily | Notes |
|---|---|---|---|
| Round 1 MiniMax | 4 | 96 | One per refresh |
| Round 3 MiniMax × 3 | 12 | 288 | Median aggregation |
| Trade gate MiniMax | ~5–15 | ~120–360 | Only when RSI hits extremes |
| Trade gate Gemini | ~5–15 | ~120–360 | Same trigger |
| Reviewer Gemini Pro | — | 1 (cron) | + on-demand from dashboard button |

---

## Kill switch

`risk.KillSwitch` anchors on the bot's starting equity (set on the first tick).
On every tick it computes drawdown vs. that anchor and trips when drawdown ≥
`DAILY_LOSS_LIMIT`. On trip it journals each open position as `EXIT` with
`exit_reason: "kill_switch"`, issues `market_close` for every symbol, then
exits with `sys.exit(1)`. Manual restart is required.

To verify the trip mechanics end-to-end without waiting for a real loss, set
`config.DEMO_FAKE_LOSS = True` — the bot reports a fake 1% drawdown on its
second tick.

---

## Deployment to a VM (GCP example)

The repo includes ready-made `deploy/` artifacts.

### 1. Firewall

| Rule | Protocol/Port | Source |
|---|---|---|
| `allow-https` | `tcp:443` | `0.0.0.0/0` |
| `allow-http-acme` | `tcp:80` | `0.0.0.0/0` (Let's Encrypt HTTP-01) |

**Don't** open `8080` — Flask binds to loopback. SSH (22) should be your
allowlist only.

### 2. Caddy

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy

caddy hash-password                              # produces $2a$... bcrypt hash
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile    # then edit hostname + hash
sudo systemctl reload caddy
sudo journalctl -u caddy -f                      # watch the cert provisioning
```

DNS: A record for your hostname → VM external IP. Caddy auto-renews certs.

### 3. systemd units

Adjust `User=` and paths in `deploy/bot.service` and
`deploy/dashboard.service` to match your VM:

```bash
sudo cp deploy/bot.service /etc/systemd/system/
sudo cp deploy/dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bot dashboard
sudo journalctl -u bot -u dashboard -f
```

### 4. Reviewer cron

```cron
5 0 * * *  /home/rain/claude-ai-trading-bot/.venv/bin/python /home/rain/claude-ai-trading-bot/strategy_reviewer.py >> /home/rain/claude-ai-trading-bot/reviewer.log 2>&1
```

### Common ops

| Command | Use |
|---|---|
| `sudo systemctl status bot` | Check the bot |
| `sudo journalctl -u bot -f` | Stream bot logs |
| `sudo journalctl -u dashboard -f` | Stream dashboard logs |
| `sudo systemctl restart bot` | After editing `config.py` (`config.json` doesn't need a restart) |
| `tail -f reviewer.log` | Daily reviewer output |
| `sudo systemctl reload caddy` | After editing `Caddyfile` |

---

## Strategy swap

The only function you need to change to swap strategies is
`strategy.decide(closes, settings) -> (Signal, info_dict)`. Keep the
signature; return one of `"BUY"` / `"SELL"` / `"HOLD"`.

The current implementation is a composite EMA(9/21) trend filter + RSI(14)
extremes 25/75 OR Bollinger(20, 2σ) breakouts. The trade gate prompt expects
`info["ema_trend"]`, `info["bb_position"]`, and `info["trigger"]`; if your
replacement preserves those, the gate's regime reasoning keeps working. The
journal expects `info["params_used"]` to record the effective parameter set
for each entry — populate it if you want full forensic attribution.

---

## Safety notes

- Always use a Hyperliquid **API/agent wallet** — never put your main wallet
  private key in `.env`. Agents can trade but cannot withdraw.
- `.gitignore` already excludes `.env` and `journal/`. Never commit either.
- `chmod 600 .env` on shared servers.
- Do not run multiple bot instances against the same main wallet — they will
  fight over position state.
- The dashboard binds to `127.0.0.1` only and trusts that Caddy in front
  enforces auth. If you ever bind it to `0.0.0.0`, add Flask-side auth first.
- The trade gate, when both AIs are unreachable, falls through to SKIP (safe
  default — no order placed).
- ADA on Hyperliquid has thinner liquidity than SOL/ETH; its base size is
  intentionally half (`$20` vs `$40`).
- Reviewer suggestions are clamped server-side. Even if the model goes haywire
  it can't push values outside the bounds in `strategy_reviewer.TUNABLE_BOUNDS`.
