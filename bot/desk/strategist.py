#!/usr/bin/env python3
"""The Strategist — the desk's cheap, memory-having, news-aware brain.

One OpenRouter/DeepSeek chat call per run (~$0.01). It reads the deterministic
desk analysis + the live book (cash/positions) + recent trading history + its own
persistent memory + a news/event digest (from the existing TradingAgents tools,
via news.py), and returns:

  - a narrative note (market pulse / thesis / what-I-expect / priorities),
  - TENTATIVE ACTIONS (buy SQQQ, trim NVDA, raise cash, buy ARM on a dip) — the
    strategist NAMES them; the deterministic pipeline (plan_trades / hedge_plan /
    audit_feasibility) SIZES and GUARDS them,
  - ESCALATIONS: the (usually 0) names that actually earn expensive deep research,
    hard-capped GLOBALLY per day, and
  - a memory update it carries forward to tomorrow.

ADVISORY ONLY — no order tools. The one bit of I/O is the OpenRouter call
(`synthesize.openrouter_chat`) plus reading/writing its own memory + state files.

  python bot/desk/strategist.py --date 2026-07-14   # dry: print the parsed result
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DESK_DIR = Path(__file__).resolve().parent
BOT_DIR = DESK_DIR.parent
for _p in (str(DESK_DIR), str(BOT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import conf
import synthesize as L6
import research as L4
import news as NEWS

ET = ZoneInfo("America/New_York")

# Actions the strategist may name. Sizing/feasibility is deterministic downstream.
_BUY = {"BUY", "NEW_BUY"}
_SELLS = {"SELL", "TRIM"}
_ACTIONABLE = _BUY | _SELLS

_MEMORY_SEED = """# Strategist Memory

## THESES
(none yet)

## LESSONS
(none yet)

## WATCHLIST
(none yet)

## OPEN_TENTATIVE_ACTIONS
(none yet)
"""


# ---- memory -----------------------------------------------------------------

def load_memory():
    """The strategist's rolling memory (small). Seed template if missing."""
    try:
        txt = conf.STRATEGIST_MEMORY_PATH.read_text().strip()
        return txt or _MEMORY_SEED
    except Exception:
        return _MEMORY_SEED


def save_memory(memory_md, *, date):
    """Persist the strategist's updated memory, bounded to memory_max_bytes. The
    model returns the COMPLETE reconciled memory (it was given yesterday's memory +
    today's activity/review), so we just stamp + clip + write."""
    if not memory_md or not memory_md.strip():
        return False
    cap = conf.STRATEGIST.get("memory_max_bytes", 8000)
    body = memory_md.strip()
    body = re.sub(r"^#\s*Strategist Memory.*\n", "", body).lstrip()
    out = f"# Strategist Memory (updated {date})\n\n{body}"
    if len(out.encode("utf-8")) > cap:
        out = out.encode("utf-8")[:cap].decode("utf-8", "ignore").rsplit("\n", 1)[0]
    try:
        conf.STRATEGIST_MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        conf.STRATEGIST_MEMORY_PATH.write_text(out)
        return True
    except Exception:
        return False


# ---- escalation state (GLOBAL daily deep-research cap) -----------------------

def load_state():
    try:
        return json.loads(conf.STRATEGIST_STATE_PATH.read_text())
    except Exception:
        return {}


def save_state(state):
    try:
        conf.STRATEGIST_STATE_PATH.write_text(json.dumps(state, indent=2))
    except Exception:
        pass


def cap_escalations(escalate, *, date, coverage=None, cap=None):
    """Return the tickers that may be deep-researched now, honoring the HARD GLOBAL
    daily ceiling shared by preopen + intraday (tracked in strategist_state.json).
    Drops names already deep-researched today, de-dupes, and records the spend."""
    cap = conf.STRATEGIST.get("escalate_cap", 2) if cap is None else cap
    coverage = coverage if coverage is not None else L4.load_coverage()
    st = load_state()
    if st.get("date") != date:
        st = {"date": date, "escalations_today": [], "intraday_fired": []}
    already = set(st.get("escalations_today", []))
    picked = []
    for e in escalate or []:
        t = (e.get("ticker") if isinstance(e, dict) else e or "").upper().strip()
        if not t or t in already or t in picked:
            continue
        if L4.stale_days(t, date, coverage) == 0:      # already researched today
            continue
        if len(already) + len(picked) >= cap:
            break
        picked.append(t)
    st["escalations_today"] = sorted(already | set(picked))
    save_state(st)
    return picked


# ---- action → sizeable call dict --------------------------------------------

