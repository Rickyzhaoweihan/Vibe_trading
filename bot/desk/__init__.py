"""The AI trading desk — a professional, ADVISORY-ONLY analysis layer.

Top-down like a real desk (macro → sectors → ideas → single-name → portfolio →
synthesis → memory). It NEVER places an order: it has no order tools and is
fully decoupled from the auto-trader's execution path (guardrails/state/PDT).
It only produces a thesis + game plan + clear KEEP/BUY/TRIM/SELL calls and
messages them to the user, who executes.

Modules are run as flat scripts (e.g. `python bot/desk/macro.py`); each adds the
desk dir and the parent bot dir to sys.path so it can import sibling desk
modules (conf, sectors, ...) and the reused bot modules (regime, signals, ...).
"""
