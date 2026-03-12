# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Review pipeline — symbol extraction, incremental cache, and staging merge.

Subcommands:
  symbols  Extract symbol definitions from a Python file using AST.
  check    Compare file/symbol content hashes against cache, pre-write staging for hits.
  build    Merge staging files, compute cache entries, and write combined cache.json.
  show     Render actionable findings as annotated source.
  refresh  Self-heal cache — rescan files, match by content hash, rebuild targets.
"""

import argparse
import ast
import hashlib
import json
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, NamedTuple, NotRequired, TypedDict, cast

import yaml

CATEGORY_ORDER: dict[str, int] = {
    "design": 1,
    "correctness": 2,
    "readability": 3,
}

TARGET_TYPE_ORDER: dict[str, int] = {
    "changeset": 0,
    "file": 1,
    "symbol": 2,
}


# --- Target descriptors ---


class ChangesetTarget(TypedDict):
    type: Literal["changeset"]


class FileTarget(TypedDict):
    type: Literal["file"]
    file: str


class SymbolTarget(TypedDict):
    type: Literal["symbol"]
    file: str
    symbol: str
    lines: tuple[int, int]


TargetDescriptor = ChangesetTarget | FileTarget | SymbolTarget


# --- AST Symbol Extraction ---


class SymbolDef(TypedDict):
    name: str
    type: Literal["function", "method", "class"]
    lines: tuple[int, int]


def extract_symbols(source: str) -> list[SymbolDef]:
    """Parse Python source with AST, return function/class definitions with
    exact line boundaries. Decorators are excluded — lineno points to the
    def/class keyword, end_lineno to the last line of the body."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    symbols: list[SymbolDef] = []
    _visit_symbols(tree, symbols, prefix="")
    return symbols


def _visit_symbols(
    node: ast.AST,
    symbols: list[SymbolDef],
    prefix: str,
) -> None:
    for child in ast.iter_child_nodes(node):
        match child:
            case ast.FunctionDef() | ast.AsyncFunctionDef():
                symbol_type = "method" if isinstance(node, ast.ClassDef) else "function"
            case ast.ClassDef():
                symbol_type = "class"
            case _:
                continue
        qualified = f"{prefix}.{child.name}" if prefix else child.name
        symbols.append(
            SymbolDef(
                name=qualified,
                type=symbol_type,
                lines=(child.lineno, child.end_lineno or child.lineno),
            )
        )
        _visit_symbols(child, symbols, qualified)


