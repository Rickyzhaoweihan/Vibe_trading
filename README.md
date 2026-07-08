# Vibe Trading

An autonomous + advisory trading system built on the [Claude](https://claude.com/claude-code)
agent stack and the `robinhood-trading` MCP server.

Two subsystems (see [CLAUDE.md](CLAUDE.md) for the full architecture):

- **`bot/`** — a four-tier / two-sleeve autonomous trader (regime → policies → LLM
  router → guardrails → reconcile). The LLM never authors orders; a pure,
  fully-unit-tested `guardrails.py` is the sole order author.
- **`bot/desk/`** — an **advisory-only** "AI trading desk": a top-down stack
  (macro → sectors → unwind-risk → portfolio → idea scout → deep research →
  synthesis) that reads your real book and messages you KEEP/BUY/TRIM/SELL calls
  with a sized, placeable game plan and a standing downside hedge. It has no order
  tools and never trades.

Deep per-ticker research uses a vendored copy of
[TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents).

## Setup

```bash
python3.10 -m venv .venv && .venv/bin/pip install -r requirements.txt   # or per TradingAgents/pyproject.toml
git clone https://github.com/TauricResearch/TradingAgents   # vendored dep, not committed here
cp .env.example .env                                        # then fill in your keys + account IDs
```

Account IDs, API keys, and alert contact details are read from `.env` (gitignored)
— nothing sensitive lives in source. Your live book, orders, logs, and reports are
also gitignored.

## Tests

```bash
.venv/bin/python -m unittest discover -s bot/tests -p 'test_*.py'
```

## Disclaimer

For personal, educational use. Not investment advice. Trading involves risk of
loss; leveraged ETFs (e.g. -3x inverse) can lose value rapidly. Use at your own risk.
