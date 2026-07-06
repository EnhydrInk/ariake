# Prompt families

Status: draft v3 (revised after round-2 review).
Referenced by: [three-light-proposal.md](./three-light-proposal.md) v3.

## What this document is

Ariake's lease protocol treats the `session_id` as the axis of shared memory: transports on the same session read from and write to the same jsonl file. That much is unchanged.

This document introduces a second, orthogonal axis: **prompt family**. Two transports on the same session may or may not share prompt cache across a switch, depending on whether they emit **byte-identical requests** to the Anthropic Messages API (given the same `session_id` and the same messages history). Cache preservation is a per-family property; session sharing is not.

Making this axis first-class in the lease protocol lets the coordinator and telemetry name what is happening ("hot swap" vs "cold swap") instead of leaving cache behaviour as a silent property of transport choice.

## What the family actually is

**A prompt family is a specific Anthropic Messages API request shape.** Concretely, for a family `F` defined over sessions in a given deployment:

- A fixed `system` field content (byte-identical string across all family members).
- A fixed `tools` array — same set, same **fully-qualified tool names** (MCP tool names are namespaced `mcp__<server>__<tool>`, so the MCP server name is part of the family), same `description` strings, same `input_schema` JSON, same ordering.
- A fixed `tool_choice` — per Anthropic's caching docs, changing `tool_choice` invalidates cached prefixes. The family pins it (minimal uses the default `auto`).
- Fixed thinking parameters — enabling/disabling extended thinking or changing its budget alters cache validity. The family pins thinking config (minimal: off).
- An image policy — the presence/absence of images in the prompt affects cache validity. Minimal family: no images in the system/tools region; images arriving in `messages` are a per-deployment decision, noted as a cache-behaviour variable.
- A fixed `cache_control` breakpoint policy — the set of positions at which `{"type": "ephemeral"}` markers are inserted, **and the TTL choice per marker** (5m default vs 1h are different shapes).
- `model` is not pinned by the family, but note: **the cache itself is model-keyed.** Two same-family requests on different models share nothing. Family membership makes cache sharing possible; running the session on one pinned model is what makes it actual. Deployments should treat a model switch as a deliberate cache-cold event, same as a cross-family swap.