def _get_diff_hunks(file_path: str, diff_range: str) -> list[tuple[int, int]]:
    """Parse git diff hunks into (start, end) line ranges (1-indexed, inclusive)."""
    try:
        diff_output = subprocess.run(
            ["git", "diff", diff_range, "--unified=0", "--", file_path],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []

    hunks: list[tuple[int, int]] = []
    for line in diff_output.stdout.splitlines():
        if not line.startswith("@@"):
            continue
        # Parse @@ -old,count +new,count @@ format
        parts = line.split("+", 1)
        if len(parts) < 2:
            continue
        new_range_parts = parts[1].split("@@")[0].strip().split(",")
        start = int(new_range_parts[0])
        count = int(new_range_parts[1]) if len(new_range_parts) > 1 else 1
        if count > 0:
            hunks.append((start, start + count - 1))
    return hunks


def _ranges_overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


def _filter_symbols_by_diff(symbols: Iterable[SymbolDef], diff_hunks: Sequence[tuple[int, int]]) -> list[SymbolDef]:
    return [symbol for symbol in symbols if any(_ranges_overlap(symbol["lines"], hunk) for hunk in diff_hunks)]


# --- Check ---


class Annotation(TypedDict):
    offset: int
    message: str


class StagingAnnotation(TypedDict):
    line: int
    message: str


# Functional form because `pass` is a Python keyword.
CheckResult = TypedDict(
    "CheckResult",
    {
        "id": str,
        "pass": bool | None,
        "category": NotRequired[str],
        "level": NotRequired[Literal["blocking", "advisory"]],
        "status": NotRequired[Literal["passed", "failed", "blocked"]],
        "description": NotRequired[str],
        "note": NotRequired[str],
        "annotations": NotRequired[list[Annotation]],
    },
)

StagingCheck = TypedDict(
    "StagingCheck",
    {
        "id": str,
        "pass": bool | None,
        "note": NotRequired[str],
        "annotations": NotRequired[list[StagingAnnotation]],
    },
)


# --- Composite types ---


class StagingSymbolEntry(TypedDict, total=False):
    target: TargetDescriptor
    checks: list[StagingCheck]
    symbol: str
    name: str
    file: str
    lines: tuple[int, int]


class StagingEntry(TypedDict, total=False):
    """A single staging file's content. Fields vary by stage type."""

    stage: str
    target: TargetDescriptor
    checks: list[StagingCheck]
    file: str
    targets: list[StagingSymbolEntry]
    symbols: list[StagingSymbolEntry]


class TargetEntry(TypedDict):
    target: TargetDescriptor
    checks: list[CheckResult]


class ReviewSummary(TypedDict):
    blocking_failures: int
    advisory_failures: int
    passed: int
    blocked: int
    symbols_reviewed: int


class MergeResult(NamedTuple):
    targets: list[TargetEntry]
    symbols_reviewed: int
    summary: ReviewSummary


class ChecklistItem(TypedDict):
    id: str
    category: str
    level: Literal["blocking", "advisory"]
    description: str


class Checklist(TypedDict):
    version: str
    items: dict[str, ChecklistItem]


# --- Cache types (v3: hash-keyed) ---


class CacheChecks(TypedDict):
    checks: list[CheckResult]


class CacheFile(TypedDict):
    version: str
    timestamp: str
    checklist_version: str
    summary: ReviewSummary
    targets: list[TargetEntry]
    files: dict[str, CacheChecks]
    symbols: dict[str, CacheChecks]


class CacheStats(TypedDict):
    file_hit: int
    file_miss: int
    symbol_hit: int
    symbol_miss: int


class CheckOutput(TypedDict):
    cached_files: list[str]
    review_files: list[str]
    cached_symbols: dict[str, list[str]]
    review_symbols: dict[str, list[str]]
    stats: CacheStats


# --- Hashing ---


def compute_file_hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def compute_symbol_hash(path: Path, lines: tuple[int, int]) -> str:
    """Hash a symbol's source lines (1-indexed, inclusive range)."""
    all_lines = path.read_text().splitlines()
    start, end = lines[0], lines[1]
    selected = all_lines[start - 1 : end]
    content = "\n".join(selected)
    return "sha256:" + hashlib.sha256(content.encode()).hexdigest()


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
            status = check.get("status")
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


# --- Cache: check ---


def load_cache(cache_path: Path, checklist_path: Path) -> CacheFile | None:
    """Load v3 cache file, returning None if missing, wrong version, or checklist mismatch."""
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if data.get("version") != "3" or "files" not in data:
        return None
    checklist = load_checklist(checklist_path)
    if data.get("checklist_version") != checklist["version"]:
        return None
    return data


class _FileCacheResult(NamedTuple):
    cached_files: list[str]
    review_files: list[str]
    hit: int
    miss: int


class _SymbolCacheResult(NamedTuple):
    cached_symbols: dict[str, list[str]]
    review_symbols: dict[str, list[str]]
    symbol_targets: list[TargetEntry]
    hit: int
    miss: int


def _check_file_cache(
    files: Iterable[str],
    cache: CacheFile | None,
    staging_dir: Path,
) -> _FileCacheResult:
    """Partition files into cached/review by content hash, pre-write staging for hits."""
    cached: list[str] = []
    review: list[str] = []
    hit = 0
    miss = 0

    for file_str in files:
        file_path = Path(file_str)
        if not file_path.exists():
            review.append(file_str)
            miss += 1
            continue

        content_hash = compute_file_hash(file_path)
        file_entry = cache["files"].get(content_hash) if cache else None

        if file_entry:
            cached.append(file_str)
            hit += 1
            _write_cached_staging(file_str, file_entry, staging_dir)
        else:
            review.append(file_str)
            miss += 1

    return _FileCacheResult(cached, review, hit, miss)


def _check_symbol_cache(
    file_list: Sequence[str],
    cache: CacheFile | None,
) -> _SymbolCacheResult:
    """Look up per-symbol content hashes against cache for a list of files.

    Uncached symbols are tracked in review_symbols; the caller decides which to use.
    """
    cached_symbols: dict[str, list[str]] = {}
    review_symbols: dict[str, list[str]] = {}
    symbol_targets: list[TargetEntry] = []
    hit = 0
    miss = 0

    for file_str in file_list:
        file_path = Path(file_str)
        if not file_path.exists():
            continue
        try:
            source = file_path.read_text()
            symbols = extract_symbols(source)
        except OSError:
            continue

        file_cached: list[str] = []
        file_review: list[str] = []
        for symbol in symbols:
            try:
                symbol_hash = compute_symbol_hash(file_path, symbol["lines"])
            except (IndexError, ValueError):
                file_review.append(symbol["name"])
                miss += 1
                continue
            symbol_entry = cache["symbols"].get(symbol_hash) if cache else None
            if symbol_entry:
                file_cached.append(symbol["name"])
                hit += 1
                symbol_targets.append(_restore_symbol_target(file_str, symbol, symbol_entry))
            else:
                file_review.append(symbol["name"])
                miss += 1
        if file_cached:
            cached_symbols[file_str] = file_cached
        if file_review:
            review_symbols[file_str] = file_review

    return _SymbolCacheResult(cached_symbols, review_symbols, symbol_targets, hit, miss)


def check(
    files: list[str],
    cache_path: Path,
    checklist_path: Path,
    staging_dir: Path,
) -> CheckOutput:
    """Compare files against cache at file and symbol level.

    File-level: hash entire file, look up in cache["files"].
    Symbol-level: for each file, extract symbols via AST, hash each symbol,
    look up in cache["symbols"]. Pre-write staging for all cache hits.
    """
    cache = load_cache(cache_path, checklist_path)

    file_result = _check_file_cache(files, cache, staging_dir)

    symbol_cached = _check_symbol_cache(file_result.cached_files, cache)
    symbol_review = _check_symbol_cache(file_result.review_files, cache)

    cached_symbols = {**symbol_cached.cached_symbols, **symbol_review.cached_symbols}
    review_symbols = symbol_review.review_symbols
    symbol_hit = symbol_cached.hit + symbol_review.hit
    symbol_miss = symbol_cached.miss + symbol_review.miss
    all_symbol_targets = symbol_cached.symbol_targets + symbol_review.symbol_targets

    # Write symbol-cached.json
    if all_symbol_targets:
        symbol_staging = {"stage": "symbol", "targets": all_symbol_targets}
        sym_path = staging_dir / "symbol-cached.json"
        sym_path.write_text(json.dumps(symbol_staging, indent=2, ensure_ascii=False) + "\n")

    return CheckOutput(
        cached_files=file_result.cached_files,
        review_files=file_result.review_files,
        cached_symbols=cached_symbols,
        review_symbols=review_symbols,
        stats=CacheStats(
            file_hit=file_result.hit,
            file_miss=file_result.miss,
            symbol_hit=symbol_hit,
            symbol_miss=symbol_miss,
        ),
    )


def _write_cached_staging(file_str: str, cache_checks: CacheChecks, staging_dir: Path) -> None:
    """Write a cached file's staging file, converting offsets back to absolute lines."""
    checks_with_lines = _convert_offsets_to_lines(cache_checks["checks"], base_line=1)
    staging = {
        "stage": "file",
        "target": {"type": "file", "file": file_str},
        "checks": checks_with_lines,
    }
    sanitized = file_str.replace("/", "-").replace(".", "-")
    staging_path = staging_dir / f"file-{sanitized}.json"
    staging_path.write_text(json.dumps(staging, indent=2, ensure_ascii=False) + "\n")


def _restore_symbol_target(file_str: str, symbol_def: SymbolDef, cache_checks: CacheChecks) -> TargetEntry:
    """Reconstruct a symbol staging target from cache, converting offsets to lines."""
    checks_with_lines = _convert_offsets_to_lines(cache_checks["checks"], base_line=symbol_def["lines"][0])
    return TargetEntry(
        target=SymbolTarget(
            type="symbol",
            file=file_str,
            symbol=symbol_def["name"],
            lines=(symbol_def["lines"][0], symbol_def["lines"][1]),
        ),
        checks=cast("list[CheckResult]", checks_with_lines),
    )


# --- Cache: build ---


def _build_files_cache(staging_files: Iterable[StagingEntry]) -> dict[str, CacheChecks]:
    """Only processes entries with stage='file'; skips others."""
    files_cache: dict[str, CacheChecks] = {}
    for staging in staging_files:
        if staging.get("stage") != "file":
            continue
        target = staging.get("target")
        if target is None or "file" not in target:
            continue
        file_str = target["file"]
        file_path = Path(file_str)
        if not file_path.exists():
            continue
        content_hash = compute_file_hash(file_path)
        offset_checks = _convert_annotations_to_offsets(staging.get("checks", []), base_line=1)
        files_cache[content_hash] = CacheChecks(checks=cast("list[CheckResult]", offset_checks))
    return files_cache


def _build_symbols_cache(staging_files: Iterable[StagingEntry]) -> dict[str, CacheChecks]:
    """Handles both grouped and per-symbol staging formats; silently skips entries
    with missing files or unparseable line ranges."""
    symbols_cache: dict[str, CacheChecks] = {}
    for staging in staging_files:
        if staging.get("stage") != "symbol":
            continue
        symbol_entries = _extract_symbol_entries(staging)
        for symbol_entry in symbol_entries:
            fallback_file = staging.get("file", "")
            sym_target = _normalize_symbol_target(symbol_entry, fallback_file)
            file_str = sym_target["file"]
            lines = sym_target["lines"]

            file_path = Path(file_str)
            if not file_path.exists():
                continue

            try:
                symbol_hash = compute_symbol_hash(file_path, lines)
            except (IndexError, ValueError):
                continue

            offset_checks = _convert_annotations_to_offsets(symbol_entry.get("checks", []), base_line=lines[0])
            symbols_cache[symbol_hash] = CacheChecks(checks=cast("list[CheckResult]", offset_checks))
    return symbols_cache


def build(
    staging_dir: Path,
    cache_path: Path,
    checklist_path: Path,
) -> CacheFile:
    """Load staging files, enrich checks from checklist, assemble hash-keyed
    cache sections, and write the combined cache.json v3 to disk."""
    staging_files = load_staging_files(staging_dir)
    if not staging_files:
        raise ValueError("No staging files found")

    checklist = load_checklist(checklist_path)
    checklist_version = checklist["version"]
    targets, _symbols_reviewed, summary = merge_staging(staging_files, checklist["items"])

    # Convert annotations from absolute line to offset in targets
    for target_entry in targets:
        target = target_entry["target"]
        match target:
            case {"type": "symbol", "lines": [start, *_]}:
                base_line = start
            case {"type": "file"}:
                base_line = 1
            case _:
                continue
        target_entry["checks"] = cast(
            "list[CheckResult]",
            _convert_annotations_to_offsets(target_entry["checks"], base_line),
        )

    files_cache = _build_files_cache(staging_files)
    symbols_cache = _build_symbols_cache(staging_files)

    # Assemble combined cache file
    cache_data = CacheFile(
        version="3",
        timestamp=datetime.now(UTC).isoformat(),
        checklist_version=checklist_version,
        summary=summary,
        targets=targets,
        files=files_cache,
        symbols=symbols_cache,
    )
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache_data, indent=2, ensure_ascii=False) + "\n")

    return cache_data


