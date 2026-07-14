# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Three parts:

- **`bot/`** — an autonomous trading bot that **executes** trades on the user's Robinhood **Agentic** account (`<BOT_ACCOUNT>`) via the `robinhood-trading` MCP server. The four-tier/two-sleeve system below.
- **`bot/desk/`** — the **advisory-only** "AI trading desk": a professional, top-down analysis layer (macro → sectors → ideas → single-name → portfolio → synthesis → memory) over the user's **real book** (margin account `<DESK_ACCOUNT>`). It **READS** that account and **never places an order** — it produces a thesis + game plan + KEEP/BUY/TRIM/SELL calls and messages them to the user, who executes manually. Fully decoupled from the auto-trader's execution path (no guardrails/state/PDT coupling). See its own section below.
- **`TradingAgents/`** — a vendored copy of the open-source [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) multi-agent LLM framework (v0.2.5). Both `bot/` (Tier 3) and `bot/desk/` (L4 research) import it as a library to produce per-ticker research ratings. Generally treat it as a dependency, not something to modify.

Dependencies live in `.venv/` (Python 3.10; install via `pip install -r requirements.txt`, which mirrors `TradingAgents/pyproject.toml` + extras). The TradingAgents repo is **not** pip-installed — modules put it on `sys.path` via `sys.path.insert(0, ROOT/"TradingAgents")` while deps come from `.venv`. TradingAgents is a vendored, unmodified Apache-2.0 copy (provenance: `TradingAgents/VENDORED.md`); the rest of the repo is MIT. Account IDs / alert contacts are env-driven from `.env` (`BOT_ACCOUNT`, `DESK_ACCOUNT`, `ALERT_EMAIL`, `ALERT_IMESSAGE`) — never hardcode them back into source.

## Commands

```bash
# Run all unit tests (pure Python, no network). Stdlib unittest; pytest is NOT in .venv.
# 294 tests: auto-trader (guardrails, regime, policies, brain/reflect, PDT, calendar, TA)
# + the desk layer (test_desk_*.py: macro, sectors, scout, research, portfolio, monitor, health, snapshot, strategist, news).
.venv/bin/python -m unittest discover -s bot/tests -p 'test_*.py'
.venv/bin/python bot/tests/test_guardrails_v2.py        # a single file (each is standalone)

# TradingAgents framework tests are pytest-based (see TradingAgents/pyproject.toml),
# but pytest isn't in .venv — install it first if you need to run them:
#   .venv/bin/pip install pytest
cd TradingAgents && ../.venv/bin/python -m pytest tests/ -v

# Run the weekly CORE research pass by hand (no trading; writes core intents to decisions.json)
.venv/bin/python bot/analyze.py --out /tmp/decisions.json --tickers PLTR NVDA

# Reflection / self-tuning, dry-run (prints proposed policy changes, writes nothing trade-affecting)
.venv/bin/python bot/reflect.py            # add --apply to actually write policies.json
```

### The advisory desk (`bot/desk/`) — never trades, safe to run anytime

```bash
# Full desk note + game plan (the primary daily deliverable). --llm adds a read-only
# claude -p enrichment pass; --no-research skips Tier-3 deep research; --no-notify won't message.
.venv/bin/python bot/desk/desk.py --mode preopen [--llm] [--no-research] [--no-notify]
.venv/bin/python bot/desk/desk.py --mode wrap          # after-close note + accountability review
.venv/bin/python bot/desk/desk.py --mode weekly --top 8 [--llm]   # deeper Sunday pass
.venv/bin/python bot/desk/desk.py --mode bootstrap     # initial full pass: deep-research ALL holdings

# Intraday alert monitor (watches plan_<date>.json for entry/stop hits + macro/unwind shifts)
.venv/bin/python bot/desk/monitor.py --once --dry      # one tick, print triggers, send nothing
.venv/bin/python bot/desk/monitor.py [--llm]           # continuous loop, self-terminates near close

# Individual layers (each module is runnable standalone for inspection)
.venv/bin/python bot/desk/macro.py                     # macro regime + net-exposure target
.venv/bin/python bot/desk/sectors.py                   # sector ranks + momentum-unwind score (0-100)
.venv/bin/python bot/desk/portfolio.py                 # real-book concentration / crowding analytics
.venv/bin/python bot/desk/scout.py --top 8             # screen SCOUT_POOL for new ideas
.venv/bin/python bot/desk/research.py --tickers NVDA TSM --date 2026-06-25
.venv/bin/python bot/desk/news.py --holdings NVDA MU   # keyless news/event digest (feeds the strategist)
.venv/bin/python bot/desk/strategist.py --date 2026-06-25  # dry: print the strategist's JSON (one OpenRouter call)
.venv/bin/python bot/desk/journal.py                   # price open calls → hit-rate review
.venv/bin/python bot/desk/health.py --watchdog         # alert if a scheduled desk mode went stale
```

