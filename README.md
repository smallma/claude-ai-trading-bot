# Claude AI Trading Bot

A fully automated multi-symbol perpetual trading bot for **Hyperliquid**, with a
multi-round dual-AI pipeline (Gemini + MiniMax) that:

1. Scores broad crypto sentiment from 6 free RSS feeds + Fear & Greed Index +
   on-chain context (BTC dominance, funding rates).
2. Tunes position sizing in real time based on that sentiment.
3. Runs every BUY/SELL signal through a **dual-AI trade gate** (Gemini AND
   MiniMax must both approve) before placing the order.

Trades **SOL, ETH, ADA** simultaneously with independent per-symbol position
limits.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                       Every 15 minutes                           │
│                                                                  │
│  ┌──────────────┐                                               │
│  │ ai_analyst   │  Round 1 (parallel)                           │
│  │   .run_once  │   ├─ Gemini  ─┐                               │
│  │              │   └─ MiniMax ─┤                               │
│  │              │                ▼                              │
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
│       fetch 1m closes → compute RSI(14)                          │
│       if RSI < 20 → BUY signal                                   │
│       if RSI > 80 → SELL signal                                  │
│       if BUY/SELL:                                               │
│         ┌────── Trade Gate (dual AI consensus) ──────┐          │
│         │  Gemini  ─┐                                │          │
│         │  MiniMax ─┤  both must say GO              │          │
│         │           ▼                                │          │
│         │       allow / skip                         │          │
│         └────────────────────────────────────────────┘          │
│       if allow → market_open(symbol, side, BASE × MULTIPLIER)   │
└─────────────────────────────────────────────────────────────────┘
```

| File | Role |
|---|---|
| `bot.py` | Main 60-second tick loop, multi-symbol orchestration |
| `strategy.py` | RSI(14) signal — locked thresholds 20/80 |
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
| `RSI_OVERSOLD` | `20.0` | **Locked** — RSI below this → BUY signal |
| `RSI_OVERBOUGHT` | `80.0` | **Locked** — RSI above this → SELL signal |
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

#### Round 1 — Parallel scoring (Gemini + MiniMax)

Both models receive the **same input**:
- 15 deduped headlines, **balanced 5-per-source** across Cointelegraph, CoinDesk,
  TheBlock, CryptoSlate, BitcoinMagazine, Bitcoinist (no API tokens required).
- Latest **Fear & Greed Index** (`alternative.me`).
- Per-symbol market state: price, 24h change, current RSI, position, funding rate.

Each returns:
```
SCORE: 1-10        (1 extreme bear, 10 extreme bull)
CONFIDENCE: 0-1
REASON: one short sentence
```

#### Round 2 — Supplementary data fetch

- **BTC dominance** from CoinGecko `/global` (free, no token).
- **Per-symbol funding rates** (8h) from Hyperliquid `meta_and_asset_ctxs`.
- (Fear & Greed already fetched in Round 1.)

#### Round 3 — MiniMax judge × N (median)

A separate prompt feeds **both Round 1 results** + Round 2 supplements +
per-symbol state to MiniMax `JUDGE_MULTI_SHOT` times (default 3). The judge
weighs the analyst views, penalises bullishness when BTC dominance is climbing
or funding is crowded long, and produces a single decisive call.

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

> **RSI thresholds are locked at 20 / 80 in `config.py`** and intentionally not
> AI-tunable. The AI controls *how big* and *how risky* — not *when* to enter.

### B. Trade gate (every signal) — `trade_gate.judge_trade()`

Whenever any symbol's 1m RSI crosses 20 or 80, that **specific symbol's**
context is sent to a dual-AI gate:

```
context to gate:
  signal (BUY/SELL)
  symbol-specific: price, 24h change, RSI, position, funding rate
  account-wide:   session PnL, latest sentiment, BTC dominance
  recent headlines (top 5)

Gemini  ─┐
         │ both called in parallel
MiniMax ─┘
            ↓
  Strict consensus rule:
   - both GO         → place order
   - any one SKIP    → skip this tick (re-evaluate next minute)
   - both fail       → fallback GO (preserve operational safety)
```

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
| Round 1 Gemini | 4 | 96 | One per refresh |
| Round 1 MiniMax | 4 | 96 | One per refresh |
| Round 3 MiniMax × 3 | 12 | 288 | Median aggregation |
| Trade gate Gemini | ~5–15 | ~120–360 | Only when RSI hits extremes |
| Trade gate MiniMax | ~5–15 | ~120–360 | Same trigger |

- **MiniMax**: ~25 calls/hr → 125 / 5 hr. Comfortably within the 1500 / 5 hr
  Starter quota (~8% utilisation).
- **Gemini 2.5 Flash**: ~10–20 calls/hr. Well within the free-tier RPD.

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

The current implementation is locked-threshold RSI(14) at 20/80; consider this
a thin reference, not a recommendation. The AI layer is independent of the
strategy and will keep working with any signal source.

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
