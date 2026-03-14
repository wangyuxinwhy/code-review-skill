"""Cache management — check, build, refresh, and content hashing."""

import hashlib
import json
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple, cast

from code_review_skill.staging import (
    compute_summary,
    convert_annotations_to_offsets,
    convert_offsets_to_lines,
    extract_symbol_entries,
    has_non_pass,
    load_checklist,
    load_staging_files,
    merge_staging,
    normalize_symbol_target,
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


def compute_file_hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def compute_symbol_hash(path: Path, lines: tuple[int, int]) -> str:
    all_lines = path.read_text().splitlines()
    start, end = lines
    selected = all_lines[start - 1 : end]
    content = "\n".join(selected)
    return "sha256:" + hashlib.sha256(content.encode()).hexdigest()


def _hash_symbol_from_lines(all_lines: list[str], lines: tuple[int, int]) -> str:
    start, end = lines
    selected = all_lines[start - 1 : end]
    content = "\n".join(selected)
    return "sha256:" + hashlib.sha256(content.encode()).hexdigest()


def load_cache(cache_path: Path, checklist_path: Path) -> CacheFile | None:
    if not cache_path.exists():
        return None
    try:
        raw_cache: dict[str, Any] = json.loads(cache_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if raw_cache.get("version") != "3" or "files" not in raw_cache:
        return None
    checklist = load_checklist(checklist_path)
    if raw_cache.get("checklist_version") != checklist["version"]:
        return None
    return cast("CacheFile", raw_cache)


class _FileCacheResult(NamedTuple):
    cached_files: list[str]
    review_files: list[str]
    cached_entries: dict[str, CacheChecks]
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
) -> _FileCacheResult:
    """Partition files into cached/review by content hash."""
    cached: list[str] = []
    review: list[str] = []
    cached_entries: dict[str, CacheChecks] = {}
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
            cached_entries[file_str] = file_entry
            hit += 1
        else:
            review.append(file_str)
            miss += 1

    return _FileCacheResult(cached, review, cached_entries, hit, miss)


class SymbolCacheChecker:
    """Check per-symbol content hashes against cache for a list of files.

    Encapsulates cache lookup state (accumulators, cache reference, diff filter)
    as instance attributes so that per-file processing methods can share them
    without complex parameter passing or tuple returns.
    """

    def __init__(
        self,
        cache: CacheFile | None,
        diff_symbols: dict[str, list[SymbolDef]] | None = None,
    ) -> None:
        self.cache = cache
        self.diff_symbols = diff_symbols
        self.cached_symbols: dict[str, list[str]] = {}
        self.review_symbols: dict[str, list[str]] = {}
        self.symbol_targets: list[TargetEntry] = []
        self.hit = 0
        self.miss = 0

    def check(self, file_list: Sequence[str]) -> _SymbolCacheResult:
        """Run the cache check across all files and return the result."""
        for file_str in file_list:
            self._process_file(file_str)
        return _SymbolCacheResult(
            self.cached_symbols,
            self.review_symbols,
            self.symbol_targets,
            self.hit,
            self.miss,
        )

    def _process_file(self, file_str: str) -> None:
        file_path = Path(file_str)
        if not file_path.exists():
            return
        try:
            source = file_path.read_text()
            all_lines = source.splitlines()
            symbols = extract_symbols(source)
        except OSError:
            return

        if self.diff_symbols is not None:
            diff_names = {s["name"] for s in self.diff_symbols.get(file_str, [])}
            symbols = [s for s in symbols if s["name"] in diff_names]

        file_cached: list[str] = []
        file_review: list[str] = []
        for symbol in symbols:
            try:
                symbol_hash = _hash_symbol_from_lines(all_lines, symbol["lines"])
            except (IndexError, ValueError):
                file_review.append(symbol["name"])
                self.miss += 1
                continue
            symbol_entry = self.cache["symbols"].get(symbol_hash) if self.cache else None
            if symbol_entry:
                file_cached.append(symbol["name"])
                self.hit += 1
                self.symbol_targets.append(restore_symbol_target(file_str, symbol, symbol_entry))
            else:
                file_review.append(symbol["name"])
                self.miss += 1

        if file_cached:
            self.cached_symbols[file_str] = file_cached
        if file_review:
            self.review_symbols[file_str] = file_review


def _check_symbol_cache(
    file_list: Sequence[str],
    cache: CacheFile | None,
    diff_symbols: dict[str, list[SymbolDef]] | None = None,
) -> _SymbolCacheResult:
    return SymbolCacheChecker(cache, diff_symbols).check(file_list)


def _build_cached_staging_entry(file_str: str, cache_checks: CacheChecks) -> dict[str, Any]:
    """Build a staging entry dict from cached checks, converting offsets to lines."""
    checks_with_lines = convert_offsets_to_lines(cache_checks["checks"], base_line=1)
    return {
        "stage": "file",
        "target": {"type": "file", "file": file_str},
        "checks": checks_with_lines,
    }


def _write_cached_staging(file_str: str, cache_checks: CacheChecks, staging_dir: Path) -> None:
    """Write a cached file's staging file, converting offsets back to absolute lines."""
    staging = _build_cached_staging_entry(file_str, cache_checks)
    sanitized = file_str.replace("/", "-").replace(".", "-")
    staging_path = staging_dir / f"file-{sanitized}.json"
    staging_path.write_text(json.dumps(staging, indent=2, ensure_ascii=False) + "\n")


def _write_cache_hits_to_staging(
    file_entries: dict[str, CacheChecks],
    symbol_targets: list[TargetEntry],
    staging_dir: Path,
) -> None:
    """Write staging files for all cache hits (file-level and symbol-level)."""
    for file_str, file_entry in file_entries.items():
        _write_cached_staging(file_str, file_entry, staging_dir)
    if symbol_targets:
        symbol_staging = {"stage": "symbol", "targets": symbol_targets}
        sym_path = staging_dir / "symbol-cached.json"
        sym_path.write_text(json.dumps(symbol_staging, indent=2, ensure_ascii=False) + "\n")


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

    file_result = _check_file_cache(files, cache)
    symbol_cached = _check_symbol_cache(file_result.cached_files, cache, diff_symbols)
    symbol_review = _check_symbol_cache(file_result.review_files, cache, diff_symbols)

    cached_symbols = {**symbol_cached.cached_symbols, **symbol_review.cached_symbols}
    review_symbols = symbol_review.review_symbols
    all_symbol_targets = symbol_cached.symbol_targets + symbol_review.symbol_targets

    _write_cache_hits_to_staging(file_result.cached_entries, all_symbol_targets, staging_dir)

    return CheckOutput(
        cached_files=file_result.cached_files,
        review_files=file_result.review_files,
        cached_symbols=cached_symbols,
        review_symbols=review_symbols,
        stats=CacheStats(
            file_hit=file_result.hit,
            file_miss=file_result.miss,
            symbol_hit=symbol_cached.hit + symbol_review.hit,
            symbol_miss=symbol_cached.miss + symbol_review.miss,
        ),
    )


def restore_symbol_target(file_str: str, symbol_def: SymbolDef, cache_checks: CacheChecks) -> TargetEntry:
    """Reconstruct a symbol staging target from cache, converting offsets to lines."""
    checks_with_lines = convert_offsets_to_lines(cache_checks["checks"], base_line=symbol_def["lines"][0])
    return TargetEntry(
        target=SymbolTarget(
            type="symbol",
            file=file_str,
            symbol=symbol_def["name"],
            lines=(symbol_def["lines"][0], symbol_def["lines"][1]),
        ),
        checks=cast("list[CheckResult]", checks_with_lines),
    )


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
        offset_checks = convert_annotations_to_offsets(staging.get("checks", []), base_line=1)
        files_cache[content_hash] = CacheChecks(checks=cast("list[CheckResult]", offset_checks))
    return files_cache


def _build_symbols_cache(staging_files: Iterable[StagingEntry]) -> dict[str, CacheChecks]:
    """Handles both grouped and per-symbol staging formats; silently skips entries
    with missing files or unparseable line ranges."""
    symbols_cache: dict[str, CacheChecks] = {}
    for staging in staging_files:
        if staging.get("stage") != "symbol":
            continue
        symbol_entries = extract_symbol_entries(staging)
        for symbol_entry in symbol_entries:
            fallback_file = staging.get("file", "")
            sym_target = normalize_symbol_target(symbol_entry, fallback_file)
            file_str = sym_target["file"]
            lines = sym_target["lines"]

            file_path = Path(file_str)
            if not file_path.exists():
                continue

            try:
                symbol_hash = compute_symbol_hash(file_path, lines)
            except (IndexError, ValueError):
                continue

            offset_checks = convert_annotations_to_offsets(symbol_entry.get("checks", []), base_line=lines[0])
            symbols_cache[symbol_hash] = CacheChecks(checks=cast("list[CheckResult]", offset_checks))
    return symbols_cache


def _convert_target_annotations_to_offsets(targets: list[TargetEntry]) -> None:
    """Convert annotation line numbers to offsets in-place for cache storage."""
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
            convert_annotations_to_offsets(target_entry["checks"], base_line),
        )


