import asyncio
from contextlib import asynccontextmanager
import json
import os
import secrets

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse, Response
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel
from .engine import Engine, ROOT

engine = Engine()
TOKEN = os.getenv("DEMO_TOKEN", secrets.token_urlsafe(24))

@asynccontextmanager
async def lifespan(app):
    engine.spawn(engine.start())
    yield
    await engine.close()

app = FastAPI(title="Self-healing checkout demo", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

@app.middleware("http")
async def protect_mutations(request: Request, call_next):
    if request.method == "POST" and request.url.path.startswith("/api/"):
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return Response("Cross-origin mutation refused", status_code=403)
        # A same-origin browser request needs no token (a stale tab keeps an old one after a restart); other clients do.
        if origin is None and not secrets.compare_digest(request.headers.get("x-demo-token", ""), TOKEN):
            return Response("Missing session token", status_code=403)
    return await call_next(request)

@app.get("/")
async def dashboard():
    return FileResponse(ROOT / "dashboard/index.html", headers={"Cache-Control": "no-store"})

@app.get("/api/state")
async def state():
    return engine.snapshot()

@app.get("/api/session")
async def session():
    return Response(json.dumps({"token": TOKEN}), media_type="application/json", headers={"Cache-Control": "no-store"})

@app.get("/api/events")
async def events(request: Request):
    try:
        cursor = int(request.headers.get("last-event-id", "0"))
    except ValueError:
        cursor = 0
    if cursor < 0 or cursor > len(engine.events):
        cursor = 0
    async def stream():
        nonlocal cursor
        while not await request.is_disconnected():
            batch = engine.events[cursor:]
            for event in batch:
                yield f"id: {event['id']}\ndata: {json.dumps(event)}\n\n"
                cursor = event["id"]
            if not batch:
                yield ": heartbeat\n\n"
            await asyncio.sleep(.5)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control":"no-cache", "X-Accel-Buffering":"no"})

class Approval(BaseModel):
    incident: str
    digest: str

async def action(coro):
    try:
        await coro
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return engine.snapshot()

@app.post("/api/inject")
async def inject(): return await action(engine.inject())
@app.post("/api/reset")
async def reset(): return await action(engine.reset())
@app.post("/api/reject")
async def reject(): return await action(engine.reject())
@app.post("/api/approve")
async def approve(body: Approval): return await action(engine.approve(body.incident, body.digest))

@app.post("/checkout")
async def checkout(request: Request):
    if not engine.active:
        raise HTTPException(503, "Java service unavailable (or replay mode)")
    body = await request.body()
    if len(body) > 16384:
        raise HTTPException(413)
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.post(engine.active + "/checkout", content=body, headers={"content-type":"application/json"})
        return Response(r.content, status_code=r.status_code, media_type=r.headers.get("content-type"))
    except httpx.HTTPError as exc:
        raise HTTPException(503, "Checkout unavailable") from exc

@app.get("/api/metrics")
async def metrics():
    if not engine.active:
        raise HTTPException(503)
    async with httpx.AsyncClient(timeout=5) as client:
        r = await client.get(engine.active + "/q/metrics")
    return Response(r.content, media_type="text/plain")
