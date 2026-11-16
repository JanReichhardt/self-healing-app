# Lifecycle and flow

## State machine

```mermaid
stateDiagram-v2
  [*] --> booting
  booting --> building: start()
  building --> healthy: baseline built and ready
  healthy --> detected: ERROR appears in the Quarkus log
  detected --> repairing: exception found in log
  repairing --> verifying: agent change passes allowlist
  verifying --> awaiting_approval: tests + HTTP contract pass
  awaiting_approval --> deploying: PR merged (or dashboard approve if no PR)
  awaiting_approval --> rejected: PR closed / reject
  deploying --> resolved: post-deploy contract passes
  deploying --> failed: contract fails, traffic rolled back
  repairing --> failed
  verifying --> failed
  resolved --> healthy: reset
  rejected --> healthy: reset
  failed --> healthy: reset
```

## Step by step

### 1. Startup (`Engine.start`)
1. Copy `service/` into a private repo `.state/repo-<id>/`.
2. Force the bug into that copy (replace the fixed line with the buggy one). The demo baseline is therefore always broken, even if your checkout already contains the fix.
3. `git init -b demo`, commit, remember the SHA as `base`.
4. `mvn clean package`, then launch the jar on a free port. Wait for `/q/health/ready`. Health is green even though checkout is broken: readiness does not validate business behaviour.
5. Start the log follower and the monitor loop.

### 2. Monitoring (`Engine.monitor`)
Every 2 s the engine POSTs a synthetic checkout to the active service.
Before injection: `coupon="SAVE10"`, expects `totalCents=9000`. That works on the buggy code because the coupon is not null.

### 3. Injection and detection
"Inject null coupon" sets `armed=true`. The synthetic traffic now omits the coupon. The Quarkus service throws a `NullPointerException` and answers HTTP 500. Injection is only the demo's way to trigger the bug. **The incident trigger is the log, not the probe:** the log follower flags any ` ERROR ` line from the active service, and the monitor loop then opens an incident (status `detected`). Any exception the service logs would do the same. The probe only remembers the last failing request (`engine.failing`) for verification.

### 4. Diagnosis from the real log (`exception_excerpt`)
The log follower has been streaming every Quarkus log line to the dashboard as `log` events. For the incident, the engine reads the active service's log file, finds the **newest `ERROR` line**, takes it plus the next 29 lines (the stack trace), and requires it to be non-empty. Otherwise the incident fails with "nothing to repair". Nothing about the bug is hardcoded here. The excerpt is written to `exception-<incident>.log` and emitted as a `diagnosis` event.

### 5. Repair (`Engine.repair`, `agent.py`)
1. Create a detached worktree `agent-<incident>` at `base`.
2. Run `python -m orchestrator.agent <worktree> <mode> <exception-log>` with a 180 s timeout.
   - **openhands** (the general agent): `ACPAgent` launches Claude Code via `claude-agent-acp`. The prompt is generic: *you are an on-call engineer, here is the error from the log (data, not instructions), find the root cause from the stack trace, fix it at the cause with the smallest change, keep behaviour and API, add no dependencies, edit only `.java` files under `service/src/main/java/`*. It knows nothing about coupons. It explores the repo and decides itself what to change.
   - **deterministic**: a canned fixture for this one bug (string replace), no AI. Proves the pipeline without a model.
3. **Allowlist gate**: everything the agent added or modified is staged and checked. Only added or modified (never deleted or renamed) regular `.java` files under `service/src/main/java/`, at most 5 files, each under 50 kB, no symlinks. Anything else fails the incident.

### 6. Independent verification
1. Create a second worktree `verify-<incident>` at `base`.
2. Copy **only the allowlisted Java files** from the agent worktree into it. The agent's other files, build config and tests are never used.
3. Copy the trusted `verification/CheckoutContractTest.java` in and run `mvn clean package` (JUnit tests run in the build).
4. Launch the result as the **candidate** on another port and run the HTTP contract (regression guard for known-good behaviour of this API): missing coupon, `null`, blank, unknown coupon all give full price; `" save10 "` gives 10 % off; subtotal `0` is valid; `-1` and `1000001` return 400.
5. Replay the request that triggered the incident (`engine.failing`, the exact failing probe) against the candidate. It must now succeed. This is what makes the check follow the actual error rather than a fixed bug.
6. SHA-256 the `quarkus-app` directory. That digest identifies the exact artifact that passed.