def actions_to_calls(actions, *, holdings, prices=None):
    """Map strategist actions into the pipeline's `calls` dict shape so the
    EXISTING deterministic sizing (plan_trades) and feasibility gate
    (audit_feasibility) own the dollars. Returns (calls, cash_stance).

    - size_hint is DISCARDED (advisory only).
    - hedge-ticker actions are dropped: the hedge engine is their single source of
      truth (desk.py forces them to KEEP anyway).
    - RAISE_CASH is not a per-name order; it sets cash_stance='raise'.
    - BUY/NEW_BUY are normalized to held/not-held."""
    holds = set(holdings or [])
    calls, cash_stance = [], "hold"
    for a in actions or []:
        if not isinstance(a, dict):
            continue
        tkr = (a.get("ticker") or "").upper().strip()
        act = (a.get("action") or "").upper().strip()
        if act in ("RAISE_CASH", "RAISE CASH", "CASH"):
            cash_stance = "raise"
            continue
        if not tkr:
            continue
        if tkr in conf.HEDGE_TICKERS:              # hedge engine owns hedge names
            continue
        if act == "HEDGE":                          # a non-instrument hedge ask → cash raise
            cash_stance = "raise"
            continue
        if act in ("DEPLOY",):
            cash_stance = "deploy"
            continue
        held = tkr in holds
        if act == "BUY" and not held:
            act = "NEW_BUY"
        elif act == "NEW_BUY" and held:
            act = "BUY"
        if act not in _ACTIONABLE | {"KEEP", "WATCH"}:
            continue
        calls.append({
            "ticker": tkr, "held": held, "action": act,
            "rating": a.get("rating") or ("Buy" if act in _BUY else
                                          "Sell" if act == "SELL" else "Hold"),
            "conviction": (a.get("conviction") or "medium").lower(),
            "horizon": a.get("horizon") or L4.horizon_of(act),
            "stop_loss": _num(a.get("stop_loss")),
            "target": _num(a.get("target")),
            "entry_zone": _num(a.get("entry_zone") or a.get("entry")),
            "reason": (a.get("reason") or "").strip() or "strategist call",
            "event_driven": bool(a.get("event_driven")),
            "source": "strategist", "ok": False,
        })
    return calls, cash_stance


def _num(x):
    try:
        return float(x) if x is not None and x != "" else None
    except (TypeError, ValueError):
        return None


# ---- the call ---------------------------------------------------------------

_SYS_FALLBACK = (
    "You are the head of an advisory trading desk. You never place orders. Read the "
    "context, memory and news, then reply with ONLY a single JSON object per the schema.")


def _schema_hint():
    return (
        'Reply with ONLY one JSON object, no markdown fences, no prose:\n'
        '{\n'
        '  "narrative": {"market_pulse": str, "thesis": str, "expect": str, "priorities": [str]},\n'
        '  "cash_stance": "raise|deploy|hold",\n'
        '  "actions": [{"ticker": str, "action": "BUY|NEW_BUY|TRIM|SELL|HEDGE|RAISE_CASH|KEEP|WATCH",\n'
        '     "size_hint": str, "entry_zone": number|null, "stop_loss": number|null,\n'
        '     "target": number|null, "conviction": "low|medium|high",\n'
        '     "horizon": str, "event_driven": bool, "reason": str}],\n'
        '  "escalate": [{"ticker": str, "why": str}],\n'
        '  "memory_update": "the COMPLETE updated memory markdown (THESES/LESSONS/WATCHLIST/'
        'OPEN_TENTATIVE_ACTIONS), reconciling open actions vs the activity/review given, bounded"\n'
        '}')


def _prompt():
    try:
        return (BOT_DIR / "prompts" / "desk_strategist.md").read_text()
    except Exception:
        return _SYS_FALLBACK


def strategist_context(desk_context, *, memory, news, mode, intraday):
    """Compact JSON the model sees — a token-cheap subset of the desk context."""
    pf = desk_context.get("portfolio", {}) or {}
    macro = desk_context.get("macro", {}) or {}
    calls = desk_context.get("calls", []) or []
    brief_calls = [{
        "ticker": c.get("ticker"), "action": c.get("action"),
        "conviction": c.get("conviction"), "stop": c.get("stop_loss"),
        "held_value": c.get("held_value"), "reason": (c.get("reason") or "")[:160],
    } for c in calls]
    return {
        "date": desk_context.get("date"), "mode": mode, "intraday": intraday,
        "macro": {"regime": macro.get("label"), "rates": macro.get("rates"),
                  "net_target": (macro.get("exposure", {}) or {}).get("net_target")},
        "unwind": {"band": (desk_context.get("unwind", {}) or {}).get("band"),
                   "score": (desk_context.get("unwind", {}) or {}).get("score")},
        "account": {"cash": desk_context.get("cash"),
                    "total_value": pf.get("total_value"),
                    "current_net": pf.get("current_net"),
                    "crowding": pf.get("crowding"),
                    "clusters": pf.get("clusters")},
        "pulse": desk_context.get("pulse", {}),
        "hedge": {k: desk_context.get("hedge", {}).get(k)
                  for k in ("target_pct", "notional", "recommend", "rationale")},
        "carried_calls": brief_calls,
        "ideas": [i.get("ticker") for i in (desk_context.get("ideas", {}) or {}).get("equity", [])[:6]],
        "activity": desk_context.get("activity", []),
        "review": desk_context.get("review", {}),
        "memory": memory,
        "news": news,
    }


