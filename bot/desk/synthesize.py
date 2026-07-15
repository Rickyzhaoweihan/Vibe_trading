#!/usr/bin/env python3
"""L6 — Synthesis (head of desk) + delivery.

Integrates L1–L5 (+ L7) into the deliverable: a full markdown desk note saved to
bot/reports/desk_<date>.md and a ≤1800-char iMessage digest. The note carries
the market thesis, the macro/exposure read, the unwind-risk read, portfolio
actions, per-name KEEP/BUY/TRIM/SELL calls, new ideas, and a game plan.

Backbone is deterministic (always works, fully testable). An optional read-only
LLM relay (prompts/desk_note.md) can enrich the thesis/game plan when --llm is
set; it is best-effort and never required.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import notify

DESK_DIR = Path(__file__).resolve().parent
BOT_DIR = DESK_DIR.parent
for _p in (str(DESK_DIR), str(BOT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import conf

IMSG_LIMIT = 1800


def _pct(x):
    return "n/a" if x is None else f"{x*100:+.1f}%"


# ---- digest (pure) ------------------------------------------------------

_REGIME_EMOJI = {"RISK_ON_TREND": "🟢", "NEUTRAL": "🟡",
                 "HIGH_VOL_CHOP": "🟠", "RISK_OFF_TREND": "🔴"}
_BAND_EMOJI = {"low": "🟢", "elevated": "🟡", "high": "🔴"}


def _pretty_date(date):
    try:
        return datetime.strptime(date, "%Y-%m-%d").strftime("%a %b %-d")
    except Exception:
        return date


def _short_reason(call):
    """A clean reason: drop the sentiment tail and the dev-y '(deep research
    skipped)' note. Clipped for the phone's 1800-char budget — but on the EMAIL
    channel there is no length cap, so the full reasoning goes out uncut."""
    r = (call.get("reason") or "").split(" | ")[0].split("(deep research")[0].strip()
    return r if _email_channel() else r[:96]


_ACTIVITY_VERB = {"NEW": "Opened", "CLOSED": "Closed", "ADDED": "Added to",
                  "REDUCED": "Reduced"}


def _activity_lines(activity):
    """Plain-language 'you traded X' lines from a diff_positions() result."""
    out = []
    for a in activity or []:
        verb = _ACTIVITY_VERB.get(a["kind"], a["kind"])
        out.append(f"{verb} {a['symbol']} — now {a['curr_qty']:g} sh ({a['delta']:+g})")
    return out


def _call_detail(c):
    """The concrete levels behind an actionable call: entry / stop / target /
    conviction — the 'more detail' a trader needs to actually place it. Leads
    with what the user HOLDS (shares + live value + live price) so an amount
    can be sanity-checked against the real position at a glance."""
    det = []
    if c.get("held_qty"):
        hv = f"≈${c['held_value']:,.0f}" if c.get("held_value") else ""
        px = f"@${c['price']:,.2f}" if c.get("price") else ""
        det.append(f"持有{c['held_qty']:g}股{hv}{px}")
    if c.get("entry_zone"):
        det.append(f"entry ${c['entry_zone']:,.2f}")
    if c.get("stop_loss"):
        det.append(f"stop ${c['stop_loss']:,.2f}")
    if c.get("target"):
        det.append(f"target ${c['target']:,.2f}")
    if c.get("conviction"):
        det.append(f"conv {c['conviction']}")
    if c.get("horizon"):
        det.append(c["horizon"])
    return " · ".join(det)


def build_digest(context):
    """Decision-first iMessage digest: a tight header, then the calls grouped by
    action (SELL / TRIM / BUY / KEEP) with reasons, the top new ideas, and the
    single most important portfolio directive. Clipped to the 1800-char cap."""
    macro = context.get("macro", {})
    unwind = context.get("unwind", {})
    pf = context.get("portfolio", {})
    calls = context.get("calls", [])
    ideas = context.get("ideas", {}).get("equity", [])
    label = macro.get("label", "?")
    net = int(macro.get("exposure", {}).get("net_target", 0) * 100)
    band = unwind.get("band", "?")

    L = []
    warnings = context.get("warnings") or []
    if warnings:
        L.append("⚠️ DESK WARNING")
        for w in warnings:
            L.append(f"• {w}")
        L.append("")
    L += [f"📊 DESK · {_pretty_date(context.get('date',''))} · {context.get('mode','note').upper()}",
          f"{_REGIME_EMOJI.get(label,'⚪')} {label.replace('_',' ').title()} · target net {net}%",
          f"{_BAND_EMOJI.get(band,'⚪')} Unwind risk: {band.upper()} ({unwind.get('score','?')})"]

    pulse = context.get("pulse") or {}
    if pulse:
        spy, qqq = pulse.get("spy", {}).get("pct"), pulse.get("qqq", {}).get("pct")
        if spy is not None or qqq is not None:
            lag = ", ".join(pulse.get("laggards", [])[:2])
            tail = f" · lagging {lag}" if lag else ""
            L.append(f"🌎 Mkt: SPY {_pct(spy)} · QQQ {_pct(qqq)}{tail}")
    if context.get("cash") is not None:
        total = pf.get("total_value", 0) + (context.get("cash") or 0)
        src = context.get("price_source") or ""
        src_s = f"（{src}报价）" if src else ""
        L.append(f"💼 账户总值 ${total:,.0f} · 持仓 ${pf.get('total_value',0):,.0f} · "
                 f"可用现金 ${context['cash']:,.0f}{src_s}")

    # The STRATEGIST's own voice — its posture, and where it OVERRODE the carried
    # deep-research verdict (its distinct judgment, not the mechanical calls).
    strat = context.get("strategist") or {}
    if strat:
        stance = {"raise": "偏防御，倾向提高现金 (raise cash)",
                  "deploy": "偏进攻，逢低部署现金 (deploy)",
                  "hold": "维持当前仓位 (hold steady)"}.get(strat.get("cash_stance"), "")
        ovr = strat.get("overrides") or []
        bits = []
        if stance:
            bits.append(stance)
        if ovr:
            downgraded = [o["ticker"] for o in ovr if o["to"] == "KEEP" and o["from"] in ("BUY", "NEW_BUY")]
            if downgraded:
                bits.append(f"下调研究看多为持有：{', '.join(downgraded[:5])}")
        if bits:
            L.append("🧠 策略台：" + " · ".join(bits))

    activity = context.get("activity") or []
    if activity:
        L.append("")
        L.append("📒 Trade monitored — your account changed:")
        for s in (_activity_lines(activity) if _email_channel() else _activity_lines(activity)[:6]):
            L.append(f"• {s}")
        L.append("↳ re-analyzed against your live book below")

    groups = [("🔴 SELL", "SELL"), ("🟠 TRIM", "TRIM"),
              ("🟢 BUY / ADD", ("BUY", "NEW_BUY"))]
    for header, actions in groups:
        acts = actions if isinstance(actions, tuple) else (actions,)
        hits = [c for c in calls if c.get("action") in acts]
        if hits:
            L.append("")
            L.append(header)
            for c in hits:
                tag = " (new)" if c.get("action") == "NEW_BUY" else ""
                amt = f" ${c['dollars']:,.0f}" if c.get("dollars") else ""
                sh = f" ≈{c['shares']:g}股" if c.get("shares") else ""
                rep = f" · 连续第{c['repeat_days']+1}天" if c.get("repeat_days") else ""
                when = f" · {c['when']}" if c.get("when") else ""
                L.append(f"• {c['ticker']}{tag}{amt}{sh}{when}{rep}")
                det = _call_detail(c)
                if det:
                    L.append(f"   • {det}")
                L.append(f"   ↳ {_short_reason(c)}")

    if ideas:
        L.append("")
        L.append("🔎 New ideas: " + ", ".join(
            f"{i['ticker']}({i.get('entry', i['setup'])}{'·分散' if i.get('diversifier') else ''})"
            for i in (ideas if _email_channel() else ideas[:4])))

    for w in context.get("ipo_watch") or []:
        px = f"（现价 ${w['last']:,.2f}）" if w.get("last") else "（未上市/无报价）"
        L.append("")
        L.append(f"🆕 关注 {w['ticker']}{px} — 入场区 ${w['ref_price']:,.2f}，"
                 f"到价会提醒你。{w.get('note','')}")

    keeps = [c["ticker"] for c in calls if c.get("action") == "KEEP"]
    if keeps:
        L.append("")
        L.append(f"⚪ KEEP ({len(keeps)}): " + ", ".join(keeps))

    hedge = context.get("hedge") or {}
    if hedge.get("options") and hedge.get("target_pct", 0) > 0:
        rec = next((o for o in hedge["options"] if o["ticker"] == hedge.get("recommend")),
                   hedge["options"][0])
        L.append("")
        L.append(f"🛡️ Hedge ~{hedge['target_pct']*100:.0f}% (${hedge.get('notional',0):,.0f}): "
                 f"{rec['ticker']} ~${rec['capital']:,.0f} · {hedge.get('urgency','')}")

    if pf.get("actions"):
        L.append("")
        L.append(f"⚠️ {pf['actions'][0]}")

    out = "\n".join(L)
    if _email_channel():
        return out                       # email has no length cap — never truncate
    return out[:IMSG_LIMIT - 1] + "…" if len(out) > IMSG_LIMIT else out


# ---- full report (pure) -------------------------------------------------

def _market_pulse_section(pulse):
    if not pulse:
        return ""
    def mv(d):
        p = (d or {}).get("pct")
        return _pct(p) if p is not None else "n/a"
    lead = ", ".join(pulse.get("leaders", [])) or "—"
    lag = ", ".join(pulse.get("laggards", [])) or "—"
    out = ["\n## Market Snapshot\n",
           f"- **S&P 500 (SPY):** {mv(pulse.get('spy'))} · **Nasdaq 100 (QQQ):** {mv(pulse.get('qqq'))} "
           f"· **VIX:** {mv(pulse.get('vix'))}\n",
           f"- **Sector leaders:** {lead}\n- **Sector laggards:** {lag}\n"]
    news = _clean_news(pulse.get("news") or "")
    if news:
        out.append("\n**Headlines (macro news):**\n" + news + "\n")
    return "".join(out)


# PR-wire sources and micro-cap-promo phrases that flood the macro feed with noise
_NEWS_SPAM = ("globenewswire", "access newswire", "pr newswire", "prnewswire",
              "business wire", "businesswire", "accesswire", "redchip", "newsfile",
              "regains compliance", "investor conference", "fireside chat", "webcast",
              "passwordless", "annual meeting", "to present at", "conference call",
              "earnings call scheduled")


def _clean_news(news):
    """Keep only market-relevant headlines from the raw macro-news digest — drop
    the micro-cap PR-wire spam (BIO-key/RedChip-style promos) that otherwise
    dominates the section — and cap to the top few. Returns '' if nothing useful."""
    kept = []
    for raw in news.splitlines():
        line = raw.strip().lstrip("#").strip()
        if not line or line.lower().startswith(("link:", "http")):
            continue
        if line.startswith("Global Market News"):        # digest header
            continue
        if any(sp in line.lower() for sp in _NEWS_SPAM):  # PR-wire / promo noise
            continue
        kept.append("- " + line)
        if len(kept) >= 5:
            break
    return "\n".join(kept)


def _hedge_section(hedge):
    if not hedge or not hedge.get("options"):
        return ""
    pct = hedge.get("target_pct", 0) * 100
    rows = ["\n## Downside Hedge — When the Market Falls, This Rises\n",
            f"- **Target hedge:** ~{pct:.0f}% of equity (≈${hedge.get('notional',0):,.0f} notional) "
            f"· {hedge.get('urgency','')}\n",
            f"- **Why this size:** {hedge.get('rationale','')}\n\n",
            "| instrument | put in | hedges | type | note |", "|---|--:|--:|---|---|"]
    for o in hedge["options"]:
        px = f" @ ${o['price']:,.2f}" if o.get("price") else ""
        kind = f"-{o['leverage']}x " + ("holdable" if o.get("holdable") else "TACTICAL — decays")
        rows.append(f"| **{o['ticker']}**{px} | ${o['capital']:,.0f} | ${o['neutralizes']:,.0f} | "
                    f"{kind} | {o.get('note','')} |")
    rec = hedge.get("recommend")
    rows.append(f"\n**Lead choice now: {rec}.** {hedge.get('confirm_note','')}")
    rows.append("\n_Pick one (don't double up). A -1x is the calm-tape standing hedge; the -3x "
                "needs a deep-research confirm on QQQ, a hard stop, and is for sharp risk-off "
                "windows only — it decays if held through chop. The intraday monitor watches any "
                "-3x position every minute with a tight stop._\n")
    return "\n".join(rows) + "\n"


def _macro_section(macro):
    exp = macro.get("exposure", {})
    rows = ["| asset | last | chg | trend |", "|---|--:|--:|---|"]
    for name, d in (macro.get("panel") or {}).items():
        last = d.get("last")
        last = f"{last:,.2f}" if isinstance(last, (int, float)) else "n/a"
        rows.append(f"| {name} | {last} | {_pct(d.get('pct'))} | {d.get('trend')} |")
    hedges = ", ".join(t for h in exp.get("hedges", []) for t in conf.HEDGES.get(h, [])) or "none"
    return (
        f"## Macro & Regime\n\n{macro.get('narrative','')}\n\n"
        f"- **Net-exposure target:** {int(exp.get('net_target',0)*100)}% invested\n"
        f"- **Rates:** {macro.get('rates_direction','?')}\n"
        f"- **Hedges/expressions:** {hedges}\n"
        + "".join(f"- {n}\n" for n in exp.get("notes", []))
        + "\n" + "\n".join(rows) + "\n"
    )


def _unwind_section(u):
    reasons = "".join(f"- {r}\n" for r in u.get("reasons", [])) or "- no unwind signals firing\n"
    return (f"\n## Momentum-Unwind Risk — {u.get('band','?').upper()} "
            f"({u.get('score','?')}/100)\n\n{reasons}")


def _portfolio_section(pf, cash=None):
    cl = "".join(f"- {c}: {d['pct']*100:.0f}% (${d['value']:,.0f})\n"
                 for c, d in (pf.get("clusters") or {}).items())
    conc = pf.get("concentration", {})
    acts = "".join(f"- {a}\n" for a in pf.get("actions", []))
    holdings_val = pf.get("total_value", 0)
    # label consistently with the digest: holdings vs account total (holdings+cash)
    if cash is not None:
        line = (f"- **Account total:** ${holdings_val + cash:,.0f} "
                f"(holdings ${holdings_val:,.0f} + cash ${cash:,.0f}) · "
                f"net {int(pf.get('current_net',1)*100)}% invested\n")
    else:
        line = f"- **Holdings value:** ${holdings_val:,.0f} · net {int(pf.get('current_net',1)*100)}% invested\n"
    return (
        f"\n## Portfolio & Risk\n\n"
        f"{line}"
        f"- **Crowded momentum bet:** {pf.get('crowding_share',0)*100:.0f}% of book\n"
        f"- **Concentration:** top1 {conc.get('top1',0)*100:.0f}%, top3 {conc.get('top3',0)*100:.0f}%, HHI {conc.get('hhi')}\n\n"
        f"**Exposure by cluster**\n{cl}\n**Actions**\n{acts}"
    )


def _activity_section(activity):
    if not activity:
        return ""
    rows = ["\n## Account Activity — Trade Monitored\n",
            "_Detected from your live account since the last snapshot and re-analyzed below._\n",
            "| ticker | you | now (sh) | change |", "|---|---|--:|--:|"]
    for a in activity:
        rows.append(f"| {a['symbol']} | {_ACTIVITY_VERB.get(a['kind'], a['kind'])} | "
                    f"{a['curr_qty']:g} | {a['delta']:+g} |")
    return "\n".join(rows) + "\n"


def _money(x, fmt="$%.2f"):
    return (fmt % x) if isinstance(x, (int, float)) else "—"


def _calls_section(calls):
    if not calls:
        return ""
    rows = ["\n## Calls — Holdings\n",
            "| ticker | you hold | live px | action | conv | $ to trade | entry | stop | target | when | reason |",
            "|---|--:|--:|---|---|--:|--:|--:|--:|---|---|"]
    for c in sorted(calls, key=lambda c: c.get("action") == "KEEP"):
        amt = f"${c['dollars']:,.0f}" if c.get("dollars") else "—"
        hold = (f"{c['held_qty']:g}股 ≈${c['held_value']:,.0f}"
                if c.get("held_qty") and c.get("held_value") else "—")
        rows.append(
            f"| {c['ticker']} | {hold} | {_money(c.get('price'))} | **{c['action']}** | "
            f"{c.get('conviction') or '—'} | {amt} | "
            f"{_money(c.get('entry_zone'))} | {_money(c.get('stop_loss'))} | {_money(c.get('target'))} | "
            f"{c.get('when') or '—'} | {_cell(c.get('reason'))} |")
    return "\n".join(rows) + "\n"


def _cell(text):
    """A reason safe to put in a markdown table cell: newlines flattened and pipes
    escaped (raw research prose contains both, which used to shatter the table into
    stray rows). Clipped for the phone; on the EMAIL channel the full text goes out."""
    t = " ".join((text or "").split()).replace("|", "/")
    return t if _email_channel() else (t[:140] + "…" if len(t) > 140 else t)


def _ideas_section(ideas):
    eq = ideas.get("equity", [])
    cr = ideas.get("crypto", [])
    if not eq and not cr:
        return ""
    out = ["\n## New Ideas (scouted)\n",
           "| ticker | entry | excess vs SPY | cluster | diversifies? |",
           "|---|---|--:|---|---|"]
    for i in eq + cr:
        div = "yes ✓" if i.get("diversifier") else "—"
        out.append(f"| {i['ticker']} | {i.get('entry', i['setup'])} | {_pct(i['excess'])} | "
                   f"{i.get('cluster', '—')} | {div} |")
    return "\n".join(out) + "\n"


def _sectors_section(sectors):
    if not sectors:
        return ""
    return (f"\n## Sector Rotation\n\n- **Leaders:** {', '.join(sectors.get('leaders',[]))}\n"
            f"- **Laggards:** {', '.join(sectors.get('laggards',[]))}\n")


def _clean_reason(c):
    """Short, human reason — strip the research boilerplate ('**Action**: X
    **Reasoning**: …'), the sentiment tail, and the dev note."""
    import re
    r = c.get("reason") or ""
    r = re.sub(r"\*\*Action\*\*:.*?\*\*Reason(?:ing)?\*\*:\s*", "", r, flags=re.I | re.S)
    r = r.split(" | ")[0].split("(deep research")[0].strip()
    return r[:110]


def _gameplan_section(context):
    """THE execution checklist — placed at the TOP of the note. Each actionable
    call is a ready-to-place order ticket: action · ticker · $ · shares · limit ·
    stop · timing, then a one-line why. This is what the owner acts on; the
    analysis below is the supporting rationale."""
    pf = context.get("portfolio", {})
    calls = context.get("calls", [])
    hedge = context.get("hedge", {})
    ideas = context.get("ideas", {}).get("equity", [])
    _order = {"SELL": 0, "TRIM": 1, "NEW_BUY": 2, "BUY": 3}
    acts = sorted((c for c in calls if c.get("action") in _order and c.get("dollars")),
                  key=lambda c: _order[c["action"]])
    lines = []
    for c in acts:
        sh = f" (~{c['shares']:g} sh)" if c.get("shares") else ""
        lvl = (f", limit ${c['entry_zone']:,.2f}" if c.get("entry_zone")
               else f", ~${c['price']:,.2f}" if c.get("price") else "")
        stop = f", stop ${c['stop_loss']:,.2f}" if c.get("stop_loss") else ""
        when = f" · {c['when']}" if c.get("when") else ""
        lines.append(f"**{c['action']} {c['ticker']} — ${c['dollars']:,.0f}**{sh}{lvl}{stop}{when}\n"
                     f"   ↳ {_clean_reason(c)}")
    # the standing hedge — a concrete, placeable action
    if hedge.get("options") and hedge.get("target_pct", 0) > 0:
        rec = next((o for o in hedge["options"] if o["ticker"] == hedge.get("recommend")),
                   hedge["options"][0])
        px = f" @ ${rec['price']:,.2f}" if rec.get("price") else ""
        lines.append(f"**Hedge: hold ~${rec['capital']:,.0f} {rec['ticker']}{px}** "
                     f"(~{hedge['target_pct']*100:.0f}% downside cover)")
    # top diversifying idea(s) to WATCH for a pullback (not chase)
    divs = [i for i in ideas if i.get("diversifier") and i.get("entry") in ("dip", "near-support")]
    for i in divs[:2]:
        lines.append(f"**Watch {i['ticker']}** ({i['entry']}, diversifies) — buy only on a pullback")
    if pf.get("actions"):
        lines.append(pf["actions"][0])       # the single most important risk directive
    if not lines:
        lines = ["No trades — hold the book, respect stops, no new risk today."]
    return ("\n## 🎯 Today's Plan — What To Do\n\n"
            + "".join(f"{i+1}. {p}\n" for i, p in enumerate(lines)))


def _accountability_section(review):
    if not review or not review.get("scored"):
        return ""
    return (f"\n## Accountability\n\n- Calls scored: {review['scored']} · "
            f"hit-rate {review.get('hit_rate')} · avg return {_pct(review.get('avg_return'))}\n")


def _research_focus_section(selected):
    if not selected:
        return ""
    rows = ["\n## Deep-Research Focus (top 6 by priority)\n",
            "| ticker | held | why |", "|---|---|---|"]
    for r in selected:
        rows.append(f"| {r['ticker']} | {'yes' if r.get('held') else 'idea'} | "
                    f"{', '.join(r.get('reasons') or ['baseline'])} |")
    return "\n".join(rows) + "\n"


def build_report_md(context):
    warn = context.get("warnings") or []
    warn_md = ("\n> ⚠️ **Warnings:** " + "; ".join(warn) + "\n\n") if warn else ""
    parts = [f"# Desk Note — {context.get('date','')}\n",
             f"_Advisory only. Generated by the desk; you execute._\n",
             warn_md,
             # EXECUTION FIRST: the placeable order tickets lead the note; the
             # analysis below is the supporting rationale, not the headline.
             _gameplan_section(context),
             _activity_section(context.get("activity", [])),
             context.get("thesis", "") + ("\n" if context.get("thesis") else ""),
             _market_pulse_section(context.get("pulse", {})),
             _hedge_section(context.get("hedge", {})),
             _macro_section(context.get("macro", {})),
             _unwind_section(context.get("unwind", {})),
             _portfolio_section(context.get("portfolio", {}), cash=context.get("cash")),
             _research_focus_section(context.get("research_selected", [])),
             _calls_section(context.get("calls", [])),
             _ideas_section(context.get("ideas", {})),
             _sectors_section(context.get("sectors", {})),
             _accountability_section(context.get("review", {}))]
    return "".join(parts)


# ---- delivery -----------------------------------------------------------

def _relay_env():
    """Env for a `claude -p` relay: drop ANTHROPIC_API_KEY so the CLI uses its own
    login rather than a raw .env key (which is for the research path and may be
    unset/invalid — forcing it breaks the relay with 'invalid x-api-key')."""
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    return env


def _log_message(subject, body, ok):
    """Append every outgoing message to logs/desk_messages.jsonl — a full audit
    trail of exactly what was sent to the phone (digests, reports, alerts)."""
    try:
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo
        rec = {"at": _dt.now(ZoneInfo("America/New_York")).isoformat(timespec="seconds"),
               "subject": subject, "body": body, "sent": bool(ok)}
        p = conf.LOGS_DIR / "desk_messages.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _email_channel():
    """True when ALERT_CHANNEL=email — delivery goes out as one full Gmail rather
    than chunked iMessage parts."""
    return os.environ.get("ALERT_CHANNEL", "auto").strip().lower() == "email"


def send_imessage(subject, body):
    """Send one message via notify.py, which routes it per ALERT_CHANNEL (email →
    Gmail SMTP; imessage/auto → the phone). Named for the legacy path; it is the
    desk's single 'send a message' entry point. Every message is logged to
    logs/desk_messages.jsonl regardless of send outcome."""
    py = str(conf.ROOT / ".venv" / "bin" / "python")
    ok = False
    try:
        subprocess.run([py, str(BOT_DIR / "notify.py"), subject],
                       input=body, text=True, capture_output=True, timeout=90)
        ok = True
    except Exception:
        ok = False
    _log_message(subject, body, ok)
    return ok


def chunk_md(text, *, size=1500):
    """Split markdown into ordered parts on line boundaries, each within the
    iMessage cap so the full report comes through intact."""
    chunks, cur = [], ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > size and cur:
            chunks.append(cur)
            cur = ""
        cur += line
    if cur.strip():
        chunks.append(cur)
    return chunks


def md_to_text(md):
    """Flatten a markdown report into readable plain text for iMessage: pipe
    tables become '• a · b · c' bullet lines (the big unreadable part), headers
    become 【section】, bold/italic markers stripped, blockquotes → ⚠️."""
    out, tbl = [], []

    def flush():
        data = [r for r in tbl if not (r and all(set(c) <= set("-: ") for c in r))]
        for r in (data[1:] if len(data) > 1 else []):     # drop header row; values self-describe
            cells = [c for c in r if c and c != "—"]
            if cells:
                out.append("• " + " · ".join(cells))
        tbl.clear()

    for raw in md.splitlines():
        s = raw.rstrip()
        if s.lstrip().startswith("|"):
            tbl.append([c.strip() for c in s.strip().strip("|").split("|")])
            continue
        flush()
        t = s.replace("**", "").replace("__", "")
        st = t.lstrip()
        if st.startswith("#"):
            out.append("")
            out.append("【" + st.lstrip("#").strip() + "】")
        elif st.startswith(">"):
            content = st.lstrip(">").strip()
            out.append(content if content.startswith("⚠️") else "⚠️ " + content)
        else:
            # strip a whole-line italic wrapper (*..* / _.._) without touching
            # underscores inside tokens like rates_10y
            b = t.strip()
            for mk in ("*", "_"):
                if len(b) > 2 and b.startswith(mk) and b.endswith(mk) and b.count(mk) == 2:
                    t = b[1:-1]
            out.append(t)
    flush()
    text = "\n".join(out)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip()


def send_report(subject, report):
    """Send a long report as ordered iMessage parts. Returns the part count."""
    parts = chunk_md(report)
    for i, c in enumerate(parts, 1):
        send_imessage(f"{subject} {i}/{len(parts)}", c)
    return len(parts)


def _cjk_ratio(s):
    """Fraction of the letters in `s` that are CJK — used to detect a chunk the
    translator left in English."""
    cjk = sum(1 for c in s if "一" <= c <= "鿿")
    latin = sum(1 for c in s if c.isascii() and c.isalpha())
    tot = cjk + latin
    return (cjk / tot) if tot else 1.0


_TRANSLATE_SYS = (
    "You translate a markdown trading report from English into Simplified "
    "Chinese. Translate ALL prose, headings, labels and table cells. Keep "
    "ticker symbols (NVDA, MU, PSQ, SKHY…), all numbers, %, $ amounts and "
    "URLs EXACTLY as-is. Preserve the markdown structure. Output ONLY the "
    "translated markdown — no preamble, no explanation.")

# The final QA / supervisor pass — the owner's request: nothing goes to the phone
# until GLM has re-read it, translated any English the first pass missed, and
# confirmed it reads coherently. Numbers are locked (enforced again by the guard).
_SUPERVISE_SYS = (
    "You are the final editor reviewing a Simplified-Chinese trading note just "
    "before it is texted to the user. Do two things: (1) rewrite ANY remaining "
    "English word, phrase or sentence into natural Simplified Chinese so the whole "
    "passage reads as fluent Chinese; (2) make sure it reads clearly and is not "
    "self-contradictory (e.g. don't say both hold and sell the same name). "
    "HARD RULES: keep every ticker symbol (NVDA, MU, PSQ, SKHY…), every number, %, "
    "$ amount, stop/limit price level and URL EXACTLY as written — never change, "
    "add or drop a figure. Do not invent any new facts or numbers. Preserve the "
    "markdown structure. Output ONLY the finalized markdown — no preamble.")


def openrouter_chat(messages, *, model=None, temperature=0, max_tokens=4000,
                    timeout=120, reasoning_enabled=False, response_format=None):
    """One OpenRouter chat-completion call. The single place the desk talks to the
    OpenRouter API (both the zh translation and the strategist go through here).

    `messages` is the OpenAI-style list. `response_format={"type":"json_object"}`
    asks for strict JSON (used by the strategist). Returns the assistant text, or
    None on any failure.

    `reasoning_enabled=False` is important: a reasoning model (e.g. GLM-5.2) left to
    think spends the whole `max_tokens` budget on chain-of-thought and returns
    content=None (finish_reason="length"); disabling it makes content come back
    directly and is a harmless no-op for non-reasoning models."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return None
    try:
        import requests
        model = model or os.environ.get("BOT_QUICK_LLM", "deepseek/deepseek-v4-pro")
        body = {"model": model, "temperature": temperature, "max_tokens": max_tokens,
                "reasoning": {"enabled": reasoning_enabled}, "messages": messages}
        if response_format:
            body["response_format"] = response_format
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=body, timeout=timeout)
        r.raise_for_status()
        return (r.json()["choices"][0]["message"]["content"] or "").strip() or None
    except Exception:
        return None


def _glm_translate(text, *, timeout=120, max_tokens=6000, system=None):
    """One OpenRouter chat call over a markdown chunk (default: translate to
    Simplified Chinese; pass `system=_SUPERVISE_SYS` for the final QA pass). A plain
    chat call is far more reliable than the `claude -p` agent, which left long
    reports half-English. `max_tokens` must be generous or a long chunk gets
    truncated (which then fails the numeric guard). Returns None on any failure.

    (effort:low is NOT a substitute for reasoning-off — it rewrites "$9.50"→"9.50美元"
    and trips the numeric guard.)"""
    try:
        return openrouter_chat(
            [{"role": "system", "content": system or _TRANSLATE_SYS},
             {"role": "user", "content": text}],
            temperature=0, max_tokens=max_tokens, timeout=timeout)
    except Exception:
        return None


TRANSLATE_CHUNK = 2800


def _split_on_lines(text, max_len):
    """Break `text` into <=max_len pieces on line boundaries (never mid-line)."""
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        if cur and len(cur) + len(line) > max_len:
            out.append(cur)
            cur = ""
        cur += line
    if cur:
        out.append(cur)
    return out


def translation_chunks(text, max_len=TRANSLATE_CHUNK, *, group=True):
    """Split a markdown report into translation-sized chunks, always SPLITTING any
    single oversized `## ` section on line boundaries.

    Splitting the big ones matters: a long section (e.g. the Calls table once its
    reasons aren't clipped) sent whole overruns `max_tokens`, comes back truncated,
    drops figures, fails `_tokens_preserved`, and silently falls back to English.

    `group=True` packs small sections together (fewer API calls) — right for the
    bulk translate pass. `group=False` keeps one chunk per section, which the
    supervisor needs: it re-touches ONLY the sections still holding English, so
    merging a clean Chinese section into an English one would re-process (and risk
    corrupting) text that was already fine."""
    import re
    chunks, cur = [], ""
    for sec in re.split(r"(?=\n## )", text):
        for piece in ([sec] if len(sec) <= max_len else _split_on_lines(sec, max_len)):
            if cur and (not group or len(cur) + len(piece) > max_len):
                chunks.append(cur)
                cur = ""
            cur += piece
    if cur.strip():
        chunks.append(cur)
    return chunks


def translate_to_zh(text):
    """Translate a markdown report to Simplified Chinese via direct OpenRouter
    calls, CHUNKED so no chunk is long enough to get truncated. Each chunk is used
    only if it preserves every $ amount / % and actually came back in Chinese; a
    chunk that fails keeps its English (delivery never breaks). Returns the input
    unchanged only if EVERY chunk failed — so `text != input` means it worked."""
    if not text or not text.strip():
        return text
    chunks = translation_chunks(text)

    def _one(c):
        zh = _glm_translate(c)
        # Accept when the figures survive AND the result is no less Chinese than the
        # source. An absolute threshold (">=40% CJK") silently rejected the
        # figure-dense sections: a faithful translation of a Calls-table row is
        # legitimately <40% CJK because tickers/prices dominate, and those chunks
        # already carry some Chinese ("5.76股", carried tags) so they missed the
        # all-English bypass too — which is why the table kept shipping in English.
        if zh and _tokens_preserved(c, zh) and _cjk_ratio(zh) >= _cjk_ratio(c):
            return (zh if zh.endswith("\n") else zh + "\n"), True
        return c, False                       # keep this section's English on failure

    # chunks are independent API calls — translate them in parallel (ordered result)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(6, len(chunks))) as ex:
        results = list(ex.map(_one, chunks))
    any_ok = any(ok for _, ok in results)
    return "".join(t for t, _ in results) if any_ok else text


def _has_english_prose(s):
    """True if `s` still contains English *words* (two+ consecutive lowercase
    letters) after stripping URLs and code spans. Tickers are ALL-CAPS so they
    don't trip this — only genuine untranslated prose does. This is what tells the
    supervisor which sections the first translation pass left in English."""
    import re
    s = re.sub(r"https?://\S+", " ", s)          # keep URLs verbatim, don't flag them
    s = re.sub(r"`[^`]*`", " ", s)               # ignore inline code
    s = re.sub(r"\S*[:=^~]\S*", " ", s)          # data symbols: idx:^KS11, ES=F, fut:NQ=F
    s = re.sub(r"\b\w+\.[A-Za-z]{2,4}\b", " ", s)  # source domains: Barrons.com, wsj.com
    return bool(re.search(r"[a-z]{2,}", s))


def supervise_zh(text):
    """Final QA pass before anything is texted (the owner's explicit request): have
    GLM re-read the already-translated note, turn any leftover English into Chinese,
    and sanity-check coherence — while every $/%/level is locked. Only sections that
    still contain English prose are re-processed (cheap, and avoids re-touching clean
    Chinese). A re-processed section is accepted only if it preserves every figure
    and comes back *more* Chinese than before; otherwise the original is kept, so
    supervision can only improve delivery, never corrupt it."""
    if not text or not text.strip():
        return text
    # One chunk per section (group=False) so we only re-touch the sections still
    # holding English — but oversized sections are still SPLIT, since sending one
    # whole would overrun max_tokens, come back truncated, fail the figure guard,
    # and be discarded, leaving exactly the English we're here to fix.
    sections = translation_chunks(text, group=False)

    def _fix(sec):
        if not sec.strip() or not _has_english_prose(sec):
            return sec                            # already clean Chinese — leave it
        best = sec                                # ratchet toward more-Chinese, never worse
        for _ in range(3):                        # stubborn sections get a few attempts
            out = _glm_translate(best, system=_SUPERVISE_SYS)
            # figures must match EXACTLY — none dropped (sec⊆out) and none the QA
            # pass hallucinated in (out⊆sec) — and it must be strictly more Chinese.
            if not (out and _tokens_preserved(sec, out) and _tokens_preserved(out, sec)
                    and _cjk_ratio(out) >= _cjk_ratio(best)):
                break                             # no gain or guard fail → stop, keep best
            best = out
            if not _has_english_prose(best):
                break                             # fully Chinese now
        return best if best.endswith("\n") else best + "\n"

    todo = [s for s in sections if s]
    if not any(_has_english_prose(s) for s in todo):
        return text                               # nothing left to supervise
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(6, len(todo))) as ex:
        return "".join(ex.map(_fix, todo))


def _tokens_preserved(src, out):
    """True if every DOLLAR AMOUNT and PERCENTAGE in `src` survives in `out`.
    Guards the translation relay from mangling a placeable figure (a stop, a size,
    a level) — the one failure that actually matters.

    The figure must survive as a NUMBER, not as a literal glyph: a faithful Chinese
    translation legitimately renders "$28.40" as "28.40美元" and "5%" as "5%"/"百分之5",
    so demanding the "$" itself rejected perfectly good translations and dropped the
    figure-dense sections (the Calls table, headlines) back to English. We therefore
    take the figures FROM `src` (where they're unambiguously $/%-marked) and only
    require that each one still appears somewhere in `out` as that number.

    Deliberately does NOT check bare integers or ALL-CAPS tokens: a faithful
    translation renders "top-3"→"前三", "10Y"→"10年期", "SELL"→"卖出".
    Commas in thousands are normalized so "$9,988" == "$9988"."""
    import re
    def _norm(s):
        return re.sub(r"(?<=\d),(?=\d)", "", s)                   # 9,988 -> 9988
    src, out = _norm(src), _norm(out)
    src_d = Counter(re.findall(r"\$\s?(\d+(?:\.\d+)?)", src))     # $ amounts / levels
    src_p = Counter(re.findall(r"(\d+(?:\.\d+)?)\s?%", src))      # percentages
    out_nums = Counter(re.findall(r"(\d+(?:\.\d+)?)", out))       # any number, however decorated
    if any(out_nums.get(k, 0) < v for k, v in src_d.items()):
        return False
    if any(out_nums.get(k, 0) < v for k, v in src_p.items()):
        return False
    return True


_PDF_CSS = """
@page { size: A4; margin: 15mm 13mm; }
* { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { font: 12.5px/1.55 -apple-system, "Helvetica Neue", "PingFang SC",
       "Hiragino Sans GB", sans-serif; color: #1b1f24; margin: 0; }
h1 { font-size: 21px; margin: 0 0 4px; border-bottom: 2px solid #1b1f24; padding-bottom: 6px; }
h2 { font-size: 15px; margin: 20px 0 6px; color: #0a3d62; border-bottom: 1px solid #dfe3e8; padding-bottom: 3px; }
h1 + p, h2 + p { margin-top: 4px; }
p, li { margin: 4px 0; }
em { color: #6b7280; }
blockquote { background: #fff8e1; border-left: 4px solid #f0ad4e; margin: 10px 0;
             padding: 8px 12px; border-radius: 4px; }
table { border-collapse: collapse; width: 100%; font-size: 11.5px; margin: 8px 0 14px; }
th, td { border: 1px solid #d0d5dd; padding: 5px 8px; text-align: left; vertical-align: top; }
th { background: #eef2f7; font-weight: 600; }
tr:nth-child(even) td { background: #fafbfc; }
td:nth-child(n+3) { text-align: right; }
strong { color: #0a0a0a; }
code { background: #f1f3f5; padding: 1px 5px; border-radius: 3px; font-size: 11px; }
"""


# Image delivery: a phone-width container on top of the report CSS.
_IMG_CSS = _PDF_CSS + "\nbody { max-width: 760px; margin: 0 auto; padding: 18px; background: #fff; }"


def render_png(md_text, out_path, *, title="Desk Report"):
    """Render a markdown report to a single tall PNG via a headless-Chrome
    full-page screenshot, auto-cropped to the content. Best-effort — returns the
    Path on success, None on any failure (caller falls back to chunked text)."""
    try:
        import markdown
        from PIL import Image, ImageChops
        body = markdown.markdown(md_text, extensions=["tables", "fenced_code", "sane_lists"])
        html = (f"<!doctype html><html><head><meta charset='utf-8'><title>{title}</title>"
                f"<style>{_IMG_CSS}</style></head><body>{body}</body></html>")
        out_path = Path(out_path).resolve()
        html_path = out_path.with_suffix(".html")
        html_path.write_text(html)
        out_path.unlink(missing_ok=True)
        chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        with tempfile.TemporaryDirectory() as ud:     # isolated profile so a running Chrome doesn't clash
            proc = subprocess.Popen(
                [chrome, "--headless=new", "--disable-gpu", "--no-first-run",
                 "--no-default-browser-check", f"--user-data-dir={ud}", "--hide-scrollbars",
                 "--force-device-scale-factor=2", "--window-size=800,9000",
                 f"--screenshot={out_path}", html_path.as_uri()],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # Chrome writes the PNG in a few seconds but often hangs on exit, so
            # poll for the finished file instead of waiting for a clean exit.
            deadline, last = time.time() + 50, -1
            try:
                while time.time() < deadline:
                    if proc.poll() is not None:
                        break
                    if out_path.exists():
                        sz = out_path.stat().st_size
                        if sz > 0 and sz == last:      # written and stable
                            break
                        last = sz
                    time.sleep(0.6)
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        proc.kill()
        if not (out_path.exists() and out_path.stat().st_size > 0):
            return None
        # crop the tall capture down to the actual content
        im = Image.open(out_path).convert("RGB")
        bbox = ImageChops.difference(im, Image.new("RGB", im.size, (255, 255, 255))).getbbox()
        if bbox:
            im.crop((0, 0, im.width, min(im.height, bbox[3] + 40))).save(out_path)
        return out_path
    except Exception:
        return None


def send_imessage_file(path, *, target=None):
    """Send a file (PDF) as an iMessage attachment via Messages.app AppleScript."""
    target = target or notify.IMESSAGE_TO
    script = f'''
    tell application "Messages"
        set ok to false
        repeat with acc in (every account whose enabled is true)
            try
                set tgt to participant "{target}" of acc
                send (POSIX file "{path}") to tgt
                set ok to true
                exit repeat
            end try
        end repeat
        if not ok then error "no enabled account could reach {target}"
    end tell
    '''
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "osascript failed")


def deliver(context, *, notify=True):
    """Write the English report, then (optionally) deliver it — translated to the
    configured delivery language. Returns (path, digest).

    Channel (`ALERT_CHANNEL`):
      email    — ONE message: the decision digest on top, then the FULL report.
                 Email has no length cap, so nothing is chunked or truncated.
      otherwise— the legacy phone path: a short digest + the report as ordered
                 1800-char iMessage parts.
    """
    conf.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report = build_report_md(context)
    digest = build_digest(context)
    date = context.get("date", "")
    mode = context.get("mode", "note")
    path = conf.REPORTS_DIR / f"desk_{date}.md"
    path.write_text(report)                      # archive stays English

    out_digest, out_report, suffix = digest, report, ""
    if conf.DELIVER_LANG == "zh":
        # translate, then a supervisor re-reads the result and fixes any English the
        # first pass missed + checks coherence — figures stay locked throughout.
        out_report = supervise_zh(translate_to_zh(report))
        out_digest = supervise_zh(translate_to_zh(digest))
        suffix = "_zh"
        try:
            (conf.REPORTS_DIR / f"desk_{date}{suffix}.md").write_text(out_report)
        except Exception:
            pass

    if notify:
        if _email_channel():
            body = out_digest + "\n\n" + ("─" * 32) + "\n\n" + md_to_text(out_report)
            send_imessage(f"{conf.MSG_PREFIX} {mode} {date}", body)   # routed to Gmail by notify.py
        else:
            send_imessage(f"{conf.MSG_PREFIX} {mode} {date}", out_digest)         # quick decision digest
            send_report(f"{conf.MSG_PREFIX} report {date}", md_to_text(out_report))  # chunked parts
    return path, digest


# ---- optional LLM enrichment (best-effort, read-only) -------------------

def enrich_with_llm(context, *, run_dir, prompt="desk_note.md"):
    """Write the structured context and ask a headless read-only claude to write
    a sharper thesis/game-plan (or alert). Returns text or None on any failure.
    The allowlist is strictly read-only — the desk can never place an order."""
    try:
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        ctx_path = run_dir / "desk_context.json"
        ctx_path.write_text(json.dumps(context, default=str, indent=2))
        prompt = (BOT_DIR / "prompts" / prompt).read_text().replace(
            "{{CONTEXT}}", str(ctx_path))
        # read-only: read the context, sanity-check a quote, and SEARCH THE WEB for
        # the day's actual market-moving news. No order tools — never trades.
        ro_tools = "Read,mcp__robinhood-trading__get_equity_quotes,WebSearch,WebFetch"
        r = subprocess.run(
            ["claude", "-p", prompt, "--allowedTools", ro_tools, "--output-format", "text"],
            capture_output=True, text=True, timeout=240, cwd=str(conf.ROOT), env=_relay_env())
        out = r.stdout.strip() or None
        # Backstop: reject the enrichment if it contains a $ figure grossly beyond
        # the whole account — a gross hallucination (the deterministic sized plan is
        # the source of truth, so dropping the prose is always safe).
        if out and not _dollars_within(out, context, factor=1.2):
            return None
        return out
    except Exception:
        return None


def _dollars_within(text, context, *, factor=1.2):
    """False if `text` states any $ amount larger than `factor`× the account's total
    value — a figure that big can't be a real trade/level for this book."""
    import re
    pf = context.get("portfolio", {}) or {}
    total = (pf.get("total_value", 0) or 0) + (context.get("cash", 0) or 0)
    if total <= 0:
        return True
    for m in re.findall(r"\$\s?([0-9][0-9,]*(?:\.[0-9]+)?)", text):
        try:
            if float(m.replace(",", "")) > total * factor:
                return False
        except ValueError:
            continue
    return True
