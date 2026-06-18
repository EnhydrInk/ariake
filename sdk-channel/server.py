#!/usr/bin/env python3
"""
Ariake SDK transport — sdk-channel/server.py
--------------------------------------------
Drives a `claude` binary via `claude-agent-sdk` and exposes the same HTTP
contract as the tmux transport (channel/server.ts). Backend can switch between
them at runtime by changing which URL it talks to.

Wire (identical to tmux transport):
  HTTP POST /message  -> build_envelope(<channel source="...">) -> client.query()
  CC calls reply tool -> in-process MCP server broadcasts to SSE /events
  HTTP POST /tick     -> verbatim envelope (timers / heartbeats; host pre-builds the tag)
  POST /lease/{acquire,release}, GET /session/current — lease protocol

Differences from tmux transport (documented; not bugs):
  - Inbound is stdin/stdout JSON-RPC, not send-keys — no modal-eat-input risk.
  - cwd MUST match the tmux transport's cwd so the two share one jsonl pool
    and one lock file.

See docs/architecture.md for the contract and docs/review-trail.md for the
five-round review history that produced the current shape.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

import claude_agent_sdk as csdk
from claude_agent_sdk import (
    AssistantMessage,
    TextBlock,
    ThinkingBlock,
    ResultMessage,
    SystemMessage,
    tool,
    create_sdk_mcp_server,
)


# ── Config ────────────────────────────────────────────────────────────
PORT = int(os.environ.get("INKIERAN_SDK_PORT", "8911"))
# CWD = `claude` cwd. MUST match the tmux transport's cwd for shared jsonl pool
# AND for shared .lease.lock file. Don't change one without the other.
CWD = os.environ.get("INKIERAN_SDK_CWD", "/opt/ariake/cc-cwd")
# BACKEND_BASE — host backend that wants sidecar (CoT / asides) reports. Optional.
# Empty -> skip sidecar entirely.
BACKEND_BASE = os.environ.get("INKIERAN_BACKEND", "").strip()
SOURCE_NAME = os.environ.get("INKIERAN_SOURCE", "ariake")
UPLOADS_DIR = os.environ.get("INKIERAN_UPLOADS", "/var/lib/ariake/uploads")
EVENT_RING_MAX = 100
# Lock file: defaults to ${CWD}/.lease.lock. MUST be the same path the tmux
# transport uses for cross-impl interlock to work.
LEASE_LOCK_PATH = os.environ.get("INKIERAN_LEASE_LOCK_PATH") or os.path.join(CWD, ".lease.lock")

# claude-agent-sdk permission_mode. dontAsk = anything not in allowed_tools is
# auto-denied (no interactive prompt that would hang the client_lock). For full
# Bash/Read/Write/Edit + external MCP, set bypassPermissions explicitly via env
# — that ACCEPTS prompt-injection risk from user input / external MCP returns.
_PERMISSION_MODE_VALID = {"default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"}
_PERMISSION_MODE_RAW = os.environ.get("INKIERAN_SDK_PERMISSION_MODE", "dontAsk").strip()
if _PERMISSION_MODE_RAW not in _PERMISSION_MODE_VALID:
    # Bad config = warn loudly; fall back to safe mode. Don't basicConfig at
    # import time — it pins root logger level WARNING and silences module INFO.
    logging.getLogger("ariake-sdk-channel").warning(
        f"INKIERAN_SDK_PERMISSION_MODE={_PERMISSION_MODE_RAW!r} invalid "
        f"(valid: {_PERMISSION_MODE_VALID}); falling back to 'dontAsk'"
    )
    _PERMISSION_MODE_RAW = "dontAsk"
PERMISSION_MODE = _PERMISSION_MODE_RAW

# System prompt = the personality / instructions CC runs with. setting_sources=[]
# below also disables CLAUDE.md loading, so this env is the ONLY way to give CC
# a personality on the SDK transport. Empty -> CC runs naked (still has the
# reply MCP tool, no other instructions).
SYSTEM_PROMPT_OVERRIDE = os.environ.get("INKIERAN_SDK_SYSTEM", "").strip() or None

log = logging.getLogger("ariake-sdk-channel")


# ── XML / sanitization (byte-identical to channel/server.ts) ──────────
def esc_xml(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def esc_attr(s: str) -> str:
    return esc_xml(s).replace('"', "&quot;").replace("'", "&#39;")


def utc_iso_z() -> str:
    """Match the JS `new Date().toISOString()` format byte-for-byte:
    `2026-06-18T13:27:31.435Z`. Critical for envelope parity across transports.
    """
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ── ID generation ─────────────────────────────────────────────────────
_id_seq = 0


def next_id(prefix: str) -> str:
    global _id_seq
    _id_seq += 1
    return f"{prefix}{int(time.time() * 1000)}-{_id_seq}"


# ── SSE outbound (ring buffer + Last-Event-ID replay) ─────────────────
# id basis = current ms timestamp so ids stay monotonic across process restarts.
# Subscribers holding an old id from a previous process won't filter out new
# events.
_event_seq = int(time.time() * 1000)
event_ring: list[tuple[int, str]] = []
listeners: set[asyncio.Queue] = set()


def _next_event_id() -> int:
    global _event_seq
    _event_seq += 1
    return _event_seq


def broadcast(ev: dict) -> None:
    eid = _next_event_id()
    frame = f"id: {eid}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
    event_ring.append((eid, frame))
    if len(event_ring) > EVENT_RING_MAX:
        event_ring.pop(0)
    dead: list[asyncio.Queue] = []
    for q in listeners:
        try:
            q.put_nowait(frame)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        listeners.discard(q)


def replay_since(last_event_id: str | None) -> list[str]:
    """Replay events newer than last_event_id. If the id is greater than our
    max (subscriber holds a stale id from a previous process), replay all."""
    if not last_event_id:
        return []
    try:
        last = int(last_event_id)
    except ValueError:
        return []
    max_id = event_ring[-1][0] if event_ring else 0
    if last > max_id:
        return [f for _, f in event_ring]
    return [f for i, f in event_ring if i > last]


# ── Envelope (byte-identical to channel/server.ts deliver()) ──────────
def build_envelope(
    chat_id: str,
    message_id: str,
    text: str,
    attachments: list[dict] | None,
    user: str,
) -> str:
    """Build the inbound <channel> tag. MUST produce byte-identical output to
    channel/server.ts deliver() for the same inputs — anything else means the
    same input switches behavior depending on which transport routed it."""
    attach_prefix = " ".join(
        f"[attachment {i + 1} -> {a.get('path', '')}]"
        for i, a in enumerate(attachments or [])
    )
    merged = " ".join(p for p in [attach_prefix, text] if p)
    # Strip \n -> space: legacy from send-keys hardening; preserved for byte
    # parity with the tmux transport. Even though the SDK doesn't need single-
    # line input, switching transports mid-conversation must not change envelope
    # bytes for the same input.
    safe = esc_xml(merged.replace("\n", " "))
    ts = utc_iso_z()
    return (
        f'<channel source="{SOURCE_NAME}" chat_id="{esc_attr(chat_id)}" '
        f'message_id="{esc_attr(message_id)}" user="{esc_attr(user)}" '
        f'ts="{esc_attr(ts)}">{safe}</channel>'
    )


# ── In-process MCP server: reply tool ─────────────────────────────────
REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "chat_id": {"type": "string", "description": "Conversation to reply in (from the inbound tag)."},
        "text": {"type": "string", "description": "Message text (may be empty when only sending attachments)."},
        "reply_to": {"type": "string", "description": "Inbound message_id this answers (optional)."},
        "attachments": {
            "type": "array",
            "description": f"Files to send back. Save into {UPLOADS_DIR} first, then list absolute paths.",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": f"Absolute path; must live inside {UPLOADS_DIR}."},
                    "filename": {"type": "string", "description": "Display name (optional)."},
                    "type": {
                        "type": "string",
                        "enum": ["image", "file"],
                        "description": "Optional; inferred from extension.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    "required": ["chat_id"],
}


@tool(
    "reply",
    "Send a message back to the inbound user. Pass chat_id (from the inbound <channel> tag) "
    "to route; optional reply_to to thread; optional attachments. "
    f"To send files, first save them to {UPLOADS_DIR}, then list their absolute paths.",
    REPLY_SCHEMA,
)
async def reply_tool(args: dict):
    text = str(args.get("text") or "")
    chat_id = str(args.get("chat_id") or "") or "default"
    reply_to = args.get("reply_to")
    attachments = args.get("attachments") or []
    if not isinstance(attachments, list):
        attachments = []
    if not text.strip() and not attachments:
        return {
            "content": [{"type": "text", "text": "reply: text and attachments cannot both be empty"}],
            "isError": True,
        }
    message_id = next_id("a")
    payload = {
        "type": "reply",
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "ts": utc_iso_z(),
        "attachments": attachments,
    }
    if reply_to:
        payload["reply_to"] = str(reply_to)
    broadcast(payload)
    return {"content": [{"type": "text", "text": f"sent ({message_id}) -> {len(listeners)} listener(s)"}]}


ariake_mcp = create_sdk_mcp_server("ariake", tools=[reply_tool])


# ── External MCP servers (optional) ───────────────────────────────────
# Load mcpServers from ~/.claude.json (user-level) + ${CWD}/.mcp.json
# (project-level) so the SDK transport can use MCP tools registered elsewhere.
# setting_sources=[] disables the CLI's automatic loader; this is a manual
# rebuild that only touches mcpServers (NOT hooks / preferences / history),
# preserving the Stop-hook isolation that setting_sources=[] is for.

def _validate_mcp_entry(name: str, entry, source_label: str) -> bool:
    """Schema-validate one mcpServers entry. Bad entries are warned + skipped.
    By type:
      stdio (default): command: str required, args: list[str] / env: dict[str,str] optional
      http / sse: url: str required, headers: dict[str,str] optional
      anything else: skip with warning
    """
    if not isinstance(entry, dict):
        log.warning(f"{source_label}: entry {name!r} not a dict, skipped")
        return False
    entry_type = entry.get("type")
    if entry_type in (None, "stdio"):
        if not isinstance(entry.get("command"), str):
            log.warning(f"{source_label}: stdio entry {name!r} missing string `command`, skipped")
            return False
        if "args" in entry:
            args = entry.get("args")
            if not (isinstance(args, list) and all(isinstance(a, str) for a in args)):
                log.warning(f"{source_label}: entry {name!r} `args` is not list[str], skipped")
                return False
        if "env" in entry:
            env = entry.get("env")
            if not (isinstance(env, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())):
                log.warning(f"{source_label}: entry {name!r} `env` is not dict[str,str], skipped")
                return False
    elif entry_type in ("http", "sse"):
        if not isinstance(entry.get("url"), str):
            log.warning(f"{source_label}: {entry_type} entry {name!r} missing string `url`, skipped")
            return False
        if "headers" in entry:
            headers = entry.get("headers")
            if not (
                isinstance(headers, dict)
                and all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items())
            ):
                log.warning(f"{source_label}: entry {name!r} `headers` is not dict[str,str], skipped")
                return False
    else:
        log.warning(f"{source_label}: entry {name!r} type={entry_type!r} unknown, skipped")
        return False
    return True


def _load_mcp_from_file(path: str) -> dict:
    """Read the `mcpServers` field from a CC-format JSON config. Skip the
    `ariake` entry (already provided in-process). Graceful on malformed JSON."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            cfg = json.load(f)
    except Exception as e:
        log.warning(f"failed to load {path}: {e}")
        return {}
    if not isinstance(cfg, dict):
        log.warning(f"{path}: top-level not a dict, ignored")
        return {}
    servers = cfg.get("mcpServers")
    if not isinstance(servers, dict):
        if servers is not None:
            log.warning(f"{path}: 'mcpServers' not a dict (got {type(servers).__name__}), ignored")
        return {}
    out: dict = {}
    for name, entry in servers.items():
        if name == "ariake":  # already in-process
            continue
        if _validate_mcp_entry(name, entry, path):
            out[name] = entry
    return out


