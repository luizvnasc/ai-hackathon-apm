"""Scripted model for `run.py --selftest`: makes the real agent run probe.py with its Bash tool."""
import json
import re


def text_of(content):
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content or [] if isinstance(b, dict))


def decide(payload):
    """The content block the fake model answers with."""
    messages = payload.get("messages") or []
    for message in reversed(messages):
        content = message.get("content")
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return {"type": "text", "text": text_of(block.get("content")) or "(no tool output)"}
    tools = {t.get("name") for t in payload.get("tools") or []}
    found = re.search(r"SELFTEST ([A-Za-z0-9+/=]+)", "\n".join(text_of(m.get("content")) for m in messages))
    if found and "Bash" in tools:
        return {"type": "tool_use", "id": "toolu_selftest", "name": "Bash",
                "input": {"command": "python3 /opt/sandbox/probe.py " + found.group(1), "description": "Probe the sandbox"}}
    return {"type": "text", "text": "ok"}


def answer(path, payload):
    if "count_tokens" in path:
        return 200, b'{"input_tokens": 1}', "application/json"
    block = decide(payload)
    stop = "tool_use" if block["type"] == "tool_use" else "end_turn"
    message = {"id": "msg_selftest", "type": "message", "role": "assistant", "model": payload["model"],
               "stop_reason": stop, "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}
    if not payload.get("stream"):
        return 200, json.dumps({**message, "content": [block]}).encode(), "application/json"
    if block["type"] == "tool_use":
        empty = {**block, "input": {}}
        delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
    else:
        empty = {"type": "text", "text": ""}
        delta = {"type": "text_delta", "text": block["text"]}
    events = [{"type": "message_start", "message": {**message, "content": [], "stop_reason": None}},
              {"type": "content_block_start", "index": 0, "content_block": empty},
              {"type": "content_block_delta", "index": 0, "delta": delta},
              {"type": "content_block_stop", "index": 0},
              {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": 1}},
              {"type": "message_stop"}]
    body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
    return 200, body.encode(), "text/event-stream"
