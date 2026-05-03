"""Pre-trade dual-AI confirmation gate.

For every BUY/SELL the bot wants to execute we ask Gemini and MiniMax in
parallel. The decision rule is:

  - 2 responded:  both must GO -> execute, otherwise SKIP
  - 1 responded:  that one's decision is authoritative
  - 0 responded:  SKIP (no AI signal)

Claude (Sonnet) is wired up via `_call_claude` and `config.CLAUDE_MODEL` and can
be re-enabled by adding it to the parallel pool in `judge_trade` once the
Anthropic account has credit.
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
            timeout=120,
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
        from google import genai
        client = genai.Client(api_key=key)
        resp = client.models.generate_content(model=config.GEMINI_MODEL, contents=prompt)
        return _parse((resp.text or "").strip())
    except Exception as e:
        log.warning(f"Trade gate Gemini failed: {e}")
        return None


def _call_claude(prompt: str) -> Optional[tuple[str, str]]:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        resp = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        # resp.content is a list of content blocks; first text block holds the answer.
        text_parts = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
        return _parse("\n".join(text_parts).strip())
    except Exception as e:
        log.warning(f"Trade gate Claude failed: {e}")
        return None


def _decide_quorum(votes: dict[str, tuple[str, str]]) -> tuple[bool, str]:
    """Apply the agreed quorum rule to the responding analysts.

    votes: { 'gemini' | 'minimax' : ('GO'|'SKIP', reason) }

    Returns (allow, rationale_text).
    """
    n = len(votes)

    if n == 0:
        return False, "both AIs failed; skipping for safety"

    if n == 1:
        only = next(iter(votes))
        d, r = votes[only]
        return d == "GO", f"only {only} responded; using its call ({d}): {r}"

    # n == 2: both must GO.
    all_go = all(d == "GO" for d, _ in votes.values())
    return all_go, ("both analysts said GO" if all_go
                    else "consensus broken (need both GO)")


def judge_trade(signal: str, ctx: dict[str, Any]) -> tuple[bool, str, str]:
    """Returns (allow, source_label, combined_reason)."""
    prompt = _build_prompt(signal, ctx)
    symbol = ctx.get("symbol", "?")

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_g = pool.submit(_call_gemini, prompt)
        f_m = pool.submit(_call_minimax, prompt)
        g_r, m_r = f_g.result(), f_m.result()

    votes: dict[str, tuple[str, str]] = {}
    if g_r is not None:
        votes["gemini"] = g_r
    if m_r is not None:
        votes["minimax"] = m_r

    allow, rationale = _decide_quorum(votes)
    src = "+".join(votes.keys()) or "none"
    per_vote = " | ".join(f"{n}={d}: {r}" for n, (d, r) in votes.items()) or "(no responses)"
    final_reason = f"{rationale} || {per_vote}"

    log.info(f"[{symbol}] Trade gate [{src}] -> {'GO' if allow else 'SKIP'} on {signal} | {rationale}")
    return allow, src, final_reason
