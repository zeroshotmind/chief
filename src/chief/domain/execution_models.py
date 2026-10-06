"""Planning choices from local CLI metadata; no credentials or network requests."""
from __future__ import annotations

import json
import os
from pathlib import Path


def execution_models() -> dict[str, list[dict[str, str]]]:
    # Stable Claude CLI aliases. Exact version ids remain available through Custom.
    models = {"claude": [{"id": name, "label": label} for name, label in (
        ("sonnet", "Sonnet (CLI alias)"), ("opus", "Opus (CLI alias)"),
    )], "codex": []}
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        cache = json.loads((home / "models_cache.json").read_text())
        entries = cache.get("models", []) if isinstance(cache, dict) else []
        seen = set()
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict) or entry.get("visibility") != "list":
                continue
            model = entry.get("slug")
            if not isinstance(model, str) or not model.strip() or model in seen:
                continue
            label = entry.get("display_name")
            models["codex"].append({
                "id": model, "label": label if isinstance(label, str) else model,
            })
            seen.add(model)
    except (OSError, ValueError):
        pass  # The custom-id field still works before Codex has populated its cache.
    return models
