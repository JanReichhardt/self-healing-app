# Verification report — 12 November 2026

Executed on macOS arm64 (Apple Silicon), Eclipse Temurin OpenJDK 21.0.13, Python 3.12.16, Maven, Quarkus 3.40.1, FastAPI 0.162.5, and OpenHands SDK/tools 1.61.0.

| Check | Result |
|---|---|
| Python dependency resolution, including optional agent extra | PASS; exact resolution in uv.lock |
| Python tests (`.venv/bin/python -m pytest -q`) | 9 passed |
| Baseline Maven build | PASS; original valid-coupon test passes |
| Baseline missing-coupon HTTP request | Expected HTTP 500 reproduced |
| Independent contract applied to buggy baseline | Expected FAILURE with NullPointerException; negative control passed |
| Deterministic repair in detached worktree | PASS |
| Clean verification checkout Maven build | PASS; 9 JUnit tests, zero failures/errors |
| Independent live candidate HTTP contract | PASS; 8 cases |
| End-to-end deterministic smoke | PASS, including monitor, pre-approval failure, stale digest and duplicate approval refusal, recovery, metrics, SSE resume, reset |
| Replay end-to-end smoke | PASS; simulated mode, no JVM deployed |
| Approval/artifact tampering/rejection/rollback tests | PASS |
| SDK adapter setup | PASS using real LLM, Agent, FileEditorTool and LocalConversation objects; only `run()` mocked to avoid provider calls |
| Dashboard browser inspection | PASS; live status, controls and event connection render |

The full deterministic smoke was rerun after the monitor event timestamp bug was fixed and after final runtime changes. The external JUnit suite independently rejects the deliberate bug and accepts the repaired source. The unit rollback test injects a post-deployment failure and verifies the previous active service is restored.

No provider credentials were supplied, so **a real OpenHands LLM repair was not executed**. The SDK adapter installation and initialization were verified, not model quality, cost, latency or successful generation of the patch. Replay is an illustrative bundled scenario, not a recording of a live agent execution. Git worktree isolation is not an operating-system sandbox.

First startup still needs network access for dependencies. Maven and Python caches, private worktrees, process logs, local SDK sessions and virtual environments are excluded from the downloadable archive. See README.md for setup, replay, smoke commands, architecture and the 25-minute session script.