# --- Show ---


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


def _read_source_lines(file_path: str) -> list[str] | None:
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


def show(cache_path: Path, root: Path | None = None) -> str:
    """Render actionable findings as annotated source for curator review.

    Auto-refreshes the cache before rendering to ensure file paths and line
    numbers are current. Reads cache.json, filters to failed/blocked checks,
    reads source files, and produces a diagnostic report with inline annotations.
    """
    if not cache_path.exists():
        raise FileNotFoundError(f"Cache file not found: {cache_path}")

    # Auto-refresh: heal stale paths/line numbers before rendering
    resolved_root = (root or Path(".")).resolve()
    refresh(cache_path, resolved_root)

    data = json.loads(cache_path.read_text())
    if data.get("version") != "3":
        raise ValueError(f"Unsupported cache version: {data.get('version')}")

    summary: ReviewSummary = data["summary"]
    out: list[str] = [f"## {_format_summary(summary)}", ""]

    # Cache of read source files
    source_cache: dict[str, list[str] | None] = {}

    for target_entry in data.get("targets", []):
        failed_checks = [check for check in target_entry.get("checks", []) if check.get("pass") is not True]
        if not failed_checks:
            continue

        target = target_entry["target"]
        target_type = target["type"]

        # Build header
        match target_type:
            case "symbol":
                file_path = target["file"]
                start, end = target["lines"]
                header = f"### {target['symbol']}  {file_path}:{start}-{end}"
            case "file":
                file_path = target["file"]
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


