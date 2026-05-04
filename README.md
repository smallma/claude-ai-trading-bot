# Claude AI Trading Bot

A fully automated multi-symbol perpetual trading bot for **Hyperliquid** that:

1. Uses **MiniMax** to score broad crypto sentiment every 15 min from 6 free
   RSS feeds + Fear & Greed Index + on-chain context (BTC dominance, funding
   rates), then tunes position sizing accordingly.
2. Runs every BUY/SELL signal through a **dual-AI trade gate** (Gemini AND
   MiniMax in parallel) before placing the order — both must approve, with
   single-AI fallback if one is unavailable.

Trades **SOL, ETH, ADA** simultaneously with independent per-symbol position
limits.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                       Every 15 minutes                           │
│                                                                  │
│  ┌──────────────┐                                               │
│  │ ai_analyst   │  Round 1: MiniMax scores headlines + FNG      │
│  │   .run_once  │                ▼                              │
│  │              │  Round 2 (supplementary data)                 │
│  │              │   ├─ BTC dominance (CoinGecko)                │
│  │              │   ├─ SOL/ETH/ADA funding (Hyperliquid)        │
│  │              │   └─ Fear & Greed Index                       │
│  │              │                ▼                              │
│  │              │  Round 3 (MiniMax judge × 3, median)          │
│  │              │     Synthesizes all inputs into ONE call      │
│  │              │                ▼                              │
│  │              │   writes TRADE_SIZE_MULTIPLIER + ai_meta      │
│  └──────────────┘                                               │
│                                ▼                                 │
│                         ┌──────────────┐                        │
│                         │ config.json  │                        │
│                         └──────┬───────┘                        │
└────────────────────────────────│────────────────────────────────┘
                                 │ reads every 60s
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│                       Every 60 seconds                           │
│                                                                  │
│   bot.tick():                                                    │
│     check kill switch                                            │
│     for symbol in [SOL, ETH, ADA]:                               │
│       1) trailing-stop check on existing position                │
│            track max ROE since entry                             │
│            ROE >= +15% → arm breakeven floor (0%)                │
│            ROE >= +30% → arm +15% floor                          │
│            ROE drops to armed floor → market_close + log         │
│       2) fetch 1m closes → compute RSI(14) + EMA(9,21) + BB(20,2)│
│       3) composite signal (locked):                              │
│            BUY  if EMA9>EMA21 AND (RSI<25 OR close<BB_lower)     │
│            SELL if EMA9<EMA21 AND (RSI>75 OR close>BB_upper)     │
│       4) if BUY/SELL → Trade Gate (Gemini ‖ MiniMax)             │
│            both GO → execute                                     │
│       5) if allow → market_open(symbol, side, BASE × MULTIPLIER) │
└─────────────────────────────────────────────────────────────────┘
```

| File | Role |
|---|---|
| `bot.py` | Main 60-second tick loop, multi-symbol orchestration |
| `strategy.py` | Composite signal — EMA(9/21) trend filter + (RSI 25/75 OR Bollinger 20/2σ breakout) |
| `exchange.py` | Hyperliquid SDK wrapper |
| `risk.py` | Account-level drawdown kill switch |
| `ai_analyst.py` | 3-round AI pipeline (Gemini + MiniMax + supplementary data) |
| `trade_gate.py` | Dual-AI per-trade approval gate (strict consensus) |
| `settings.py` | Atomic load/save for `config.json` |
| `config.py` | Static settings (symbols, base sizes, RSI thresholds, refresh interval) |
| `config.json` | Dynamic AI-tuned multiplier + risk limit + metadata |
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

```bash
python bot.py
```

Long-running deployment with tmux:

```bash
tmux new -s bot 'source .venv/bin/activate && python -u bot.py 2>&1 | tee -a bot.log'
# Detach: Ctrl+B then d
# Reattach: tmux attach -t bot
```

Test the AI pipeline in isolation (no orders placed):

```bash
python ai_analyst.py
# Runs the 3-round cycle and writes config.json. Useful for verifying API keys
# and seeing the AI's current view of the market.
```

A/B compare Gemini vs MiniMax stability on the same headlines:

```bash
python compare_ai.py 5    # 5 runs each, prints mean/stdev/latency
```

---

## Configuration

### Static (`config.py`) — change requires restart

| Field | Default | Meaning |
|---|---|---|
| `SYMBOLS` | `["SOL", "ETH", "ADA"]` | Hyperliquid perp symbols traded in parallel |
| `BASE_TRADE_SIZE_USD` | `{SOL: 40, ETH: 40, ADA: 20}` | Per-symbol base notional. Final size = base × `TRADE_SIZE_MULTIPLIER` |
| `MAX_OPEN_POSITIONS_PER_SYMBOL` | `1` | One net position per symbol (the bot rebalances toward the target) |
| `LOOP_SECONDS` | `60` | Bot tick interval |
| `CANDLE_INTERVAL` | `"1m"` | Candle resolution feeding RSI |
| `RSI_PERIOD` | `14` | Wilder RSI lookback |
| `RSI_OVERSOLD` | `25.0` | **Locked** — one of the BUY trigger conditions |
| `RSI_OVERBOUGHT` | `75.0` | **Locked** — one of the SELL trigger conditions |
| `EMA_FAST_PERIOD` | `9` | EMA fast period for trend filter |
| `EMA_SLOW_PERIOD` | `21` | EMA slow period (BULL when fast > slow) |
| `BB_PERIOD` | `20` | Bollinger Bands lookback |
| `BB_STDEV` | `2.0` | Bollinger Bands stdev multiplier |
| `TRAILING_TIERS` | `[(15,0),(30,15)]` | List of `(arm ROE%, floor ROE%)`. ROE crossing arm raises the floor; ROE dropping back to floor closes the position |
| `USE_TESTNET` | `False` | Switch to Hyperliquid testnet |
| `DEMO_FAKE_LOSS` | `False` | Force kill switch on second tick (testing only) |
| `AI_REFRESH_SECONDS` | `900` | How often `ai_analyst.run_once()` runs (15 min) |
| `GEMINI_MODEL` | `"gemini-2.5-flash"` | Gemini model id |
| `MINIMAX_MODEL` | `"MiniMax-M2.7"` | MiniMax model id |
| `JUDGE_MULTI_SHOT` | `3` | Round 3 takes the median of N MiniMax calls |
| `TRADE_GATE_ENABLED` | `True` | Toggle the per-trade dual-AI gate (False = pure RSI) |

### Dynamic (`config.json`) — written by AI, read every tick

| Field | Default | Meaning |
|---|---|---|
| `TRADE_SIZE_MULTIPLIER` | `1.0` | AI-tuned scalar applied to every symbol's base size |
| `DAILY_LOSS_LIMIT` | `0.02` | Anchored at startup; tripping it closes all positions and halts |
| `ai_meta.last_sentiment` | `null` | Final 1-10 score from Round 3 |
| `ai_meta.last_confidence` | `null` | Final confidence 0-1 from Round 3 |
| `ai_meta.last_reason` | `null` | One-sentence rationale from the judge |
| `ai_meta.last_fng` | `null` | Latest Fear & Greed Index value + classification |
| `ai_meta.btc_dominance` | `null` | BTC market cap dominance % |
| `ai_meta.funding_rates` | `null` | Per-symbol 8h funding rates from Hyperliquid |
| `ai_meta.headline_count` | `null` | Number of filtered headlines fed to the AI |
| `ai_meta.round1` | `null` | Per-analyst (Gemini, MiniMax) Round 1 outputs |
| `ai_meta.judge_shots` | `null` | How many of the N judge calls succeeded |
| `ai_meta.judge_score_stdev` | `null` | Stdev across judge calls (lower = more agreement) |

You can hand-edit `config.json` while the bot is running — changes take effect
on the next tick. The AI will overwrite your edits on its next cycle
(default every 15 minutes).

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

Returns:
```
SCORE: 1-10        (1 extreme bear, 10 extreme bull)
CONFIDENCE: 0-1
REASON: one short sentence
```

> Gemini is intentionally **not** used in the cycle — it's reserved for the
> per-trade gate, where its 20-RPD free-tier limit is unlikely to bite.

#### Round 2 — Supplementary data fetch

- **BTC dominance** from CoinGecko `/global` (free, no token).
- **Per-symbol funding rates** (8h) from Hyperliquid `meta_and_asset_ctxs`.
- (Fear & Greed already fetched in Round 1.)

#### Round 3 — MiniMax judge × N (median)

A separate prompt feeds the **Round 1 result** + Round 2 supplements +
per-symbol state to MiniMax `JUDGE_MULTI_SHOT` times (default 3). The judge
re-evaluates the analyst view against the fresh data — penalising bullishness
when BTC dominance is climbing or funding is crowded long — and produces a
single decisive call.

The N runs are aggregated:
- `score`, `confidence` → median of successful runs.
- `reason` → from the run closest to the median score.

If all MiniMax judge calls fail, **Gemini steps in as fallback judge**. If both
fail, the previous `config.json` is kept (no untuned overrides).

#### Mapping to live parameters

```
confidence < 0.4              → mult=0.83, loss=2%   (low conviction → shrink)
score > 8  (high conf bull)   → mult=1.25, loss=2%
score < 3  (high conf bear)   → mult=0.40, loss=1%   (tightened stop)
otherwise (neutral)           → mult=1.0,  loss=2%
```

Final per-symbol order USD = `BASE_TRADE_SIZE_USD[symbol] × TRADE_SIZE_MULTIPLIER`.

> **All entry thresholds are locked in `config.py`** and intentionally not
> AI-tunable. The AI controls *how big* and *how risky* — not *when* to enter.

### B. Composite signal (every tick) — `strategy.decide()`

Pure-Python implementations of EMA, RSI, and Bollinger Bands (no `pandas-ta`
dependency).

```
For each symbol on every 60s tick:
  ema_fast = EMA(closes, 9)
  ema_slow = EMA(closes, 21)
  rsi      = RSI(closes, 14)
  upper, mid, lower = Bollinger(closes, 20, 2σ)
  ema_trend = "BULL" if ema_fast > ema_slow else "BEAR"

  BUY  if ema_trend == "BULL" and (rsi < 25  or  close < lower)
  SELL if ema_trend == "BEAR" and (rsi > 75  or  close > upper)
  else HOLD
