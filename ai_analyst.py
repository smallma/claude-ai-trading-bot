"""AI sentiment pipeline (multi-round, dual-model).

Round 1 (parallel) : Gemini + MiniMax each score the same 15 multi-source
                     headlines + Fear & Greed index.
Round 2            : Fetch supplementary market data (BTC dominance from
                     CoinGecko, SOL funding rate from Hyperliquid).
Round 3 (judge)    : MiniMax acts as the final synthesizer N times (default 3),
                     given Round 1 outputs + Round 2 supplements + market_ctx.
                     Median of the N runs is used to write config.json.

Failure handling   : If MiniMax fails on Round 1 OR Round 3, Gemini steps in.
                     If both fail, the cycle is skipped and config.json is
                     untouched (the bot keeps using the previous overrides).
"""
import os
import re
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

import feedparser
import requests

import config
import settings
from logger import get_logger

log = get_logger("ai")

RSS_SOURCES = {
    "Cointelegraph":    "https://cointelegraph.com/rss",
    "CoinDesk":         "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "TheBlock":         "https://www.theblock.co/rss.xml",
    "CryptoSlate":      "https://cryptoslate.com/feed/",
    "BitcoinMagazine":  "https://bitcoinmagazine.com/.rss/full/",
    "Bitcoinist":       "https://bitcoinist.com/feed/",
}

INCLUDE_KEYWORDS = [
    "sol", "solana",
    "btc", "bitcoin",
    "eth", "ethereum",
    "crypto", "market",
    "fed", "sec", "etf",
    "rate", "inflation",
]

FEAR_GREED_URL = "https://api.alternative.me/fng/?limit=1"
COINGECKO_GLOBAL_URL = "https://api.coingecko.com/api/v3/global"
MINIMAX_URL = "https://api.minimax.io/v1/chat/completions"

MAX_HEADLINES = 15
PER_SOURCE_LIMIT = 5

# ---------- RSS / news ----------

def _fetch_one_rss(name: str, url: str) -> list[str]:
    try:
        feed = feedparser.parse(url)
        if feed.bozo:
            log.warning(f"RSS [{name}] parse warning: {feed.bozo_exception}")
        return [
            e.title.strip()
            for e in feed.entries
            if getattr(e, "title", "").strip()
        ]
    except Exception as e:
        log.error(f"RSS [{name}] fetch failed: {e}")
        return []


