"""Per-invocation Codex context controls; never edit the user's CLI configuration."""
from __future__ import annotations

import json
from pathlib import Path

from ..models.definition import StepExecution


def profile_args(config: StepExecution, folder: Path) -> list[str]:
    if config.profile == "full_agent":
        return []
    settings: dict[str, str | int | bool] = {
        "project_doc_max_bytes": 0,
        "skills.include_instructions": False,
        "skills.bundled.enabled": False,
        "memories.use_memories": False,
        "tools.update_plan.enabled": False,
        "tools.experimental_request_user_input.enabled": False,
        "include_apps_instructions": False,
        # This runtime executes selected tools; enabling it does not expose extra tools.
        "features.code_mode_host": True,
        "web_search": "live" if "web_search" in config.tools else "disabled",
    }
    for feature in ("plugins", "apps", "hooks", "multi_agent", "image_generation",
                    "browser_use", "computer_use", "goals", "sleep_tool", "skill_search",
                    "tool_suggest", "code_mode"):
        settings[f"features.{feature}"] = False
    settings["features.shell_tool"] = "shell" in config.tools
    settings["features.view_image"] = "view_image" in config.tools
    if config.profile == "text_only":
        instructions = folder / "instructions.txt"
        instructions.write_text(
            "Follow the request. Return the required JSON. No tools or file access.")
        settings["model_instructions_file"] = str(instructions)
    # An empty mcp_servers table merges with inherited entries, so it does NOT isolate
    # a session. Ignore user config instead; retain auth, managed policy and exec rules.
    args = ["--ignore-user-config", "--strict-config"]
    for key, value in settings.items():
        args.extend(["-c", f"{key}={json.dumps(value)}"])
    return args