```

The EMA acts as a **regime filter**: even an extreme RSI or a Bollinger
breakout will be ignored if the short-term trend disagrees. This makes the
strategy stricter than pure RSI — it deliberately misses some early reversals
to avoid catching falling knives in a sustained downtrend (and vice-versa for
shorts in a rally).

### C. Trailing stop (every tick) — runs before signal evaluation

Per symbol, the bot tracks `unrealizedPnl / marginUsed × 100` (Hyperliquid's
own fields, so leverage is reflected) and the **max ROE seen since position
opened**. `config.TRAILING_TIERS = [(15, 0), (30, 15)]` arms tiered floors:

| Max ROE seen | Armed floor | Behaviour |
|---|---|---|
| ≥ +15% | breakeven (0%) | Position closes if ROE drops to 0% |
| ≥ +30% | +15% lock-in | Position closes if ROE drops to +15% |

When a floor is breached, the bot logs `[Trailing Stop Triggered]` and issues
`market_close`. State (`_position_max_roe`) is in-memory and resets on bot
restart — a position already past +30% before restart will re-arm from
whatever ROE it shows on the first tick after restart.

### D. Trade gate (every entry signal) — `trade_gate.judge_trade()`

Whenever the composite signal returns BUY or SELL, that **specific symbol's**
context is sent to a dual-AI gate:

```
context to gate:
  signal (BUY/SELL) + which sub-trigger fired (RSI extreme / BB break)
  symbol-specific: price, 24h change, RSI, EMA trend, BB position, position, funding rate
  account-wide:   session PnL, latest sentiment, BTC dominance
  recent headlines (top 5)

