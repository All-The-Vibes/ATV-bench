# ATV-bench

A lightweight harness benchmark for coding agents. Where SWE-bench and CodeClash
measure the **model**, ATV-bench measures the whole **harness** — your skills,
MCP servers, plugins, custom agents, and config — by putting two harnesses
head-to-head in a game arena (Battlesnake, Tron) and producing an ELO score plus
heuristic recommendations for improving the losing harness.

Optimized for GitHub Copilot OAuth (CLI + ADE app), Claude Code, and
bring-your-own-key.

## Status

Design + gating spikes complete. See `spikes/SPIKE_REPORT.md`. Implementation plan
next (`/plan-eng-review`).

## Quick start (dev)

```bash
uv venv && uv pip install pytest
uv run pytest -m "not live"   # fast: contract + decoupling seam
uv run pytest -m live -s      # live: real claude/copilot CLIs
```

## Credits & license

Built on [CodeClash](https://github.com/CodeClash-ai/CodeClash) (MIT) — arenas,
Docker match engine, ELO/viewer. Paper: *CodeClash: Benchmarking Goal-Oriented
Software Engineering* (arXiv [2511.00839](https://arxiv.org/abs/2511.00839)),
John Yang, Kilian Lieret, et al. See `NOTICE`.

ATV-bench is MIT licensed.
