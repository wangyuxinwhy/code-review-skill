"""Report rendering — annotated source output."""

import json
from pathlib import Path
from typing import cast

from code_review_skill.types import CacheFile, FileTarget, ReviewSummary, SymbolTarget


def _format_summary(summary: ReviewSummary) -> str:
    parts: list[str] = []
    if summary["blocking_failures"]:
        parts.append(f"{summary['blocking_failures']} blocking")
    if summary["advisory_failures"]:
        parts.append(f"{summary['advisory_failures']} advisory")
    parts.append(f"{summary['passed']} passed")
    if summary["blocked"]:
        parts.append(f"{summary['blocked']} blocked")
    return f"Findings: {', '.join(parts)} | Symbols: {summary['symbols_reviewed']} reviewed"


def _read_source_lines(file_path: str | Path) -> list[str] | None:
    try:
        return Path(file_path).read_text().splitlines()
    except OSError:
        return None


def _annotate_source(
    source_lines: list[str],
    start: int,
    end: int,
    annotations: dict[int, str],
) -> str:
    """Render source lines start..end (1-indexed inclusive) with inline annotations.

    annotations maps absolute line number -> message.
    """
    start = max(start, 1)
    out: list[str] = []
    width = len(str(end))
    for lineno in range(start, end + 1):
        idx = lineno - 1
        line_text = source_lines[idx] if idx < len(source_lines) else ""
        out.append(f"  {lineno:>{width}} | {line_text}")
        if lineno in annotations:
            marker = " " * width + "   "
            out.append(f"  {marker} ^ {annotations[lineno]}")
    return "\n".join(out)


def show(cache_path: Path) -> str:
    """Render actionable findings as annotated source for curator review.

    Reads cache.json, filters to failed/blocked checks, reads source files,
    and produces a diagnostic report with inline annotations.
    Caller is responsible for refreshing the cache beforehand if needed.
    """
    if not cache_path.exists():
        raise FileNotFoundError(f"Cache file not found: {cache_path}")

    cache: CacheFile = json.loads(cache_path.read_text())
    if cache.get("version") != "3":
        raise ValueError(f"Unsupported cache version: {cache.get('version')}")

    summary: ReviewSummary = cache["summary"]
    out: list[str] = [f"## {_format_summary(summary)}", ""]

    source_cache: dict[str, list[str] | None] = {}

    for target_entry in cache.get("targets", []):
        failed_checks = [check for check in target_entry.get("checks", []) if check.get("pass") is not True]
        if not failed_checks:
            continue

        target = target_entry["target"]
        target_type = target["type"]

        # Build header
        match target_type:
            case "symbol":
                sym = cast("SymbolTarget", target)
                file_path = sym["file"]
                start, end = sym["lines"]
                header = f"### {sym['symbol']}  {file_path}:{start}-{end}"
            case "file":
                ft = cast("FileTarget", target)
                file_path = ft["file"]
                start, end = 1, 0  # will be set per-annotation
                header = f"### File: {file_path}"
            case _:
                header = "### Changeset"
                file_path = ""
                start, end = 0, 0

        out.append(header)

        # List failed checks as diagnostics
        for check in failed_checks:
            check_id = check.get("id", "?")
            level = check.get("level", "advisory").upper()
            note = check.get("note", "")
            out.append(f"[{level} {check_id}] {note}")

        # Render annotated source for symbol targets
        if target_type == "symbol" and file_path:
            if file_path not in source_cache:
                source_cache[file_path] = _read_source_lines(file_path)
            source_lines = source_cache[file_path]
            if source_lines:
                # Collect all annotations: offset -> absolute line
                annotation_map: dict[int, str] = {}
                for check in failed_checks:
                    for annotation in check.get("annotations", []):
                        abs_line = annotation["offset"] + start
                        annotation_map[abs_line] = f"[{check.get('id', '?')}] {annotation['message']}"
                out.append("```")
                out.append(_annotate_source(source_lines, start, end, annotation_map))
                out.append("```")

        out.append("")

    return "\n".join(out)
