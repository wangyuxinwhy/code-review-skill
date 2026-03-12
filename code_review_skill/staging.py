"""Staging I/O, enrichment, sorting, annotation conversion, and merge logic."""

import json
from collections.abc import Iterable, Mapping
from importlib.resources import files as pkg_files
from pathlib import Path
from typing import Any, Literal, cast

import yaml

from code_review_skill.types import (
    CATEGORY_ORDER,
    TARGET_TYPE_ORDER,
    Checklist,
    ChecklistItem,
    CheckResult,
    MergeResult,
    ReviewSummary,
    StagingCheck,
    StagingEntry,
    StagingSymbolEntry,
    SymbolTarget,
    TargetEntry,
)

LOCAL_CHECKLIST = Path(".code-review-checklist.yaml")


def resolve_checklist(explicit: Path | None = None) -> Path:
    """Resolve checklist path with fallback chain:

    1. Explicit path (--checklist argument) — must exist if provided
    2. .code-review-checklist.yaml (project-local customization)
    3. Built-in default (shipped with the package)
    """
    if explicit is not None:
        if not explicit.exists():
            msg = f"Checklist not found: {explicit}"
            raise FileNotFoundError(msg)
        return explicit

    if LOCAL_CHECKLIST.exists():
        return LOCAL_CHECKLIST

    return Path(str(pkg_files("code_review_skill.data").joinpath("checklist.yaml")))


# --- Checklist ---


def load_checklist(checklist_path: Path) -> Checklist:
    """Load checklist YAML and build a lookup dict by item id."""
    data = yaml.safe_load(checklist_path.read_text())
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


# --- Staging I/O ---


def load_staging_files(staging_dir: Path) -> list[StagingEntry]:
    files = sorted(path for path in staging_dir.glob("*.json") if not path.name.startswith("_"))
    return [json.loads(file_path.read_text()) for file_path in files]


# --- Enrichment and sorting ---


def enrich_check(check: StagingCheck | CheckResult, checklist_items: Mapping[str, ChecklistItem]) -> CheckResult:
    """Fill in category/level/summary/status from checklist when missing."""
    check_id = check.get("id", "")
    item = checklist_items.get(check_id)
    enriched = dict(check)
    if item:
        enriched.setdefault("category", item["category"])
        enriched.setdefault("level", item["level"])
        enriched.setdefault("description", item["description"])
    # Derive status from pass field
    if "status" not in enriched:
        pass_value = enriched.get("pass")
        if pass_value is True:
            enriched["status"] = "passed"
        elif pass_value is False:
            enriched["status"] = "failed"
        else:
            enriched["status"] = "blocked"
    return cast("CheckResult", enriched)


def sort_checks(checks: Iterable[CheckResult]) -> list[CheckResult]:
    return sorted(checks, key=lambda check: CATEGORY_ORDER.get(check.get("category", ""), 99))


def target_sort_key(entry: TargetEntry) -> tuple[int, str, int]:
    target = entry["target"]
    type_order = TARGET_TYPE_ORDER.get(target["type"], 99)

    match target:
        case {"type": "file", "file": file}:
            return (type_order, file, 0)
        case {"type": "symbol", "file": file, "lines": [start, *_]}:
            return (type_order, file, start)
        case _:
            return (type_order, "", 0)


def has_non_pass(checks: Iterable[CheckResult]) -> bool:
    """True if any check is not a clean pass (failed, blocked, or missing pass field)."""
    for check in checks:
        if "pass" in check:
            if check["pass"] is not True:
                return True
        elif check.get("status") in ("failed", "blocked"):
            return True
    return False


def _extract_symbol_entries(staging: StagingEntry) -> list[StagingSymbolEntry]:
    """Extract symbol entries from staging, supporting both grouped and per-symbol formats.

    Grouped (legacy): { "stage": "symbol", "targets": [{ "target": ..., "checks": ... }, ...] }
    Per-symbol:       { "stage": "symbol", "target": ..., "checks": [...] }
    """
    match staging:
        case {"targets": targets}:
            return targets
        case {"symbols": symbols}:
            return symbols
        case {"target": target}:
            return [{"target": target, "checks": staging.get("checks", [])}]
        case _:
            return []