def _load_external_mcp_servers() -> dict:
    """user-level (~/.claude.json) + project-level (${CWD}/.mcp.json), merged.
    Project-level wins on name conflict. Returns {} if neither file exists."""
    user_path = os.path.expanduser("~/.claude.json")
    project_path = os.path.join(CWD, ".mcp.json")
    user_mcp = _load_mcp_from_file(user_path)
    project_mcp = _load_mcp_from_file(project_path)
    if user_mcp:
        log.info(f"loaded {len(user_mcp)} user-level MCP from {user_path}: {list(user_mcp.keys())}")
    if project_mcp:
        log.info(f"loaded {len(project_mcp)} project-level MCP from {project_path}: {list(project_mcp.keys())}")
    return {**user_mcp, **project_mcp}


# ── ClaudeSDKClient + lease + admission ───────────────────────────────
# Three locks, in nesting order:
#   state_lock     wraps the whole lease change (release/acquire mutex)
#   admission_lock atomic is_active check + in_flight inc/dec (held briefly)
#   client_lock    serializes turns (held = currently writing session jsonl)
# Nesting order (deadlock-safe): state -> admission(short) -> drain_event(no lock) -> client -> admission(short)

def build_options(resume: str | None = None) -> csdk.ClaudeAgentOptions:
    return csdk.ClaudeAgentOptions(
        # WARNING: setting_sources=[] disables filesystem settings loading
        # including the user/project CLAUDE.md path. For CC to have any
        # personality on this transport, INKIERAN_SDK_SYSTEM must be set with
        # the inline system prompt. Empty -> CC runs naked.
        system_prompt=SYSTEM_PROMPT_OVERRIDE,
        # External MCP + in-process ariake. project-level loader puts itself
        # AFTER ariake here so ariake never gets clobbered by an accidental
        # name collision in .mcp.json.
        mcp_servers={**_load_external_mcp_servers(), "ariake": ariake_mcp},
        allowed_tools=["mcp__ariake__reply"],
        # default permission_mode = 'dontAsk' (any tool not in allowed_tools is
        # auto-denied, never prompts — important: prompts would hang client_lock
        # since there's no human to answer them).
        permission_mode=PERMISSION_MODE,
        include_partial_messages=False,
        cwd=CWD,
        resume=resume,
        # Plugin/hook isolation (three-piece set):
        #   plugins=[]               — no SDK plugin discovery
        #   setting_sources=[]       — no settings.json loader (Stop hooks etc.
        #                              would otherwise run alongside our in-
        #                              process sidecar capture)
        #   strict_mcp_config=True   — trust only the explicit mcp_servers dict
        plugins=[],
        setting_sources=[],
        strict_mcp_config=True,
    )


