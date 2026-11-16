import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import sys
import time
import uuid

import httpx
from .agent import BUG, FIX, SOURCE

ROOT = Path(__file__).resolve().parents[1]


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
        self.digest = None
        self.active = None
        self.baseline = None
        self.candidate = None
        self.procs = []
        self.logs = {}
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
                    patch=self.patch, digest=self.digest, armed=self.armed,
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

    async def build(self, checkout, contract=False):
        if contract:
            dest = checkout / "service/src/test/java/demo/CheckoutContractTest.java"
            dest.write_bytes((self.root / "verification/CheckoutContractTest.java").read_bytes())
        await self.command(["mvn", "-B", "-ntp", "-Dquarkus.analytics.disabled=true", f"-Dmaven.repo.local={self.state / 'm2'}",
                            "clean", "package"], cwd=checkout / "service", timeout=600)

    async def launch(self, checkout):
        port = free_port()
        logfile = (self.state / f"java-{port}.log").open("ab")
        try:
            proc = await asyncio.create_subprocess_exec("java", f"-Dquarkus.http.port={port}",
                "-jar", str(checkout / "service/target/quarkus-app/quarkus-run.jar"),
                stdout=logfile, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        finally:
            logfile.close()
        self.procs.append(proc)
        url = f"http://127.0.0.1:{port}"
        self.logs[url] = self.state / f"java-{port}.log"
        self.spawn(self.follow(self.logs[url], port))
        async with httpx.AsyncClient(timeout=2) as client:
            for _ in range(100):
                if proc.returncode is not None:
                    raise RuntimeError(f"Java exited; see .state/java-{port}.log")
                try:
                    if (await client.get(url + "/q/health/ready")).status_code == 200:
                        return url
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(.2)
        raise RuntimeError("Java readiness timeout")

    async def follow(self, path, port):
        """Stream a Quarkus log file to the dashboard, one event per line."""
        with path.open(errors="replace") as f:
            while True:
                pos = f.tell()
                line = f.readline()
                if line.endswith("\n"):
                    self.emit("log", line.rstrip()[:500], port=port)
                else:
                    f.seek(pos)  # partial line: re-read once complete
                    await asyncio.sleep(.3)

    def exception_excerpt(self):
        """The newest ERROR block (with stack trace) the active Quarkus service logged for checkout."""
        lines = self.logs[self.active].read_text(errors="replace").splitlines()
        errors = [i for i, l in enumerate(lines) if " ERROR " in l]
        excerpt = "\n".join(lines[errors[-1]:errors[-1] + 30]) if errors else ""
        if "CheckoutResource" not in excerpt:
            raise ValueError("No checkout exception found in the Quarkus log; nothing to repair")
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
            shutil.copytree(self.root / "service", self.repo / "service", ignore=shutil.ignore_patterns("target"))
            # The working tree may sit on a fix branch; the demo baseline must always carry the bug.
            src = self.repo / SOURCE
            src.write_text(src.read_text().replace(FIX, BUG))
            await self.command(["git", "init", "-b", "demo"], cwd=self.repo)
            await self.command(["git", "add", "."], cwd=self.repo)
            await self.command(["git", "-c", "user.name=Demo", "-c", "user.email=demo@localhost", "commit", "-m", "Reproducible null-coupon baseline"], cwd=self.repo)
            self.base = await self.git_text("rev-parse", "HEAD")
            self.transition("building", "Building the intentionally buggy baseline")
            await self.build(self.repo)
            self.baseline = self.active = await self.launch(self.repo)
            self.transition("healthy", "Baseline ready; valid coupon traffic succeeds")
            self.spawn(self.monitor())
        except Exception as exc:
            self.transition("failed", str(exc))

    async def probe(self, url, payload=None, expected=10000):
        async with httpx.AsyncClient(timeout=4) as client:
            r = await client.post(url + "/checkout", json=payload if payload is not None else {"subtotalCents":10000,"coupon":None})
            try:
                body = r.json()
            except ValueError:
                body = {}
            return r.status_code == 200 and body == {"totalCents":expected,"currency":"EUR"}, r.status_code

    async def monitor(self):
        while True:
            try:
                payload = {"subtotalCents":10000,"coupon":None if self.armed else "SAVE10"}
                ok, code = await self.probe(self.active, payload, 10000 if self.armed else 9000)
                self.last_probe = dict(ok=ok, code=code, time=time.time())
                self.emit("probe", "Synthetic checkout passed" if ok else "Synthetic checkout failed", **self.last_probe)
                self.failures = 0 if ok else self.failures + 1
                if self.armed and self.failures >= 2 and self.status == "healthy":
                    self.incident = uuid.uuid4().hex[:10]
                    self.transition("detected", "Two consecutive null-coupon failures; incident opened")
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
            self.emit("injection", "Synthetic traffic now omits the optional coupon")
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

    async def repair(self):
        try:
            excerpt = self.exception_excerpt()
            (self.state / f"exception-{self.incident}.log").write_text(excerpt)
            self.emit("diagnosis", "Agent receives the Quarkus exception from the service log", excerpt=excerpt)
            self.transition("repairing", f"Creating isolated Git worktree; adapter={self.mode}")
            self.agent_tree = self.state / ("agent-" + self.incident)
            self.verify_tree = self.state / ("verify-" + self.incident)
            await self.command(["git", "worktree", "add", "--detach", self.agent_tree, self.base], cwd=self.repo)
            await self.command([sys.executable, "-m", "orchestrator.agent", self.agent_tree, self.mode,
                                self.state / f"exception-{self.incident}.log"], timeout=180,
                               logfile=f"agent-{self.incident}.log")
            # Extract only the approved regular source file, never run the agent's build/test files.
            source = self.agent_tree / SOURCE
            if source.is_symlink() or not source.is_file() or source.stat().st_size > 50000:
                raise ValueError("Invalid source proposal")
            changes = await self.git_text("diff", self.base, "--name-only", cwd=self.agent_tree)
            untracked = await self.git_text("ls-files", "--others", "--exclude-standard", cwd=self.agent_tree)
            if changes != SOURCE or untracked:
                raise ValueError("Proposal changed files outside the source allowlist or made no change")
            self.patch = await self.git_text("diff", self.base, "--", SOURCE, cwd=self.agent_tree)
            proposal = source.read_bytes()
            self.transition("verifying", "Rebuilding in a separate clean checkout with trusted regression tests")
            await self.command(["git", "worktree", "add", "--detach", self.verify_tree, self.base], cwd=self.repo)
            (self.verify_tree / SOURCE).write_bytes(proposal)
            await self.build(self.verify_tree, contract=True)
            self.candidate = await self.launch(self.verify_tree)
            await self.contract(self.candidate)
            self.release = self.verify_tree / "service/target/quarkus-app"
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
        cases = [({"subtotalCents":10000},10000), ({"subtotalCents":10000,"coupon":None},10000),
                 ({"subtotalCents":10000,"coupon":"  "},10000), ({"subtotalCents":10000,"coupon":"NOPE"},10000),
                 ({"subtotalCents":10000,"coupon":" save10 "},9000), ({"subtotalCents":0},0)]
        for payload, expected in cases:
            ok, _ = await self.probe(url, payload, expected)
            if not ok:
                raise ValueError(f"Independent HTTP contract failed for {payload}")
        async with httpx.AsyncClient(timeout=4) as client:
            for n in [-1, 1000001]:
                r = await client.post(url + "/checkout", json={"subtotalCents":n})
                if r.status_code != 400:
                    raise ValueError("Invalid subtotal was not rejected")

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
                self.transition("resolved", "Post-deploy checks passed; null-coupon checkout recovered")
                await self.live_check()
            except Exception:
                self.active = previous
                self.transition("failed", "Post-deploy verification failed; traffic rolled back")
                raise ValueError("Deployment failed and was rolled back")

    async def live_check(self):
        """Call the real, fixed endpoint with the failing request; its access log line shows up in the dashboard."""
        ok, code = await self.probe(self.active)
        self.live = (f"POST /checkout with coupon=null on the fixed service: HTTP {code}, totalCents=10000, no error"
                     if ok else f"Fixed service still fails: HTTP {code}")
        self.emit("live_check", self.live, ok=ok, code=code)

    async def publish(self):
        """Commit the verified fix to a new branch of the real repository, push it and open a pull request."""
        branch, tree = f"self-heal/{self.incident}", self.state / f"publish-{self.incident}"
        try:
            await self.command(["git", "worktree", "add", "-b", branch, tree, "main"], cwd=self.root)
            (tree / SOURCE).write_bytes((self.verify_tree / SOURCE).read_bytes())
            await self.command(["git", "add", SOURCE], cwd=tree)
            await self.command(["git", "commit", "-m", "Fix NullPointerException for checkout without coupon",
                                "-m", "Found in the Quarkus log, repaired by the agent and verified locally."], cwd=tree)
            await self.command(["git", "push", "-u", "origin", branch], cwd=tree, timeout=60)
            self.pr = await self.text("gh", "pr", "create", "--base", "main", "--head", branch,
                "--title", "Fix NullPointerException for checkout without coupon",
                "--body", "Automated fix.\n\nQuarkus log excerpt the agent worked from:\n\n```\n" + self.exception_excerpt() + "\n```\n\n"
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
            self.patch = ""
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
