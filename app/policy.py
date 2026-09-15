"""Policy: what the automation is permitted to do. Config, consulted, never decided."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from app.schema import Strict


class Policy(Strict):
    allowed_origins: list[str]
    allowed_actions: list[str]
    risk: dict[str, Literal["allow", "require_confirmation", "block"]]


def load_policy(path: str | Path = "policy.json") -> Policy:
    return Policy.model_validate(json.loads(Path(path).read_text()))


def check_origin(policy: Policy, base_url: str) -> None:
    origin = base_url.rstrip("/")
    if origin not in policy.allowed_origins:
        raise PermissionError(f"origin not on the allowlist: {origin}")


def check_action(policy: Policy, action: str) -> None:
    if action not in policy.allowed_actions:
        raise PermissionError(f"action not on the allowlist: {action}")