client: csdk.ClaudeSDKClient | None = None
client_lock = asyncio.Lock()
state_lock = asyncio.Lock()
admission_lock = asyncio.Lock()
current_session_id: str | None = None
is_active: bool = True
in_flight: int = 0
drain_event = asyncio.Event()
drain_event.set()  # start with no work in flight

# Cross-process lease fd. Keeping the fd open = holding the lock. Closing the
# fd (or process death) automatically releases it (OS guarantee) — no stale
# locks to clean up.
_lease_lock_fd: int | None = None


class AdmissionTicket:
    """One-shot ticket. Idempotent release_sync so the dual release paths
    (turn's finally block + Task.add_done_callback) don't double-decrement.

    release_sync is NOT async — it runs under the single asyncio event loop,
    so the int/Event mutations inside are atomic without an additional lock.
    """

    __slots__ = ("_released",)

    def __init__(self) -> None:
        self._released = False

    def release_sync(self) -> None:
        global in_flight
        if self._released:
            return
        self._released = True
        in_flight -= 1
        if in_flight <= 0:
            in_flight = 0
            drain_event.set()


async def _admit() -> AdmissionTicket | None:
    """Return a ticket on success, None if standby. Endpoint MUST guarantee
    that a non-None ticket is eventually released (use _spawn_with_admission)."""
    global in_flight
    async with admission_lock:
        if not is_active:
            return None
        in_flight += 1
        drain_event.clear()
        return AdmissionTicket()