# --- Refresh (self-heal) ---


class RefreshStats(TypedDict):
    files_scanned: int
    file_hit: int
    symbol_hit: int
    targets_before: int
    targets_after: int
    orphaned_file_hashes: int
    orphaned_symbol_hashes: int
    fresh: bool


def _discover_python_files(root: Path) -> list[Path]:
    """Find all .py files under root, excluding hidden dirs and common non-source dirs."""
    exclude = {".git", ".venv", "venv", "__pycache__", "node_modules", ".tox", ".mypy_cache"}
    results: list[Path] = []
    for path in root.rglob("*.py"):
        if any(part in exclude for part in path.parts):
            continue
        results.append(path)
    return sorted(results)


def _verify_targets(data: CacheFile, root: Path) -> bool:
    """Fast path: check if all existing targets still have matching content hashes.

    Returns True if cache is fresh (all hashes match current files), False if stale.
    """
    files_cache = data.get("files", {})
    symbols_cache = data.get("symbols", {})

    for target_entry in data.get("targets", []):
        target = target_entry["target"]

        match target:
            case {"type": "file", "file": file_str}:
                file_path = root / file_str
                if not file_path.exists():
                    return False
                if compute_file_hash(file_path) not in files_cache:
                    return False

            case {"type": "symbol", "file": file_str, "lines": [start, end]}:
                file_path = root / file_str
                if not file_path.exists():
                    return False
                try:
                    if compute_symbol_hash(file_path, (start, end)) not in symbols_cache:
                        return False
                except (IndexError, ValueError):
                    return False

            case _:
                pass

    return True


