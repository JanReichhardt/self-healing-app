import asyncio
import contextlib
import hashlib
import json
import os
import re
from pathlib import Path
import signal
import socket
import sys
import time
import uuid

import httpx
from .agent import ALLOWED_ROOT, ALLOWED_SUFFIXES, BUG, FIX, SOURCE
from .target import ROOT, T


def digest_tree(path):
    h = hashlib.sha256()
    for f in sorted(Path(path).rglob("*")):
        if f.is_symlink():
            raise ValueError("Symlinks are not accepted in a release")
        if f.is_file():
            h.update(str(f.relative_to(path)).encode())
            h.update(f.read_bytes())
    return h.hexdigest()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Engine:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.state = self.root / ".state"
        self.state.mkdir(exist_ok=True)
        self.mode = os.getenv("AGENT_MODE", "deterministic")
        if self.mode not in {"deterministic", "openhands", "replay"}:
            raise ValueError("AGENT_MODE must be deterministic, openhands, or replay")
        self.events = []
        self.status = "booting"
        self.incident = None
        self.patch = ""
        self.diagnosis = ""
        self.digest = None
        self.active = None
        self.baseline = None
        self.candidate = None
        self.procs = []
        self.logs = {}
        self.error_logged = False
        self.failing = None
        self.excerpt = ""
        self.changed = []
        self.tasks = set()
        self.lock = asyncio.Lock()
        self.armed = False
        self.failures = 0
        self.last_probe = None
        self.live = None
        self.pr = self.ci = None
        self.merged = False

    def emit(self, kind, message, **data):
        event = dict(id=len(self.events) + 1, time=time.time(), kind=kind,
                     message=message, incident=self.incident, mode=self.mode)
        event.update(data)
        self.events.append(event)
        with (self.state / "events.jsonl").open("a") as f:
            f.write(json.dumps(event) + "\n")
        return event

    def transition(self, status, message):
        self.status = status
        self.emit("state", message, status=status)

    def snapshot(self):
        return dict(status=self.status, mode=self.mode, incident=self.incident,
                    patch=self.patch, diagnosis=self.diagnosis, digest=self.digest, armed=self.armed,
                    last_probe=self.last_probe, live=self.live, pr=self.pr, ci=self.ci, event_count=len(self.events))

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        def finished(done):
            self.tasks.discard(done)
            if not done.cancelled() and done.exception() is not None:
                self.transition("failed", f"Background operation failed: {done.exception()}")
        task.add_done_callback(finished)
        return task

    async def command(self, args, cwd=None, timeout=300, logfile="commands.log"):
        log = self.state / logfile
        with log.open("ab") as f:
            f.write(("\n$ " + " ".join(map(str,args)) + "\n").encode())
            proc = await asyncio.create_subprocess_exec(*map(str,args), cwd=cwd or self.root,
                stdout=f, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
            try:
                await asyncio.wait_for(proc.wait(), timeout)
            except (TimeoutError, asyncio.CancelledError):
                self.kill(proc)
                await proc.wait()
                raise
        if proc.returncode:
            raise RuntimeError(f"Command failed ({proc.returncode}); see .state/{logfile}")

    @staticmethod
    def kill(proc):
        if proc and proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    async def git_text(self, *args, cwd=None):
        return await self.text("git", *args, cwd=cwd or self.repo)

    async def text(self, *args, cwd):
        proc = await asyncio.create_subprocess_exec(*args, cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        if proc.returncode:
            raise RuntimeError(err.decode())
        return out.decode().strip()

    def fmt(self, cmd, **kw):
        return [a.format(state=self.state, **kw) for a in cmd]

    async def build(self, checkout, contract=False):
        if contract:  # controller-owned tests overwrite whatever the agent wrote
            for src, dest in T["trusted_files"].items():
                (checkout / dest).parent.mkdir(parents=True, exist_ok=True)
                (checkout / dest).write_bytes((self.root / src).read_bytes())
        if T["build"]:
            await self.command(self.fmt(T["build"]), cwd=checkout / T["app_dir"], timeout=600)

    async def launch(self, checkout):
        port = free_port()
        logfile = (self.state / f"app-{port}.log").open("ab")
        try:
            proc = await asyncio.create_subprocess_exec(*self.fmt(T["run"], port=port), cwd=checkout / T["app_dir"],
                env={**os.environ, "PORT": str(port)},
                stdout=logfile, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        finally:
            logfile.close()
        self.procs.append(proc)
        url = f"http://127.0.0.1:{port}"
        self.logs[url] = self.state / f"app-{port}.log"
        self.spawn(self.follow(self.logs[url], port))
        async with httpx.AsyncClient(timeout=2) as client:
            for _ in range(100):
                if proc.returncode is not None:
                    raise RuntimeError(f"App exited; see .state/app-{port}.log")
                try:
                    if (await client.get(url + T["ready_path"])).status_code == 200:
                        return url
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(.2)
        raise RuntimeError("App readiness timeout")

    async def follow(self, path, port=None, kind="log"):
        """Stream a log file (service or agent) to the dashboard, one event per line."""
        with path.open(errors="replace") as f:
            while True:
                pos = f.tell()
                line = f.readline()
                if line.endswith("\n"):
                    self.emit(kind, line.rstrip()[:500], port=port)
                    if T["error_marker"] in line and self.logs.get(self.active) == path and self.status == "healthy":
                        self.error_logged = True
                else:
                    f.seek(pos)  # partial line: re-read once complete
                    await asyncio.sleep(.3)

    def exception_excerpt(self):
        """The newest error block (with stack trace) the active service logged."""
        lines = self.logs[self.active].read_text(errors="replace").splitlines()
        errors = [i for i, l in enumerate(lines) if T["error_marker"] in l]
        excerpt = "\n".join(lines[errors[-1]:errors[-1] + 30]) if errors else ""
        if not excerpt:
            raise ValueError("No error found in the service log; nothing to repair")
        return excerpt

    async def start(self):
        if self.mode == "replay":
            self.transition("healthy", "Replay ready: simulated events, no Java or agent execution")
            return
        try:
            # A private repository avoids touching the user's own Git checkout.
            self.repo = self.state / ("repo-" + uuid.uuid4().hex[:8])
            self.repo.mkdir()
            import shutil
            shutil.copytree(self.root / T["app_dir"], self.repo / T["app_dir"], ignore=shutil.ignore_patterns(*T["ignore"]))
            if SOURCE:  # The working tree may sit on a fix branch; the demo baseline must always carry the bug.
                src = self.repo / SOURCE
                src.write_text(src.read_text().replace(FIX, BUG))
            (self.repo / ".gitignore").write_text("".join(f"{p}/\n" for p in T["ignore"]))
            await self.command(["git", "init", "-b", "demo"], cwd=self.repo)
            await self.command(["git", "add", "."], cwd=self.repo)
            await self.command(["git", "-c", "user.name=Demo", "-c", "user.email=demo@localhost", "commit", "-m", "Reproducible baseline"], cwd=self.repo)
            self.base = await self.git_text("rev-parse", "HEAD")
            self.transition("building", "Building the baseline, which carries the bug")
            await self.build(self.repo)
            self.baseline = self.active = await self.launch(self.repo)
            self.transition("healthy", "Baseline ready; normal traffic succeeds")
            self.spawn(self.monitor())
        except Exception as exc:
            self.transition("failed", str(exc))

    async def probe(self, url, case):
        async with httpx.AsyncClient(timeout=4) as client:
            r = await client.post(url + T["endpoint"], json=case["json"])
        try:
            body = r.json()
        except ValueError:
            body = None
        return r.status_code == case.get("status", 200) and ("body" not in case or body == case["body"]), r.status_code

    async def monitor(self):
        while True:
            try:
                case = T["fault"] if self.armed else T["normal"]
                ok, code = await self.probe(self.active, case)
                self.last_probe = dict(ok=ok, code=code, time=time.time())
                self.emit("probe", "Synthetic probe passed" if ok else "Synthetic probe failed", **self.last_probe)
                self.failures = 0 if ok else self.failures + 1
                if not ok:
                    self.failing = case
                if self.error_logged and self.status == "healthy":
                    self.error_logged = False
                    self.incident = uuid.uuid4().hex[:10]
                    self.transition("detected", "Error in the service log; incident opened")
                    self.spawn(self.repair())
            except httpx.HTTPError as exc:
                self.last_probe = dict(ok=False, code=0, time=time.time())
                self.emit("probe", f"Service unreachable: {type(exc).__name__}")
                # Infrastructure faults are visible but never treated as this source bug.
            await asyncio.sleep(float(os.getenv("MONITOR_INTERVAL", "2")))

    async def inject(self):
        async with self.lock:
            if self.status != "healthy" or self.armed:
                raise ValueError("Reset to a healthy baseline before injecting")
            self.armed = True
            self.emit("injection", "Fault injected: synthetic traffic now sends the failing request")
            if self.mode == "replay":
                self.incident = uuid.uuid4().hex[:10]
                self.spawn(self.replay())

    async def replay(self):
        for item in json.loads((self.root / "replay/scenario.json").read_text()):
            await asyncio.sleep(.6)
            self.transition(item["status"], item["message"])
        self.patch = "SIMULATED: String coupon = order.coupon() == null ? \"\" : order.coupon().trim();"
        self.digest = "replay-simulated-release"
        self.emit("candidate", "Replay approval available", digest=self.digest)

    async def alert(self, excerpt):
        """External trigger (e.g. a Sentry/Datadog webhook): the alert's stack trace replaces the log tail."""
        async with self.lock:
            if self.mode == "replay" or self.status != "healthy":
                raise ValueError("Alert ignored: needs a healthy live service")
            self.incident = uuid.uuid4().hex[:10]
            self.transition("detected", "External alert received; incident opened")
            self.spawn(self.repair(excerpt))

    async def repair(self, alert_excerpt=None):
        try:
            excerpt = self.excerpt = alert_excerpt or self.exception_excerpt()
            (self.state / f"exception-{self.incident}.log").write_text(excerpt)
            self.emit("diagnosis", "Agent receives the exception from the service log", excerpt=excerpt)
            self.transition("triaging", f"Agent diagnoses the root cause, read-only; adapter={self.mode}")
            self.agent_tree = self.state / ("agent-" + self.incident)
            self.verify_tree = self.state / ("verify-" + self.incident)
            await self.command(["git", "worktree", "add", "--detach", self.agent_tree, self.base], cwd=self.repo)
            agent_log = self.state / f"agent-{self.incident}.log"
            agent_log.touch()
            self.spawn(self.follow(agent_log, kind="agent"))  # the agent's reasoning shows up live on the dashboard
            notes = self.state / f"triage-{self.incident}.txt"
            agent = [sys.executable, "-m", "orchestrator.agent", self.agent_tree, self.mode, self.state / f"exception-{self.incident}.log"]
            await self.command([*agent, "triage", notes], timeout=600, logfile=agent_log.name)
            if await self.git_text("status", "--porcelain", cwd=self.agent_tree):
                raise ValueError("Triage must not modify files")
            if not notes.exists():
                raise ValueError("Triage produced no diagnosis")
            self.diagnosis = notes.read_text()[:4000]
            self.emit("triage", "Root-cause diagnosis ready", diagnosis=self.diagnosis)
            if self.diagnosis.lstrip().upper().startswith("FIXABLE: NO"):
                raise ValueError("Triage: not a code bug the agent can fix; see the diagnosis")
            self.transition("repairing", "Agent fixes the root cause in the isolated worktree")
            await self.command([*agent, "fix", notes], timeout=600, logfile=agent_log.name)
            # Extract only regular allowlisted files under the allowed source root; never run the agent's build/test files.
            await self.command(["git", "add", "-A"], cwd=self.agent_tree)
            status = (await self.git_text("diff", "--cached", "--name-status", self.base, cwd=self.agent_tree)).splitlines()
            self.changed = [l.split("\t", 1)[1] for l in status if l.startswith(("A\t", "M\t"))]
            if not status or len(self.changed) != len(status) or len(self.changed) > 5 or not all(
                    f.startswith(ALLOWED_ROOT) and f.endswith(ALLOWED_SUFFIXES) and (self.agent_tree / f).is_file()
                    and not (self.agent_tree / f).is_symlink() and (self.agent_tree / f).stat().st_size <= 50000
                    for f in self.changed):
                raise ValueError("Proposal changed files outside the source allowlist or made no change")
            self.patch = await self.git_text("diff", "--cached", self.base, cwd=self.agent_tree)
            proposal = {f: (self.agent_tree / f).read_bytes() for f in self.changed}
            self.transition("verifying", "Rebuilding in a separate clean checkout with trusted regression tests")
            await self.command(["git", "worktree", "add", "--detach", self.verify_tree, self.base], cwd=self.repo)
            for f, data in proposal.items():
                (self.verify_tree / f).parent.mkdir(parents=True, exist_ok=True)
                (self.verify_tree / f).write_bytes(data)
            await self.build(self.verify_tree, contract=True)
            self.candidate = await self.launch(self.verify_tree)
            await self.contract(self.candidate)
            if self.failing:  # the request that triggered the incident must now succeed
                ok, code = await self.probe(self.candidate, self.failing)
                if not ok:
                    raise ValueError(f"Fix does not resolve the failing request (HTTP {code})")
            self.release = self.verify_tree / T["app_dir"] / T["release_dir"]
            self.digest = digest_tree(self.release)
            self.transition("awaiting_approval", "Independent tests and HTTP contract passed; deployment blocked for approval")
            self.emit("candidate", "Review the patch and approve this artifact digest", digest=self.digest, patch=self.patch)
            if os.getenv("PUBLISH_FIX", "1") != "0":
                await self.publish()
                if self.pr:
                    self.spawn(self.await_merge())
        except Exception as exc:
            self.transition("failed", str(exc))

    async def contract(self, url):
        for case in T["contract"]:
            ok, _ = await self.probe(url, case)
            if not ok:
                raise ValueError(f"Independent HTTP contract failed for {case['json']}")

    async def approve(self, incident, digest):
        async with self.lock:
            if self.status != "awaiting_approval" or incident != self.incident or digest != self.digest:
                raise ValueError("Stale or invalid approval; nothing deployed")
            if self.pr and not self.merged:
                raise ValueError("Merge the pull request on GitHub to deploy")
            if self.mode == "replay":
                self.transition("resolved", "SIMULATED approval and recovery; no software deployed")
                return
            if digest_tree(self.release) != self.digest:
                self.transition("failed", "Artifact changed since verification; approval refused")
                raise ValueError("Artifact digest mismatch")
            self.transition("deploying", "Approval recorded; switching traffic to the tested release")
            previous = self.active
            try:
                await self.contract(self.candidate)
                self.active = self.candidate
                await self.contract(self.active)
                self.transition("resolved", "Post-deploy checks passed; the failing request recovered")
                await self.live_check()
            except Exception:
                self.active = previous
                self.transition("failed", "Post-deploy verification failed; traffic rolled back")
                raise ValueError("Deployment failed and was rolled back")

    async def live_check(self):
        """Call the real, fixed endpoint with the failing request; its access log line shows up in the dashboard."""
        ok, code = await self.probe(self.active, T["fault"])
        self.live = (f"POST {T['endpoint']} with the failing request on the fixed service: HTTP {code}, as expected"
                     if ok else f"Fixed service still fails: HTTP {code}")
        self.emit("live_check", self.live, ok=ok, code=code)

    async def publish(self):
        """Commit the verified fix to a new branch of the real repository, push it and open a pull request."""
        branch, tree = f"self-heal/{self.incident}", self.state / f"publish-{self.incident}"
        try:
            await self.command(["git", "worktree", "add", "-b", branch, tree, "main"], cwd=self.root)
            for f in self.changed:
                (tree / f).parent.mkdir(parents=True, exist_ok=True)
                (tree / f).write_bytes((self.verify_tree / f).read_bytes())
            await self.command(["git", "add", *self.changed], cwd=tree)
            m = re.search(r"([\w.]+(?:Exception|Error))", self.excerpt)
            title = f"Fix {m.group(1).rsplit('.', 1)[-1] if m else 'error'} found in the service log"
            await self.command(["git", "commit", "-m", title,
                                "-m", "Found in the service log, repaired by the agent and verified locally."], cwd=tree)
            await self.command(["git", "push", "-u", "origin", branch], cwd=tree, timeout=60)
            self.pr = await self.text("gh", "pr", "create", "--base", "main", "--head", branch,
                "--title", title,
                "--body", "Automated fix.\n\nService log excerpt the agent worked from:\n\n```\n" + self.excerpt + "\n```\n\n"
                          "CI builds the service, runs the contract tests and checks the running app. Merge to deploy.",
                cwd=tree)
            self.emit("publish", f"Pushed {branch} and opened pull request {self.pr}", branch=branch, pr=self.pr)
            self.transition("awaiting_approval", "Pull request open; CI is running. Merge it on GitHub to deploy")
        except Exception as exc:
            self.emit("publish", f"Commit/push/PR failed, falling back to dashboard approval: {exc}")
        finally:
            with contextlib.suppress(RuntimeError):
                await self.command(["git", "worktree", "remove", "--force", tree], cwd=self.root)

    async def await_merge(self):
        """The merge is the approval: poll the PR, mirror CI into the dashboard, deploy once merged."""
        while self.status == "awaiting_approval":
            try:
                info = json.loads(await self.text("gh", "pr", "view", self.pr, "--json", "state,statusCheckRollup", cwd=self.root))
            except RuntimeError as exc:
                self.emit("ci", f"Could not read the pull request: {exc}")
                await asyncio.sleep(10)
                continue
            checks = info["statusCheckRollup"]
            ci = ("no checks yet" if not checks else
                  "running" if any(c.get("status") not in (None, "COMPLETED") for c in checks) else
                  "passed" if all(c.get("conclusion", c.get("state")) == "SUCCESS" for c in checks) else "failed")
            if ci != self.ci:
                self.ci = ci
                self.emit("ci", f"CI pipeline: {ci}")
            if info["state"] == "MERGED":
                self.merged = True
                self.emit("merge", "Pull request merged; deploying the tested release")
                return await self.approve(self.incident, self.digest)
            if info["state"] == "CLOSED":
                return await self.reject()
            await asyncio.sleep(5)

    async def reject(self):
        async with self.lock:
            if self.status != "awaiting_approval":
                raise ValueError("No candidate to reject")
            self.transition("rejected", "Candidate rejected; baseline remains active")

    async def reset(self):
        async with self.lock:
            if self.status not in {"resolved","rejected","failed","healthy"}:
                raise ValueError("Wait for the current operation to finish")
            if self.mode != "replay" and not self.baseline:
                raise ValueError("Baseline unavailable; restart after fixing startup error")
            self.active = self.baseline
            self.armed = False
            self.failures = 0
            self.incident = self.digest = None
            self.patch = self.diagnosis = ""
            self.live = self.pr = self.ci = None
            self.merged = False
            self.transition("healthy", "Reset to reproducible baseline")

    async def close(self):
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*list(self.tasks), return_exceptions=True)
        for proc in self.procs:
            self.kill(proc)
        for proc in self.procs:
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except TimeoutError:
                os.killpg(proc.pid, signal.SIGKILL)
                await proc.wait()
