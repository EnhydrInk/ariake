# Proposal — Adding a third transport (`-p`), and clearing a path to a fourth (`api`)

Status: draft v3 (revised after round-2 review). Round 2 called this the last pass; if round-3 has no showstoppers this is the shape we implement.
Depends on: [prompt-family.md](./prompt-family.md) (also at v3).
Scope: introduce `-p` as a third transport, with the design framed so a fourth (direct Anthropic API) can be added later without further protocol changes.

## Round-1 review debt paid

Round 1 raised five findings. This v2 is written against them explicitly:

- **P0 (SDK ≠ zero-tools):** the previous "minimal = no tools" definition was incompatible with the SDK transport's existing `reply` MCP tool. v2 redefines `minimal` as *"one Anthropic Messages API request shape that includes a specific `reply` tool schema"*, so SDK's reply tool is not a violation of the family but a required member of it. See "MINIMAL as a request shape" below and [prompt-family.md](./prompt-family.md) §"What the family actually is".
- **P1 (turn-level lease unhooked):** v2 defines `POST /lease/borrow` as an explicit coordinator-orchestrated flow (`active pause → active release → borrower acquire/run/release → active reacquire`). Turn-level acquire is no longer described as a thing `-p` does behind the coordinator's back.
- **P1 (acquire contradiction):** v2 removes v1's ambiguity; the final semantics (refined once more in round 2) are: contention fails immediately with `409`, and the only wait is the bounded active-holder drain governed by `timeout`, matching the existing `/lease/acquire` drain. See the borrow state machine.
- **P1 (tmux telemetry):** v2 admits tmux does not surface a `ResultMessage` and reclassifies tmux cache observation as a separate future-work item (jsonl post-hoc parse or plugin hook), not part of the initial telemetry rollout.
- **P2 (SSE wire compatibility):** resolved — one complete `reply` SSE event per turn, produced by the reply-tool path exactly as on SDK. No deltas on the wire. (The first v2 draft answered this with wrapper-side buffer-to-final synthesis; round-2 review caught that this created a second, conflicting output path — see round-2 debt below.)

## Round-2 review debt paid

Round 2 (on the first v2 draft) raised five findings. This revision is written against them:

- **P0 (dual reply path):** the first draft had the wrapper synthesizing SSE from buffered `assistant` text *and* `reply_mcp` POSTing tool calls to the host — two producers, guaranteed duplicates or empty sends. Resolved: **the reply tool call is the single SSE producer.** `reply_mcp` bridges the tool call to the wrapper's `/internal/reply`; the wrapper broadcasts exactly one `reply` event and never parses assistant text into SSE. stream-json stdout is used only for `result` (telemetry) and lifecycle. This also restores structured `chat_id` routing on `-p` — CC fills the tool arguments from the envelope, same as SDK.
- **P0 (borrow not actually fail-fast):** the first draft said both "returns 200 once the flock is releasable" and "no blocking anywhere". Resolved with precise semantics: **contention is immediate-fail, drain is bounded-wait.** Kernel flock is always `LOCK_NB` (held → fail now); the borrow slot is single-occupancy (occupied → `409` now); draining the active holder's in-flight turn is a wait bounded by `timeout` (expiry → `408` + rollback), mirroring the drain semantics that already exist in `/lease/acquire`. No unbounded waits, no queues.
- **P1 (`--tools none` leftover):** removed from the rollout plan. `-p` runs with `--mcp-config` + `--allowedTools mcp__reply__reply`, nothing else.
- **P1 (family definition too narrow):** [prompt-family.md](./prompt-family.md) now pins `tool_choice`, thinking parameters, image policy, cache TTL, and fully-qualified MCP tool names, and documents the minimum-cacheable-prefix threshold with a guard against Level-1 false positives.
- **P1 (long-lived stdio MCP incoherent):** stdio MCP servers are spawned by the client per `--mcp-config` — there is no long-lived process to put under systemd. Resolved: `reply_mcp` is a per-invocation stdio shim that lives and dies with each `claude -p` run. The single-point-of-failure open question dissolves with it.

## Round-3 review debt paid

Round 2 (post-first-v2-draft) resolved five findings. A subsequent round called those fixes clean but raised four more — each of them a hidden invariant the earlier drafts glossed. This section is written against them:

- **P0 (`reply` tool result not pinned):** the round-2 draft pinned tool call → SSE and the `reply` tool schema, but not the string the tool *returns* to CC. That string becomes a `tool_result` block in the next turn's `messages` history; if SDK returns `sent (<message_id>) -> N listener(s)` (its current dynamic string, `sdk-channel/server.py:274`) and `-p`'s shim returns anything else, the `messages` history diverges between families and any downstream cache breakpoint on `messages` is invalidated. Resolved by adding `REPLY_TOOL_RESULT` (a fixed static string, no interpolations) to `shared_spec`, and marking the current SDK dynamic-string as a **phase 2 regression to fix**. See "MINIMAL as a request shape" and "Phase 2 SDK behaviour changes".
- **P0 (borrow-complete assumed simple for all active holders):** if the prior active is tmux, its CC process either survived the pause but has stale in-memory conversation history vs the just-mutated jsonl, or it needs a restart-and-resume that re-enters the `restart_pending` machinery documented in `docs/review-trail.md`. Neither is safe as a drive-by. Resolved by explicitly restricting phase 1 borrow to SDK-held sessions (`active.prompt_family == "full-cc"` → `409` at borrow-request time). Tmux participation in borrow is a phase 4+ item that will need its own round on review-trail. See "Phase 1 scope: SDK↔`-p` only".
- **P1 (reaper kill-tree targeted the wrong process):** the round-2 draft had the FastAPI dispatcher holding the flock and the reaper killing the `claude -p` subprocess tree — but killing a grandchild does not release an fd held in the grandparent. The lock would silently stay held. Resolved by splitting `p-channel` into a long-lived dispatcher and a **per-turn worker subprocess**; the worker owns the flock fd, `claude -p` is the worker's child, and the reaper targets the worker pid (registered via new `PUT /lease/borrow/pid`). Worker exit releases the lock through kernel fd cleanup. See "Process structure and reaper safety".
- **P1 (SDK attachments quietly disappear from minimal reply):** the round-2 minimal schema (`chat_id / text / reply_to`) is a strict subset of SDK's current schema (`sdk-channel/server.py:221`), which supports `attachments`. Bringing SDK into minimal-family alignment therefore drops attachment sends — a real feature loss that must not slip in unannounced. Resolved by naming it explicitly in "Phase 2 SDK behaviour changes" and framing it as a deliberate reduction: hosts that need attachments route those turns through tmux, or a follow-up defines `minimal-plus-attachments` as a second family with its own shared_spec.

## Summary

Add `p-channel/` — a third transport whose backing process is a fresh `claude -p` subprocess per turn. To make it participate in the lease and share prompt cache with the SDK transport, we first make one architectural admission:

> **What ariake actually shares across transports is not "a Claude Code session" — it is a specific Anthropic Messages API request shape.**
>
> The `session_id` is the axis of shared memory (unchanged from v1).
> A **prompt family** is now defined as a specific request shape — system prompt + tools array + cache_control breakpoint positions. Two transports are in the same family iff, given the same `session_id` and `messages` history, they produce byte-identical requests to Anthropic.

With this reframing:
- SDK and `-p` can share family membership by both emitting the same shape (specifically, both must present `reply` with a byte-identical tool schema).
- A direct Anthropic API transport (`api-channel/`, phase 3) trivially joins the same family, because it *is* the request shape by construction.
- tmux does not join — not because it's inferior, but because its whole reason for existing is to use CC's default assembled request shape (with the full CLAUDE.md chain and full toolkit).

The two families this repo ships are:
- **`full-cc`** — CC's default assembled shape. tmux only.
- **`minimal`** — the shape defined in `shared_spec.py` (system prompt string + `reply` tool schema). SDK, `-p`, and eventually `api-channel`.

The concept and its verification path live in [prompt-family.md](./prompt-family.md); this document is about the transport and its lease integration.

## MINIMAL as a request shape (P0 resolution)

The request shape that defines the `minimal` family is exactly:

```
POST https://api.anthropic.com/v1/messages
{
  "model": "<pinned>",
  "system": [
    {"type": "text", "text": "<MINIMAL_SYSTEM_PROMPT>", "cache_control": {"type": "ephemeral"}}
  ],
  "tools": [
    {"name": "mcp__reply__reply", "description": "...", "input_schema": {...}, "cache_control": {"type": "ephemeral"}}
  ],
  "messages": [ ...conversation history..., {"role": "user", "content": "..."} ]
}
```

Two cache_control breakpoints: end of system prompt, end of tools array. Every turn on the same session and same family byte-appends to `messages`, so the two cached prefixes stay live turn-to-turn.

The `reply` tool has three cache-load-bearing surfaces, all of which the family pins:
1. **Schema** — `name`, `description`, `input_schema` in the on-wire tools array.
2. **Server name** — MCP tool names are namespaced `mcp__<server>__<tool>`; the server name is part of the cached prefix.
3. **Tool result content** — what the tool returns after CC calls it. This becomes a `tool_result` block in the next turn's `messages` history, so if SDK returns `"sent (abc123) -> 3 listener(s)"` and `-p`'s shim returns `"delivered"`, the two produce different history and any downstream cache breakpoints on `messages` diverge. `shared_spec` pins the returned content to a **static string with no interpolated variables** (message_id, listener count, timestamp — all forbidden). The current `sdk-channel/server.py:274` dynamic return string is a **regression to fix** as part of phase 2 SDK alignment; see "Phase 2 SDK behaviour changes" below.

How each transport realizes these three surfaces:

