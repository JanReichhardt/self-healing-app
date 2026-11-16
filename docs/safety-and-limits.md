# Safety model, what is real, and known limits

## Real vs. simulated

| Part | Real? |
|---|---|
| Quarkus REST API, its HTTP errors, its stack trace and access log | Real |
| Agent reading the log and editing code (`openhands` mode) | Real, uses your Claude subscription |
| Git commit, push, pull request, GitHub Actions run | Real (your repo) |
| Merge triggering the deploy | Real |
| "Deployment" | **Local only**: the orchestrator switches its `active` pointer to the tested jar. Nothing is deployed to a server. |
| `deterministic` mode "agent" | A string replace. Tests the pipeline, says nothing about AI quality |
| `replay` mode | Fully simulated events from `replay/scenario.json`, no Java, no agent |
| The bug and the traffic | Deliberate and synthetic |

## Trust boundaries

The agent output is treated as untrusted input.

1. **Isolated worktree.** The agent edits `agent-<incident>/`, a detached worktree. It does not touch your checkout.
2. **Path allowlist.** After the agent finishes, its changes must be added or modified `.java` files under `service/src/main/java/` only: at most 5, each under 50 kB, no symlinks, no deletions or renames. Tests, build files and config are off limits.
3. **Separate verification checkout.** Only those files' bytes are copied into a fresh worktree. Build files, tests and scripts from the agent's tree are never executed.
4. **Trusted tests.** `verification/CheckoutContractTest.java`, the HTTP contract and the replay of the failing request come from the controller, not from the agent.
5. **Digest binding.** The artifact (`quarkus-app/`) is hashed after verification and re-hashed before deployment. A changed artifact is refused.
6. **Commit from verified bytes.** The pushed commit contains the files that were verified, copied from the verification checkout.
7. **Rollback.** If post-deploy checks fail, traffic returns to the previous service.
8. **Log content is data.** The stack trace in the prompt is marked as data, not instructions. Log text can be influenced by request content in real systems, so this matters.
9. **Independent second gate.** GitHub Actions repeats build, tests and runtime behaviour on the PR before a human merges.

## Control-plane protection (`app.py`)

- `TrustedHostMiddleware`: only `127.0.0.1`, `localhost`, `testserver` hosts. Blocks DNS rebinding.
- `POST /api/*`: a request with an `Origin` header must match the server's own origin (blocks cross-site forms and fetches). Requests without `Origin` (scripts) must send the session token in `x-demo-token`.
- Request body on `/checkout` limited to 16 kB.
- The orchestrator binds to `127.0.0.1` only.

## Known limits and honest caveats

- **The `openhands` agent has a shell.** `claude-agent-acp` brings Claude Code's own tools and the SDK defaults it to `bypassPermissions`. The previous "file editor only" restriction no longer exists at the agent level. The protection is steps 1 to 6 above, not tool restriction. For production you would run the agent in a sandbox or container and restrict its network and credentials.
- **Prompt injection.** A hostile log line could try to steer the agent. Mitigations are the allowlist, the trusted tests, the CI and the human merge, not the prompt wording.
- **General agent, bounded verification.** The agent is generic and fixes whatever the log shows, but the controller's regression tests (`CheckoutContractTest`, HTTP contract) only cover this checkout API, and the demo's bug trigger (null coupon) is fixed. For a different bug, the build, the replay of the failing request and the CI still run, but behaviour outside checkout is only as well protected as the project's own tests. The deterministic mode only fixes the coupon bug.
- **The agent can only change main Java sources.** A bug whose fix lives in config, a pom or a test is rejected by the allowlist.
- **Detection is log-driven but simple.** Any ` ERROR ` line from the active service while healthy opens an incident. There is no deduplication, severity filter or rate limit. Noisy logs would trigger repeated repairs.
- **Merge protection is not configured by this repo.** To make CI and review mandatory, set branch protection on `main` (required status check `verify`, required review). GitHub does not let you approve your own PR, so use a second reviewer or an admin merge for the demo.
- **Reject** in the dashboard does not close the PR on GitHub. Closing the PR on GitHub puts the demo in `rejected`.
- **Repeat runs.** After the fix is merged to `main`, the next real PR run has nothing to change on `main`. Restore the bug on `main` (or use `PUBLISH_FIX=0`) before the next run.
- **Uncommitted work is fragile.** `git reset --hard` and similar wipe local changes. Commit before experimenting.