### Scheduling (launchd)

Templates in `bot/launchd/templates/*.plist.template` (`{{ROOT}}`/`{{PREFIX}}` placeholders); `bot/launchd/install_schedule.sh` renders + loads them into `~/Library/LaunchAgents/` (desk jobs by default; `--with-autotrader` opt-in; `--uninstall` removes only its own). The owner's live jobs run under the legacy `com.rickyhan.*` labels — installed copies are separate files, so repo changes don't affect them. `pmset` wakes the machine before the open.

| Label (`com.rickyhan.tradingbot.*`) | Script | When (ET) |
|---|---|---|
| `daemon` | `run_daemon.sh` → `intraday.py` | 09:25 daily (runs the live session loop) |
| `core` | `run_core.sh` | Mondays 11:00 (weekly deep research) |
| `morning` / `afternoon` | `run.sh` | 09:00 / 14:45 (legacy twice-daily pipeline) |
| `reflect` | `reflect.py` | 18:00 daily (self-tuning, log-only until enabled) |

The advisory desk has its own plists (`com.rickyhan.desk.*` → `desk/run_desk.sh <mode>`):

| Label (`com.rickyhan.desk.*`) | Mode | When (ET) |
|---|---|---|
| `preopen` | `desk.py --mode preopen --llm` | ~08:00 (full note + game plan + `plan_<date>.json`) |
| `monitor` | `monitor.py --llm` | ~09:25 (sleeps to open, then intraday alert loop) |
| `wrap` | `desk.py --mode wrap` | ~16:30 (after-close refresh + accountability) |
| `weekly` | `desk.py --mode weekly --top 8` | Sundays (deeper pass; not gated on a trading day) |
| `watchdog` | `health.py --watchdog` | morning (alert if yesterday's preopen never completed) |

## Architecture — a four-tier, two-sleeve system

The bot is organized as **cost-ascending tiers** (most ticks are free; LLM tokens are spent only when needed) feeding **two sleeves** of capital.

**Tiers** (cost rises as you go down):
- **Tier 0 — `regime.py`** ($0): pure-Python indicators + a market-regime classifier (`RISK_ON_TREND` / `NEUTRAL` / `HIGH_VOL_CHOP` / `RISK_OFF_TREND`). `fetch_market` (yfinance) is the only networked part; the compute functions take plain price lists and are fully unit-tested offline.
- **Tier 1 — `policies.py`** ($0): a library of **pure** strategy functions (`sma200_trend`, `rsi2_meanrev`, `dual_momentum`) mapping `(regime, market, positions, params)` → a list of trade **intents**. No LLM, no network, no state. `POLICIES` is the registry; `evaluate(routing, …)` runs the active ones. Policies decide *what* and *how convinced* (`target_frac`); guardrails decides *how much is actually allowed*.
- **Tier 2 — `brain.py`** (one cheap LLM call): given regime + each policy's recent performance + day-trade budget + positions, it picks which policies are **active**, their **weights**, and a global **aggressiveness** multiplier. It **routes between proven policies — it never names a ticker and never authors an order.** `default_route` is the deterministic regime→policy fallback and safety floor; the LLM only refines it.
- **Tier 3 — `deepresearch.py`** (TradingAgents multi-agent stack, expensive): two callers — the weekly CORE researcher (`analyze.py`) on a few names, and the daemon's deep-**CONFIRM** gate that researches a leveraged ETF's *underlying* index to veto obviously-bad aggressive entries. Best-effort: failure returns a neutral result so it never blocks trading.

**Sleeves** (`sleeves.py`) — the 80/20 capital split, capped independently by guardrails:
- **`aggressive`** (80%): leveraged-ETF rotation (`TQQQ/SOXL/TECL/UPRO/FNGU` + inverse pairs). Owned by the **intraday daemon**. Hold-classes `swing_lev` / `daytrade_lev` (0-day min hold, tight stops).
- **`core`** (20%): deep-researched long-term book from the weekly run. Hold-class `core` (12% max pos, 5-day hold, 15% stop).

### Entry points (orchestrators)

All three end in the **same trust boundary**: write `decisions.json`/intents → headless `claude -p` relay → `guardrails.py` authors orders → `reconcile.py` reconciles.

- **`run_daemon.sh` → `intraday.py`** — the live session. Launched at the open, self-terminates near the close. Each tick recomputes regime (Tier 0) + intents (Tier 1) for free; re-routes via the brain (Tier 2) only on an interval or regime change; spends tokens on the execution relay **only when a tick produces orders**. Tunables via `DAEMON_*` / `CONFIRM_ENTRIES` env vars.
- **`run_core.sh`** — weekly Tier-3 research over the core watchlist → core-sleeve intents → placed during market hours. Holds the shared `bot/.lock` so the daemon defers placements meanwhile.
- **`run.sh {morning|afternoon}`** — the legacy twice-daily pipeline (gates → `analyze.py` → relay → `reconcile.py`). Note: `analyze.py` is now the *core researcher* and no longer accepts `--slot`; treat the daemon + core + reflect as the live architecture.

### The trust boundary (most important invariant)

The LLM is **never** trusted to decide trades — not the executor, not the brain. Authority is split:

- **`guardrails.py` is the ONLY component allowed to author orders.** Pure, fully-unit-tested. Two entry points: `validate(snap, decisions, state)` (legacy rating→order path) and `validate_intents(snap, decisions, state)` (the policy/sleeve path). All sizing, vol-targeting, risk caps, sleeve caps, and account selection live here.
- **`brain.py` routes but never names tickers or sizes orders.** It only refines `default_route`.
- The executor prompt (`bot/prompts/execute_trades.md`) instructs the model to copy `orders.json` payloads **verbatim** and place nothing else. The tool allowlist (set per-run in the shell scripts) is the only thing granting trade ability.
- **`reconcile.py` is the single writer of `state.json`** and the watchdog: if `get_equity_orders` shows any order guardrails didn't author (and isn't an existing position), it writes `bot/logs/ALERT`, which blocks all future runs until a human clears it.
- **`reflect.py` is the SOLE writer of `policies.json`** (kept separate from `state.json` so no file has two writers).

