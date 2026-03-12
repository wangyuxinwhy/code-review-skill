# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Review pipeline — symbol extraction, incremental cache, and staging merge.

Three subcommands:
  symbols Extract symbol definitions from a Python file using AST.
  check   Compare file/symbol content hashes against cache, pre-write staging for hits.
  build   Merge staging files, compute cache entries, and write combined cache.json.
"""

import argparse
import ast
import hashlib
import json
import subprocess
import sys
from collections.abc import Iterable, Sequence
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


def _filter_symbols_by_diff(
    symbols: Iterable[SymbolDef], diff_hunks: Sequence[tuple[int, int]]
) -> list[SymbolDef]:
    return [
        symbol
        for symbol in symbols
        if any(_ranges_overlap(symbol["lines"], hunk) for hunk in diff_hunks)
    ]


# --- Check ---


class Annotation(TypedDict):
    offset: int
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


# --- Composite types ---


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
    checks: list[dict[str, Any]]


class CacheFile(TypedDict):
    version: str
    timestamp: str
    checklist_version: str
    summary: ReviewSummary
    targets: list[TargetEntry]
    files: dict[str, CacheChecks]
    symbols: dict[str, CacheChecks]


class CheckOutput(TypedDict):
    cached: list[str]
    review: list[str]
    cached_symbols: dict[str, list[str]]
    review_symbols: dict[str, list[str]]
    stats: dict[str, int]


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


def load_staging_files(staging_dir: Path) -> list[dict[str, Any]]:
    files = sorted(path for path in staging_dir.glob("*.json") if not path.name.startswith("_"))
    return [json.loads(file_path.read_text()) for file_path in files]


# --- Enrichment and sorting ---


def enrich_check(check: CheckResult, checklist_items: dict[str, ChecklistItem]) -> CheckResult:
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
    return cast(CheckResult, enriched)


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


def _extract_symbol_entries(staging: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract symbol entries from staging, supporting both grouped and per-symbol formats.

    Grouped (legacy): { "stage": "symbol", "targets": [{ "target": ..., "checks": ... }, ...] }
    Per-symbol:       { "stage": "symbol", "target": ..., "checks": [...] }
    """
    if "targets" in staging:
        return staging["targets"]
    if "symbols" in staging:
        return staging["symbols"]
    if "target" in staging:
        return [{"target": staging["target"], "checks": staging.get("checks", [])}]
    return []


def _normalize_symbol_target(entry: dict[str, Any], fallback_file: str) -> SymbolTarget:
    """Handle subagent format variants for symbol targets."""
    if "target" in entry:
        return entry["target"]
    return SymbolTarget(
        type="symbol",
        file=fallback_file,
        symbol=entry.get("symbol", entry.get("name", "")),
        lines=entry.get("lines", [0, 0]),
    )


# --- Annotation conversion ---


def _convert_annotations_to_offsets(
    checks: Iterable[dict[str, Any]], base_line: int
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
                {"offset": ann["line"] - base_line, "message": ann["message"]}
                for ann in check_copy["annotations"]
            ]
        result.append(check_copy)
    return result


def _convert_offsets_to_lines(
    checks: Iterable[dict[str, Any]], base_line: int
) -> list[dict[str, Any]]:
    """Convert annotation offsets back to absolute line numbers.

    Inverse of _convert_annotations_to_offsets.
    """
    result: list[dict[str, Any]] = []
    for check in checks:
        check_copy = dict(check)
        if check_copy.get("annotations"):
            check_copy["annotations"] = [
                {"line": ann["offset"] + base_line, "message": ann["message"]}
                for ann in check_copy["annotations"]
            ]
        result.append(check_copy)
    return result


# --- Merge logic ---


