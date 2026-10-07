"""Provider token accounting. Cache/reasoning details are subsets of normalized totals."""
from __future__ import annotations

from typing import Any

TOKEN_FIELDS = ("input_tokens", "output_tokens", "reasoning_tokens", "cache_read_tokens",
                "cache_write_tokens", "total_tokens")


def count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def normalize(raw: dict, provider: str) -> dict[str, int]:
    data = {}
    aliases = {
        "input_tokens": ("input_tokens", "inputTokens"),
        "output_tokens": ("output_tokens", "outputTokens"),
        "reasoning_tokens": (
            "reasoning_output_tokens", "reasoning_tokens", "reasoningOutputTokens"),
        "cache_read_tokens": (
            "cached_input_tokens", "cache_read_input_tokens", "cacheReadInputTokens"),
        "cache_write_tokens": ("cache_creation_input_tokens", "cacheCreationInputTokens",
                               "cache_write_input_tokens"),
    }
    for field, keys in aliases.items():
        for key in keys:
            value = count(raw.get(key))
            if value is not None:
                data[field] = value
                break
    if provider == "claude" and "input_tokens" in data:
        data["input_tokens"] += data.get("cache_read_tokens", 0) + data.get("cache_write_tokens", 0)
    if "input_tokens" in data and "output_tokens" in data:
        data["total_tokens"] = data["input_tokens"] + data["output_tokens"]
    return data


def sum_tokens(reports: list[dict]) -> dict[str, int]:
    # A missing field means unavailable, not zero (especially reasoning on older CLIs).
    if not reports:
        return {}
    return {key: sum(report[key] for report in reports) for key in TOKEN_FIELDS
            if all(key in report for report in reports)}


class UsageAccumulator:
    def __init__(self) -> None:
        self.messages: dict[str, dict] = {}
        self.turns: list[dict] = []
        self.result: dict | None = None

    def accept(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "result" or (kind is None and "result" in event):
            self.result = event
        elif kind == "turn.completed" and isinstance(event.get("usage"), dict):
            self.turns.append(normalize(event["usage"], "codex"))
        elif kind == "assistant" and not event.get("parent_tool_use_id"):
            message = event.get("message")
            if not isinstance(message, dict) or not isinstance(message.get("id"), str):
                return
            if isinstance(message.get("usage"), dict):
                report = normalize(message["usage"], "claude")
                # Claude assistant output counts are placeholders. Only final results
                # contain the true generated output total. Deduplicate shared message ids.
                for key in ("output_tokens", "reasoning_tokens", "total_tokens"):
                    report.pop(key, None)
                self.messages[message["id"]] = report

    def snapshot(self) -> dict:
        if self.result is not None:
            result = self.result
            usage = {key: result[key] for key in ("total_cost_usd", "duration_ms", "usage")
                     if key in result}
            model_usage = result.get("modelUsage") or result.get("model_usage")
            reports = [normalize(value, "claude") for value in model_usage.values()
                       if isinstance(value, dict)] if isinstance(model_usage, dict) else []
            if reports and all("total_tokens" in report for report in reports):
                tokens = sum_tokens(reports)
                scope = "all_reported_models"
            else:
                raw = result.get("usage")
                tokens = normalize(raw, "claude") if isinstance(raw, dict) else {}
                scope = "main_loop"
            if tokens:
                usage.update(tokens=tokens, source="claude_result", scope=scope, provisional=False)
            elif self.messages:
                usage.update(tokens=sum_tokens(list(self.messages.values())),
                             source="claude_messages", scope="main_loop", provisional=True)
            return usage
        if self.turns:
            return {"tokens": sum_tokens(self.turns), "source": "codex_turns",
                    "scope": "reported_turns", "provisional": False}
        if self.messages:
            return {"tokens": sum_tokens(list(self.messages.values())),
                    "source": "claude_messages", "scope": "main_loop", "provisional": True}
        return {}