When changing trading logic, change `guardrails.py`/`policies.py` and their tests — not the prompt or the brain. The prompt stays a mechanical relay; the brain stays a router.

### Guardrail rules (in `guardrails.py`)

Legacy `validate` path (`RULES` + `TARGET_FRAC`): agentic account only · max 35% equity/position · min 5-day hold (stop-loss overrides) · max 4 trades/day · ≥$10 cash buffer · min $5 order · no leverage · market orders, regular hours · 1-day re-entry cooldown after a sell · `Overweight` skipped unless cash ≥ 40% equity · sizing `Buy`→35%, `Overweight`→20%.

Intent path `validate_intents` (`CLASS` + `INTENT_RULES`): per **hold-class** caps replace the blanket rule (`core` 12%/5d/15% · `swing_lev` 30%/0d/6% · `daytrade_lev` 25%/0d/3%) · sleeve caps via `sleeves.py` · gross leveraged exposure ≤ 80% · vol-targeting (annualized target 0.50, scale clamp 0.30–1.50) · binding day-trade cap comes from `account_type.py` (PDT/settlement), not the trade-count rule.

**`account_type.py` fails closed**: it cannot read from code whether `<BOT_ACCOUNT>` is CASH or MARGIN, so anything it can't positively confirm as margin-with-day-trading is treated as **cash, zero same-day round trips** (avoids PDT 90-day lockout and good-faith violations).

