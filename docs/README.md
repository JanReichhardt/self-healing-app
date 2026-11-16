# Self-healing checkout: technical docs

A real Quarkus REST API breaks in production-like conditions. An AI agent reads the Quarkus log, fixes the code, the fix goes through a Git branch, a pull request and a CI pipeline. A human merges the PR, and the fixed service takes over traffic.

## The story in one paragraph

`POST /checkout` crashes with a `NullPointerException` when the optional `coupon` is `null`. The orchestrator notices failing probes, reads the stack trace from the live Quarkus log, and hands it to a coding agent (OpenHands SDK `ACPAgent` driving Claude Code). The agent edits one Java file in an isolated worktree. The orchestrator rebuilds that change in a *separate clean checkout* with trusted tests, pushes a branch, opens a PR, and GitHub Actions re-verifies build and runtime behaviour. Merging the PR is the only manual step. The orchestrator then switches traffic to the tested artifact and calls the real endpoint to prove it works.

## Documents

| File | Read it for |
|---|---|
| [architecture.md](architecture.md) | Components, processes, ports, data flow, file map |
| [flow.md](flow.md) | Step-by-step lifecycle, state machine, event types |
| [safety-and-limits.md](safety-and-limits.md) | Trust boundaries, what is real vs. simulated, known limits |
| [demo-walkthrough.md](demo-walkthrough.md) | Setup, run script, what to say and show, troubleshooting |

Older files: `../README.md` (quick start) and `../VERIFICATION.md` (original verification notes).