Gemini  ─┐
         │ both called in parallel
MiniMax ─┘
            ↓
  Decision rule:
   - both responded, both GO        → place order
   - both responded, any one SKIP   → skip this tick
   - only one responded             → that one's decision is authoritative
   - both failed                    → SKIP (no AI signal)
```

> **Gemini billing note**: the free tier of `gemini-2.5-flash` only allows
> 20 requests per day, far below this bot's usage. To make the consensus rule
> actually run on every call, enable billing on the Google Cloud project
> linked to your `GEMINI_API_KEY` (Tier 1 pay-as-you-go, ~$0.50 / month at
> the default refresh schedule). Without billing the gate gracefully degrades
> to MiniMax-only via the fallback path above.

Gate prompt heuristics (built into `trade_gate.py`):
- SKIP if signal disagrees with strong-confidence sentiment.
- SKIP if BTC dominance is rising hard AND signal is BUY for an alt.
- SKIP if funding is extreme (>0.05% / 8h) and signal is BUY (crowded long).
- SKIP if session PnL < −1.5% (avoid revenge trades).

To bypass the gate (pure RSI behaviour), set `TRADE_GATE_ENABLED = False` in
`config.py`.

---

## AI quota usage

Per typical hour with default settings:

| Path | Calls/hr | Daily | Notes |
|---|---|---|---|
| Round 1 MiniMax | 4 | 96 | One per refresh |
| Round 3 MiniMax × 3 | 12 | 288 | Median aggregation |
| Trade gate MiniMax | ~5–15 | ~120–360 | Only when RSI hits 25/75 extremes |
| Trade gate Gemini | ~5–15 | ~120–360 | Same trigger as above |

- **MiniMax**: ~25 calls/hr → 125 / 5 hr. Comfortably within the 1500 / 5 hr
  Starter quota (~8% utilisation).
- **Gemini 2.5 Flash**: ~5–15 calls/hr is **above the 20-RPD free-tier ceiling**.
  Without billing enabled, Gemini will 429 most of the time and the trade gate
  silently degrades to MiniMax-only via the single-respondent fallback.

---

## Kill switch

`risk.KillSwitch` anchors on the bot's starting equity (set on the first tick).
On every tick it computes drawdown vs. that anchor and trips when drawdown ≥
`DAILY_LOSS_LIMIT`. On trip it issues `market_close` for **every symbol in
`SYMBOLS`** then exits with `sys.exit(1)`. Manual restart is required.

To verify the trip mechanics end-to-end without waiting for a real loss, set
`config.DEMO_FAKE_LOSS = True` — the bot reports a fake 1% drawdown on its
second tick.

---

## Strategy swap

The only function you need to change to swap strategies is
`strategy.decide(closes, settings) -> (Signal, info_dict)`. Keep the
signature, return one of `"BUY"` / `"SELL"` / `"HOLD"`.

The current implementation is a composite EMA(9/21) trend filter combined with
RSI(14) extremes 25/75 OR Bollinger(20, 2σ) breakouts. The AI layer and the
trailing-stop logic are independent of the signal function — any replacement
that returns the same `(Signal, info_dict)` shape will keep them working,
though the trade gate prompt expects `info["ema_trend"]`, `info["bb_position"]`,
and `info["trigger"]` fields if you want the gate's regime reasoning to remain
informed.

---

## Safety notes

- Always use a Hyperliquid **API/agent wallet** — never put your main wallet
  private key in `.env`. Agents can trade but cannot withdraw.
- `.gitignore` already excludes `.env`. Never commit it.
- Set `chmod 600 .env` on shared servers.
- Do not run multiple bot instances against the same main wallet — they will
  fight over position state and funding allocation.
- The AI gate is an **additive filter**. When both AIs are unreachable (network
  down, key revoked), the bot defaults to GO so it stays operational. If you
  want strict "no AI = no trade" behaviour, change the fallback in
  `trade_gate.judge_trade` to return `False`.
- ADA on Hyperliquid has thinner liquidity than SOL/ETH; its base size is
  intentionally half (`$20` vs `$40`).
