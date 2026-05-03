"""A/B comparison: Gemini vs MiniMax-M2.7 on the same sentiment-scoring task.

Runs each model N times against the SAME headlines + FNG input, then prints:
  - mean / stdev of SCORE
  - mean of CONFIDENCE
  - mean of RSI thresholds
  - mean latency
  - sample REASON from each model

Usage: python compare_ai.py [N]   (default N=5)
"""
import os
import re
import sys
import time
import json
import statistics
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

import ai_analyst as ai

N_RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 5

GEMINI_MODEL = "gemini-2.5-flash"
MINIMAX_MODEL = "MiniMax-M2.7"
MINIMAX_URL = "https://api.minimax.io/v1/chat/completions"


def build_prompt(headlines: list[str], fng: Optional[dict]) -> str:
    bullets = "\n".join(f"- {h}" for h in headlines)
    fng_block = f"{fng['value']}/100 ({fng['classification']})" if fng else "(unavailable)"
    return f"""You are a crypto market sentiment analyst tuning parameters
for an automated SOL perpetual futures trading bot.

Recent crypto headlines (multi-source):
{bullets}

Fear & Greed Index: {fng_block}

Output strictly five lines:
SCORE: <integer 1-10>
CONFIDENCE: <decimal 0.0-1.0>
RSI_OVERSOLD: <number in [15,35]>
RSI_OVERBOUGHT: <number in [65,85]>
REASON: <one short sentence>
"""


def strip_think(text: str) -> str:
    """Remove <think>...</think> blocks emitted by thinking models."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def call_gemini(prompt: str) -> tuple[str, float]:
    import google.generativeai as genai
    genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
    model = genai.GenerativeModel(GEMINI_MODEL)
    t0 = time.time()
    resp = model.generate_content(prompt)
    elapsed = time.time() - t0
    return (resp.text or "").strip(), elapsed


def call_minimax(prompt: str) -> tuple[str, float]:
    key = os.getenv("MINIMAX_API_KEY")
    t0 = time.time()
    resp = requests.post(
        MINIMAX_URL,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": MINIMAX_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 2000,
            "temperature": 0.3,
        },
        timeout=60,
    )
    elapsed = time.time() - t0
    resp.raise_for_status()
    body = resp.json()
    text = body["choices"][0]["message"]["content"]
    return strip_think(text), elapsed


def run_one(call_fn, prompt: str) -> tuple[Optional[dict], float, str]:
    try:
        text, elapsed = call_fn(prompt)
    except Exception as e:
        return None, 0.0, f"ERROR: {e}"
    parsed = ai._parse_gemini_output(text)
    return parsed, elapsed, text


def summarize(label: str, results: list[tuple]):
    print(f"\n{'=' * 70}\n{label}\n{'=' * 70}")
    parsed_list = [r[0] for r in results if r[0]]
    failures = sum(1 for r in results if r[0] is None)
    if not parsed_list:
        print(f"  ALL {len(results)} RUNS FAILED. Sample raw output:")
        print(f"  {results[0][2][:400]!r}")
        return

    scores = [p["score"] for p in parsed_list]
    confs = [p["confidence"] for p in parsed_list]
    os_vals = [p["rsi_oversold"] for p in parsed_list]
    ob_vals = [p["rsi_overbought"] for p in parsed_list]
    latencies = [r[1] for r in results if r[0]]

    def mstd(xs):
        m = statistics.mean(xs)
        s = statistics.stdev(xs) if len(xs) > 1 else 0.0
        return f"mean={m:.2f} stdev={s:.2f}"

    print(f"  successful: {len(parsed_list)}/{len(results)} (failures: {failures})")
    print(f"  SCORE       : {mstd(scores)}  values={scores}")
    print(f"  CONFIDENCE  : {mstd(confs)}")
    print(f"  RSI oversold: {mstd(os_vals)}")
    print(f"  RSI overbo. : {mstd(ob_vals)}")
    print(f"  latency (s) : {mstd(latencies)}")
    print(f"\n  Sample reasons:")
    for p in parsed_list[:3]:
        print(f"   - {p['reason']}")


def main():
    print(f"A/B test: {N_RUNS} runs each, models = Gemini ({GEMINI_MODEL}) vs MiniMax ({MINIMAX_MODEL})")
    print("\nFetching shared input (headlines + FNG)...")
    headlines = ai._fetch_headlines()
    fng = ai._fetch_fear_greed()
    print(f"  -> {len(headlines)} headlines, FNG = {fng}")

    prompt = build_prompt(headlines, fng)

    print(f"\nRunning Gemini x{N_RUNS}...")
    gemini_results = [run_one(call_gemini, prompt) for _ in range(N_RUNS)]

    print(f"Running MiniMax x{N_RUNS}...")
    minimax_results = [run_one(call_minimax, prompt) for _ in range(N_RUNS)]

    summarize(f"Gemini ({GEMINI_MODEL})", gemini_results)
    summarize(f"MiniMax ({MINIMAX_MODEL})", minimax_results)


if __name__ == "__main__":
    main()