def refresh(cache_path: Path, root: Path) -> RefreshStats:
    """Self-heal cache by rescanning files and matching content hashes.

    Fast path: verify existing targets' hashes still match. If all match,
    the cache is fresh and no work is needed.

    Slow path: if any target is stale, rescan all Python files under root,
    match content hashes against the cache's hash-keyed sections, and
    rebuild targets with current file paths and line numbers.
    """
    if not cache_path.exists():
        raise FileNotFoundError(f"Cache file not found: {cache_path}")
    data: CacheFile = json.loads(cache_path.read_text())
    if data.get("version") != "3":
        raise ValueError(f"Unsupported cache version: {data.get('version')}")

    files_cache = data.get("files", {})
    symbols_cache = data.get("symbols", {})
    targets_before = len(data.get("targets", []))

    # Fast path: verify existing targets are still valid
    if _verify_targets(data, root):
        return RefreshStats(
            files_scanned=0,
            file_hit=0,
            symbol_hit=0,
            targets_before=targets_before,
            targets_after=targets_before,
            orphaned_file_hashes=0,
            orphaned_symbol_hashes=0,
            fresh=True,
        )

    # Slow path: full rescan
    new_targets: list[TargetEntry] = [
        entry for entry in data.get("targets", []) if entry["target"]["type"] == "changeset"
    ]

    matched_file_hashes: set[str] = set()
    matched_symbol_hashes: set[str] = set()
    all_entries: list[TargetEntry] = list(new_targets)
    symbols_reviewed = 0

    python_files = _discover_python_files(root)
    files_scanned = 0

    for file_path in python_files:
        files_scanned += 1
        rel_path = str(file_path.relative_to(root))

        # File-level match
        file_hash = compute_file_hash(file_path)
        if file_hash in files_cache:
            matched_file_hashes.add(file_hash)
            cached_checks = files_cache[file_hash]
            entry = TargetEntry(
                target=FileTarget(type="file", file=rel_path),
                checks=cached_checks["checks"],
            )
            new_targets.append(entry)
            all_entries.append(entry)

        # Symbol-level match
        try:
            source = file_path.read_text()
            symbols = extract_symbols(source)
        except OSError:
            continue

        for symbol in symbols:
            try:
                symbol_hash = compute_symbol_hash(file_path, symbol["lines"])
            except (IndexError, ValueError):
                continue

            if symbol_hash in symbols_cache:
                matched_symbol_hashes.add(symbol_hash)
                symbols_reviewed += 1
                cached_checks = symbols_cache[symbol_hash]
                target = SymbolTarget(
                    type="symbol",
                    file=rel_path,
                    symbol=symbol["name"],
                    lines=(symbol["lines"][0], symbol["lines"][1]),
                )
                entry = TargetEntry(target=target, checks=cached_checks["checks"])
                all_entries.append(entry)
                if has_non_pass(entry["checks"]):
                    new_targets.append(entry)

    new_targets.sort(key=target_sort_key)
    summary = _count_checks(all_entries, symbols_reviewed)

    refreshed = CacheFile(
        version="3",
        timestamp=datetime.now(UTC).isoformat(),
        checklist_version=data.get("checklist_version", "unknown"),
        summary=summary,
        targets=new_targets,
        files=files_cache,
        symbols=symbols_cache,
    )
    cache_path.write_text(json.dumps(refreshed, indent=2, ensure_ascii=False) + "\n")

    return RefreshStats(
        files_scanned=files_scanned,
        file_hit=len(matched_file_hashes),
        symbol_hit=len(matched_symbol_hashes),
        targets_before=targets_before,
        targets_after=len(new_targets),
        orphaned_file_hashes=len(files_cache) - len(matched_file_hashes),
        orphaned_symbol_hashes=len(symbols_cache) - len(matched_symbol_hashes),
        fresh=False,
    )


