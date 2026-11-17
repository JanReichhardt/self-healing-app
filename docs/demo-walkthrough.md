# Demo walkthrough

## Session plan (25 minutes)

| Time | Activity | Where |
|---|---|---|
| 0–4 min | Opening talk: what self-healing software is, and what this demo does and does not claim | Slides or talk, see "Opening talk" below |
| 4–7 min | Show the healthy dashboard; readiness stays green while only the business probe catches faults; architecture and the human gate | Dashboard, steps 1 |
| 7–10 min | **Inject fault**; watch the error in the service log open an incident | Steps 2–3 |
| 10–14 min | Triage card (root cause, `FIXABLE`), then the agent's fix, allowlist and separate verification | Steps 4–5 |
| 14–19 min | Pull request, CI, review and merge as the approval; deploy and live check | Steps 6–8 |
| 19–23 min | Reset; discuss rejection, rollback, trust boundaries and how this maps to production (Sentry + Seer, your own CI/CD) | Step 9, "Points worth stressing" |
| 23–25 min | Questions. If model latency threatens timing, switch to the replay fallback | `AGENT_MODE=replay` |

Preflight: run the deterministic smoke test once to warm dependencies, run one real `openhands` cycle if you present AI, reset, and keep a second terminal ready for `AGENT_MODE=replay`. Model output and latency cannot be guaranteed.

## Opening talk: self-healing software (3–4 minutes)

Say it in this order.

1. **The idea.** Self-healing software notices that it is broken, works out why, fixes itself and confirms the fix, with little or no human work in between.
2. **Two levels.**
   - *Infrastructure healing* is old and common: restart a crashed process, reschedule a pod, roll back a bad release, scale out. It treats the symptom and needs no understanding of the code.
   - *Code-level healing* is new: the bug is in the source, so the system must read the error, find the cause, change the code and ship it. Coding agents make this possible now.
3. **The loop.** Detect, triage, fix, verify, approve, deploy, observe. Every tool in this space, from Sentry with its AI debugger (Seer) to home-built pipelines, is a variation of this loop. The demo shows each stage.
4. **Why a green health check is not enough.** A readiness endpoint says the process is up. It cannot see a business failure such as checkout crashing when the optional coupon is missing. Detection has to look at errors and real behaviour.
5. **Where the risk is.** The hard part is not generating a fix, it is trusting it:
   - the agent can be wrong or be steered by hostile text in logs (prompt injection)
   - a fix is only as safe as the tests that check it
   - an unreviewed deploy is a new way to break production
6. **The design answer, which is what you will show.** The agent is treated as untrusted. It works in an isolated checkout, may only change allowlisted files, and is judged by tests it did not write, in a separate checkout. A human reviews and merges a normal pull request, and CI re-checks it. The merge is the approval, and a failed deploy rolls back.
7. **What is real in the demo.** A real Quarkus service, real errors, a real coding agent in `openhands` mode, real branch, PR and CI. The deploy step is a local traffic switch. In production your own CD pipeline, canary and rollback do that job, and an observability tool such as Sentry does the detection.
8. **What it does not claim.** It is one planted bug, not general reliability. The deterministic and replay modes are labelled, they are not AI runs and say nothing about model quality.

Then say what the audience will watch for: a fault, an incident, a diagnosis, a patch, a pull request, a merge and a recovery.


## Prerequisites

- Java 27 (`java -version` shows 27), Maven, Python 3.12 with `uv`, Node (`npx`), `gh` CLI logged in (`gh auth status`).
- For the AI agent: `claude login` done once (your Claude subscription). No API key.
- `main` on GitHub contains `.github/workflows/ci.yml` and the **buggy** `CheckoutResource.java`.
- Your checkout is on `main` (`git branch --show-current`) and your changes are committed and pushed.

## Start

```bash
uv sync --frozen --python 3.12 --extra dev --extra agent
export AGENT_MODE=openhands     # or deterministic (no AI)
export ACP_MODEL=sonnet         # optional
./scripts/start.sh
```
Open `http://127.0.0.1:8000`. Wait for status **healthy** (first Maven build takes a minute).

Variants: `AGENT_MODE=deterministic` (no AI, same pipeline), `AGENT_MODE=replay` (no Java, simulated), `PUBLISH_FIX=0` (no push/PR, approve in the dashboard).

## External alert trigger

Instead of the log tail, an alerting tool can open the incident: `POST /api/alert` with `{"excerpt": "<stack trace>"}` and the `x-demo-token` header (from `GET /api/session`). The excerpt becomes the agent's only context. A real Sentry/Datadog webhook needs a small adapter that maps its payload to `excerpt`. The agent's live output appears in the dashboard's "Agent activity" card.

## Target configuration

