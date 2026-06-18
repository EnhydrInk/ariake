"""
Ariake transport coordinator.

Drop-in FastAPI router that exposes /api/transport/{state,switch} and tracks
two pieces of module-level state:

    CHANNEL_BASE       — base URL of the currently active transport (mutable)
    transport_paused   — True between step 2 and step 7 of a switch

The host is expected to:

  1) call `configure(...)` at startup with its transport map + callbacks
  2) `app.include_router(transport.router)`
  3) call `transport.is_paused()` at every inbound point that injects into CC
     and return 503 if True (see docs/architecture.md "step 2")
  4) read `transport.current_url()` instead of caching CHANNEL_BASE — the value
     changes at step 6 of a switch and Python module-level lookup is the
     source of truth

The seven-step switch algorithm is the heart of this module. See
docs/architecture.md for the contract and docs/review-trail.md for why each
piece is shaped the way it is.
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse


router = APIRouter(prefix="/api/transport")


# ── Module state ───────────────────────────────────────────────────────
TRANSPORTS: dict[str, str] = {}
CHANNEL_BASE: str = ""
transport_lock = asyncio.Lock()
transport_paused: bool = False

ReconfigureCallback = Callable[[str], None]
SseRestartCallback = Callable[[], Optional[Awaitable[None]]]
_reconfigure_callback: Optional[ReconfigureCallback] = None
_sse_restart_callback: Optional[SseRestartCallback] = None


def configure(
    *,
    transports: dict[str, str],
    initial: str,
    reconfigure_callback: Optional[ReconfigureCallback] = None,
    sse_restart_callback: Optional[SseRestartCallback] = None,
) -> None:
    """Wire the coordinator. Must be called before the router handles any switch.

    `transports`            name -> base URL (no trailing slash). At least two entries.
    `initial`               which name is active at startup; must be a key of `transports`.
    `reconfigure_callback`  invoked (sync) after CHANNEL_BASE is swapped at step 6.
                            Typical use: re-target dependent producers (heartbeat /
                            timer / whatever else injects into CC) at the new URL.
    `sse_restart_callback`  invoked after CHANNEL_BASE swap so any existing SSE
                            subscription reconnects at the new URL. May be sync or
                            async (an awaitable return is awaited).

    Callback exceptions are caught and logged — a callback failure must not undo
    a successful switch (lock has been transferred; rolling back would corrupt state).
    """
    global TRANSPORTS, CHANNEL_BASE, _reconfigure_callback, _sse_restart_callback
    if initial not in transports:
        raise ValueError(f"initial={initial!r} not in transports={list(transports)}")
    if len(transports) < 2:
        raise ValueError("at least two transports required")
    TRANSPORTS = dict(transports)
    CHANNEL_BASE = transports[initial]
    _reconfigure_callback = reconfigure_callback
    _sse_restart_callback = sse_restart_callback


def is_paused() -> bool:
    """Hosts must call this on every inbound path that injects into CC and return
    503 if True. See docs/architecture.md step 2 for why."""
    return transport_paused


def current_url() -> str:
    """Currently active transport's base URL. Re-read at every use; the value
    changes at step 6 of a switch and Python module-level lookup is the source
    of truth."""
    return CHANNEL_BASE


# ── Endpoints ──────────────────────────────────────────────────────────
@router.get("/state")
async def transport_state() -> dict:
    """List configured transports + their health + the currently active name."""
    health: dict[str, dict] = {}
    async with httpx.AsyncClient(timeout=3) as client:
        for name, url in TRANSPORTS.items():
            try:
                r = await client.get(f"{url}/")
                health[name] = {
                    "url": url,
                    "ok": r.status_code == 200,
                    "info": r.json() if r.status_code == 200 else None,
                }
            except Exception as e:
                health[name] = {"url": url, "ok": False, "error": f"{type(e).__name__}: {e}"}
    active_name = next((n for n, u in TRANSPORTS.items() if u == CHANNEL_BASE), None)
    return {
        "active": active_name,
        "channel_base": CHANNEL_BASE,
        "paused": transport_paused,
        "transports": health,
    }


@router.post("/switch")
async def transport_switch(payload: dict) -> JSONResponse:
    """Atomically swap the active transport.

    Body: {"target": "<name>", "timeout": 30.0}

    Algorithm — strictly in this order:

      1. acquire transport_lock              one switch in flight at a time
      2. transport_paused = True             host returns 503 on inbound
      3. GET old/session/current             capture session_id for resume
      4. POST old/lease/release {timeout}    drain old transport
      5. POST new/lease/acquire {resume}     switch over (handles 200 / 200+restart_pending / 404 / non-2xx / exception)
      6. commit                              swap CHANNEL_BASE + callbacks
      7. transport_paused = False
    """
    global CHANNEL_BASE, transport_paused

    target = (payload.get("target") or "").strip()
    # Clamp timeout ≥ 30s. Smaller values are racy: the channel-side derives
    # its internal sub-budgets from this value, and if it ever exceeds the
    # coordinator's HTTP timeout we get a half-switch. 30s gives the channel
    # at least 25s of internal budget. See review-trail round 3, HIGH-1.
    try:
        raw_timeout = float(payload.get("timeout") or 30.0)
    except (TypeError, ValueError):
        raw_timeout = 30.0
    timeout = max(30.0, raw_timeout)

    if target not in TRANSPORTS:
        return JSONResponse(
            {"ok": False, "error": f"unknown target {target!r}; available: {list(TRANSPORTS)}"},
            status_code=400,
        )
    new_url = TRANSPORTS[target]
    if new_url == CHANNEL_BASE:
        return JSONResponse({"ok": True, "noop": True, "active": target})

    async with transport_lock:
        old_url = CHANNEL_BASE
        transport_paused = True
        try:
            # Step 3: capture session_id (may be None if old has no /session/current)
            sid: Optional[str] = None
            try:
                async with httpx.AsyncClient(timeout=3) as h:
                    r = await h.get(f"{old_url}/session/current")
                    if r.status_code == 200:
                        sid = r.json().get("session_id")
            except Exception:
                pass

            # Step 4: release old. Non-2xx (other than 404) aborts the switch.
            try:
                async with httpx.AsyncClient(timeout=timeout + 5) as h:
                    rr = await h.post(f"{old_url}/lease/release", json={"timeout": timeout})
                if rr.status_code == 404:
                    pass  # passive transport (no lease) — fine
                elif rr.status_code != 200:
                    return JSONResponse(
                        {
                            "ok": False,
                            "error": "release failed",
                            "detail": _safe_body(rr),
                            "release_status": rr.status_code,
                            "old": old_url,
                        },
                        status_code=502,
                    )
            except Exception as e:
                return JSONResponse(
                    {"ok": False, "error": f"release error: {type(e).__name__}: {e}", "old": old_url},
                    status_code=502,
                )

            # Step 5: acquire new. Five paths: 200, 200+restart_pending, 404,
            # non-2xx, exception. The fire-and-forget restart pattern lives in
            # the 200+restart_pending and exception paths.
            try:
                async with httpx.AsyncClient(timeout=timeout + 5) as h:
                    r = await h.post(
                        f"{new_url}/lease/acquire",
                        json={"resume": sid, "timeout": timeout},
                    )
                if r.status_code == 404:
                    pass  # passive ready — fine
                elif r.status_code != 200:
                    rollback = await _rollback_release_then_reacquire(new_url, old_url, sid)
                    return JSONResponse(
                        {
                            "ok": False,
                            "error": "acquire failed",
                            "detail": _safe_body(r),
                            "session_id": sid,
                            **rollback,
                        },
                        status_code=502,
                    )
                else:
                    try:
                        resp_body = r.json()
                    except Exception:
                        resp_body = {}
                    if resp_body.get("restart_pending"):
                        # The new transport dispatched a process restart and is
                        # about to be killed. Poll /session/current until ready.
                        # 40s default: claude --resume can take 25s+ on cold cache.
                        poll_timeout = max(40, timeout)
                        ready, last_err = await _poll_until_ready(new_url, sid, poll_timeout)
                        if not ready:
                            rollback = await _rollback_release_then_reacquire(new_url, old_url, sid)
                            return JSONResponse(
                                {
                                    "ok": False,
                                    "error": f"new transport not ready after {poll_timeout}s poll",
                                    "session_id": sid,
                                    "last_poll_error": last_err,
                                    **rollback,
                                },
                                status_code=504,
                            )
            except Exception as e:
                # Exception path. May be an HTTP-level error (e.g. RemoteProtocolError)
                # from a write race when the channel self-killed mid-response.
                # If sid is known, give the poll a chance before declaring failure —
                # the channel may have already dispatched the restart even though
                # the HTTP response never reached us.
                exc_err = f"{type(e).__name__}: {e}"
                if sid:
                    poll_timeout = max(40, timeout)
                    ready, last_err = await _poll_until_ready(new_url, sid, poll_timeout)
                    if not ready:
                        rollback = await _rollback_release_then_reacquire(new_url, old_url, sid)
                        return JSONResponse(
                            {
                                "ok": False,
                                "error": "acquire exception + poll timeout",
                                "session_id": sid,
                                "exception": exc_err,
                                "last_poll_error": last_err,
                                **rollback,
                            },
                            status_code=504,
                        )
                    # fall through into commit
                else:
                    # No sid -> no restart_pending pattern possible, but the new
                    # transport may still hold the lock from a half-completed
                    # acquire (HTTP connect failed AFTER channel grabbed flock).
                    # Must release new before reacquiring old — same reasoning
                    # as the sid path (review-trail round 4: zombie holder).
                    rollback = await _rollback_release_then_reacquire(new_url, old_url, sid)
                    return JSONResponse(
                        {
                            "ok": False,
                            "error": f"acquire exception: {exc_err}",
                            "session_id": sid,
                            **rollback,
                        },
                        status_code=502,
                    )

            # Step 6: commit. Order matters: CHANNEL_BASE first, then callbacks.
            CHANNEL_BASE = new_url
            if _reconfigure_callback is not None:
                try:
                    _reconfigure_callback(new_url)
                except Exception as e:
                    # Do not undo a successful switch. The new transport holds the
                    # lock; rolling back at this point would corrupt state.
                    print(
                        f"[ariake] reconfigure_callback raised "
                        f"({type(e).__name__}: {e}); continuing",
                        flush=True,
                    )
            if _sse_restart_callback is not None:
                try:
                    maybe = _sse_restart_callback()
                    if asyncio.iscoroutine(maybe):
                        await maybe
                except Exception as e:
                    print(
                        f"[ariake] sse_restart_callback raised "
                        f"({type(e).__name__}: {e}); continuing",
                        flush=True,
                    )
            print(
                f"[ariake] switched to {target} ({new_url}) resume={sid}",
                flush=True,
            )
        finally:
            # Step 7: unpause unconditionally
            transport_paused = False

    return JSONResponse(
        {"ok": True, "active": target, "channel_base": new_url, "resumed_session_id": sid}
    )


# ── Helpers ────────────────────────────────────────────────────────────
def _safe_body(r: httpx.Response) -> Any:
    """Best-effort decode of an httpx response body for error reporting."""
    try:
        return r.json()
    except Exception:
        return r.text[:200]


async def _poll_until_ready(
    new_url: str, sid: Optional[str], poll_timeout: float
) -> tuple[bool, Optional[str]]:
    """Poll new_url/session/current until session_id matches sid AND active=True,
    or timeout. Connection refused while polling = transport still restarting,
    keep going. Returns (ready, last_error_or_None)."""
    deadline = asyncio.get_event_loop().time() + poll_timeout
    last_err: Optional[str] = None
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(1.5)
        try:
            async with httpx.AsyncClient(timeout=3) as hp:
                rp = await hp.get(f"{new_url}/session/current")
            if rp.status_code == 200:
                info = rp.json()
                if info.get("session_id") == sid and info.get("active"):
                    return True, None
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
    return False, last_err


async def _rollback_release_then_reacquire(
    new_url: str, old_url: str, sid: Optional[str]
) -> dict:
    """Rollback a failed acquire. MUST release new before reacquiring old.

    Why: the fire-and-forget restart pattern can have the new transport still
    holding its lock even when it returned non-2xx — e.g. systemctl exited 0
    but systemd hasn't actually killed the process yet, so the file lock is
    still owned by it. Reacquire-on-old without release-on-new would deadlock
    on the cross-process lock.

    Best-effort: if `new` is unreachable, OS will release the fd on the
    eventual process death. We just need to not block on it.

    Returns {rollback_ok, reacquire_old_status, reacquire_old_error}.
    """
    # Best-effort release new
    try:
        async with httpx.AsyncClient(timeout=8) as h_rel:
            await h_rel.post(f"{new_url}/lease/release", json={"timeout": 5})
    except Exception:
        pass

    # Reacquire old
    status: Optional[int] = None
    err: Any = None
    try:
        async with httpx.AsyncClient(timeout=15) as h2:
            ra = await h2.post(f"{old_url}/lease/acquire", json={"resume": sid})
            status = ra.status_code
            if ra.status_code != 200:
                err = _safe_body(ra)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"

    ok = status == 200
    return {
        "rollback_ok": ok,
        "reacquire_old_status": status,
        "reacquire_old_error": err if not ok else None,
    }