Reference for which request dimensions affect cache validity: [Anthropic prompt caching docs](https://platform.claude.com/docs/en/build-with-claude/prompt-caching). That page is normative; this list is our reading of it and should be re-checked against it when the API adds parameters.

Given the above all fixed, and given the same `messages` history growing at the tail, every request to Anthropic produces the same cached prefixes, so subsequent same-family turns cache-hit.

**One more surface, easily missed:** if the family's tools are called during a turn, their **return content** becomes a `tool_result` block on the *next* turn's `messages`. Any variance in what the tool returns (a message id, a timestamp, a listener count) diverges history across family members and breaks any cache breakpoint on `messages`. The family therefore also pins the tool-return payload as a fixed string. See `REPLY_TOOL_RESULT` in the shared spec below and the phase-2 SDK regression called out in [three-light-proposal.md](./three-light-proposal.md) §"Phase 2 SDK behaviour changes".

This reframing is v2's response to the round-1 P0 finding: "family" is defined by what goes on the wire to Anthropic, not by "does the transport allow tools" or "what CLI flag was passed". The right question about any transport is *what request shape does it emit*, and any two transports that emit the same shape are in the same family regardless of implementation.

### Why the reframing matters

The v1 draft defined `minimal` as "no tools, minimal prompt", which was too coarse:
- SDK carries a `reply` MCP tool (needed for outbound), so under the v1 definition SDK wasn't `minimal`.
- `-p` with `--tools none` was `minimal`.
- These two could never share cache, defeating the entire point of adding `-p`.

The v2 definition — `minimal` = "system prompt X + tools=[reply schema Y] + cache_control at (system end, tools end)" — has three consequences:
- SDK is in `minimal` iff its `reply` tool schema matches the shared spec (achievable — SDK just needs to import from `shared_spec.py` instead of hand-rolling).
- `-p` is in `minimal` iff it invokes an external `reply` MCP that presents the same schema.
- A future direct API transport is in `minimal` by trivially constructing the shape.

The family is now a specification, not a lifestyle. Transports opt in by producing the artifact.

## The two families this repo ships

### `full-cc`
CC's default assembled request shape. tmux uses this because CC's default is what makes tmux worth having (plugins, hooks, `CLAUDE.md` chain, complete toolkit — all of which would break under a replace-mode system prompt or a stripped tool list).

`full-cc`'s request shape is a moving target across CC versions: it can shift when CC updates its default system prompt, adds tools, or reorders tool definitions. That's fine — `full-cc` is the family that says "whatever CC ships is the request shape, and by convention we do not enforce byte alignment against anything else".

Members: tmux.

### `minimal`
The shape defined in `shared_spec.py`. Concretely:

```python
# shared_spec.py (illustrative — final schema TBD in phase 1)

MINIMAL_SYSTEM_PROMPT = "You are Kieran, a conversational assistant. When a user message arrives via the <channel> tag, reply by calling the reply tool with a single message. Do not call any other tool."

REPLY_TOOL_SCHEMA = {
    "name": "reply",
    "description": "Send a reply to the user's channel. Called once per user turn.",
    "input_schema": {
        "type": "object",
        "properties": {
            "chat_id": {"type": "string", "description": "The channel id from the inbound envelope."},
            "text": {"type": "string", "description": "The reply body, verbatim."},
            "reply_to": {"type": "string", "description": "Optional inbound message_id for threading."},
        },
        "required": ["chat_id", "text"],
    },
}

CACHE_BREAKPOINTS = ("system_end", "tools_end")  # positions, not offsets
CACHE_TTL = "5m"          # per-marker TTL is part of the request shape

MCP_SERVER_NAME = "reply"  # MCP tool names are namespaced mcp__<server>__<tool>.
                           # The on-wire tool name is therefore mcp__reply__reply,
                           # and the server name is part of the family: SDK's
                           # in-process server and -p's stdio shim must both
                           # register under this name or the tools arrays differ.

TOOL_CHOICE = {"type": "auto"}  # changing tool_choice invalidates cached prefixes
THINKING = None                 # extended thinking off; changing this is a family change

# Fixed string CC will see as the reply tool's return value. Becomes the
# tool_result block on the next turn's messages history, so any variance
# across family members breaks messages-level cache hits. No interpolated
# fields — no message_id, no listener count, no timestamp, no attempt count.
# Diagnostics that used to live in this return string (see current
# sdk-channel/server.py:274) move to the broadcast site's server log,
# where they don't ride along in the API request.
REPLY_TOOL_RESULT = "reply delivered"
```

Members: SDK, `-p`, and future `api-channel`.

Any transport claiming `minimal` membership imports these constants (does not hand-roll them) and passes them through whatever runtime layer it uses to reach Anthropic. Details in [three-light-proposal.md](./three-light-proposal.md) §"MINIMAL as a request shape".

## What the families cannot do

They cannot make cache survive a **cross-family** switch. If tmux (`full-cc`) writes turn N and SDK (`minimal`) resumes on turn N+1, SDK's request has a different (system, tools, breakpoints) prefix, so nothing at the Anthropic side matches — SDK pays full `cache_creation_input_tokens` on turn N+1. Subsequent SDK-to-SDK turns then re-hit from that new baseline.

This is not a bug the families are supposed to fix. It is the underlying reality the families make visible. The coordinator logs cold swaps so operators can see the cost when they choose to pay it.

## Family membership declaration

Each transport declares its family at startup and reports it in two places:

- `GET /session/current` response gains `prompt_family: "full-cc" | "minimal" | "unknown"`.
- `POST /lease/acquire` request gains an optional `prompt_family` with the same enum values.
- `POST /lease/borrow` request (new — see [three-light-proposal.md](./three-light-proposal.md) §"Lease protocol changes") requires `prompt_family`.

Backwards compatibility: transports that don't set `prompt_family` on `/lease/acquire` are treated as family-agnostic and coordinator does not distinguish hot vs cold. Transports that don't declare `prompt_family` on `/session/current` are reported as `"unknown"`. This lets pre-v2 code run without breakage during rollout.

## Coordinator behaviour

The seven-step atomic switch is unchanged. Family produces log output and no gate:

```
if from_family == to_family and to_family != "unknown":
    log.info("prompt_family hot swap", family=from_family)
elif "unknown" in (from_family, to_family):
    log.info("prompt_family unknown, cache prediction n/a")
else:
    log.warn("prompt_family cold swap",
             from_=from_family, to=to_family,
             note="cache reheat expected on next turn")
```

A host that wants to forbid cold swaps enforces that policy above ariake (e.g. by refusing to call `/api/transport/switch` if the target's family differs from current). Ariake does not know deployment policy.

For borrow (turn-level, `-p` only in phase 1), the coordinator emits the same style of log with `borrow_hot` / `borrow_cold` prefixes to distinguish from full transport switches.

## Verifying family alignment empirically

The claim "two transports in the same family share cache" is only true if the underlying binding actually produces the same request shape. That is an empirical claim, not a guaranteed one — it depends on what `claude-agent-sdk` and `claude -p` do internally when composing the API request. **We do not currently know they agree, and no amount of code-reading substitutes for capture.**

The verification is a phase 1 deliverable of [three-light-proposal.md](./three-light-proposal.md), not a "nice to have". Until it runs green, the `minimal` family is a hypothesis.

### Two verification levels

**Level 1 — cheap, weak:** compare `usage.cache_read_input_tokens` on same-session same-family consecutive turns.

1. Turn 1: SDK sends a message on a fresh session. Record `cache_creation_input_tokens` = `X`, `cache_read_input_tokens` = small.
2. **Precondition check: `X > 0`.** If `X == 0`, the test is **void, not passing** — nothing was cached on the priming turn, so any comparison against it is vacuous (`Y >= 0.9 * 0` is trivially true). The likely cause is the minimum-cacheable-prefix threshold below; investigate before drawing family conclusions.
3. Turn 2 (via borrow): `-p` sends a message on the same session. Record `cache_read_input_tokens` = `Y`.
4. If `Y >= 0.9 * X`, most of what SDK cached is being read by `-p` — the families likely agree at the content level.
5. If `Y << X`, prefixes are not identical. The `usage` numbers can't tell you *why*, only *that*.

This is automatable without a proxy but is a weak signal — it can be fooled by partial matches, aggregate rounding, or the void case in step 2.

### The minimum cacheable prefix threshold

Anthropic ignores `cache_control` markers on prefixes shorter than a per-model minimum (1024 tokens on most current models; higher on some smaller ones — check the [prompt caching docs](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) for the current table). The minimal family's static prefix — a short system prompt plus one `reply` tool schema — plausibly sits **below** this threshold.

Consequences:

- Early in a session, the `system_end` and `tools_end` breakpoints may be silently dead. Caching then rides on whatever breakpoints the binding adds further into `messages` (bindings typically add tail markers automatically — Level 2 capture will show exactly which). As history grows past the threshold this resolves itself; the cost is a few uncached early turns, not a broken design.
- Verification runs must not use near-empty sessions, or Level 1 hits the void case above. The verification script should prime the session past the threshold (a few turns of realistic traffic) before measuring.
- If the deployment wants the static prefix itself to cache from turn 1, the honest options are: accept the few cold turns (recommended), or acknowledge that padding the system prompt to clear the threshold is a token-cost trade dressed as an optimization — it spends real input tokens every turn to save cache-write ones. Do the arithmetic before choosing padding.

**Level 2 — expensive, strong:** capture the outgoing HTTP requests and diff them.

Options for capture:
- **mitmproxy** in front of `api.anthropic.com` for the SDK / `-p` processes, TLS bump via a local CA. High fidelity, works for any binding.
- **claude-agent-sdk debug hook** if one exists — needs an audit of the SDK's public API. Would avoid MITM.
- **HTTP proxy env vars** (`HTTPS_PROXY`) pointed at a lightweight recording proxy — simplest deployment but only works if the binding respects env proxies.

Diff the captured requests on four axes:

- `system` field: content string, and (if it's an array) block structure and per-block cache_control.
- `tools` array: length, per-tool **fully-qualified name** (namespacing scheme may differ between bindings — this is where a server-name mismatch or a differing `mcp__` prefix convention shows up), description, input_schema JSON (after canonical serialization to normalize key order).
- `tool_choice`, thinking parameters, and any other top-level request fields the binding sets implicitly — diff the whole request body, not just the fields we expect to matter.
- `cache_control` positions **and TTLs**: which blocks have the marker, in what order, with which `ttl`. This is the position axis that Level 1 cannot check.
- `messages` history reconstruction: sanity check that both transports rebuild the same conversation history from the same jsonl.

Any diff means family alignment is broken and the fix goes in `shared_spec.py` or in the transport layer's plumbing. Rerun Level 2 after any binding version bump (both `claude-agent-sdk` and the CC binary).

### The cache_control positions question, called out

Even with byte-identical `system` and `tools`, if SDK inserts `cache_control` at (system, tools, last-user-message) and `-p` inserts it at (system, tools) only, the cached prefixes at Anthropic are different sets. Both requests may still cache-*read* on the earlier prefixes, but the later prefix only exists on one side, so the next turn from the other side pays extra `cache_creation` for the tail.

This is subtler than "content matches" and is precisely what Level 2 captures and Level 1 does not. Round-1 review did not raise this; it becomes visible only after grokking that the underlying mechanism is `cache_control` markers, not "magic session-aware caching" (see internal note under [three-light-proposal.md](./three-light-proposal.md) §"Empirical verification"; also user-facing tutorial at pepechino.github.io/-tutorials/claude-p-cache.html).

We do not yet know whether the two bindings agree. Verification will tell us.

### If they don't agree

In descending order of preference:

1. **Pin binding versions where they do agree** and document the pin. Explicit `claude-agent-sdk==X.Y.Z` and `claude` CLI version in `p-channel/`'s requirements. Reverify on any bump.

2. **Make `api-channel` (phase 3) the golden reference and match to it.** Handwritten API requests are byte-controllable; use them to derive a canonical shape, then coax SDK and `-p` to align (env vars, config knobs, occasionally source patches upstream).

3. **Downgrade the `minimal` family claim** to "same content, breakpoint alignment best-effort". Log the observed mismatch and accept partial cache hits. Not preferred but transparent.

4. **Split `minimal` into `minimal-sdk` and `minimal-p` families.** Explicit admission that these two don't share cache, and only `minimal-sdk` shares with `api-channel`. Ugly but honest.

## Constructing the families in code

`full-cc` is CC's own behaviour with no `--system-prompt` and no `--tools` override. It is not something ariake code needs to construct — tmux gets it by default.

`minimal` requires a specific system prompt string and a specific tool schema. Both live in a shared module (`shared_spec.py`) that every minimal-family transport imports. See the example under §"The two families this repo ships" above and the more detailed layout in [three-light-proposal.md](./three-light-proposal.md) §"MINIMAL as a request shape".

Rules of engagement:

- **Do not copy the strings into a second file.** Copy = silent fork = cache drops without a signal. The single import point is the enforcement mechanism.
- **Changing `shared_spec.py` is a cache-invalidation event.** All currently-cached prefixes in the family become dead. Commit messages that touch `shared_spec.py` should note this so operators know a cache-cold day is coming and can plan around it (e.g., avoid touching during production peak).
- **The `reply` tool schema is load-bearing.** Even the tool's `description` string counts — Anthropic hashes the whole tool JSON for cache matching. Small copy edits to `description` invalidate cache. If you want to tweak wording, tweak once, deploy, and accept the reheat.

## Family stability under CC binary bumps

The `full-cc` family is defined by whatever CC ships. When you upgrade the CC binary, the default system prompt may change (new safety instructions, new tool descriptions). `full-cc` cache silently invalidates and reheats — this is expected, not a bug.

The `minimal` family is defined by `shared_spec.py`. It should survive CC binary bumps unchanged, **unless** the binding layer (`claude-agent-sdk` or `claude -p`) changes how it composes requests around the fixed system/tools content. That's the phase 1 verification: re-run after every CC bump.

## The naming choice

"Family" over "profile" or "mode":
- A profile is something one process picks; a family is something multiple processes belong to.
- Ariake's lease protocol is a coordination between multiple processes, so the term that emphasises shared membership is the more useful one.
- `claude-agent-sdk`'s `options` object and CC's `--profile` flag both use "profile" for user-config concepts; using "family" here avoids overloading the term.

"prompt_family" as the field name over "family" alone:
- Conveys what's being categorized (the prompt shape, not the transport role or the deployment env).
- Room for a future orthogonal concept (e.g. "output_family" for structured-output modes) without renaming.

## Round-1 debt

- **P0 (SDK ≠ zero-tools):** resolved by redefining `minimal` as a request shape that includes the `reply` tool. Details above and in [three-light-proposal.md](./three-light-proposal.md).
- **P1 (turn-level lease):** addressed in [three-light-proposal.md](./three-light-proposal.md) via `/lease/borrow`. This document reflects the new endpoint in "Family membership declaration".
- **P1 (acquire contradiction):** addressed in [three-light-proposal.md](./three-light-proposal.md). This document is not affected.
- **P1 (tmux telemetry):** acknowledged here — tmux's cache is architecturally invisible to the family-alignment verification, so tmux membership in `full-cc` is asserted by construction (CC's default) rather than verified. Fine for the initial design; deeper telemetry is future work.
- **P2 (SSE wire):** addressed in [three-light-proposal.md](./three-light-proposal.md) — one complete `reply` event per turn via the reply-tool path (single producer). Not a families-doc concern.

## Diff from v1 (for reviewer convenience)

- Family definition is now "a specific Anthropic Messages API request shape" rather than "a prompt-and-tool configuration". This is the round-1 P0 resolution.
- Added `shared_spec.py` sketch as the single source of truth for the minimal family.
- Split verification into Level 1 (cheap, weak, `usage`-based) and Level 2 (expensive, strong, HTTP capture).
- Added `cache_control` breakpoint positions as a first-class verification axis, with fallback options if bindings disagree.
- Added the "cache invalidation event" note for `shared_spec.py` changes.
- Reframed `full-cc` as "whatever CC ships, by convention not verified" — explicit rather than implicit.

## Diff from v2 first draft (round-2 fixes)

- Family definition widened beyond (system, tools, breakpoints): now also pins `tool_choice`, thinking parameters, image policy, per-marker cache TTL, and fully-qualified MCP tool names (the server name is part of the family). Added the model-keyed-cache note.
- `shared_spec.py` sketch gains `MCP_SERVER_NAME`, `CACHE_TTL`, `TOOL_CHOICE`, `THINKING`.
- New subsection on the **minimum cacheable prefix threshold**: the minimal family's static prefix plausibly sits below the per-model minimum, so early-session breakpoints may be silently dead; verification must prime sessions past the threshold.
- Level 1 verification gains a precondition: `cache_creation_input_tokens > 0` on the priming turn, else the test is **void, not passing** (the `Y >= 0.9 * X` check is vacuous at `X = 0`).
- Level 2 diff axes extended: fully-qualified tool names, `tool_choice`/thinking/implicit fields (diff the whole body), cache_control TTLs.

## Diff from v2 second draft (round-3 fixes)

- **Tool-return content pinned.** `REPLY_TOOL_RESULT` added to `shared_spec` as a static string with no interpolated fields. Round-2 draft pinned the tool call surface but missed that the tool's *return* becomes `tool_result` in the next turn's history, so a dynamic return string would silently diverge family members turn-over-turn. Called out as the phase-2 SDK regression to fix; diagnostics that were carried in the return string (message id, listener count) move to server logs at the broadcast site.
- "Given all four fixed" reworded to "Given the above all fixed" (the list is now more than four items and was drifting toward a stale count).
