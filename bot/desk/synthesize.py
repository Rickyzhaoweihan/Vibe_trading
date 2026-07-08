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
    """A clean one-liner reason: drop the sentiment tail and the dev-y
    '(deep research skipped)' note, clip for the phone."""
    r = (call.get("reason") or "").split(" | ")[0].split("(deep research")[0].strip()
    return r[:96]


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

    activity = context.get("activity") or []
    if activity:
        L.append("")
        L.append("📒 Trade monitored — your account changed:")
        for s in _activity_lines(activity)[:6]:
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
            for i in ideas[:4]))

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
            f"{c.get('when') or '—'} | {(c.get('reason') or '')[:140]} |")
    return "\n".join(rows) + "\n"


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


def _gameplan_section(context):
    macro = context.get("macro", {})
    pf = context.get("portfolio", {})
    calls = context.get("calls", [])
    hedge = context.get("hedge", {})
    ideas = context.get("ideas", {}).get("equity", [])
    plan = []
    # 1) actionable trades first (sized, placeable)
    for c in calls:
        if c.get("action") in ("SELL", "TRIM", "BUY", "NEW_BUY") and c.get("dollars"):
            amt = f"${c['dollars']:,.0f}"
            lvl = f" @ ${c['entry_zone']:,.2f}" if c.get("entry_zone") else ""
            plan.append(f"{c['action']} {c['ticker']} {amt}{lvl} — {_short_reason(c)}")
    # 2) the standing hedge (always an action the user can place)
    if hedge.get("options") and hedge.get("target_pct", 0) > 0:
        rec = next((o for o in hedge["options"] if o["ticker"] == hedge.get("recommend")),
                   hedge["options"][0])
        px = f" @ ${rec['price']:,.2f}" if rec.get("price") else ""
        plan.append(f"Hedge: hold ~${rec['capital']:,.0f} {rec['ticker']}{px} "
                    f"(~{hedge['target_pct']*100:.0f}% downside cover)")
    # 3) the single most important risk directive
    if pf.get("actions"):
        plan.append(pf["actions"][0])
    # 4) top diversifying idea(s) to watch, on a dip (not a chase)
    divs = [i for i in ideas if i.get("diversifier") and i.get("entry") in ("dip", "near-support")]
    for i in divs[:2]:
        plan.append(f"Watch {i['ticker']} ({i['entry']}, diversifies) for a pullback entry")
    if not plan:
        plan = ["Hold the book, respect stops, no new risk today."]
    return "\n## Game Plan\n\n" + "".join(f"{i+1}. {p}\n" for i, p in enumerate(plan))


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
             context.get("thesis", "") + ("\n" if context.get("thesis") else ""),
             _activity_section(context.get("activity", [])),
             _market_pulse_section(context.get("pulse", {})),
             _hedge_section(context.get("hedge", {})),
             _macro_section(context.get("macro", {})),
             _unwind_section(context.get("unwind", {})),
             _portfolio_section(context.get("portfolio", {}), cash=context.get("cash")),
             _research_focus_section(context.get("research_selected", [])),
             _calls_section(context.get("calls", [])),
             _ideas_section(context.get("ideas", {})),
             _sectors_section(context.get("sectors", {})),
             _gameplan_section(context),
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


def send_imessage(subject, body):
    """Send via the existing notify.py (iMessage → Mail/SMTP fallback)."""
    py = str(conf.ROOT / ".venv" / "bin" / "python")
    try:
        subprocess.run([py, str(BOT_DIR / "notify.py"), subject],
                       input=body, text=True, capture_output=True, timeout=90)
        return True
    except Exception:
        return False


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


def translate_to_zh(text):
    """Translate a markdown report to Simplified Chinese via a headless,
    read-only `claude -p` pass. Best-effort — returns the original English on any
    failure so delivery never breaks. Tickers / numbers / $ are kept verbatim."""
    if not text or not text.strip():
        return text
    prompt = (
        "Translate the following markdown trading report into Simplified Chinese. "
        "Keep every ticker (e.g. NVDA, MU, QQQ), every number, percentage and $ "
        "amount EXACTLY as-is. Preserve the markdown structure — headers, tables, "
        "bullet lists. Translate only the prose, labels and table headers. Output "
        "ONLY the translated markdown, no preamble or sign-off.\n\n----\n" + text
    )
    try:
        r = subprocess.run(["claude", "-p", prompt, "--output-format", "text"],
                           capture_output=True, text=True, timeout=240,
                           cwd=str(conf.ROOT), env=_relay_env())
        out = r.stdout.strip()
        if not out:
            return text
        # A translation that drops or alters a $ amount, price level, or ticker
        # would ship a WRONG directly-placeable instruction. Verify the numeric and
        # ticker tokens survived unchanged; on any mismatch, deliver English.
        if not _tokens_preserved(text, out):
            return text
        return out
    except Exception:
        return text


def _tokens_preserved(src, out):
    """True if every DOLLAR AMOUNT and PERCENTAGE in `src` survives (count-wise) in
    `out`. Guards the translation relay from mangling a placeable figure (a stop,
    a size, a level) — the one failure that actually matters on the phone.

    Deliberately does NOT check bare integers or ALL-CAPS tokens: a faithful
    Chinese translation legitimately renders "top-3"→"前三", "10Y"→"10年期",
    "SELL"→"卖出" etc., and flagging those caused spurious English fallbacks.
    Commas in thousands are normalized so "$9,988" == "$9988"."""
    import re
    def figs(s):
        s = re.sub(r"(?<=\d),(?=\d)", "", s)                      # 9,988 -> 9988
        dollars = re.findall(r"\$\s?(\d+(?:\.\d+)?)", s)          # $ amounts / levels
        pcts = re.findall(r"(\d+(?:\.\d+)?)\s?%", s)              # percentages
        return Counter(dollars), Counter(pcts)
    sd, sp = figs(src)
    od, op = figs(out)
    if any(od.get(k, 0) < v for k, v in sd.items()):
        return False
    if any(op.get(k, 0) < v for k, v in sp.items()):
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
    """Write the English report, then (optionally) text a short decision digest +
    a styled PDF of the full report — translated to the configured delivery
    language. Falls back to chunked text if the PDF can't be rendered/sent.
    Returns (path, digest)."""
    conf.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report = build_report_md(context)
    digest = build_digest(context)
    date = context.get("date", "")
    mode = context.get("mode", "note")
    path = conf.REPORTS_DIR / f"desk_{date}.md"
    path.write_text(report)                      # archive stays English

    out_digest, out_report, suffix = digest, report, ""
    if conf.DELIVER_LANG == "zh":
        out_report = translate_to_zh(report)
        out_digest = translate_to_zh(digest)
        suffix = "_zh"
        try:
            (conf.REPORTS_DIR / f"desk_{date}{suffix}.md").write_text(out_report)
        except Exception:
            pass

    if notify:
        send_imessage(f"{conf.MSG_PREFIX} {mode} {date}", out_digest)         # quick decision digest
        send_report(f"{conf.MSG_PREFIX} report {date}", md_to_text(out_report))  # readable text, tables flattened
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
        return r.stdout.strip() or None
    except Exception:
        return None