Everything specific to the application under repair lives in `target.json` (the Quarkus demo): app directory, build and run commands (`{state}`, `{port}` placeholders; the app also gets `PORT`), readiness path, release directory for the digest, error marker in the log, the failing and normal requests, the HTTP contract cases, trusted test files, and the path/suffix allowlist for the agent. Point `TARGET_CONFIG` at another file to repair another service, for example the Python one:

```bash
TARGET_CONFIG=examples/python-service/target.json ./scripts/start.sh
```

The optional `fixture` plants the bug into the baseline and feeds the deterministic adapter. Without it the target must already contain the bug, and only `openhands` mode can repair it. The target directory must live inside this repository, because the PR is created from it.

## Live script (about 5 minutes)

| Step | You do | What the audience sees | What to explain |
|---|---|---|---|
| 1 | Point at the dashboard | Status healthy, probe PASS, Quarkus log scrolling with `POST /checkout ... 200` | Real Quarkus service, real access log. Health endpoint is green and would stay green. |
| 2 | Click **Inject fault** | Probes turn FAIL; log shows `ERROR ... NullPointerException ... CheckoutResource.java` | Optional field dereferenced without a null check. Readiness checks cannot see this class of bug, only the business probe does. |
| 3 | Wait | "ERROR in the Quarkus log; incident opened", then a `diagnosis` event | The trigger is the log, not the probe. The engine extracts the stack trace. That excerpt is the agent's only description of the problem; the agent has no knowledge of coupons. |
| 4 | Wait | Status triaging: the "Triage: root cause" card fills in, then status repairing | Two agent phases in an isolated Git worktree. Triage is read-only: it names the root cause and says `FIXABLE: yes/no` before any code changes. If no, the incident stops there. The agent prompt names no language or framework. |
| 5 | Wait | Status verifying, then the patch appears in "Proposed source change" | Allowlist check, then a **separate** clean checkout with trusted tests and an HTTP contract. The agent's tree is never executed. |
| 6 | Show the PR link in Service control | PR link, CI `running` then `passed` | A real branch and commit were pushed, and CI re-verifies build and runtime behaviour. |
| 7 | Open the PR on GitHub, review the diff, **merge** | Dashboard: "Pull request merged; deploying", status resolved | The merge is the approval. The engine checks the artifact digest, runs the contract, switches traffic, rolls back on failure. |
| 8 | Point at the green line in Service control | "POST /checkout with coupon=null on the fixed service: HTTP 200 ..." and a `200` access-log line | Same request that crashed earlier now succeeds on the real endpoint. |
| 9 | Click **Reset** | Back to healthy | Ready for the next run. Restore the bug on `main` first if you want another real PR. |

## Points worth stressing to other developers

- The agent is **not trusted**: one-file allowlist, separate verification checkout, controller-owned tests, digest binding.
- Two independent gates (local verification, GitHub CI) before a human decision.
- The human step is a normal PR review, so existing review and branch-protection practice applies.
- Deployment here is a local traffic switch. In production this would be your CD pipeline triggered by the merge.
- Show the logs: `.state/events.jsonl` (everything that happened), `.state/app-<port>.log` (Quarkus), `.state/agent-<incident>.log` (agent), `.state/commands.log` (git/maven commands).

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `Missing session token` / 403 on buttons | Stale tab after a server restart. Hard-reload (Cmd+Shift+R). Same-origin browser requests no longer need the token. |
| Repair fails with `Command failed (1); see .state/agent-<id>.log` | Read that file. Common: Node/`npx` missing, not logged in to Claude (`claude login`), or old code asking for `LLM_MODEL`. |
| "No checkout exception found in the Quarkus log" | The error block did not mention `CheckoutResource`. Check `.state/app-<port>.log`. |
| PR created but CI never runs | `.github/workflows/ci.yml` is not on `main` on GitHub. |
| PR "no changes" / commit fails | The fix is already on `main`. Restore the bug or use `PUBLISH_FIX=0`. |
| Push or PR creation failed | `gh auth status`, remote permissions. The demo falls back to dashboard approval and the Deployment approval box appears. |
| Quarkus logs missing in the dashboard | Hard-reload the page. The log card fills from live events. |
| Java fails to start | `java -version` must show 27. Set `JAVA_HOME`. |

## Where to read the code

| Topic | File |
|---|---|
| The bug | `service/src/main/java/demo/CheckoutResource.java` |
| Whole pipeline | `orchestrator/engine.py` (`start`, `monitor`, `exception_excerpt`, `repair`, `publish`, `await_merge`, `approve`, `live_check`) |
| Agent | `orchestrator/agent.py` |
| API and request protection | `orchestrator/app.py` |
| Trusted tests | `verification/CheckoutContractTest.java` |
| CI | `.github/workflows/ci.yml` |
| Dashboard | `dashboard/index.html` |
| Safety tests | `tests/test_safety.py` |