| transport | how `reply` reaches CC | alignment enforcement |
|---|---|---|
| SDK | in-process MCP server (`create_sdk_mcp_server`) | `sdk-channel/server.py` imports schema, server name, AND result string from `shared_spec`; the tool decorator wraps a function that returns `REPLY_TOOL_RESULT` verbatim |
| `-p` | per-invocation stdio MCP shim, spawned by CC via `--mcp-config` | the shim (`reply_mcp/server.py`, new) imports the same three constants and returns `REPLY_TOOL_RESULT` verbatim after POSTing the args |
| api (future) | direct `tools=[...]` in the API request | `api-channel/server.py` imports from `shared_spec`, constructs the fully-qualified tool name, and after handling the `tool_use` block synthesizes the `tool_result` content as `REPLY_TOOL_RESULT` verbatim |

Note the server name matters (server-name mismatch → `mcp__ariake__reply` vs `mcp__reply__reply`, different bytes, family broken). Note the result string matters equally, and is easier to overlook because it is not visible in the request headed to Anthropic on the current turn — it only manifests as a mismatch on the *next* turn's history. This is the failure mode round-2 review flagged.

### Phase 2 SDK behaviour changes (minimal-family alignment)

Bringing SDK into the minimal family isn't purely additive; it costs three current behaviours:

- **Dynamic reply result string dropped.** Current `sdk-channel/server.py:274` returns `sent (<message_id>) -> N listener(s)`, useful in logs but a family-breaking variable. Replaced with `REPLY_TOOL_RESULT` (constant string). Message_id and listener count remain in the SSE payload and can be added to server logs at the broadcast site — moving the diagnostics out of the tool return, not deleting them.
- **Attachments dropped from the minimal reply.** The current schema (`sdk-channel/server.py:221`) includes an `attachments` array with file-path uploads. The minimal schema in `shared_spec` is `chat_id / text / reply_to` only. `attachments` is not a minimal-family feature; hosts that need attachment sends should either route those turns through tmux, or accept that phase 2 makes the SDK transport text-only in minimal-family sessions. This is deliberate reduction, not oversight. If attachments matter for a deployment, define a second family (`minimal-plus-attachments`, its own shared_spec) rather than smuggling optional-field variance into `minimal`.
- **Error return shape aligned.** `sdk-channel/server.py:258` currently returns `{"content": [...], "isError": True}` for the empty-text case; that path also has to yield `REPLY_TOOL_RESULT` verbatim, and the empty-text validation moves to schema-level (making `text` required with `minLength: 1`) so the tool body never returns a divergent shape.

The `shared_spec` module is the enforcement mechanism. If a transport hand-rolls its own reply schema, it silently forks the family and cache silently drops. The commit hook for `shared_spec.py` should note that changes there invalidate all currently-cached minimal-family prefixes.

## Motivation

Ariake as it stands is a two-transport layer. The name refers to two lights (残月 / 朝日) sharing a sky. Adding a third light — even one whose backing binary is the same as an existing transport's — is worth doing when:

1. It **exposes a distinction** the current protocol quietly papers over. In this case: the existing lease spec assumes long-lived holders. `-p` has no long-lived process. Making the lease express both flavours honestly is a protocol improvement, not just a new transport.
2. It **clears a road for the actual generalization** we want. Adding `-p` first proves out the shared-spec + turn-level-borrow patterns on a transport that still lives inside the `claude` binary. Once that works, adding a direct API transport (which lives *outside* the `claude` binary and is a stricter test of family alignment) becomes an incremental step, not a rewrite.