# --- CLI ---


def main() -> None:
    parser = argparse.ArgumentParser(description="Review pipeline — symbols, cache, and merge")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # symbols subcommand
    symbols_parser = subparsers.add_parser("symbols", help="Extract symbols from a Python file using AST")
    symbols_parser.add_argument("--file", required=True, help="Python file to analyze")
    symbols_parser.add_argument("--diff", default=None, help="Git diff range to filter by changed hunks")

    # check subcommand
    check_parser = subparsers.add_parser("check", help="Check files against cache")
    check_parser.add_argument("--files", nargs="+", required=True, help="Files to check")
    check_parser.add_argument("--cache", type=Path, default=Path(".claude/review/cache.json"))
    check_parser.add_argument("--checklist", type=Path, default=Path(".claude/review/checklist.yaml"))
    check_parser.add_argument("--staging", type=Path, default=Path(".claude/review/staging"))

    # build subcommand (replaces merge.py + old update)
    build_parser = subparsers.add_parser("build", help="Merge staging and build cache")
    build_parser.add_argument("--staging", type=Path, default=Path(".claude/review/staging"))
    build_parser.add_argument("--cache", type=Path, default=Path(".claude/review/cache.json"))
    build_parser.add_argument("--checklist", type=Path, default=Path(".claude/review/checklist.yaml"))

    # show subcommand
    show_parser = subparsers.add_parser("show", help="Show findings from cache.json")
    show_parser.add_argument("--cache", type=Path, default=Path(".claude/review/cache.json"))

    # refresh subcommand
    refresh_parser = subparsers.add_parser(
        "refresh", help="Self-heal cache — rescan files, match by hash, rebuild targets"
    )
    refresh_parser.add_argument("--cache", type=Path, default=Path(".claude/review/cache.json"))
    refresh_parser.add_argument(
        "--root",
        type=Path,
        default=Path("."),
        help="Project root to scan for Python files",
    )

    args = parser.parse_args()

    match args.command:
        case "symbols":
            file_path = Path(args.file)
            if not file_path.exists():
                print(f"File not found: {args.file}", file=sys.stderr)
                sys.exit(1)
            source = file_path.read_text()
            symbols = extract_symbols(source)
            if args.diff:
                diff_hunks = _get_diff_hunks(args.file, args.diff)
                symbols = _filter_symbols_by_diff(symbols, diff_hunks)
            print(json.dumps(symbols, indent=2))
        case "check":
            result = check(
                files=args.files,
                cache_path=args.cache,
                checklist_path=args.checklist,
                staging_dir=args.staging,
            )
            print(json.dumps(result, indent=2))
        case "build":
            try:
                cache_data = build(
                    staging_dir=args.staging,
                    cache_path=args.cache,
                    checklist_path=args.checklist,
                )
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                sys.exit(1)
            summary = cache_data["summary"]
            print(f"Build complete: {args.cache}")
            print(
                f"  {summary['blocking_failures']} blocking, "
                f"{summary['advisory_failures']} advisory, "
                f"{summary['passed']} passed, "
                f"{summary['blocked']} blocked"
            )
            print(f"  Symbols reviewed: {summary['symbols_reviewed']}")
            print(f"  Cache: {len(cache_data['files'])} file(s), {len(cache_data['symbols'])} symbol(s)")
        case "show":
            try:
                report = show(cache_path=args.cache)
            except (FileNotFoundError, ValueError) as exc:
                print(str(exc), file=sys.stderr)
                sys.exit(1)
            print(report)
        case "refresh":
            try:
                stats = refresh(cache_path=args.cache, root=args.root.resolve())
            except (FileNotFoundError, ValueError) as exc:
                print(str(exc), file=sys.stderr)
                sys.exit(1)
            if stats["fresh"]:
                print(f"Cache is fresh: {args.cache}")
            else:
                print(f"Refresh complete: {args.cache}")
                print(f"  Scanned: {stats['files_scanned']} files")
                print(f"  Matched: {stats['file_hit']} file(s), {stats['symbol_hit']} symbol(s)")
                print(f"  Targets: {stats['targets_before']} -> {stats['targets_after']}")
                if stats["orphaned_file_hashes"] or stats["orphaned_symbol_hashes"]:
                    print(
                        f"  Orphaned hashes: {stats['orphaned_file_hashes']} file(s), "
                        f"{stats['orphaned_symbol_hashes']} symbol(s)"
                    )
        case _:
            pass


if __name__ == "__main__":
    main()
