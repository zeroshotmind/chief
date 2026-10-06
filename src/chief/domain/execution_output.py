"""Turn CLI JSONL into readable activity while retaining the original logs."""
from __future__ import annotations

import json
from typing import Any


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _objects(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def stream_output(raw: str) -> dict[str, Any]:
    activity: list[dict[str, str]] = []
    text: list[str] = []
    result: dict[str, Any] | None = None
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "result" or (kind is None and "result" in event):
            result = event
        elif kind == "assistant":
            for block in _objects(_mapping(event.get("message")).get("content")):
                if block.get("type") == "text":
                    text.append(_text(block.get("text")))
                elif block.get("type") == "tool_use":
                    inputs = _mapping(block.get("input"))
                    activity.append({"kind": "tool", "title": block.get("name", "Tool"),
                                     "detail": str(inputs.get("command") or
                                                   inputs.get("file_path") or "")[:2000]})
        elif kind == "user":
            for block in _objects(_mapping(event.get("message")).get("content")):
                if block.get("type") != "tool_result":
                    continue
                content = block.get("content")
                detail = content if isinstance(content, str) else "\n".join(
                    _text(part.get("text")) for part in _objects(content)
                    if part.get("type") == "text"
                )
                activity.append({"kind": "error" if block.get("is_error") else "output",
                                 "title": "Tool failed" if block.get("is_error") else "Tool result",
                                 "detail": detail[-4000:]})
        elif kind in ("item.started", "item.completed"):
            item = _mapping(event.get("item"))
            item_type = item.get("type")
            if item_type == "agent_message" and kind == "item.completed":
                text.append(_text(item.get("text")))
            elif item_type == "command_execution":
                activity.append({"kind": "command", "title": (
                    "Command finished" if kind == "item.completed" else "Running command"),
                    "detail": str(item.get("command", ""))[:2000]})
                if item.get("aggregated_output"):
                    activity.append({"kind": "output", "title": "Command output",
                                     "detail": _text(item["aggregated_output"])[-4000:]})
            elif item_type == "file_change":
                changed = [_text(c.get("path")) for c in _objects(item.get("changes"))]
                activity.append({"kind": "file", "title": "Files updated",
                                 "detail": ", ".join(changed)[:2000]})
        elif kind == "error":
            activity.append({"kind": "error", "title": "CLI error",
                             "detail": str(event.get("message", ""))[:4000]})
    usage = {}
    if result is not None:
        usage = {key: result[key] for key in ("total_cost_usd", "duration_ms", "usage")
                 if key in result}
    return {"activity": activity[-80:], "live_text": "\n\n".join(text)[-64000:],
            "usage": usage, "envelope": result}