The proposal is not "we need `-p` for its features" (we don't — SDK already covers what `-p` can do). It is "adding `-p` is the cheapest way to make ariake's protocol express what it actually promises, and it unblocks `api-channel` as a natural next step".

## Design

### Three transports, two families (P0 rewritten)

| transport | process model | family | lease flavour |
|---|---|---|---|
| tmux (`channel/server.ts`) | long-lived TUI, one CC per session | `full-cc` | transport-level |
| SDK (`sdk-channel/server.py`) | long-lived Python + `claude-agent-sdk` | `minimal` | transport-level |
| `-p` (`p-channel/server.py`, new) | short-lived subprocess per turn | `minimal` | turn-level (borrow) |

Family membership is declared by the transport at startup and reported in `GET /session/current` as `prompt_family: "full-cc" | "minimal" | "unknown"`. The coordinator reads it to decide hot vs cold swap logging (see [prompt-family.md](./prompt-family.md) §"Coordinator behaviour").

### The `-p` transport shape

`p-channel/server.py` is a **thin FastAPI dispatcher**: it owns HTTP endpoints and coordinator communication but does **not** own the flock — that lives in a per-turn worker subprocess (see "Process structure and reaper safety" below). Inbound `/message` handling:

1. Dispatcher `POST /lease/borrow` to the coordinator with `{prompt_family: "minimal", resume: <sid>, timeout: 30}`. `timeout` is the **drain budget** — the bounded wait for the active holder to finish its in-flight turn, with the same meaning as the existing `timeout` on `/lease/acquire`. See "Lease protocol changes" for the full state machine; outcomes are `200 granted`, `409` (slot occupied / no active holder — immediate, no wait), or `408` (drain expired).
2. Dispatcher `fork+exec`s a per-turn **worker subprocess** (`p-channel/worker.py`) and hands it the envelope + granted sid via stdin. The worker opens `.lease.lock`, calls `flock(LOCK_EX | LOCK_NB)`, and holds the fd for the duration of the turn. If flock fails (something outside the protocol holds the lock), the worker exits non-zero; dispatcher `POST /lease/borrow/abort` and returns `503` to the host — do not retry the flock in-band.
3. Worker spawns `claude -p`:
   ```
   claude -p
     --resume <sid>
     --mcp-config p-channel/reply-mcp.json
     --system-prompt "<MINIMAL_SYSTEM_PROMPT>"
     --allowedTools "mcp__reply__reply"
     --output-format stream-json
   ```
   Envelope on stdin. The `--mcp-config` entry causes CC to spawn `reply_mcp` as a stdio child for the duration of this run; the config's `env` block carries the dispatcher's internal port so the shim knows where to POST.
4. **Outbound = the reply tool, single producer.** CC calls `reply` with structured arguments (`chat_id`, `text`, optional `reply_to` — filled from the envelope, same as on SDK). `reply_mcp` POSTs the arguments to the dispatcher's `/internal/reply` (dispatcher owns the SSE ring — worker does not); dispatcher broadcasts **one** complete SSE `reply` event (byte-compatible with tmux and SDK events) and marks the turn as replied. No one synthesizes SSE from assistant text.
5. Worker reads stream-json from the `claude -p` subprocess stdout **only** for the `result` event (usage telemetry, turn end) and for detecting abnormal termination. `assistant` events are not parsed into output.
6. On `claude -p` exit: worker releases flock (closing the fd is sufficient — flock is fd-scoped), then exits itself. Dispatcher observes worker exit via `wait()` and `POST /lease/borrow/complete` to the coordinator so the active transport can reacquire.

Failure paths (worker crash, `claude -p` non-zero exit, timeout waiting for `result`, stdin write error):
- Dispatcher's `wait()` on the worker always fires (the worker is a child process); on any non-zero worker exit, dispatcher posts `/lease/borrow/complete` anyway (never skip this — SDK/tmux would be starved). Worker death alone releases the flock via kernel fd cleanup, so no explicit unlock in an error path is needed.
- If the worker exited and no `/internal/reply` arrived, emit an SSE `error` event carrying the envelope's `message_id` so the host can display "the reply failed" instead of hanging forever. Because replies are atomic tool calls, the "partial reply" question from the first draft dissolves: either the reply fired (complete event already broadcast) or it didn't (error event).
- Edge case: reply fired, then `claude -p` died before emitting `result`. The reply was already delivered; the turn just has no usage record. Log a telemetry-gap line and move on — do not retract the reply.

### Process structure and reaper safety

The dispatcher–worker split resolves a defect the first v2 draft would have shipped: if the flock fd lived in the long-lived dispatcher process, a coordinator-side reaper that "kills the borrower" would kill the `claude -p` subprocess only, leaving the fd (and therefore the flock) held in the dispatcher — a silent starvation of the active transport, exactly the case round-2 review flagged.

The rules that follow from putting the flock in the worker:

- **Worker holds the fd.** The flock is fd-scoped in Linux — closing the fd (or process exit) releases the lock. Nothing else does.
- **Dispatcher never opens `.lease.lock`.** Not even for status inspection. Any diagnostic that would need to poke at it goes through the coordinator instead.
- **Dispatcher registers the worker pid with the coordinator** as part of the `POST /lease/borrow` response acknowledgement (a follow-up `PUT /lease/borrow/pid`, or the same call carries it — TBD in phase 1 implementation). Coordinator persists the pid in the borrow-slot record.
- **Reaper action is `kill(pid, SIGTERM)` followed by `SIGKILL` after 2s.** The pid is the *worker*, not the `claude -p` grandchild — killing the worker collapses its whole subtree via kernel fd cleanup and any `atexit` handlers register cleanly. If the worker is genuinely wedged past SIGKILL (D-state on disk), that is an operator escalation, not a protocol problem.
- **Dispatcher restart is safe.** If the FastAPI dispatcher itself crashes and restarts mid-turn, in-flight workers keep running (they are independent processes) and will still POST to `/internal/reply` on the same port when the dispatcher comes back up — provided the dispatcher's port is stable. On workers that finish while the dispatcher is down, the POST fails and the worker's `finally` posts `/lease/borrow/abort` directly to the coordinator, allowing recovery without waiting for the reaper.

### Phase 1 scope: SDK↔`-p` only (borrow from tmux deferred)

**In phase 1, borrow is only supported when the prior active transport is SDK.** If tmux holds the lease, `POST /lease/borrow` returns `409 {"error": "active holder is full-cc; borrow not supported"}` immediately.

Why the restriction:

- Coordinator's `borrow complete` step drives the prior active back to `acquire` with `resume=<sid>`. If prior active is tmux and the sid it was already running matches, that acquire is a re-attach (idempotent flock reacquire, no CC restart, no `restart_pending` machinery). If instead the acquire has to spin CC back up from scratch (because the tmux CC lost state during pause — see below), it becomes the full sdk→tmux restart_pending path documented in `docs/review-trail.md`, with all the danger the five review rounds spent working through.
- The subtler problem: even if tmux's CC survives the pause, **its in-memory conversation history no longer matches the jsonl on disk** after `-p` writes turn N+1. CC pushes appends to the file assuming its memory is the authoritative tail; a mismatch produces either interleaved jsonl (multi-writer, corruption) or CC overwriting `-p`'s turn on its next append. The naive fix is to make tmux restart-and-resume on every borrow completion, which multiplies the tmux CC boot cost per `-p` turn and reopens the `restart_pending` complexity.
- Neither problem is unsolvable; both are bigger than "add borrow endpoints". Phase 1 stays out of that surface. Phase 4 or later can lift the restriction by extending the borrow-complete handler with the tmux-specific restart-resume path (or by having tmux pin a "snapshot before pause, reload after" hook), and adding the round-6 entry to `review-trail.md`.

Coordinator log lines for a `full-cc` active + minimal borrow attempt: `WARN borrow refused: active=full-cc, borrower=minimal, phase1_restriction`. The 409 also carries this in the response body so hosts can present a diagnostic rather than a mystery.

### The `reply` stdio MCP shim (new component)

`reply_mcp/server.py` is a small **per-invocation** stdio MCP server providing exactly one tool: `reply`, schema imported from `shared_spec.py`, registered under the pinned server name (`MCP_SERVER_NAME = "reply"`, so the fully-qualified tool name is `mcp__reply__reply` — the name CC's `--allowedTools` whitelist and the on-wire tools array both see).

Lifecycle: **CC spawns it** as a stdio child on each `claude -p` run, per the `--mcp-config` entry, and it dies when the run ends. This is what stdio MCP means — the client owns the child process. There is no long-lived service, no systemd unit, and consequently no single point of failure: a shim crash is contained to that one turn, which then follows the no-reply error path above. Spawn overhead (tens of ms for a small Python process) is noise against model latency.

Behaviour: on receiving a `reply` tool call, POST the arguments to the p-channel dispatcher's `/internal/reply` (URL from the mcp-config `env` block) and return `REPLY_TOOL_RESULT` (imported from `shared_spec`) verbatim to CC. The dispatcher — not the shim, not the worker — owns the SSE ring, mirroring how `sdk-channel/server.py` owns its own. The shim is a stateless bridge that only bridges args out and result back.

SDK keeps its in-process MCP unchanged. The alignment point between SDK and `-p` is the **schema and server name imported from `shared_spec`**, not the process serving it.

### Lease protocol changes

Four new endpoints on the coordinator side:

- `POST /lease/borrow` — request a one-turn borrow. State machine below.
- `PUT /lease/borrow/pid` — borrower registers its worker pid so the reaper can target the right process. Called immediately after `borrow` grant and worker fork, before the worker spawns `claude -p`.
- `POST /lease/borrow/complete` — borrower signals it is done (worker exited, flock released via fd cleanup). Coordinator drives the active transport to reacquire with `resume=<sid>`, clears the borrow slot.
- `POST /lease/borrow/abort` — borrower signals a failure between grant and worker start (or a worker signals from its own `finally` if the dispatcher is unreachable). Coordinator restores the active transport immediately.

**Borrow state machine** (`POST /lease/borrow`):

1. Borrow slot already occupied → `409 {"error": "borrow slot occupied"}`. **Immediate — no queue.** The slot is single-occupancy.
2. No transport-level holder currently active (e.g. mid-switch) → `409`. Immediate.
3. **Phase 1 restriction check:** active holder's `prompt_family == "full-cc"` (i.e. tmux) → `409 {"error": "active holder is full-cc; borrow not supported"}`. See "Phase 1 scope" above.
4. Coordinator asks the active transport to pause inbound and drain in-flight turns, **bounded by `timeout`** — the same drain semantics (and the same failure code) as the existing `/lease/acquire`: drain expiry → active resumes (rollback), coordinator returns `408`, slot freed.
5. Drain complete → active releases its flock. Coordinator marks the slot occupied with `borrow_until = now + borrow_ttl`, logs hot/cold borrow by comparing families, returns `200 {"granted": true, "sid": "...", "until": "..."}`. Borrower is expected to `PUT /lease/borrow/pid {"pid": <worker_pid>}` promptly after step 2 of the transport flow above, so the reaper can target the right process.

**Blocking policy, stated precisely** (this replaces the first draft's overbroad "no blocking anywhere"): *contention never waits; drain waits are bounded.* The kernel flock is always `LOCK_NB` — a held lock is an immediate failure, never a queue. The borrow slot is immediate-fail on occupancy. The only wait in the borrow path is step 3's drain of the active holder, explicitly bounded by `timeout`, identical in kind to the bounded drain the lease spec already has. No unbounded waits, no queues, anywhere.

**Reaping stuck borrows**: if `now > borrow_until` and neither `complete` nor `abort` arrived, the coordinator escalates in three stages against the worker pid it received via `PUT /lease/borrow/pid`: `SIGTERM` → wait 2s → `SIGKILL` → wait 2s → `abort borrow slot`. Because the worker (not the dispatcher) owns the flock fd, killing the worker releases the lock through kernel fd cleanup. If no pid was ever registered (dispatcher crashed between `borrow` grant and the pid PUT), the reaper skips signals and goes straight to slot-abort; the dispatcher's crash separately released whatever it was doing. See "Process structure and reaper safety" above for the ownership rules that make this correct.

The existing `POST /lease/acquire` and `POST /lease/release` are **unchanged** and remain the only entry points for transport-level (long-lived) holders. `-p` never calls them directly.

Rationale for making borrow a coordinator flow rather than direct transport-to-transport negotiation:
- Keeps the "who is currently authoritative" state in one place (coordinator's memory), same as v1.
- Makes the seven-step atomic switch reusable — borrow is essentially a compact switch (release + reacquire on the same holder) with a `-p` turn wedged in between.
- Debug: coordinator logs are the single trail for lease events. Adding turn-level lock churn without central logging would make production incidents impossible to reconstruct.

Rationale for immediate-409 on contention (rather than queuing):
- Consistent with the existing lease spec's non-blocking flock discipline; the only waits are the explicitly bounded drains that `/lease/acquire` already has.
- Host decides retry/fallback policy. Some hosts might retry with backoff, some might fall back to SDK. Coordinator does not guess.

### The lease starvation question (round-1 open question #3, resolved)

Round 1 asked what happens if SDK holds the lease for 90 seconds on a long agent loop and three `-p` requests arrive. Under the borrow state machine:

- The first request takes the borrow slot and waits on the active holder's drain, bounded by `timeout`. If the SDK turn outlives the budget → `408`, slot freed, active resumes.
- The second and third get `409 borrow slot occupied` immediately.
- Host is responsible for deciding: retry with backoff, fall back to SDK (join the same active transport's inbound), or fail the user request.
- The design pressure is on the host: **don't route short `-p` turns and long SDK turns onto the same session unless you have a queueing strategy**. Ariake will not queue for you.

This is documented as a constraint in `p-channel/README.md`, not treated as a bug to work around.

## Cache telemetry (P1 resolution, scope corrected)

**Scope**: SDK and `-p` gain per-turn cache logging. tmux does **not** in phase 1.

- SDK: on `ResultMessage`, log `{ts, sid, transport: "sdk", family: "minimal", cache_read, cache_creation, input, output}`.
- `-p`: on `result` stream-json event, log the same shape with `transport: "p"`.
- tmux: **future work**. The tmux transport has no `ResultMessage` — CC's usage is internal to the CC process and only reaches disk via `~/.claude/projects/<slug>/<sid>.jsonl`. Two possible paths, both out of scope for phase 1:
  - Post-hoc jsonl parser (batch job, reads assistant message metadata).
  - A tmux plugin that hooks CC's response-received event and posts usage to the host.

The v1 doc's blanket "all three transports emit usage" was wrong (P1). The revised claim is narrower: SDK and `-p` (and eventually `api-channel`) all sit on top of a client that has explicit `usage` in its response, so they can emit uniform telemetry cheaply. tmux is architecturally different and its telemetry story is separate.

## Empirical verification of same-family alignment

See [prompt-family.md](./prompt-family.md) §"Verifying family alignment empirically" for the full procedure. Summary of what the verification script must check:

1. **Byte-level request diff.** Run SDK and `-p` against the same envelope on the same session (via a proxy that captures the outgoing HTTP request). Diff:
   - `system` field content and length.
   - `tools` array (including each tool's `description` and `input_schema`).
   - `cache_control` breakpoint count and positions.
   - `messages` history reconstruction.
   Any diff = family alignment claim broken. Fix at `shared_spec.py`.

2. **`cache_read_input_tokens` cross-check.** After a hot borrow, borrower's first turn should show `cache_read_input_tokens ≥ 0.9 * previous_holder's cache_creation_input_tokens`. Weaker than a byte diff, but automatable without a proxy.

Round-1 open question #1 asked whether SDK and `-p` prompt strings are byte-identical. v2 admits this cannot be answered by inspection — only by capture. The verification script is now a phase 1 deliverable (not a "nice to have"), because until it runs green, the minimal family is a hypothesis, not a fact.

### The subtler question: cache_control positions (new)

`claude-agent-sdk` and `claude -p` both decide where to insert `cache_control: {"type": "ephemeral"}` in the request they emit. Even with byte-identical `system` and `tools`, if SDK puts breakpoints at (system end, tools end, last message end) but `-p` puts them at (system end, tools end) only, the cached prefixes at the Anthropic side are different sets → potential cache miss even with equal content.

We do not currently know whether SDK and `-p` agree on breakpoint positions. Anthropic Messages API caches up to 4 breakpoints; the practical question is whether both bindings emit the same subset. The verification script must capture and diff the `cache_control` positions as a first-class field, not just the content.

If they diverge, options:
- Report to `claude-agent-sdk` maintainers and pin a version that behaves.
- Have `api-channel` (phase 3) emit a *known-good* breakpoint pattern and require SDK / `-p` to match by controlling knobs (env vars, config).
- Downgrade the `minimal` family to a smaller claim: "same content, breakpoint alignment best-effort".

## What this proposal does NOT do

- **Does not rename or move existing transports.** tmux and SDK stay put, same contract. Changes: SDK declares `prompt_family="minimal"` at startup, emits usage telemetry, and (phase 2) reroutes its reply schema through `shared_spec`.

- **Does not require sharing a `session_id` across all three transports at all times.** Deployments can split by session. The proposal is about what happens when they *do* share.

- **Does not change the envelope format.** Same `<channel>` tag, same fields. `-p` gets it on stdin.

- **Does not touch `docs/review-trail.md`.** The five-round trail is about sdk↔tmux `restart_pending` correctness. `-p` acquire/release is a strictly simpler code path (no `systemctl restart`, no fire-and-forget). If phase 1 uncovers new dangers, we add a round 6.

## Open questions (for round-3 review)

Of the first draft's five open questions, two dissolved under round-2 fixes (the MCP SPOF — the shim is now per-invocation, so a crash is contained to one turn; and `--mcp-config` lifecycle — the config is a static file checked into `p-channel/`, with the dispatcher's internal port passed via its `env` block) and one (borrow bookkeeping) was absorbed into the state machine's `borrow_until` + reaper. Remaining and new:

1. **(carryover) Are SDK and `-p` request shapes byte-identical when instructed identically?** Verification is a phase 1 deliverable, but if we go into implementation and find they aren't, what's plan B? Options listed under "cache_control positions" above.

2. **(reworked) Tool namespacing and flag plumbing.** `shared_spec` pins `MCP_SERVER_NAME = "reply"` so both SDK's in-process server and `-p`'s shim should emit `mcp__reply__reply`. But: do both bindings apply the same namespacing scheme across versions? Does either inject extra tool metadata (annotations, ordering) around the schema? And does `--allowedTools` filtering itself alter the emitted tools array vs SDK's `allowed_tools`? All of this collapses into the Level-2 capture diff, but it's worth naming because the failure is silent.

3. **(new) Reply-then-crash telemetry gap.** The reply tool fired (SSE already delivered), then the subprocess died before `result`. The design says log a telemetry-gap line and move on. Is that enough, or do we want the gap surfaced in the telemetry stream itself (a synthetic row with `usage: null`) so cache-hit-rate dashboards don't silently undercount turns?

4. **(new) Concurrent `/message` at the dispatcher.** The dispatcher is long-lived; two inbound `/message` calls arriving while a worker is running would each try to borrow. Second one gets `409 borrow slot occupied` from the coordinator, but this leaves the host holding an unrouted message. Design says host handles fallback; do we want the dispatcher to serialize inbound with an in-process queue capped at 1, returning `503` after that? Bounded in-memory queue vs pure fail-fast is a real trade — pure fail-fast burdens every host; bounded queue reintroduces the "hidden state at ariake" pressure I was trying to keep out. Leaning fail-fast, but not decided.

## Rollout plan (unchanged from v1 in structure, updated to match v2 content)

1. **Land the v2 docs.** [prompt-family.md](./prompt-family.md) v2 and this file. Both existing transports declare `prompt_family`. Coordinator logs hot/cold on switch (no `-p` yet). Minimal code change; unblocks the verification conversation.

2. **Land `shared_spec` and cache telemetry.** SDK imports its reply tool schema, server name, and `REPLY_TOOL_RESULT` from `shared_spec`; the current dynamic result string (`sdk-channel/server.py:274`) and the `attachments` schema field are removed with a `PHASE2_MINIMAL_ALIGNMENT.md` migration note. Telemetry logs on SDK's `ResultMessage`. Run for a week to establish a baseline: what are SDK's actual cache hit rates on our production traffic? If they're already 99%, phase 1 is confidence-building for cache; if they're 30%, we have a pre-existing bug to fix before adding `-p`.

3. **Build `reply_mcp/server.py`.** Per-invocation stdio shim, imports schema, server name, and `REPLY_TOOL_RESULT` from `shared_spec`, POSTs `reply` calls to the dispatcher's `/internal/reply`, returns `REPLY_TOOL_RESULT` verbatim. No deployment of its own — it is spawned by each `claude -p` run. Verified by a unit test driving it with a fake MCP client, plus one manual `claude -p --mcp-config` smoke run.

4. **Build `p-channel/` as dispatcher + worker.** Dispatcher (`p-channel/server.py`, FastAPI) owns HTTP and `/internal/reply` SSE. Worker (`p-channel/worker.py`) owns the flock fd and spawns `claude -p`. Coordinator gets the four borrow endpoints (`POST /lease/borrow`, `PUT /lease/borrow/pid`, `POST /lease/borrow/complete`, `POST /lease/borrow/abort`) and the reaper. Phase 1 restriction (borrow only from SDK-held sessions) is enforced at borrow-request time — no tmux borrow this phase.

5. **Run the verification script.** Capture SDK and `-p` requests, diff. Fix or downgrade the family claim before phase 6.

6. **Cross-family e2e test.** tmux writes turn N (cold prefix), `-p` reads and replies (cold prefix, cache_creation heavy), `-p` reads and replies again (should be hot), SDK reads and replies (should be hot). Verify session id survives and cache metrics match the prediction.

7. **Document `-p`'s constraints prominently in `p-channel/README.md`:** no tools beyond reply, no sidecar, turn-level lease via `/lease/borrow` (single borrow slot, bounded drain wait, `409`/`408` semantics), reply delivered atomically via the tool path, error event on no-reply exit.

Phase 3 (`api-channel`) is a separate follow-up, not part of this proposal. It becomes cheap once phases 1–7 land, because `api-channel` is a byte-for-byte reproducer of what SDK/`-p` are emitting, minus one runtime layer.

## Diff from v1 (for reviewer convenience)

- Family definition moved from "no tools" to "one specific request shape including a required `reply` tool". Resolves round-1 P0.
- Lease protocol adds `borrow / borrow/complete / borrow/abort` as explicit endpoints. Resolves round-1 P1 (turn-level unhooked).
- Cache telemetry scope narrowed to SDK and `-p`; tmux moved to future work. Resolves round-1 P1 (all-three too glib).
- Added `cache_control` breakpoint position as a first-class verification concern.

## Diff from v2 first draft (round-2 fixes)

- **Single reply producer.** Wrapper-side buffer-to-final synthesis deleted; the reply tool call (bridged by `reply_mcp` to the dispatcher's `/internal/reply`) is the only SSE path. stream-json stdout is telemetry + lifecycle only. Bonus: `chat_id` routing works on `-p` after all.
- **Borrow state machine made precise.** Contention (slot occupied, no active holder, kernel flock held) fails immediately with `409`; draining the active holder is a bounded wait governed by `timeout` with `408` on expiry — same semantics as the existing `/lease/acquire` drain. The overbroad "no blocking anywhere" claim is retired. Reaper with `borrow_until` absorbs the stuck-borrower bookkeeping question.
- **`--tools none` leftover removed from the rollout plan** — contradicted the family definition (reply tool must be present).
- **`reply_mcp` lifecycle corrected**: per-invocation stdio child spawned by CC via `--mcp-config`, not a long-lived systemd service (stdio MCP servers are client-owned). SPOF and config-lifecycle open questions dissolve.
- **MCP server name pinned in `shared_spec`** (`mcp__reply__reply`): the namespaced tool name is part of the cached prefix, so the server name is part of the family.
- Prompt-family definition widened (tool_choice, thinking params, images, TTL, minimum cacheable prefix) — see [prompt-family.md](./prompt-family.md).

## Diff from v2 second draft (round-3 fixes)

- **`REPLY_TOOL_RESULT` pinned in `shared_spec`.** Tool result text becomes a `tool_result` block in the next turn's history; SDK's current dynamic `"sent (id) -> N listener(s)"` string (`sdk-channel/server.py:274`) would silently break family alignment turn-over-turn. New static constant, both SDK and shim return it verbatim, diagnostics move out of the tool return to the broadcast site.
- **Phase 1 borrow restricted to SDK-held sessions.** tmux as prior active is `409`'d at request time. Removes the risk of tmux CC's in-memory history diverging from a `-p`-mutated jsonl, and keeps `restart_pending` (the topic of the five-round review-trail) out of borrow scope. Tmux borrow deferred to phase 4+ with its own review round.
- **Dispatcher–worker split for `p-channel`.** Round-2 draft had the dispatcher holding the flock; a reaper killing the `claude -p` subtree would not have released it. Split moves the fd to a per-turn worker subprocess; reaper kills the worker (via new `PUT /lease/borrow/pid`) and kernel fd cleanup releases the lock. Added the four-endpoint coordinator surface (borrow / borrow/pid / borrow/complete / borrow/abort).
- **SDK attachments dropped from minimal reply, named explicitly.** Bringing SDK into `minimal` costs `attachments`; documented as deliberate reduction rather than silent regression. Deployments needing attachments route those turns through tmux or define a `minimal-plus-attachments` follow-up family.
- **Open question #4 replaced.** Reaper-vs-flock is now solved by the dispatcher–worker split, not a policy choice. Its slot in the open questions is now "concurrent `/message` at the dispatcher" (in-process queue vs pure fail-fast) — an open policy question the split surfaced.