def _write_cache_file(cache_data: CacheFile, cache_path: Path) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache_data, indent=2, ensure_ascii=False) + "\n")


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
    targets, _symbols_reviewed, summary = merge_staging(staging_files, checklist["items"])

    _convert_target_annotations_to_offsets(targets)

    files_cache = _build_files_cache(staging_files)
    symbols_cache = _build_symbols_cache(staging_files)

    cache_data = CacheFile(
        version="3",
        timestamp=datetime.now(UTC).isoformat(),
        checklist_version=checklist["version"],
        summary=summary,
        targets=targets,
        files=files_cache,
        symbols=symbols_cache,
    )
    _write_cache_file(cache_data, cache_path)
    return cache_data


_DEFAULT_EXCLUDE = frozenset({".git", ".venv", "venv", "__pycache__", "node_modules", ".tox", ".mypy_cache"})


def _discover_python_files(root: Path, exclude: frozenset[str] = _DEFAULT_EXCLUDE) -> list[Path]:
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

            case {"type": "changeset"}:
                continue

            case _:
                return False

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
    cache: CacheFile = json.loads(cache_path.read_text())
    if cache.get("version") != "3":
        raise ValueError(f"Unsupported cache version: {cache.get('version')}")

    targets_before = len(cache.get("targets", []))

    if _verify_targets(cache, root):
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

    return _rescan_files(cache, cache_path, root)