def _normalize(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", title.lower()).strip()


def _filter_and_dedupe(titles: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for t in titles:
        norm = _normalize(t)
        if not norm or norm in seen:
            continue
        if not any(kw in norm for kw in INCLUDE_KEYWORDS):
            continue
        seen.add(norm)
        out.append(t)
    return out


def _fetch_headlines(limit: int = MAX_HEADLINES) -> list[str]:
    per_source: dict[str, list[str]] = {}
    with ThreadPoolExecutor(max_workers=len(RSS_SOURCES)) as pool:
        futures = {pool.submit(_fetch_one_rss, name, url): name
                   for name, url in RSS_SOURCES.items()}
        for fut in as_completed(futures):
            name = futures[fut]
            raw = fut.result()
            kept = _filter_and_dedupe(raw)[:PER_SOURCE_LIMIT]
            per_source[name] = kept
            log.info(f"RSS [{name}]: {len(raw)} raw -> {len(kept)} kept")

    merged: list[str] = []
    seen: set[str] = set()
    for i in range(PER_SOURCE_LIMIT):
        for name in RSS_SOURCES:
            titles = per_source.get(name, [])
            if i >= len(titles):
                continue
            t = titles[i]
            norm = _normalize(t)
            if norm in seen:
                continue
            seen.add(norm)
            merged.append(t)
            if len(merged) >= limit:
                return merged
    return merged


# ---------- Supplementary data ----------

def _fetch_fear_greed() -> Optional[dict]:
    try:
        resp = requests.get(FEAR_GREED_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if not data:
            return None
        item = data[0]
        return {"value": int(item["value"]), "classification": item.get("value_classification", "")}
    except Exception as e:
        log.error(f"Fear & Greed fetch failed: {e}")
        return None


def _fetch_btc_dominance() -> Optional[float]:
    """BTC market cap dominance % from CoinGecko (free, no token)."""
    try:
        resp = requests.get(COINGECKO_GLOBAL_URL, timeout=10)
        resp.raise_for_status()
        return float(resp.json()["data"]["market_cap_percentage"]["btc"])
    except Exception as e:
        log.error(f"BTC dominance fetch failed: {e}")
        return None


def _fetch_funding_rates(client, symbols: list[str]) -> dict[str, Optional[float]]:
    """Latest funding rate (per 8h) for each symbol perp from Hyperliquid."""
    out: dict[str, Optional[float]] = {s: None for s in symbols}
    if client is None:
        return out
    try:
        meta = client.info.meta_and_asset_ctxs()
        universe = meta[0]["universe"]
        ctxs = meta[1]
        wanted = set(symbols)
        for i, asset in enumerate(universe):
            name = asset.get("name")
            if name in wanted:
                try:
                    out[name] = float(ctxs[i]["funding"])
                except (KeyError, ValueError, TypeError):
                    pass
        return out
    except Exception as e:
        log.error(f"Funding rate fetch failed: {e}")
        return out


# ---------- Prompt builders ----------

def _format_market_context(ctx: Optional[dict]) -> str:
    """Format the per-symbol bot view for prompts.

    ctx may be either:
      - a single-symbol dict (legacy / standalone test): keys price, rsi, position, ...
      - a multi-symbol dict: {"symbols": [{"symbol": "SOL", "price": ..., ...}, ...],
                              "session_pnl_pct": ...}
    """
    if not ctx:
        return "(not provided)"

    if "symbols" in ctx and isinstance(ctx["symbols"], list):
        lines = []
        for s in ctx["symbols"]:
            price = s.get("price")
            change = s.get("change_24h_pct")
            rsi = s.get("rsi")
            pos = s.get("position", "FLAT")
            funding = s.get("funding_rate")
            parts = [f"  - {s.get('symbol', '?')}: ${price:.4f}" if price else f"  - {s.get('symbol', '?')}"]
            if change is not None:
                parts.append(f"24h={change:+.2f}%")
            if rsi is not None:
                parts.append(f"RSI={rsi:.1f}")
            parts.append(f"pos={pos}")
            if funding is not None:
                parts.append(f"funding={funding * 100:.4f}%/8h")
            lines.append(" ".join(parts))
        if ctx.get("session_pnl_pct") is not None:
            lines.append(f"  - account session PnL: {ctx['session_pnl_pct']:+.2f}%")
        return "\n".join(lines)

    # Single-symbol fallback (standalone test)
    lines = []
    if "price" in ctx:
        lines.append(f"  - {ctx.get('symbol', 'asset')} price: ${ctx['price']:.4f}")
    if ctx.get("change_24h_pct") is not None:
        lines.append(f"  - 24h change: {ctx['change_24h_pct']:+.2f}%")
    if ctx.get("rsi") is not None:
        lines.append(f"  - current RSI(14): {ctx['rsi']:.1f}")
    if "position" in ctx:
        lines.append(f"  - position: {ctx['position']}")
    if ctx.get("session_pnl_pct") is not None:
        lines.append(f"  - session PnL: {ctx['session_pnl_pct']:+.2f}%")
    return "\n".join(lines) if lines else "(not provided)"


def _build_round1_prompt(headlines: list[str], fng: Optional[dict],
                         market_ctx: Optional[dict],
                         symbols: list[str]) -> str:
    bullets = "\n".join(f"- {h}" for h in headlines)
    fng_block = f"{fng['value']}/100 ({fng['classification']})" if fng else "(unavailable)"
    symbols_str = ", ".join(symbols)
    return f"""You are a crypto market sentiment analyst tuning SHARED parameters
for an automated perpetual futures bot trading a basket of {symbols_str}.
Your scoring should reflect the broad crypto regime (it applies to all symbols).

Recent crypto headlines (last hour, multi-source):
{bullets}

Fear & Greed Index: {fng_block}

Current per-symbol market state:
{_format_market_context(market_ctx)}

Output strictly three lines:
SCORE: <integer 1-10>
CONFIDENCE: <decimal 0.0-1.0>
REASON: <one short sentence>
"""


def _build_judge_prompt(round1_results: list[tuple[str, dict]], headlines: list[str],
                        fng: Optional[dict], btc_dom: Optional[float],
                        funding_rates: dict[str, Optional[float]],
                        market_ctx: Optional[dict],
                        symbols: list[str]) -> str:
    bullets = "\n".join(f"- {h}" for h in headlines[:8])
    fng_block = f"{fng['value']}/100 ({fng['classification']})" if fng else "n/a"
    dom_block = f"{btc_dom:.2f}%" if btc_dom is not None else "n/a"
    funding_block = "\n".join(
        f"  - {sym}: {(r * 100):.4f}%/8h" if r is not None else f"  - {sym}: n/a"
        for sym, r in funding_rates.items()
    ) or "  (none)"
    symbols_str = ", ".join(symbols)

    r1_summary = "\n".join(
        f"  {name}: score={r['score']}/10 conf={r['confidence']:.2f} — {r['reason']}"
        for name, r in round1_results
    )

    return f"""You are the FINAL JUDGE re-evaluating an initial analyst opinion
against fresh macro/on-chain data, producing ONE shared parameter decision for a
multi-symbol perp bot trading the basket [{symbols_str}].

=== Initial analyst opinion (Round 1) ===
{r1_summary}

=== Macro & on-chain (Round 2) ===
- BTC Dominance: {dom_block}  (rising = capital fleeing alts to BTC)
- Per-symbol funding (8h, positive = longs pay shorts; >0.05% = crowded long):
{funding_block}
- Fear & Greed: {fng_block}

=== Per-symbol market state ===
{_format_market_context(market_ctx)}

=== Reference headlines (top 8) ===
{bullets}

=== Your job ===
Re-examine the analyst view against the fresh data. The basket includes alts
(SOL, ADA), so penalize bullishness when BTC dominance is climbing. Penalize
bullishness if multiple symbols show crowded-long funding. Reward bearishness
if multiple symbols are
already RSI-stretched in the opposite direction of the news.

Be more decisive than the initial analyst alone (use supplementary data to
sharpen the call). Stay within the same output schema.

Output strictly three lines:
SCORE: <integer 1-10>
CONFIDENCE: <decimal 0.0-1.0>
REASON: <one short sentence on what shifted vs the initial analyst view>
"""


# ---------- Model callers ----------

def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _parse_output(text: str) -> Optional[dict]:
    score_m = re.search(r"SCORE:\s*(\d+)", text, re.IGNORECASE)
    conf_m = re.search(r"CONFIDENCE:\s*([0-9.]+)", text, re.IGNORECASE)
    reason_m = re.search(r"REASON:\s*(.+)", text, re.IGNORECASE)

    if not score_m:
        return None

    score = max(1, min(10, int(score_m.group(1))))
    confidence = float(conf_m.group(1)) if conf_m else 0.5
    confidence = max(0.0, min(1.0, confidence))
    reason = reason_m.group(1).strip() if reason_m else ""
    return {"score": score, "confidence": confidence, "reason": reason}


def call_gemini(prompt: str, max_tokens: int = 500) -> Optional[dict]:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        log.warning("GEMINI_API_KEY not set")
        return None
    try:
        from google import genai
    except ImportError:
        log.error("google-genai not installed — run pip install -r requirements.txt")
        return None
    try:
        client = genai.Client(api_key=api_key)
        resp = client.models.generate_content(model=config.GEMINI_MODEL, contents=prompt)
        return _parse_output((resp.text or "").strip())
    except Exception as e:
        log.error(f"Gemini call failed: {e}")
        return None


def call_minimax(prompt: str, max_tokens: int = 2000) -> Optional[dict]:
    key = os.getenv("MINIMAX_API_KEY")
    if not key:
        log.warning("MINIMAX_API_KEY not set")
        return None
    try:
        resp = requests.post(
            MINIMAX_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": config.MINIMAX_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": 0.3,
            },
            timeout=120,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
        return _parse_output(_strip_think(text))
    except Exception as e:
        log.error(f"MiniMax call failed: {e}")
        return None


# ---------- Round orchestration ----------

def _round1_minimax(prompt: str) -> Optional[dict]:
    """Round 1 is MiniMax only — Gemini is reserved for the trade gate."""
    return call_minimax(prompt)


def _round3_judge(round1: list[tuple[str, dict]], headlines, fng, btc_dom,
                  funding_rates, market_ctx, symbols) -> Optional[dict]:
    prompt = _build_judge_prompt(round1, headlines, fng, btc_dom, funding_rates, market_ctx, symbols)
    n = max(1, config.JUDGE_MULTI_SHOT)
    results: list[dict] = []
    for i in range(n):
        r = call_minimax(prompt)
        if r is not None:
            results.append(r)
            log.info(f"Judge shot {i+1}/{n}: score={r['score']} conf={r['confidence']:.2f}")
    if not results:
        log.warning("All MiniMax judge shots failed; AI cycle skipped.")
        return None

    scores = [r["score"] for r in results]
    confs = [r["confidence"] for r in results]
    median_score = int(statistics.median(scores))
    pivot = min(results, key=lambda r: abs(r["score"] - median_score))
    return {
        "score": median_score,
        "confidence": round(statistics.median(confs), 2),
        "reason": pivot["reason"],
        "judge_shots": len(results),
        "judge_score_stdev": round(statistics.stdev(scores), 2) if len(scores) > 1 else 0.0,
    }


def _build_params(parsed: dict) -> dict:
    """Map AI score+confidence to a per-symbol size MULTIPLIER + risk params.

    Actual order USD = config.BASE_TRADE_SIZE_USD[symbol] * TRADE_SIZE_MULTIPLIER.
    RSI thresholds are LOCKED in config.py and not returned here.
    """
    score = parsed["score"]
    confidence = parsed["confidence"]
    if confidence < 0.4:
        return {"TRADE_SIZE_MULTIPLIER": 0.83, "DAILY_LOSS_LIMIT": 0.02}
    if score > 8:
        return {"TRADE_SIZE_MULTIPLIER": 1.25, "DAILY_LOSS_LIMIT": 0.02}
    if score < 3:
        return {"TRADE_SIZE_MULTIPLIER": 0.40, "DAILY_LOSS_LIMIT": 0.01}
    return {"TRADE_SIZE_MULTIPLIER": 1.0, "DAILY_LOSS_LIMIT": 0.02}


def run_once(market_ctx: Optional[dict] = None, client=None,
             symbols: Optional[list[str]] = None) -> Optional[int]:
    """Multi-round AI cycle. Returns final score or None on total failure.

    `symbols` lets bot.py pass the dashboard-edited active universe so prompts
    and funding-rate lookups stay in sync. Falls back to config.SYMBOLS when
    invoked standalone (e.g. `python ai_analyst.py`).
    """
    syms = symbols or list(config.SYMBOLS)
    log.info(f"AI cycle: fetching headlines + supplementary data for {syms}...")
    headlines = _fetch_headlines()
    fng = _fetch_fear_greed()
    btc_dom = _fetch_btc_dominance()
    funding_rates = _fetch_funding_rates(client, syms)
    log.info(
        f"AI cycle: {len(headlines)} headlines | FNG={fng} | "
        f"BTC_dom={btc_dom} | funding={funding_rates}"
    )

    # Round 1: MiniMax-only (Gemini reserved for trade gate)
    r1_prompt = _build_round1_prompt(headlines, fng, market_ctx, syms)
    minimax_r = _round1_minimax(r1_prompt)
    log.info(f"Round 1: minimax={minimax_r and minimax_r['score']}/10")

    if minimax_r is None:
        log.warning("AI cycle skipped: MiniMax Round 1 failed.")
        return None

    round1: list[tuple[str, dict]] = [("MiniMax", minimax_r)]

    # Round 3: MiniMax judge (multi-shot, median)
    final = _round3_judge(round1, headlines, fng, btc_dom, funding_rates, market_ctx, syms)
    if final is None:
        log.warning("AI cycle: judge failed; falling back to Round 1 result.")
        final = minimax_r

    params = _build_params(final)
    log.info(
        f"FINAL: score={final['score']}/10 conf={final['confidence']:.2f} | "
        f"size_x{params['TRADE_SIZE_MULTIPLIER']} loss={params['DAILY_LOSS_LIMIT']*100:.2f}% "
        f"(RSI locked at {config.RSI_OVERSOLD}/{config.RSI_OVERBOUGHT}) | {final['reason']}"
    )

    current = settings.load()
    auto_capital = bool(current.get("AUTO_CAPITAL_TUNE", False))
    applied = auto_capital
    if applied:
        current.update(params)
    else:
        log.info(
            f"AUTO_CAPITAL_TUNE=False — keeping live mult={current.get('TRADE_SIZE_MULTIPLIER')} "
            f"loss={current.get('DAILY_LOSS_LIMIT')}; storing suggestion only."
        )

    suggestion = {
        "TRADE_SIZE_MULTIPLIER": params["TRADE_SIZE_MULTIPLIER"],
        "DAILY_LOSS_LIMIT": params["DAILY_LOSS_LIMIT"],
        "score": final["score"],
        "confidence": final["confidence"],
        "reason": final["reason"],
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "applied": applied,
    }

    current["ai_meta"] = {
        "last_sentiment": final["score"],
        "last_confidence": final["confidence"],
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "last_reason": final["reason"],
        "last_fng": fng,
        "btc_dominance": btc_dom,
        "funding_rates": funding_rates,
        "headline_count": len(headlines),
        "round1": {name: {"score": r["score"], "confidence": r["confidence"]} for name, r in round1},
        "judge_shots": final.get("judge_shots"),
        "judge_score_stdev": final.get("judge_score_stdev"),
        "suggested_capital": suggestion,
    }
    settings.save(current)
    return final["score"]


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    run_once()
