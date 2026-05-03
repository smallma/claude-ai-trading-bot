"""Pre-trade dual-AI confirmation gate.

For every BUY/SELL the bot wants to execute we ask BOTH Gemini and MiniMax in
parallel. Strict consensus rule: BOTH must say GO, otherwise SKIP.

Failure handling:
- Both AIs return: apply consensus rule.
- Only one returns: that single decision is used.
- Both fail: GO (fallback — gate is an additive filter, not a kill switch).
"""
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

import requests

import ai_analyst
import config
from logger import get_logger

log = get_logger("gate")


def _build_prompt(signal: str, ctx: dict[str, Any]) -> str:
    rsi = ctx.get("rsi")
    rsi_str = f"{rsi:.1f}" if rsi is not None else "n/a"
    last_sent = ctx.get("last_sentiment")
    last_sent_str = f"{last_sent}/10" if last_sent is not None else "n/a"
    pnl = ctx.get("session_pnl_pct")
    pnl_str = f"{pnl:+.2f}%" if pnl is not None else "n/a"
    change = ctx.get("change_24h_pct")
    change_str = f"{change:+.2f}%" if change is not None else "n/a"
    funding = ctx.get("funding_rate")
    funding_str = f"{funding * 100:.4f}% / 8h" if funding is not None else "n/a"

    headlines = ctx.get("recent_headlines") or []
    headlines_block = "\n".join(f"- {h}" for h in headlines[:5]) or "(none)"

    return f"""You are a final trade-confirmation gate for an automated multi-coin perp bot.
A {signal} signal just fired for {ctx.get('symbol', '?')} (its 1m RSI hit the
LOCKED extreme of {config.RSI_OVERSOLD if signal == 'BUY' else config.RSI_OVERBOUGHT}).
Your one binary call: execute right now, or skip and let the next minute decide?

=== Current state for this symbol ===
- Signal: {signal} {ctx.get('symbol', '?')}
- Price: ${ctx.get('price', 0):.4f} (24h {change_str})
- RSI(14): {rsi_str}
- Position: {ctx.get('position', 'FLAT')}
- Funding rate: {funding_str}

=== Account-wide context ===
- Session PnL: {pnl_str}
- Latest basket sentiment: {last_sent_str}  (reason: {ctx.get('last_reason', '-')})
- BTC dominance: {ctx.get('btc_dominance', 'n/a')}%

=== Recent headlines ===
{headlines_block}

=== Decision rules ===
- SKIP if signal disagrees with strong-confidence sentiment (e.g., BUY but bearish news).
- SKIP if BTC dominance is rising hard AND signal is BUY for an alt (SOL/ADA).
- SKIP if funding rate is extreme (>0.05% per 8h) and signal is BUY (crowded long).
- SKIP if session PnL is already < -1.5% (avoid revenge trades).
- Otherwise GO.

=== Output (strictly two lines) ===
DECISION: <GO or SKIP>
REASON: <one short sentence>
"""


def _parse(text: str) -> Optional[tuple[str, str]]:
    decision_m = re.search(r"DECISION:\s*(GO|SKIP)", text, re.IGNORECASE)
    reason_m = re.search(r"REASON:\s*(.+)", text, re.IGNORECASE)
    if not decision_m:
        return None
    decision = decision_m.group(1).upper()
    reason = reason_m.group(1).strip() if reason_m else ""
    return decision, reason


def _call_minimax(prompt: str) -> Optional[tuple[str, str]]:
    key = os.getenv("MINIMAX_API_KEY")
    if not key:
        return None
    try:
        resp = requests.post(
            ai_analyst.MINIMAX_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": config.MINIMAX_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 1500,
                "temperature": 0.2,
            },
            timeout=30,
        )
        resp.raise_for_status()
        text = ai_analyst._strip_think(resp.json()["choices"][0]["message"]["content"])
        return _parse(text)
    except Exception as e:
        log.warning(f"Trade gate MiniMax failed: {e}")
        return None


def _call_gemini(prompt: str) -> Optional[tuple[str, str]]:
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return None
    try:
        import google.generativeai as genai
        genai.configure(api_key=key)
        model = genai.GenerativeModel(config.GEMINI_MODEL)
        resp = model.generate_content(prompt)
        return _parse((resp.text or "").strip())
    except Exception as e:
        log.warning(f"Trade gate Gemini failed: {e}")
        return None


def judge_trade(signal: str, ctx: dict[str, Any]) -> tuple[bool, str, str]:
    """Returns (allow, source_label, combined_reason).

    Calls both AIs in parallel. STRICT CONSENSUS: both must say GO.
    Single-AI fallback if the other fails. Both fail -> GO (operational safety).
    """
    prompt = _build_prompt(signal, ctx)
    symbol = ctx.get("symbol", "?")

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_m = pool.submit(_call_minimax, prompt)
        f_g = pool.submit(_call_gemini, prompt)
        m_result = f_m.result()
        g_result = f_g.result()

    parts: list[tuple[str, str, str]] = []  # (label, decision, reason)
    if m_result is not None:
        parts.append(("minimax", m_result[0], m_result[1]))
    if g_result is not None:
        parts.append(("gemini", g_result[0], g_result[1]))

    if not parts:
        log.warning(f"[{symbol}] Trade gate: both AIs failed — defaulting to GO.")
        return True, "fallback-go", "AI unavailable"

    # Strict consensus: every present analyst must say GO.
    allow = all(d == "GO" for _, d, _ in parts)
    src = "+".join(label for label, _, _ in parts)
    reasons = " | ".join(f"{lbl}={d}: {r}" for lbl, d, r in parts)
    log.info(f"[{symbol}] Trade gate [{src}] {'GO' if allow else 'SKIP'} on {signal} — {reasons}")
    return allow, src, reasons
