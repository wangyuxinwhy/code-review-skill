"""Type definitions for the code review pipeline."""

from typing import Literal, NamedTuple, NotRequired, TypedDict

# --- Constants ---

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

LineRange = tuple[int, int]
"""1-indexed inclusive (start, end) line range."""


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
    lines: LineRange


TargetDescriptor = ChangesetTarget | FileTarget | SymbolTarget


# --- AST Symbol Extraction ---


class SymbolDef(TypedDict):
    name: str
    type: Literal["function", "method", "class"]
    lines: LineRange


class DiscoverOutput(TypedDict):
    files: list[str]
    symbols: dict[str, list[SymbolDef]]


# --- Check ---


class Annotation(TypedDict):
    """Check annotation stored in cache.

    offset is relative to the target's base line (1 for file targets,
    symbol start line for symbol targets).
    """

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
    """A single symbol's staging data. Fields vary by subagent format.

    Canonical format uses 'target' with nested SymbolTarget fields.
    Legacy flat format uses 'symbol' (or 'name' as fallback) plus 'file'/'lines'.
    """

    target: TargetDescriptor
    checks: list[StagingCheck]
    symbol: str  # canonical: qualified symbol name
    name: str  # legacy fallback for symbol name
    file: str
    lines: LineRange


class StagingEntry(TypedDict, total=False):
    """A single staging file's content. Fields vary by stage type."""

    stage: Literal["changeset", "file", "symbol"]
    target: TargetDescriptor
    checks: list[StagingCheck]
    file: str
    targets: list[StagingSymbolEntry]
    symbols: list[StagingSymbolEntry]


class TargetEntry(TypedDict):
    target: TargetDescriptor
    checks: list[CheckResult]


class ReviewSummary(TypedDict):
    """Aggregate counts across all checks in a review.

    blocking_failures: checks at level='blocking' that failed.
    blocked: checks with indeterminate (None) pass status.
    """

    blocking_failures: int
    advisory_failures: int
    passed: int
    blocked: int
    symbols_reviewed: int


class MergeResult(NamedTuple):
    targets: list[TargetEntry]
    symbols_reviewed: int
    summary: ReviewSummary


class ReviewPlan(TypedDict):
    diff_range: str
    changed_files: list[str]
    diff_symbols: dict[str, list[SymbolDef]]
    review_files: list[str]
    cached_files: list[str]
    review_symbols: dict[str, list[str]]
    cached_symbols: dict[str, list[str]]
    stats: "CacheStats"


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


class RefreshStats(TypedDict):
    files_scanned: int
    file_hit: int
    symbol_hit: int
    targets_before: int
    targets_after: int
    orphaned_file_hashes: int
    orphaned_symbol_hashes: int
    fresh: bool