def merge_staging(
    staging_files: Iterable[dict[str, Any]],
    checklist_items: dict[str, ChecklistItem] | None = None,
) -> MergeResult:
    """Summary counts checks from all symbols, including those filtered out of
    targets as all-pass."""
    items = checklist_items or {}
    all_entries: list[TargetEntry] = []
    filtered_targets: list[TargetEntry] = []
    symbols_reviewed = 0

    for staging in staging_files:
        stage = staging["stage"]

        if stage in ("changeset", "file"):
            checks = [enrich_check(check, items) for check in staging.get("checks", [])]
            entry = TargetEntry(
                target=staging["target"],
                checks=sort_checks(checks),
            )
            all_entries.append(entry)
            filtered_targets.append(entry)

        elif stage == "symbol":
            file_path = staging.get("file", "")
            raw_targets = _extract_symbol_entries(staging)
            for symbol_entry in raw_targets:
                symbols_reviewed += 1
                target = _normalize_symbol_target(symbol_entry, file_path)
                checks = [enrich_check(check, items) for check in symbol_entry.get("checks", [])]
                sorted_checks = sort_checks(checks)
                entry = TargetEntry(target=target, checks=sorted_checks)
                all_entries.append(entry)
                if has_non_pass(sorted_checks):
                    filtered_targets.append(entry)

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

            if pass_value is True:
                passed += 1
                continue
            if pass_value is None and "status" not in check:
                blocked += 1
                continue

            status = check.get("status")
            if status is None:
                status = "failed" if pass_value is False else "blocked"
            level = check.get("level", "advisory")

            match status:
                case "passed":
                    passed += 1
                case "failed":
                    if level == "blocking":
                        blocking_failures += 1
                    else:
                        advisory_failures += 1
                case "blocked":
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
    cached: list[str]
    review: list[str]
    hit: int
    miss: int


