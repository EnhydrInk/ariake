#!/usr/bin/env python3
"""
Ariake example host — example/echo_app.py
-----------------------------------------
Minimal FastAPI host wiring Ariake's transport coordinator to a working demo.

  POST /api/message {"text": "..."}        -> forwarded to active transport
  POST /api/transport/switch {"target":"sdk"|"tmux"} -> coordinator's endpoint
  GET  /events                              -> SSE re-broadcast of active SSE

Run alongside channel/server.ts (port 8910) and sdk-channel/server.py (port 8911).

This file is the wire diagram you can run; adapt freely for your own host.
"""
from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

# Ariake transport coordinator. See backend/transport.py.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
import transport  # type: ignore


# ── Config ────────────────────────────────────────────────────────────
PORT = int(os.environ.get("ARIAKE_ECHO_PORT", "18080"))
TRANSPORTS = {
    "tmux": os.environ.get("ARIAKE_TRANSPORT_TMUX", "http://127.0.0.1:8910"),
    "sdk":  os.environ.get("ARIAKE_TRANSPORT_SDK",  "http://127.0.0.1:8911"),
}
INITIAL = os.environ.get("ARIAKE_INITIAL", "sdk")


# ── SSE subscriber to the active transport ────────────────────────────
# Re-read transport.current_url() every loop so a switch immediately retargets
# the URL we connect to.
_sse_task: Optional[asyncio.Task] = None


async def sse_subscriber():
    """Stay subscribed to the active transport's /events; forward each frame
    to our own hub for re-broadcast to local /events clients."""
    while True:
        url = f"{transport.current_url()}/events"
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("GET", url) as resp:
                    if resp.status_code != 200:
                        await asyncio.sleep(2)
                        continue
                    async for line in resp.aiter_lines():
                        if line.startswith("data: "):
                            await _hub_broadcast(line[6:])
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(2)


def reconfigure(new_url: str) -> None:
    """Coordinator callback after CHANNEL_BASE swaps. echo_app has nothing
    extra to re-target; a real host would re-aim heartbeat / timer producers
    at the new URL here."""
    pass


async def restart_sse():
    """Coordinator callback: cancel + respawn the SSE subscriber so it
    reconnects at the new URL."""
    global _sse_task
    if _sse_task is not None and not _sse_task.done():
        _sse_task.cancel()
    _sse_task = asyncio.create_task(sse_subscriber())


# ── Local SSE hub ─────────────────────────────────────────────────────
_hub_listeners: set[asyncio.Queue] = set()


async def _hub_broadcast(data: str) -> None:
    dead = []
    for q in _hub_listeners:
        try:
            q.put_nowait(data)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        _hub_listeners.discard(q)


# ── Lifespan ──────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    transport.configure(
        transports=TRANSPORTS,
        initial=INITIAL,
        reconfigure_callback=reconfigure,
        sse_restart_callback=restart_sse,
    )
    global _sse_task
    _sse_task = asyncio.create_task(sse_subscriber())
    yield
    if _sse_task is not None:
        _sse_task.cancel()


app = FastAPI(lifespan=lifespan)
app.include_router(transport.router)  # /api/transport/{state,switch}


# ── Echo wire ─────────────────────────────────────────────────────────
@app.post("/api/message")
async def api_message(payload: dict):
    """Forward an inbound message to whichever transport is currently active.
    Host MUST check transport.is_paused() — see docs/architecture.md step 2."""
    if transport.is_paused():
        return JSONResponse({"ok": False, "error": "transport switch in progress"}, status_code=503)
    text = (payload.get("text") or "").strip()
    chat_id = payload.get("chat_id") or "default"
    if not text:
        return JSONResponse({"ok": False, "error": "empty message"}, status_code=400)
    url = f"{transport.current_url()}/message"
    try:
        async with httpx.AsyncClient(timeout=10) as h:
            r = await h.post(url, json={"text": text, "chat_id": chat_id, "user": "user"})
        return JSONResponse(r.json(), status_code=r.status_code)
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": f"transport unreachable: {type(e).__name__}: {e}"},
            status_code=502,
        )


@app.get("/events")
async def events(req: Request):
    """SSE re-broadcast: every client gets the active transport's replies,
    regardless of which transport is active right now."""
    q: asyncio.Queue = asyncio.Queue(maxsize=512)
    _hub_listeners.add(q)

    async def gen():
        try:
            yield ": connected\n\n"
            while True:
                if await req.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(q.get(), timeout=30.0)
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            _hub_listeners.discard(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache", "connection": "keep-alive"},
    )


@app.get("/")
async def root():
    return {
        "service": "ariake-echo",
        "port": PORT,
        "active_transport_url": transport.current_url(),
        "configured": list(TRANSPORTS.keys()),
        "paused": transport.is_paused(),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