def run_strategist(desk_context, *, mode, date, intraday=False):
    """The one strategist call. Returns a validated result dict, or None on any
    failure (the caller falls back to the deterministic carried-verdict calls)."""
    holdings = [c.get("ticker") for c in (desk_context.get("calls") or []) if c.get("ticker")]
    if not holdings:
        holdings = [p.get("symbol") for p in
                    (desk_context.get("portfolio", {}) or {}).get("positions", []) if p.get("symbol")]
    ideas = [i.get("ticker") for i in (desk_context.get("ideas", {}) or {}).get("equity", [])]
    try:
        news = NEWS.event_digest(date, holdings, ideas,
                                 per_name=conf.STRATEGIST.get("news_per_name", 4))
    except Exception:
        news = ""
    memory = load_memory()
    ctx = strategist_context(desk_context, memory=memory, news=news, mode=mode, intraday=intraday)

    sys_prompt = _prompt() + "\n\n" + _schema_hint()
    user = ("Here is today's desk context (JSON), your memory, and the news. "
            "Think like a portfolio manager who remembers and watches the world. "
            "Propose tentative, feasible actions and name only the tickers that truly "
            "need expensive deep research.\n\n```json\n"
            + json.dumps(ctx, default=str)[:16000] + "\n```")
    messages = [{"role": "system", "content": sys_prompt},
                {"role": "user", "content": user}]

    max_tok = conf.STRATEGIST.get("max_tokens", 3500)
    raw = L6.openrouter_chat(messages, max_tokens=max_tok, temperature=0.3,
                             response_format={"type": "json_object"})
    result = _parse(raw, desk_context)
    if result is None:                              # one retry with a stern nudge
        messages.append({"role": "user",
                         "content": "Your previous reply was not valid JSON. Return ONLY the JSON object."})
        raw = L6.openrouter_chat(messages, max_tokens=max_tok, temperature=0,
                                 response_format={"type": "json_object"})
        result = _parse(raw, desk_context)
    return result


def _parse(raw, desk_context):
    """Tolerant JSON extraction + the $-hallucination backstop. None on failure."""
    if not raw:
        return None
    txt = raw.strip()
    txt = re.sub(r"^```(?:json)?", "", txt).strip()
    txt = re.sub(r"```$", "", txt).strip()
    if "{" in txt:                                  # balance-match the first object
        start = txt.index("{")
        depth = 0
        for i in range(start, len(txt)):
            if txt[i] == "{":
                depth += 1
            elif txt[i] == "}":
                depth -= 1
                if depth == 0:
                    txt = txt[start:i + 1]
                    break
    try:
        obj = json.loads(txt)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    # backstop: reject a gross $ hallucination (bigger than 1.2x the whole account)
    if not L6._dollars_within(json.dumps(obj, default=str), desk_context, factor=1.2):
        return None
    obj.setdefault("narrative", {})
    obj.setdefault("actions", [])
    obj.setdefault("escalate", [])
    obj.setdefault("cash_stance", "hold")
    return obj


def narrative_md(result):
    """Render the strategist's narrative dict into the markdown the report expects
    (## Market Pulse / ## Thesis / ## What I Expect / ## Priorities)."""
    n = (result or {}).get("narrative") or {}
    if isinstance(n, str):
        return n.strip()
    out = []
    if n.get("market_pulse"):
        out.append("## Market Pulse — Why It's Moving\n\n" + n["market_pulse"].strip())
    if n.get("thesis"):
        out.append("## Thesis\n\n" + n["thesis"].strip())
    if n.get("expect"):
        out.append("## What I Expect\n\n" + n["expect"].strip())
    pri = n.get("priorities")
    if pri:
        if isinstance(pri, list):
            pri = "\n".join(f"- {p}" for p in pri)
        out.append("## Priorities\n\n" + str(pri).strip())
    return "\n\n".join(out).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now(ET).strftime("%Y-%m-%d"))
    args = ap.parse_args()
    import portfolio as L5
    pos = L5.load_positions()
    holdings = [p["symbol"] for p in pos.get("positions", [])] or list(conf.HOLDINGS)
    ctx = {"date": args.date, "cash": pos.get("cash", 0.0),
           "portfolio": {"positions": pos.get("positions", [])},
           "calls": [{"ticker": s, "action": "KEEP", "conviction": "low"} for s in holdings],
           "ideas": {"equity": []}, "activity": [], "review": {}, "pulse": {}, "hedge": {},
           "macro": {}, "unwind": {}}
    res = run_strategist(ctx, mode="preopen", date=args.date)
    print(json.dumps(res, indent=2, default=str) if res else "STRATEGIST FAILED (None)")


if __name__ == "__main__":
    main()