class _SymbolCacheResult(NamedTuple):
    cached_syms: dict[str, list[str]]
    review_syms: dict[str, list[str]]
    symbol_targets: list[dict[str, Any]]
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
    file_list: list[str],
    cache: CacheFile | None,
    *,
    is_review_file: bool,
) -> _SymbolCacheResult:
    """Look up per-symbol content hashes against cache for a list of files.

    When is_review_file is True, unhashed/missed symbols are tracked in review_syms.
    When False (cached files), only cached symbols are tracked.
    """
    cached_syms: dict[str, list[str]] = {}
    review_syms: dict[str, list[str]] = {}
    symbol_targets: list[dict[str, Any]] = []
    hit = 0
    miss = 0

    for file_str in file_list:
        file_path = Path(file_str)
        if is_review_file and not file_path.exists():
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
                if is_review_file:
                    file_review.append(symbol["name"])
                miss += 1
                continue
            symbol_entry = cache["symbols"].get(symbol_hash) if cache else None
            if symbol_entry:
                file_cached.append(symbol["name"])
                hit += 1
                symbol_targets.append(_restore_symbol_target(file_str, symbol, symbol_entry))
            else:
                if is_review_file:
                    file_review.append(symbol["name"])
                miss += 1
        if file_cached:
            cached_syms[file_str] = file_cached
        if file_review:
            review_syms[file_str] = file_review

    return _SymbolCacheResult(cached_syms, review_syms, symbol_targets, hit, miss)


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

    sym_cached = _check_symbol_cache(file_result.cached, cache, is_review_file=False)
    sym_review = _check_symbol_cache(file_result.review, cache, is_review_file=True)

    cached_symbols = {**sym_cached.cached_syms, **sym_review.cached_syms}
    review_symbols = sym_review.review_syms
    symbol_hit = sym_cached.hit + sym_review.hit
    symbol_miss = sym_cached.miss + sym_review.miss
    all_symbol_targets = sym_cached.symbol_targets + sym_review.symbol_targets

    # Write symbol-cached.json
    if all_symbol_targets:
        symbol_staging = {"stage": "symbol", "targets": all_symbol_targets}
        sym_path = staging_dir / "symbol-cached.json"
        sym_path.write_text(json.dumps(symbol_staging, indent=2, ensure_ascii=False) + "\n")

    return CheckOutput(
        cached=file_result.cached,
        review=file_result.review,
        cached_symbols=cached_symbols,
        review_symbols=review_symbols,
        stats={
            "file_hit": file_result.hit,
            "file_miss": file_result.miss,
            "symbol_hit": symbol_hit,
            "symbol_miss": symbol_miss,
        },
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


def _restore_symbol_target(
    file_str: str, symbol_def: SymbolDef, cache_checks: CacheChecks
) -> dict[str, Any]:
    """Reconstruct a symbol staging target from cache, converting offsets to lines."""
    checks_with_lines = _convert_offsets_to_lines(
        cache_checks["checks"], base_line=symbol_def["lines"][0]
    )
    return {
        "target": {
            "type": "symbol",
            "file": file_str,
            "symbol": symbol_def["name"],
            "lines": [symbol_def["lines"][0], symbol_def["lines"][1]],
        },
        "checks": checks_with_lines,
    }


# --- Cache: build ---


def _build_files_cache(staging_files: Iterable[dict[str, Any]]) -> dict[str, CacheChecks]:
    """Build the files cache section: content hash -> checks with offsets."""
    files_cache: dict[str, CacheChecks] = {}
    for staging in staging_files:
        if staging.get("stage") != "file":
            continue
        file_str = staging["target"]["file"]
        file_path = Path(file_str)
        if not file_path.exists():
            continue
        content_hash = compute_file_hash(file_path)
        offset_checks = _convert_annotations_to_offsets(staging.get("checks", []), base_line=1)
        files_cache[content_hash] = CacheChecks(checks=offset_checks)
    return files_cache


def _build_symbols_cache(staging_files: Iterable[dict[str, Any]]) -> dict[str, CacheChecks]:
    """Build the symbols cache section: content hash -> checks with offsets."""
    symbols_cache: dict[str, CacheChecks] = {}
    for staging in staging_files:
        if staging.get("stage") != "symbol":
            continue
        raw_targets = _extract_symbol_entries(staging)
        for symbol_entry in raw_targets:
            target = symbol_entry.get("target", symbol_entry)
            file_str = target.get("file", staging.get("file", ""))
            lines = target.get("lines", [0, 0])

            file_path = Path(file_str)
            if not file_path.exists():
                continue

            try:
                symbol_hash = compute_symbol_hash(file_path, lines)
            except (IndexError, ValueError):
                continue

            offset_checks = _convert_annotations_to_offsets(
                symbol_entry.get("checks", []), base_line=lines[0]
            )
            symbols_cache[symbol_hash] = CacheChecks(checks=offset_checks)
    return symbols_cache


def build(
    staging_dir: Path,
    cache_path: Path,
    checklist_path: Path,
) -> CacheFile:
    """Merge staging files and build combined cache.json v3."""
    staging_files = load_staging_files(staging_dir)
    if not staging_files:
        print("No staging files found.", file=sys.stderr)
        sys.exit(1)

    checklist = load_checklist(checklist_path)
    checklist_version = checklist["version"]
    targets, symbols_reviewed, summary = merge_staging(staging_files, checklist["items"])

    # Convert annotations from absolute line to offset in targets
    for entry in targets:
        target = entry["target"]
        if target["type"] == "symbol":
            base_line = target["lines"][0]
        elif target["type"] == "file":
            base_line = 1
        else:
            continue
        # list[CheckResult] <-> list[dict[str, Any]]: TypedDict is not assignable to
        # dict[str, Any] in basedpyright strict mode, but compatible at runtime
        checks = cast(list[dict[str, Any]], entry["checks"])
        entry["checks"] = cast(
            list[CheckResult], _convert_annotations_to_offsets(checks, base_line)
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

    print(f"Build complete: {len(staging_files)} staging files -> {cache_path}")
    print(
        f"  {summary['blocking_failures']} blocking, "
        f"{summary['advisory_failures']} advisory, "
        f"{summary['passed']} passed, "
        f"{summary['blocked']} blocked"
    )
    print(f"  Symbols reviewed: {symbols_reviewed}")
    print(f"  Cache: {len(files_cache)} file(s), {len(symbols_cache)} symbol(s)")

    return cache_data


# --- CLI ---


def main() -> None:
    parser = argparse.ArgumentParser(description="Review pipeline — symbols, cache, and merge")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # symbols subcommand
    symbols_parser = subparsers.add_parser(
        "symbols", help="Extract symbols from a Python file using AST"
    )
    symbols_parser.add_argument("--file", required=True, help="Python file to analyze")
    symbols_parser.add_argument(
        "--diff", default=None, help="Git diff range to filter by changed hunks"
    )

    # check subcommand
    check_parser = subparsers.add_parser("check", help="Check files against cache")
    check_parser.add_argument("--files", nargs="+", required=True, help="Files to check")
    check_parser.add_argument("--cache", type=Path, default=Path(".claude/review/cache.json"))
    check_parser.add_argument(
        "--checklist", type=Path, default=Path(".claude/review/checklist.yaml")
    )
    check_parser.add_argument("--staging", type=Path, default=Path(".claude/review/staging"))

    # build subcommand (replaces merge.py + old update)
    build_parser = subparsers.add_parser("build", help="Merge staging and build cache")
    build_parser.add_argument("--staging", type=Path, default=Path(".claude/review/staging"))
    build_parser.add_argument("--cache", type=Path, default=Path(".claude/review/cache.json"))
    build_parser.add_argument(
        "--checklist", type=Path, default=Path(".claude/review/checklist.yaml")
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
            build(
                staging_dir=args.staging,
                cache_path=args.cache,
                checklist_path=args.checklist,
            )
        case _:
            pass


if __name__ == "__main__":
    main()