Trailing stops are ratcheted in `reconcile.py` (12% below latest price, never lowered).

## The advisory desk (`bot/desk/`) — a top-down stack fronted by a strategist agent

A **completely separate subsystem** from the auto-trader. It analyzes the user's *real* book (margin account `<DESK_ACCOUNT>`, hardcoded in `conf.py`) and emits advice; it has **no order tools** and cannot touch the auto-trader's `guardrails.py`/`state.json`/PDT path. The subprocesses it may spawn are **read-only `claude -p` relays** (snapshot + optional `--llm` enrichment) and a **plain OpenRouter chat call** for the strategist + zh translation — never to place a trade.

**The cost/decision model (important):** deterministic layers L1–L3/L5 run every time (free); expensive Tier-3 deep research (L4) is now **weekly-sweep + on-demand only**; and a cheap **strategist agent (L4.5)** is the weekday brain that authors the tentative actions and decides the rare deep-research escalations. On a calm weekday the desk spends **zero** deep-research calls — just one ~$0.01 strategist call.

`desk.py::run()` is the orchestrator; it runs the layers in this order (note: not the docstring's nominal order — portfolio runs before scout/research because they need the held set):

- **L1 `macro.py`** — cross-asset panel (`MACRO_PANEL`: rates/dollar/gold/oil/VIX/BTC + global indices/futures, all yfinance) → reuses the auto-trader's `regime.py` classifier → net-equity-exposure target (`EXPOSURE_BY_REGIME`) + hedge menu.
- **L2 `sectors.py`** — sector-ETF leaders/laggards **and** a 0–100 **momentum-unwind risk score** (`UNWIND` thresholds: MTUM-vs-SPY fade, leader RSI/SMA extension, crowding correlation, VIX). The unwind read is the desk's signature defensive signal.
- **L5 `portfolio.py`** — prices the real book, computes cluster concentration / crowding (`CLUSTERS`, `MOMENTUM_CLUSTERS`), and sizes defensive actions.
- **L3 `scout.py`** — screens `SCOUT_POOL` (∪ `universe.json`) for ideas not already held, ranked by excess return + trend.
- **L4 `research.py`** — Tier-3 TradingAgents deep research (the dominant cost). It runs **weekly-sweep + on-demand only**: `weekly`/`bootstrap` refresh the whole book, but on a **weekday `preopen`/`wrap` NO deep research runs by default** — every holding **carries its last verdict forward** from `coverage.json` (`{ticker: {date, verdict}}`), and the only weekday deep calls are the ones the **strategist (L4.5) escalates** (hard-capped globally per day) plus the conditional SQQQ/QQQ hedge confirm. The old per-name heuristic selection (`research_priority` / `select_for_research` / `min_score` / `max_daily` / `reserve_ideas`) is now used **only by the weekly full sweep**. Maps the 5-tier rating → KEEP/BUY/NEW_BUY/TRIM/SELL and attaches `conviction` / `horizon` / parsed `stop_loss` / `target`. **LLM provider is env-driven** (`BOT_LLM_PROVIDER` / `BOT_DEEP_LLM` / `BOT_QUICK_LLM` in `.env`; default Anthropic Opus/Sonnet, currently DeepSeek V4 Pro via OpenRouter).
- **L4.5 `strategist.py` — the desk's brain (this is where weekday decisions are made).** One cheap OpenRouter/DeepSeek chat call per run (`synthesize.openrouter_chat`, ~$0.01) that reads the deterministic analysis + the live book (cash/positions) + recent trades (`activity`) + a prior-calls `review` + its own persistent **memory** (`logs/strategist_memory.md`) + a **news digest** (`news.py`, below), and returns structured JSON: a narrative (Market Pulse/Thesis/What-I-Expect/Priorities → becomes `context["thesis"]`), **tentative ACTIONS** (buy SQQQ / trim NVDA / raise cash / buy ARM on a dip — flexible + event-driven), an **escalate** list (the usually-empty set of names that truly earn on-demand deep research, capped by `escalate_cap` via `strategist_state.json`, a GLOBAL daily counter shared with the intraday monitor), and a `memory_update`. **Trust boundary preserved:** the strategist NAMES actions; deterministic code SIZES/GUARDS them — `actions_to_calls` discards its size hints and maps to the pipeline `calls` shape, then `plan_trades` (cash-capped) + `audit_feasibility` + `_dollars_within` own feasibility; hedge tickers still route to the hedge engine. It has NO order tools. Runs only when `conf.STRATEGIST["enabled"]` and `research`; on any failure the desk falls back to carried-verdict calls + the read-only `--llm` claude relay. News comes from `news.py` — a keyless digest built from the **same TradingAgents tools the deep-research news analyst uses** (`get_global_news` / `get_news` via `signals.macro_digest` / `signals.ticker_news`) + StockTwits sentiment + yfinance earnings, ordered high-signal-first (event-watch/earnings/sentiment/macro/per-name); **no web plugin** (DeepSeek gets the news in-prompt).
- **L6 `synthesize.py`** — deterministic backbone builds the iMessage digest (≤1800 chars, decision-first, with per-call entry/stop/target/conviction detail) + full markdown report; optional `--llm` relay (`prompts/desk_note.md`) writes an account-grounded `## Thesis` / `## What I Expect` (scenario+probability predictive read) / `## Game Plan`. Writes `reports/desk_<date>.md` (+ `_zh.md` translation when `DESK_LANG=zh`; code default is `en`). **Delivery i18n (`deliver()`):** the English report is always archived; when `DELIVER_LANG=zh` it is `translate_to_zh()` → `supervise_zh()` before texting. Translation is CHUNKED by section (parallel OpenRouter chat calls via `OPENROUTER_API_KEY`, model from `BOT_QUICK_LLM`, default `deepseek/deepseek-v4-pro`) so no chunk truncates; a chunk/section is accepted only if it preserves every `$`/`%`/level (figures locked) and comes back *more* Chinese than before — a failed chunk keeps its English so delivery never breaks. The request sets `reasoning:{enabled:false}` — a reasoning model (the previous GLM-5.2 default) otherwise burns the whole `max_tokens` budget on chain-of-thought and returns `content=null`, silently failing every chunk back to English. `supervise_zh()` is a final QA re-read that only re-touches sections still containing English prose (the owner's "fully-Chinese guarantee"). This is separate from the read-only `--llm` claude enrichment relay.
- **L7 `journal.py`** — appends every actionable call to `logs/desk_journal.jsonl`; `review_outcomes()` re-prices open calls for hit-rate accountability.

`monitor.py` is the cheap intraday loop (600s cadence, self-terminates near close): it fires **new** alerts only (dedup via `desk/state.json`) when a per-name move ≥4%, a macro shift (10Y +8bp / VIX≥22 / BTC≥6%), an unwind-band crossing, or — most importantly — a price hitting an entry/stop level from today's `plan_<date>.json` (the "good time to trade now" alert). With `--strategist` (on in the scheduled `run_desk.sh monitor`), a **book-wide** macro/unwind trigger (`kind in rates/vix/unwind/btc`, not per-name noise) also wakes the strategist for a tentative-action line appended to the alert (`_intraday_strategist`, deduped per day; escalations defer to the next scheduled run so the tick never blocks on deep research).

### Desk files & invariants

- **`desk/conf.py`** — all desk config as pure data (holdings, universes, panels, clusters, thresholds). No network, no heavy imports, so every desk module and test imports it freely. Account `<DESK_ACCOUNT>` and `MSG_PREFIX="Desk"` live here.
- **`desk/positions.json`** — the live book, refreshed before every run by the read-only snapshot relay (`desk/snapshot.py` → `prompts/desk_snapshot.md`; positions/portfolio MCP reads + Write only, **no order tools**). The prior snapshot rotates to **`desk/positions_prev.json`**; `portfolio.diff_positions()` diffs the two so the desk reports trades you made ("trade monitored") and re-analyzes the live book. `health.check_positions()` flags a missing/seed-fallback/undated/>4-day-stale book as a **critical** warning (top of the digest + not-ok heartbeat) so the desk never silently analyzes the wrong book. Intraday, `monitor.py` re-snapshots every `DESK_SNAP_EVERY` ticks (default 3) to catch mid-session trades.
- **`desk/plan_<date>.json`** — today's machine-readable calls + entry/stop levels; written by `desk.py`, watched by `monitor.py`.
- **`desk/coverage.json`** — `{ticker: {date, verdict}}` for the deep-research rotation: each daily pass researches names clearing `conf.RESEARCH["min_score"]` up to `max_daily` (both env-overridable: `DESK_RESEARCH_MIN_SCORE` / `DESK_RESEARCH_MAX`), while `DESK_RESERVE_IDEAS` reserves slots for the top freshly-scouted ideas so new names aren't crowded out by the held book.
- **`desk/state.json`** — **desk-only** alert-dedup state; do **not** confuse with the auto-trader's `bot/state.json`.
- **`logs/strategist_memory.md`** — the strategist's rolling, bounded (~8KB) memory: `## THESES` / `## LESSONS` (outcome-reconciled against the journal) / `## WATCHLIST` / `## OPEN_TENTATIVE_ACTIONS`. Rewritten wholesale each run — deliberately **separate** from the 1.1MB append-only `logs/decision_memory.md` (which is deep-research-only). **`desk/strategist_state.json`** — strategist run-state + the **GLOBAL daily deep-research escalation counter** (shared by preopen + intraday); a separate file from `desk/state.json`.
- **`logs/desk_journal.jsonl`** / **`logs/desk_heartbeat.json`** — call ledger / per-mode heartbeat for the watchdog.
- **Hedge engine** — the desk's standing downside hedge (owner preference). `portfolio.hedge_plan()` sizes an inverse-ETF hedge (`conf.HEDGE_INSTRUMENTS` / `HEDGES` / `HEDGE_TICKERS`, e.g. SQQQ/PSQ/SH) off equity × regime × unwind-band × net-exposure gap, base fraction `DESK_BASE_HEDGE` (0.05); `synthesize._hedge_section()` renders it and is the **single source of truth** for hedge names. `desk.py` (~L248) forces any *held* hedge instrument to `KEEP` so a per-name rating can't contradict the hedge section (the "sell PSQ vs hold PSQ" bug). `monitor.py::held_leveraged_hedges()` / `hedge_triggers()` manage a held -3x hedge's stop/reversal on the fast `HEDGE_MONITOR` interval (`DESK_HEDGE_TICK`, per-minute when `DESK_HEDGE_REALTIME=1`).
- Each module runs as a flat script and self-bootstraps `sys.path` (desk dir + parent `bot/` dir) so it can import sibling desk modules and reuse auto-trader modules (`regime`, `signals`, `notify`).
- Env: `DESK_LANG`/`DELIVER_LANG` (`en` default; `zh` → translate+supervise report before sending — the owner sets `zh` in `.env`) · `DESK_SNAP_EVERY` (intraday re-snapshot cadence in monitor ticks; `0` disables) · research rotation `DESK_RESEARCH_MIN_SCORE` / `DESK_RESEARCH_MAX` / `DESK_RESERVE_IDEAS` · hedge `DESK_BASE_HEDGE` / `DESK_HEDGE_REALTIME` / `DESK_HEDGE_TICK` · `DESK_DEFAULT_STOP_PCT` (0.12) · `DESK_MCP_QUOTES` / `DESK_SCOUT_DISCOVER` · strategist `DESK_STRATEGIST` / `DESK_STRATEGIST_INTRADAY` / `DESK_ESCALATE_CAP` (default 2) / `DESK_STRATEGIST_MAXTOK` · the strategist + zh translation need `OPENROUTER_API_KEY` (model `BOT_QUICK_LLM`, default `deepseek/deepseek-v4-pro`).

## Key files & data flow

- **`state.json`** (single writer: `reconcile.py`) — `positions` (entry_date, dollar_in, stop_loss, last_rating, **sleeve** tag), `cooldowns`, `daily_trades`, `baseline` (SPY-alpha tracking), `sleeves` (per-sleeve target overrides). Authoritative bot memory.
- **`policies.json`** (single writer: `reflect.py`) — per-policy `enabled` / `weight` / `params` / `trailing` stats / `change_log`. `reflect.default_policies_cfg()` is the schema.
- **`runs/<TS>/`** — per-run artifacts: `decisions.json` (intents/ratings) → `snapshot.json` (executor) → `orders.json` (guardrails) → `execution_result.json` (executor) → consumed by reconcile.
- **`logs/ALERT`** — presence halts ALL trading. Clearing it is a deliberate human action.
- **`logs/trades.jsonl`** (policy_id-tagged, read by reflect for P&L attribution), **`logs/decision_memory.md`** (TradingAgents decision memory, reflect attaches outcomes), **`reports/<date>.md`** (daily human report w/ SPY alpha).
- **`trading_calendar.py`** — NYSE 2026 holidays/half-days hardcoded; exit-code CLI used as shell gates (`--check-today`, `--too-late`, `--sleep-until-open`). Also `within_exec_window`, `is_trading_day`.
- **`signals.py`** — cheap (non-multi-agent) news/sentiment digests from TradingAgents data tools for brain routing; best-effort, returns "" on any failure.
- **`notify.py`** — alert channels in order: macOS notification → iMessage (`<ALERT_IMESSAGE>`, primary) → Mail.app → SMTP (`ALERT_GMAIL_USER` / `ALERT_GMAIL_APP_PASSWORD`). Triggered on analysis failure, broker rejection, ALERT.
- **`universe.json`** — core watchlist (also AI-focus fallback list).

## PAPER vs LIVE

Controlled by `PAPER_MODE` in `.env` (`1`=paper, `0`=live). Enforced **structurally**, not just by a flag: every orchestrator selects `execute_paper.md` and an allowlist with **no order-placement tools** in paper mode, and `guardrails.py`/`reconcile.py` branch on `paper`. The system is currently **LIVE** (`PAPER_MODE=0`). Be careful: manual runs of `run.sh`/`run_core.sh`/`intraday.py` place real orders unless `PAPER_MODE=1` is set in the environment.

## TradingAgents usage notes

The bot calls `TradingAgentsGraph(selected_analysts=["market","news","fundamentals"], config=cfg).propagate(ticker, date)` and reads `final_state["final_trade_decision"]` (parsed via `tradingagents.agents.utils.rating.parse_rating` into the 5-tier scale: Buy/Overweight/Hold/Underweight/Sell) and `trader_investment_plan` (stop-loss parsed by regex). All access goes through `deepresearch.py` (graphs cached per config, results per `(ticker, date, profile)`). Bot config: `llm_provider=anthropic`, `deep_think_llm=claude-opus-4-8`, `quick_think_llm=claude-sonnet-4-6`, 1 debate / 1 risk round. The brain/reflect Anthropic model is overridable via `BRAIN_MODEL`.

The framework is independently configurable via `TRADINGAGENTS_*` env vars (see `tradingagents/default_config.py`) and has its own Typer CLI (`cli/main.py`) and `main.py` example — neither is used by the bot.
