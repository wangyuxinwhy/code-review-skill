"""Report rendering — annotated source output."""

import json
from pathlib import Path

from code_review_skill.types import (
    CacheFile,
    CheckResult,
    ReviewSummary,
    TargetDescriptor,
    TargetEntry,
)


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


class ReportRenderer:
    """Render actionable findings as annotated source for curator review.

    Encapsulates the output buffer and source file cache as instance state,
    allowing per-target rendering methods to share them without parameter passing.
    """

    def __init__(self, cache: CacheFile) -> None:
        self.cache = cache
        self.out: list[str] = []
        self.source_cache: dict[str, list[str] | None] = {}

    def render(self) -> str:
        summary: ReviewSummary = self.cache["summary"]
        self.out = [f"## {_format_summary(summary)}", ""]
        for target_entry in self.cache.get("targets", []):
            self._render_target(target_entry)
        return "\n".join(self.out)

    def _render_target(self, target_entry: TargetEntry) -> None:
        failed_checks = [c for c in target_entry.get("checks", []) if c.get("pass") is not True]
        if not failed_checks:
            return

        target = target_entry["target"]
        header, file_path, start, end = self._build_header(target)
        self.out.append(header)

        for check in failed_checks:
            check_id = check.get("id", "?")
            level = check.get("level", "advisory").upper()
            note = check.get("note", "")
            self.out.append(f"[{level} {check_id}] {note}")

        if target["type"] == "symbol" and file_path:
            self._render_annotated_source(file_path, start, end, failed_checks)

        self.out.append("")

    def _build_header(self, target: TargetDescriptor) -> tuple[str, str, int, int]:
        match target["type"]:
            case "symbol":
                file_path = target["file"]
                start, end = target["lines"]
                return f"### {target['symbol']}  {file_path}:{start}-{end}", file_path, start, end
            case "file":
                return f"### File: {target['file']}", target["file"], 1, 0
            case _:
                return "### Changeset", "", 0, 0

    def _render_annotated_source(self, file_path: str, start: int, end: int, failed_checks: list[CheckResult]) -> None:
        if file_path not in self.source_cache:
            self.source_cache[file_path] = _read_source_lines(file_path)
        source_lines = self.source_cache[file_path]
        if not source_lines:
            return
        annotation_map: dict[int, str] = {}
        for check in failed_checks:
            for annotation in check.get("annotations", []):
                abs_line = annotation["offset"] + start
                annotation_map[abs_line] = f"[{check.get('id', '?')}] {annotation['message']}"
        self.out.append("```")
        self.out.append(_annotate_source(source_lines, start, end, annotation_map))
        self.out.append("```")


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
    return ReportRenderer(cache).render()