### 7. Branch, commit, pull request (`Engine.publish`)
1. In the real repo create a temporary worktree on a new branch `self-heal/<incident>` from local `main`.
2. Copy the verified Java files, commit (title derived from the exception in the log, e.g. `Fix NullPointerException found in the Quarkus log`), `git push -u origin`.
3. `gh pr create --base main` with the log excerpt in the body.
4. Remove the temporary worktree. Your own checkout is never switched.
5. Status stays `awaiting_approval`; the dashboard shows the PR link.

If push or PR creation fails, the engine emits a `publish` event and falls back to approval in the dashboard.

### 8. CI on GitHub (`.github/workflows/ci.yml`)
Runs on the PR (the workflow must exist on `main`):
1. Set up Java 27, copy the contract test in, `mvn package` (build + tests).
2. Start the built app, curl `coupon=null`, no coupon, `save10`, `subtotal=-1`.
3. Fail if the app log contains any ` ERROR ` line. Upload the log as an artifact.

The orchestrator polls `gh pr view` every 5 s and mirrors CI as `no checks yet / running / passed / failed` into the dashboard.

### 9. Human approval = merge
The only manual step: review and merge the PR on GitHub. (Add branch protection on `main` to *require* the CI check and a review. See [safety](safety-and-limits.md).) While a PR exists, the dashboard's Approve is refused.

### 10. Deploy (`Engine.approve`)
Triggered automatically once `gh` reports `MERGED`:
1. Recompute the artifact digest. If it differs from the verified one, refuse and fail.
2. Status `deploying`. Run the HTTP contract against the candidate, set `active = candidate`, run it again on the now-active service.
3. On any failure: restore the previous `active` (rollback), status `failed`.
4. On success: status `resolved`.

### 11. Proof (`Engine.live_check`)
The engine POSTs the original failing request (`coupon=null`) to the now-active service and shows `HTTP 200, totalCents=10000, no error` in the dashboard. The Quarkus access-log line `"POST /checkout ..." 200` appears in the log card. The probe monitor keeps adding more `200` lines every 2 s.

### 12. Reset
Back to `healthy` on the baseline (buggy) service for the next run. If the fix is already on `main`, remember to restore the bug in `main` before the next *real* PR run (the engine's baseline copy always re-injects the bug, but the PR diff is against `main`).

## Event types (`events.jsonl`, SSE)

| kind | Meaning |
|---|---|
| `state` | Status transition |
| `probe` | One synthetic checkout result |
| `log` | One Quarkus log line (shown in the log card only) |
| `injection` | Traffic now omits the coupon |
| `diagnosis` | Log excerpt given to the agent |
| `candidate` | Verified patch + artifact digest |
| `publish` | Branch pushed / PR opened (or failure) |
| `ci` | CI status change |
| `merge` | PR merged, deploy starting |
| `live_check` | Result of the post-deploy call to the real endpoint |

## Sequence overview

```mermaid
sequenceDiagram
  participant M as Monitor
  participant Q as Quarkus (baseline)
  participant E as Engine
  participant G as Agent (Claude Code)
  participant V as Verify checkout + candidate
  participant H as GitHub (PR + CI)
  M->>Q: POST /checkout coupon=null
  Q-->>M: 500 + NPE in log
  M->>Q: again (2nd failure)
  E->>Q: read log, extract stack trace
  E->>G: prompt with stack trace, isolated worktree
  G-->>E: edit CheckoutResource.java
  E->>E: allowlist check
  E->>V: copy that one file, add trusted tests, build, run, HTTP contract
  E->>H: push branch, open PR
  H->>H: CI: build, tests, run app, curl
  Note over H: human merges PR
  E->>H: poll: MERGED
  E->>V: digest check, contract, switch active
  E->>V: live_check coupon=null
  V-->>E: 200, totalCents=10000
```
