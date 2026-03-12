"""Cache management — check, build, refresh, and content hashing."""

import hashlib
import json
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple, cast

from code_review_skill.staging import (
    _convert_annotations_to_offsets,
    _convert_offsets_to_lines,
    _count_checks,
    _extract_symbol_entries,
    _normalize_symbol_target,
    has_non_pass,
    load_checklist,
    load_staging_files,
    merge_staging,
    target_sort_key,
)
from code_review_skill.symbols import extract_symbols
from code_review_skill.types import (
    CacheChecks,
    CacheFile,
    CacheStats,
    CheckOutput,
    CheckResult,
    FileTarget,
    RefreshStats,
    StagingEntry,
    SymbolDef,
    SymbolTarget,
    TargetEntry,
)

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
    diff_symbols: dict[str, list[SymbolDef]] | None = None,
) -> _SymbolCacheResult:
    """Look up per-symbol content hashes against cache for a list of files.

    When diff_symbols is provided, only symbols that appear in the diff are
    considered. This filters out unchanged symbols from the review list.
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

        # When diff_symbols is provided, only consider symbols touched by the diff
        if diff_symbols is not None:
            diff_names = {s["name"] for s in diff_symbols.get(file_str, [])}
            symbols = [s for s in symbols if s["name"] in diff_names]

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
    diff_symbols: dict[str, list[SymbolDef]] | None = None,
) -> CheckOutput:
    """Compare files against cache at file and symbol level.

    File-level: hash entire file, look up in cache["files"].
    Symbol-level: for each file, extract symbols via AST, hash each symbol,
    look up in cache["symbols"]. Pre-write staging for all cache hits.

    When diff_symbols is provided, only those symbols are considered for review.
    Callers should obtain diff_symbols from discover() to avoid redundant work.
    """
    cache = load_cache(cache_path, checklist_path)

    file_result = _check_file_cache(files, cache, staging_dir)

    symbol_cached = _check_symbol_cache(file_result.cached_files, cache, diff_symbols)
    symbol_review = _check_symbol_cache(file_result.review_files, cache, diff_symbols)

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


# --- Refresh (self-heal) ---


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
    return _rescan_files(data, cache_path, root, files_cache, symbols_cache, targets_before)


def _rescan_files(
    data: CacheFile,
    cache_path: Path,
    root: Path,
    files_cache: dict[str, CacheChecks],
    symbols_cache: dict[str, CacheChecks],
    targets_before: int,
) -> RefreshStats:
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

    # Prune orphaned hashes — only keep entries that matched current files
    live_files = {h: v for h, v in files_cache.items() if h in matched_file_hashes}
    live_symbols = {h: v for h, v in symbols_cache.items() if h in matched_symbol_hashes}

    refreshed = CacheFile(
        version="3",
        timestamp=datetime.now(UTC).isoformat(),
        checklist_version=data.get("checklist_version", "unknown"),
        summary=summary,
        targets=new_targets,
        files=live_files,
        symbols=live_symbols,
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
