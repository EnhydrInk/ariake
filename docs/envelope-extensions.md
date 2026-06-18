# Envelope Extensions

The standard inbound envelope is:

```
<channel source="..." chat_id="..." message_id="..." user="..." ts="...">body</channel>
```

and the standard outbound SSE event is:

```json
{
  "type": "reply",
  "chat_id": "...",
  "message_id": "...",
  "reply_to": "...",
  "text": "...",
  "ts": "..."
}
```

Both reference transports use those shapes. Anything you need beyond them — internal-state attributes, file attachments, voice / audio markers, group-routing tags — is yours to wire in. This document describes how to do it without breaking the contract.

## Adding inbound attributes

Custom attributes on the inbound `<channel>` tag are passed through to CC literally. CC sees them as XML attributes and may incorporate them into its reply via the system prompt that explains them.

**Pattern**: extend `build_envelope` in `sdk-channel/server.py` and `deliver` in `channel/server.ts` to read the attribute from the request body and append it to the tag. Sanitize:

- strip control characters (`\x00-\x1F`, `\x7F`) and any newline form (`\r`, `\n`, ` `, ` `) — they break the single-line `send-keys -l` assumption on the tmux side and may inject XML structure either way
- limit length aggressively (40 chars is a reasonable cap for short status hints) — long attribute values can become an injection vector if a downstream code path reflects them anywhere

Example: an `inner` attribute carrying a one-line internal state hint:

```typescript
// channel/server.ts deliver()
const innerClean = toSafeLine(inner).trim().slice(0, 40)
const innerAttr = innerClean ? ` inner="${escAttr(innerClean)}"` : ''
const tag = `<channel source="${SOURCE}" chat_id="${safeAttr(chat_id)}" message_id="..." user="..." ts="..."${innerAttr}>${safe}</channel>`
```

```python
# sdk-channel/server.py build_envelope()
inner_clean = inner.replace("\r", " ").replace("\n", " ").strip()[:40] if inner else ""
inner_attr = f' inner="{esc_attr(inner_clean)}"' if inner_clean else ""
```

**Keep both transports byte-identical**. The envelope is the wire format CC sees; if the two transports build different tags for the same input, switching transports mid-conversation will look like an attribute disappearing or changing format. The `tests/test_lease_lock.ts` pattern can be adapted into a parity test.

## Adding inbound attachments

Files are referenced by absolute path in a body prefix that CC can `Read`. Pattern:

```
<channel ...>[attachment 1 → /abs/path/file.txt] [attachment 2 → /abs/path/img.png] message body</channel>
```

Sanitize hard:

- path must be absolute (`startsWith('/')`)
- path must live inside an allow-listed uploads directory (use `path.resolve` + `path.relative` to defeat `../` traversal — `(p + '/').startsWith(root)` is not enough; a literal `/uploads/..` slipped through that check in early versions)
- reject paths containing control characters (`\r\n\x00-\x1F\x7F`) — they break the tag

The reference implementations have both checks; copy them when extending.

## Adding outbound event types

The SSE event shape `{type, chat_id, message_id, reply_to, text, ts}` is the minimum. You can extend it with extra fields (e.g. a `voice: true` flag to mark TTS events) — older consumers that don't recognize the field will see a regular `reply` event and gracefully degrade.

**Pattern**: register a second MCP tool that broadcasts the same `type: "reply"` event but with the extra field set.

```python
# sdk-channel/server.py
@tool("voice_reply", "...", VOICE_SCHEMA)
async def voice_reply_tool(args: dict):
    payload = {
        "type": "reply",  # same type — old consumers see plain reply
        "voice": True,    # new field — new consumers know to render audio
        "chat_id": args["chat_id"],
        "message_id": next_id("a"),
        "text": args["text"],
        "ts": utc_iso_z(),
    }
    broadcast(payload)
    return {"content": [{"type": "text", "text": f"voice sent ({payload['message_id']})"}]}
```

Add the same tool to `channel/server.ts` with the matching schema. The MCP tool name + schema must be identical across transports for CC to call it the same way regardless of which transport is active.

## Adding the `user` attribute for multi-party routing

The reference envelope carries a `user="..."` attribute, defaulting to the conversation's primary user identifier. In a multi-party setup (e.g. a group chat where multiple humans or AIs feed messages to one CC instance), reuse `user` to mark the speaker. CC can be instructed (via system prompt) to recognize `user` and adjust tone / addressee accordingly.

When the same CC speaks back, the SSE `reply` event reaches the host; the host decides which party should see it based on `chat_id` (and any host-side routing table) — `user` is inbound-only.

## What not to do

- **Do not stuff structured payloads into the body.** CC reads the body as natural-language input. JSON in the body becomes a string CC has to parse. Use attributes for structure.
- **Do not change `<channel>` itself.** Anything calling CC and using the official channel plugin convention (Telegram, Discord, etc.) expects exactly the tag name `channel` with attributes. Renaming the wrapper breaks compatibility.
- **Do not skip sanitization on the tmux side and hope it'll be fine.** `send-keys -l` interprets `\n` as `Enter`. A path or attribute value containing a newline will cut the tag in half and commit a malformed XML fragment as a user message.
- **Do not let extension attributes carry secrets.** They are passed verbatim to CC and may be logged in the jsonl transcript. Anything sensitive belongs on a side channel the host controls, not in the envelope.

## A note on backward compatibility

If you extend the envelope with new attributes, old CC system prompts won't know what they mean — CC will just ignore them. That is fine; do not retroactively change the meaning of an existing attribute. Add new ones; do not rename or repurpose old ones. The envelope is a wire contract between transports, host, and CC; once values are in production jsonls, you cannot rewrite history.
