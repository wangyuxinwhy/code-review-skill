"""Checklist resolution and loading."""

from importlib.resources import files as pkg_files
from pathlib import Path
from typing import Any

import yaml

from code_review_skill.types import Checklist, ChecklistItem

LOCAL_CHECKLIST = Path(".code-review-checklist.yaml")


def resolve_checklist(override_path: Path | None = None) -> Path:
    """Resolve checklist path with fallback chain:

    1. Explicit path (--checklist argument) — must exist if provided
    2. .code-review-checklist.yaml (project-local customization)
    3. Built-in default (shipped with the package)
    """
    if override_path is not None:
        if not override_path.exists():
            msg = f"Checklist not found: {override_path}"
            raise FileNotFoundError(msg)
        return override_path

    if LOCAL_CHECKLIST.exists():
        return LOCAL_CHECKLIST

    return Path(str(pkg_files("code_review_skill.data").joinpath("checklist.yaml")))


def load_checklist(checklist_path: Path) -> Checklist:
    """Load checklist YAML and build a lookup dict by item id."""
    data: dict[str, Any] = yaml.safe_load(checklist_path.read_text())
    version = str(data.get("version", "unknown"))
    items: dict[str, ChecklistItem] = {}
    for item in data.get("items", []):
        items[item["id"]] = ChecklistItem(
            id=item["id"],
            category=item["category"],
            level=item["level"],
            description=item.get("description", item["id"]),
        )
    return Checklist(version=version, items=items)