# Strong reference set: asyncio.Task is only weakly held by create_task; without
# a strong reference it can be GC'd before completion (and its done_callback
# never fires, leaking the in_flight ticket).
_background_tasks: set[asyncio.Task] = set()


def _spawn_with_admission(coro_factory, ticket: AdmissionTicket) -> asyncio.Task:
    """create_task + done_callback ensures the ticket is released even if the
    coroutine is cancelled before its body runs (the coroutine's finally block
    wouldn't fire in that case). release_sync's idempotency guard prevents
    double-decrement when both paths fire on the normal path."""
    task = asyncio.create_task(coro_factory())
    _background_tasks.add(task)

    def _on_done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        ticket.release_sync()

    task.add_done_callback(_on_done)
    return task


async def _connect_client(resume_sid: str | None = None) -> None:
    """Build + connect a new client. Build a local variable first; only assign
    to the global `client` on success. Failure path disconnects the half-built
    client and clears current_session_id so we don't leave a stale sid for the
    coordinator's poll to match against."""
    global client, current_session_id
    if client is not None:
        try:
            await client.disconnect()
        except Exception:
            pass
        client = None
        current_session_id = None
    new_client = csdk.ClaudeSDKClient(options=build_options(resume=resume_sid))
    try:
        await new_client.connect()
    except Exception:
        try:
            await new_client.disconnect()
        except Exception:
            pass
        current_session_id = None
        raise
    client = new_client
    # Placeholder until the next ResultMessage's session_id corrects it. For a
    # fresh session, this stays None until CC writes the first turn.
    current_session_id = resume_sid
    log.info(f"ClaudeSDKClient connected; resume={resume_sid!r}")


