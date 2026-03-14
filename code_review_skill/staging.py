"""Staging I/O, enrichment, sorting, annotation conversion, and merge logic."""

import json
from collections.abc import Iterable, Mapping
from importlib.resources import files as pkg_files
from pathlib import Path
from typing import Any, cast

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
    raw_checklist: dict[str, Any] = yaml.safe_load(checklist_path.read_text())
    version = str(raw_checklist.get("version", "unknown"))
    items: dict[str, ChecklistItem] = {}
    for item in raw_checklist.get("items", []):
        items[item["id"]] = ChecklistItem(
            id=item["id"],
            category=item["category"],
            level=item["level"],
            description=item.get("description", item["id"]),
        )
    return Checklist(version=version, items=items)


def load_staging_files(staging_dir: Path) -> list[StagingEntry]:
    files = sorted(path for path in staging_dir.glob("*.json") if not path.name.startswith("_"))
    return [cast("StagingEntry", json.loads(file_path.read_text())) for file_path in files]


def _derive_staging_filename(entry: StagingEntry) -> str:
    stage = entry.get("stage", "unknown")

    def sanitize(s: str) -> str:
        return s.replace("/", "-").replace(".", "-")

    match stage:
        case "changeset":
            return "changeset.json"
        case "file":
            target = entry.get("target", {})
            file_str = target.get("file", "unknown")
            return f"file-{sanitize(file_str)}.json"
        case "symbol":
            target = entry.get("target", {})
            file_str = target.get("file", "unknown")
            symbol_name = target.get("symbol", "unknown")
            return f"symbol-{sanitize(file_str)}-{sanitize(symbol_name)}.json"
        case _:
            return f"{stage}.json"


def write_staging_entry(staging_dir: Path, entry: StagingEntry) -> Path:
    """Write a staging entry to the staging directory, return the written path."""
    filename = _derive_staging_filename(entry)
    staging_dir.mkdir(parents=True, exist_ok=True)
    path = staging_dir / filename
    path.write_text(json.dumps(entry, indent=2, ensure_ascii=False) + "\n")
    return path


def enrich_check(check: StagingCheck | CheckResult, checklist_items: Mapping[str, ChecklistItem]) -> CheckResult:
    """Fill in category/level/summary/status from checklist when missing."""
    check_id = check.get("id", "")
    item = checklist_items.get(check_id)
    enriched = dict(check)
    if item:
        enriched.setdefault("category", item["category"])
        enriched.setdefault("level", item["level"])
        enriched.setdefault("description", item["description"])
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
    return any(
        (check["pass"] is not True) if "pass" in check
        else check.get("status") in ("failed", "blocked")
        for check in checks
    )


def _extract_symbol_entries(staging: StagingEntry) -> list[StagingSymbolEntry]:
    """Extract symbol entries from staging, supporting both grouped and per-symbol formats.

    Grouped (legacy): { "stage": "symbol", "targets": [{ "target": ..., "checks": ... }, ...] }
    Per-symbol:       { "stage": "symbol", "target": ..., "checks": [...] }
    """
    if "targets" in staging:
        return staging["targets"]
    elif "symbols" in staging:
        return staging["symbols"]
    elif "target" in staging:
        return [{"target": staging["target"], "checks": staging.get("checks", [])}]
    else:
        return []


def _normalize_symbol_target(entry: StagingSymbolEntry, fallback_file: str) -> SymbolTarget:
    """Handle subagent format variants for symbol targets."""
    if "target" in entry:
        return cast("SymbolTarget", entry["target"])
    lines = entry.get("lines", (0, 0))
    return SymbolTarget(
        type="symbol",
        file=fallback_file,
        # Fallback chain: 'symbol' (canonical) -> 'name' (legacy flat format) -> empty
        symbol=entry.get("symbol", entry.get("name", "")),
        lines=(lines[0], lines[1]),
    )


def _convert_annotations_to_offsets(
    checks: Iterable[StagingCheck | CheckResult], base_line: int,
) -> list[dict[str, Any]]:
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


def _convert_offsets_to_lines(
    checks: Iterable[StagingCheck | CheckResult], base_line: int,
) -> list[dict[str, Any]]:
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


def _enrich_and_collect_entries(
    staging_files: Iterable[StagingEntry],
    checklist_lookup: Mapping[str, ChecklistItem],
) -> tuple[list[TargetEntry], list[TargetEntry], int]:
    """Dispatch by stage, enrich checks, return (all_entries, filtered_targets, symbols_reviewed).

    All-pass symbols are included in all_entries (for summary counts)
    but excluded from filtered_targets.
    """
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

    return all_entries, filtered_targets, symbols_reviewed


def merge_staging(
    staging_files: Iterable[StagingEntry],
    checklist_items: Mapping[str, ChecklistItem] | None = None,
) -> MergeResult:
    """Merge staging files into sorted targets, enriching checks from checklist.

    All-pass symbols are counted but excluded from the returned targets.
    """
    checklist_lookup = checklist_items or {}
    all_entries, filtered_targets, symbols_reviewed = _enrich_and_collect_entries(
        staging_files, checklist_lookup,
    )
    filtered_targets.sort(key=target_sort_key)
    summary = _compute_summary(all_entries, symbols_reviewed)
    return MergeResult(filtered_targets, symbols_reviewed, summary)


def _compute_summary(entries: Iterable[TargetEntry], symbols_reviewed: int) -> ReviewSummary:
    """Count checks by status/level to build summary statistics."""
    blocking_failures = 0
    advisory_failures = 0
    passed = 0
    blocked = 0

    for entry in entries:
        for check in entry["checks"]:
            status = check.get("status")
            if status is None:
                pass_value = check.get("pass")
                status = "passed" if pass_value is True else "failed" if pass_value is False else "blocked"
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