def _normalize_symbol_target(entry: StagingSymbolEntry, fallback_file: str) -> SymbolTarget:
    """Handle subagent format variants for symbol targets."""
    if "target" in entry:
        return cast("SymbolTarget", entry["target"])
    lines = entry.get("lines", (0, 0))
    return SymbolTarget(
        type="symbol",
        file=fallback_file,
        symbol=entry.get("symbol", entry.get("name", "")),
        lines=(lines[0], lines[1]),
    )


# --- Annotation conversion ---


def _convert_annotations_to_offsets(checks: Iterable[Mapping[str, Any]], base_line: int) -> list[dict[str, Any]]:
    """Convert annotation absolute line numbers to offsets relative to base_line.

    For file targets: base_line = 1 (offset becomes 0-indexed from file start).
    For symbol targets: base_line = symbol start line.
    """
    result: list[dict[str, Any]] = []
    for check in checks:
        check_copy = dict(check)
        if check_copy.get("annotations"):
            check_copy["annotations"] = [
                {"offset": annotation["line"] - base_line, "message": annotation["message"]}
                for annotation in check_copy["annotations"]
            ]
        result.append(check_copy)
    return result


def _convert_offsets_to_lines(checks: Iterable[Mapping[str, Any]], base_line: int) -> list[dict[str, Any]]:
    """Restore cached offset-based annotations to absolute line numbers for staging output."""
    result: list[dict[str, Any]] = []
    for check in checks:
        check_copy = dict(check)
        if check_copy.get("annotations"):
            check_copy["annotations"] = [
                {"line": annotation["offset"] + base_line, "message": annotation["message"]}
                for annotation in check_copy["annotations"]
            ]
        result.append(check_copy)
    return result


# --- Merge logic ---


def merge_staging(
    staging_files: Iterable[StagingEntry],
    checklist_items: Mapping[str, ChecklistItem] | None = None,
) -> MergeResult:
    """Counts include all-pass symbols that are filtered out of the returned targets."""
    checklist_lookup = checklist_items or {}
    all_entries: list[TargetEntry] = []
    filtered_targets: list[TargetEntry] = []
    symbols_reviewed = 0

    for staging in staging_files:
        stage = staging.get("stage", "")

        match stage:
            case "changeset" | "file":
                checks = [enrich_check(check, checklist_lookup) for check in staging.get("checks", [])]
                target = staging.get("target")
                if target is None:
                    continue
                entry = TargetEntry(
                    target=target,
                    checks=sort_checks(checks),
                )
                all_entries.append(entry)
                filtered_targets.append(entry)

            case "symbol":
                file_path = staging.get("file", "")
                symbol_entries = _extract_symbol_entries(staging)
                for symbol_entry in symbol_entries:
                    symbols_reviewed += 1
                    target = _normalize_symbol_target(symbol_entry, file_path)
                    checks = [enrich_check(check, checklist_lookup) for check in symbol_entry.get("checks", [])]
                    sorted_checks = sort_checks(checks)
                    entry = TargetEntry(target=target, checks=sorted_checks)
                    all_entries.append(entry)
                    if has_non_pass(sorted_checks):
                        filtered_targets.append(entry)

            case _:
                pass

    filtered_targets.sort(key=target_sort_key)
    summary = _count_checks(all_entries, symbols_reviewed)
    return MergeResult(filtered_targets, symbols_reviewed, summary)


def _count_checks(entries: Iterable[TargetEntry], symbols_reviewed: int) -> ReviewSummary:
    blocking_failures = 0
    advisory_failures = 0
    passed = 0
    blocked = 0

    for entry in entries:
        for check in entry["checks"]:
            pass_value = check.get("pass")
            status: Literal["passed", "failed", "blocked"] | None = check.get("status")
            if status is None:
                if pass_value is True:
                    status = "passed"
                elif pass_value is False:
                    status = "failed"
                else:
                    status = "blocked"
            level = check.get("level", "advisory")

            match (status, level):
                case ("passed", _):
                    passed += 1
                case ("failed", "blocking"):
                    blocking_failures += 1
                case ("failed", _):
                    advisory_failures += 1
                case ("blocked", _):
                    blocked += 1

    return ReviewSummary(
        blocking_failures=blocking_failures,
        advisory_failures=advisory_failures,
        passed=passed,
        blocked=blocked,
        symbols_reviewed=symbols_reviewed,
    )