async def _disconnect_client() -> None:
    global client
    if client is not None:
        try:
            await client.disconnect()
        except Exception as e:
            log.warning(f"disconnect error (ignored): {e}")
        client = None


def _try_acquire_lease_lock() -> str:
    """Non-blocking acquire of the cross-process file lock.

    Returns one of:
      'acquired'      — just acquired
      'already_held'  — this process already holds it (idempotent no-op)
      'denied'        — another process holds it
    """
    global _lease_lock_fd
    if _lease_lock_fd is not None:
        return "already_held"
    try:
        fd = os.open(LEASE_LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as e:
        log.error(f"lease lock open failed at {LEASE_LOCK_PATH}: {e}")
        raise
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return "denied"
    _lease_lock_fd = fd
    log.info(f"lease lock acquired ({LEASE_LOCK_PATH})")
    return "acquired"


def _release_lease_lock() -> None:
    global _lease_lock_fd
    if _lease_lock_fd is None:
        return
    try:
        fcntl.flock(_lease_lock_fd, fcntl.LOCK_UN)
    except OSError as e:
        log.warning(f"lease lock unlock error (ignored): {e}")
    try:
        os.close(_lease_lock_fd)
    except OSError:
        pass
    _lease_lock_fd = None
    log.info("lease lock released")


# ── Optional sidecar (CoT / asides reporter) ──────────────────────────
# If INKIERAN_BACKEND is set, every turn flushes ThinkingBlock + TextBlock
# contents to {BACKEND_BASE}/api/sidecar. Empty BACKEND_BASE -> no-op.
async def _post_sidecar(
    chat_id: str, thinking: str, asides: list[str], session_id: str | None
) -> None:
    if not BACKEND_BASE:
        return
    if not thinking and not asides:
        return
    payload = {
        "chat_id": chat_id,
        "thinking": thinking or None,
        "asides": asides,
        "session_id": session_id,
    }
    try:
        async with httpx.AsyncClient(timeout=3.0) as h:
            await h.post(f"{BACKEND_BASE}/api/sidecar", json=payload)
    except Exception as e:
        log.warning(f"sidecar post failed: {e}")


# ── Turn runner ───────────────────────────────────────────────────────
async def _run_turn(envelope: str, chat_id: str, ticket: AdmissionTicket) -> None:
    """One complete turn:
      query -> receive_response -> harvest thinking/text blocks -> on
      ResultMessage, flush sidecar.

    The ticket is double-pathed: release in `finally` (normal completion) AND
    via add_done_callback (cancel-before-body). release_sync is idempotent so
    both paths firing is safe.
    """
    global current_session_id
    try:
        async with client_lock:
            if client is None:
                log.warning("turn dropped: client is None (lease released?)")
                return
            thinking_parts: list[str] = []
            asides: list[str] = []
            try:
                await client.query(envelope)
                async for msg in client.receive_response():
                    if isinstance(msg, SystemMessage) and msg.subtype == "init":
                        current_session_id = msg.data.get("session_id")
                    elif isinstance(msg, AssistantMessage):
                        for blk in msg.content:
                            if isinstance(blk, ThinkingBlock):
                                t = (blk.thinking or "").strip()
                                if t:
                                    thinking_parts.append(t)
                            elif isinstance(blk, TextBlock):
                                t = (blk.text or "").strip()
                                if t:
                                    asides.append(t)
                            # ToolUseBlock not captured here: the reply tool
                            # callback already broadcasts to SSE.
                    elif isinstance(msg, ResultMessage):
                        # ResultMessage.session_id is the SDK's contract value,
                        # more reliable than init's session_id. Don't `break` —
                        # let the generator stop iteration naturally. Breaking
                        # leaves the SDK transport state stuck in "not ready for
                        # writing" and the NEXT turn will throw.
                        sid = getattr(msg, "session_id", None)
                        if sid:
                            current_session_id = sid
            except Exception as e:
                log.error(f"turn failed: {type(e).__name__}: {e}")
                return
            thinking_joined = "\n\n".join(thinking_parts)
            await _post_sidecar(chat_id, thinking_joined, asides, current_session_id)
    finally:
        # Idempotent. add_done_callback will also fire and call this; the
        # second call is a no-op due to the _released guard.
        ticket.release_sync()


# ── Lifespan ──────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """AUTOSTART=1 (default): acquire the file lock + connect the client.
    AUTOSTART=0: standby. Wait for /lease/acquire to promote us."""
    global is_active
    autostart = os.environ.get("INKIERAN_SDK_AUTOSTART", "1") != "0"
    if autostart:
        lock_state = _try_acquire_lease_lock()
        if lock_state == "denied":
            is_active = False
            log.warning(
                f"lease lock held by another process at {LEASE_LOCK_PATH}; "
                f"falling back to standby. Coordinator can promote via /lease/acquire "
                f"once the holder releases."
            )
        else:
            await _connect_client()
            is_active = True
    else:
        is_active = False
        log.info("standby mode: waiting for /lease/acquire")
    yield
    await _disconnect_client()
    _release_lease_lock()


app = FastAPI(lifespan=lifespan)


# ── HTTP endpoints ────────────────────────────────────────────────────
@app.post("/message")
async def post_message(req: Request):
    """Inbound user message. Byte-equivalent to channel POST /message."""
    try:
        body = await req.json()
    except Exception:
        body = {}
    text = str(body.get("text") or "")
    chat_id = str(body.get("chat_id") or "default")
    user = str(body.get("user") or "user")
    attachments = body.get("attachments") or []
    if not isinstance(attachments, list):
        attachments = []
    if not text.strip() and not attachments:
        return JSONResponse({"error": "empty message"}, status_code=400)
    # Admission inside the endpoint, BEFORE awaiting anything that could be
    # raced by /lease/release. Once a ticket exists, release must drain on it.
    ticket = await _admit()
    if ticket is None:
        return JSONResponse({"error": "lease released; transport idle"}, status_code=503)
    message_id = next_id("u")
    envelope = build_envelope(chat_id, message_id, text.strip(), attachments, user)
    _spawn_with_admission(lambda: _run_turn(envelope, chat_id, ticket), ticket)
    return JSONResponse({"ok": True, "chat_id": chat_id, "message_id": message_id}, status_code=202)


@app.post("/tick")
async def post_tick(req: Request):
    """Verbatim envelope (timers / heartbeats). Host has pre-built the full tag.
    No SSE broadcast, no sidecar — ticks are not user messages."""
    try:
        body = await req.json()
    except Exception:
        body = {}
    payload = str(body.get("payload") or "").replace("\n", " ").strip()
    if not payload:
        return JSONResponse({"error": "empty payload"}, status_code=400)
    ticket = await _admit()
    if ticket is None:
        return JSONResponse({"error": "lease released; transport idle"}, status_code=503)

    async def _tick():
        try:
            async with client_lock:
                if client is None:
                    log.warning("tick dropped: client is None")
                    return
                try:
                    await client.query(payload)
                    async for _ in client.receive_response():
                        pass  # drain to end-of-turn; see _run_turn comment on no-break
                except Exception as e:
                    log.error(f"tick failed: {type(e).__name__}: {e}")
        finally:
            ticket.release_sync()

    _spawn_with_admission(_tick, ticket)
    return JSONResponse({"ok": True}, status_code=202)


# ── Lease ──────────────────────────────────────────────────────────────
DEFAULT_DRAIN_TIMEOUT = float(os.environ.get("INKIERAN_SDK_DRAIN_TIMEOUT", "30"))


async def _drain_admitted(timeout: float = DEFAULT_DRAIN_TIMEOUT) -> bool:
    """Wait for in_flight to reach 0. Returns False on timeout (so the caller
    can rollback admission instead of hanging forever)."""
    try:
        await asyncio.wait_for(drain_event.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False


@app.post("/lease/release")
async def lease_release(req: Request):
    """Stop accepting -> drain -> disconnect client -> release file lock.
    On drain timeout: rollback is_active, KEEP the lock, return 408. The
    coordinator should treat 408 as "this transport is stuck" and either
    retry with a larger timeout or abandon the switch."""
    body = {}
    try:
        body = await req.json()
    except Exception:
        pass
    timeout = (
        float(body.get("timeout", DEFAULT_DRAIN_TIMEOUT))
        if isinstance(body, dict)
        else DEFAULT_DRAIN_TIMEOUT
    )
    global is_active
    async with state_lock:
        async with admission_lock:
            was_active = is_active
            is_active = False
        drained = await _drain_admitted(timeout=timeout)
        if not drained:
            async with admission_lock:
                if was_active:
                    is_active = True
            return JSONResponse(
                {"ok": False, "error": "drain timeout", "in_flight": in_flight, "timeout": timeout},
                status_code=408,
            )
        async with client_lock:
            await _disconnect_client()
        # Release the lock LAST. If we released before disconnect, the other
        # transport could acquire while our client was still writing.
        _release_lease_lock()
    return JSONResponse({"ok": True, "active": False, "in_flight": in_flight})


@app.post("/lease/acquire")
async def lease_acquire(req: Request):
    """Acquire the lease and connect (optionally resuming a session).

    If currently active (re-acquire): drain in-flight first, then swap client.
    Cross-process lock: standby -> active path tries to acquire the file lock;
    if held by another process, return 409 (the coordinator should release the
    other holder first).

    On connect failure: release the file lock if we just acquired it (don't
    release on already_held — that means the lock was already in our hand and
    didn't get freshly grabbed by this call).
    """
    body = {}
    try:
        body = await req.json()
    except Exception:
        pass
    resume_sid = body.get("resume") if isinstance(body, dict) else None
    timeout = (
        float(body.get("timeout", DEFAULT_DRAIN_TIMEOUT))
        if isinstance(body, dict)
        else DEFAULT_DRAIN_TIMEOUT
    )
    global is_active
    async with state_lock:
        async with admission_lock:
            was_active = is_active
            is_active = False
        if was_active:
            drained = await _drain_admitted(timeout=timeout)
            if not drained:
                async with admission_lock:
                    is_active = True
                return JSONResponse(
                    {"ok": False, "error": "drain timeout during acquire", "in_flight": in_flight},
                    status_code=408,
                )
        lock_state = _try_acquire_lease_lock()
        if lock_state == "denied":
            async with admission_lock:
                if was_active:
                    is_active = True
            return JSONResponse(
                {
                    "ok": False,
                    "error": "lease lock held by another process",
                    "lock_path": LEASE_LOCK_PATH,
                    "in_flight": in_flight,
                },
                status_code=409,
            )
        try:
            async with client_lock:
                await _connect_client(resume_sid)
        except Exception as e:
            log.error(f"acquire failed: {type(e).__name__}: {e}")
            # Only release if WE just acquired the lock. already_held = it was
            # ours before this call; leave it alone.
            if lock_state == "acquired":
                _release_lease_lock()
            return JSONResponse(
                {"ok": False, "error": f"connect failed: {type(e).__name__}: {e}"},
                status_code=500,
            )
        async with admission_lock:
            is_active = True
    return JSONResponse({"ok": True, "active": True, "resumed": resume_sid, "lock": lock_state})


@app.get("/session/current")
async def session_current():
    return JSONResponse({"session_id": current_session_id, "active": is_active})


@app.get("/events")
async def get_events(req: Request):
    """SSE outbound. Supports Last-Event-ID replay; stale ids (greater than
    our max, i.e. from a previous process) trigger a full ring replay."""
    last_id = req.headers.get("last-event-id")
    q: asyncio.Queue = asyncio.Queue(maxsize=512)
    listeners.add(q)

    async def gen():
        try:
            yield ": connected\n\n"
            for frame in replay_since(last_id):
                yield frame
            while True:
                if await req.is_disconnected():
                    break
                try:
                    frame = await asyncio.wait_for(q.get(), timeout=30.0)
                    yield frame
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"  # defeat intermediary idle timeouts
        finally:
            listeners.discard(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "cache-control": "no-cache",
            "connection": "keep-alive",
            "x-accel-buffering": "no",
        },
    )


@app.get("/")
async def health():
    return {
        "service": "ariake-sdk-channel",
        "port": PORT,
        "cwd": CWD,
        "listeners": len(listeners),
        "session_id": current_session_id,
        "active": is_active,
        "in_flight": in_flight,
        "lease_lock_path": LEASE_LOCK_PATH,
        "lease_lock_held": _lease_lock_fd is not None,
        "permission_mode": PERMISSION_MODE,
        "sidecar_target": BACKEND_BASE or None,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