class _RescanResult(NamedTuple):
    """Results from scanning files against cache hashes."""

    new_targets: list[TargetEntry]
    all_entries: list[TargetEntry]
    symbols_reviewed: int
    matched_file_hashes: set[str]
    matched_symbol_hashes: set[str]
    files_scanned: int


def _scan_files_against_cache(
    root: Path,
    changeset_targets: list[TargetEntry],
    files_cache: dict[str, CacheChecks],
    symbols_cache: dict[str, CacheChecks],
) -> _RescanResult:
    """Scan Python files under root, matching content hashes against cache."""
    new_targets: list[TargetEntry] = list(changeset_targets)
    all_entries: list[TargetEntry] = list(changeset_targets)
    matched_file_hashes: set[str] = set()
    matched_symbol_hashes: set[str] = set()
    symbols_reviewed = 0

    python_files = _discover_python_files(root)
    files_scanned = 0

    for file_path in python_files:
        files_scanned += 1
        rel_path = str(file_path.relative_to(root))

        file_hash = compute_file_hash(file_path)
        if file_hash in files_cache:
            matched_file_hashes.add(file_hash)
            entry = TargetEntry(
                target=FileTarget(type="file", file=rel_path),
                checks=files_cache[file_hash]["checks"],
            )
            new_targets.append(entry)
            all_entries.append(entry)

        try:
            source = file_path.read_text()
            all_lines = source.splitlines()
            symbols = extract_symbols(source)
        except OSError:
            continue

        for symbol in symbols:
            try:
                symbol_hash = _hash_symbol_from_lines(all_lines, symbol["lines"])
            except (IndexError, ValueError):
                continue

            if symbol_hash in symbols_cache:
                matched_symbol_hashes.add(symbol_hash)
                symbols_reviewed += 1
                target = SymbolTarget(
                    type="symbol",
                    file=rel_path,
                    symbol=symbol["name"],
                    lines=(symbol["lines"][0], symbol["lines"][1]),
                )
                entry = TargetEntry(target=target, checks=symbols_cache[symbol_hash]["checks"])
                all_entries.append(entry)
                if has_non_pass(entry["checks"]):
                    new_targets.append(entry)

    return _RescanResult(
        new_targets,
        all_entries,
        symbols_reviewed,
        matched_file_hashes,
        matched_symbol_hashes,
        files_scanned,
    )


def _prune_orphaned_hashes(
    cache: dict[str, CacheChecks],
    matched: set[str],
) -> dict[str, CacheChecks]:
    """Keep only cache entries whose hashes matched current files."""
    return {h: v for h, v in cache.items() if h in matched}


def _rescan_files(
    data: CacheFile,
    cache_path: Path,
    root: Path,
) -> RefreshStats:
    """Full rescan and cache rebuild — matches file/symbol hashes and reconstructs targets."""
    files_cache = data.get("files", {})
    symbols_cache = data.get("symbols", {})
    targets_before = len(data.get("targets", []))

    changeset_targets = [entry for entry in data.get("targets", []) if entry["target"]["type"] == "changeset"]

    scan = _scan_files_against_cache(root, changeset_targets, files_cache, symbols_cache)
    scan.new_targets.sort(key=target_sort_key)
    summary = compute_summary(scan.all_entries, scan.symbols_reviewed)

    refreshed = CacheFile(
        version="3",
        timestamp=datetime.now(UTC).isoformat(),
        checklist_version=data.get("checklist_version", "unknown"),
        summary=summary,
        targets=scan.new_targets,
        files=_prune_orphaned_hashes(files_cache, scan.matched_file_hashes),
        symbols=_prune_orphaned_hashes(symbols_cache, scan.matched_symbol_hashes),
    )
    _write_cache_file(refreshed, cache_path)

    return RefreshStats(
        files_scanned=scan.files_scanned,
        file_hit=len(scan.matched_file_hashes),
        symbol_hit=len(scan.matched_symbol_hashes),
        targets_before=targets_before,
        targets_after=len(scan.new_targets),
        orphaned_file_hashes=len(files_cache) - len(scan.matched_file_hashes),
        orphaned_symbol_hashes=len(symbols_cache) - len(scan.matched_symbol_hashes),
        fresh=False,
    )
