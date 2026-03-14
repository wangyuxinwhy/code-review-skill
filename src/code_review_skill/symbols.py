"""AST-based symbol extraction and diff filtering."""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from code_review_skill.types import DiscoverOutput, LineRange, SymbolDef

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence


def extract_symbols(source: str) -> list[SymbolDef]:
    """Parse Python source with AST, return function/class definitions with
    exact line boundaries. Decorators are excluded — lineno points to the
    def/class keyword, end_lineno to the last line of the body."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    return list(_visit_symbols(tree, prefix=""))


def _visit_symbols(
    node: ast.AST,
    prefix: str,
) -> Iterator[SymbolDef]:
    for child in ast.iter_child_nodes(node):
        match child:
            case ast.FunctionDef() | ast.AsyncFunctionDef():
                symbol_type = "method" if isinstance(node, ast.ClassDef) else "function"
            case ast.ClassDef():
                symbol_type = "class"
            case _:
                continue
        qualified = f"{prefix}.{child.name}" if prefix else child.name
        yield SymbolDef(
            name=qualified,
            type=symbol_type,
            lines=(child.lineno, child.end_lineno or child.lineno),
        )
        yield from _visit_symbols(child, qualified)


_HUNK_RE = re.compile(r"^@@\s.*?\+(\d+)(?:,(\d+))?\s@@")


def get_diff_hunks(file_path: str, diff_range: str) -> list[LineRange]:
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

    hunks: list[LineRange] = []
    for line in diff_output.stdout.splitlines():
        m = _HUNK_RE.match(line)
        if not m:
            continue
        start = int(m.group(1))
        count = int(m.group(2)) if m.group(2) else 1
        if count > 0:
            hunks.append((start, start + count - 1))
    return hunks


def _ranges_overlap(range_a: LineRange, range_b: LineRange) -> bool:
    return range_a[0] <= range_b[1] and range_b[0] <= range_a[1]


def filter_symbols_by_diff(
    symbols: Iterable[SymbolDef],
    diff_hunks: Sequence[LineRange],
) -> list[SymbolDef]:
    return [symbol for symbol in symbols if any(_ranges_overlap(symbol["lines"], hunk) for hunk in diff_hunks)]


def extract_symbols_batch(
    files: Iterable[str],
    diff_range: str | None = None,
) -> dict[str, list[SymbolDef]]:
    """Extract symbols from multiple files, optionally filtering by diff hunks.

    Files that don't exist or fail to parse are silently skipped.
    """
    result: dict[str, list[SymbolDef]] = {}
    for file_str in files:
        file_path = Path(file_str)
        if not file_path.exists():
            continue
        try:
            source = file_path.read_text()
        except OSError:
            continue
        symbols = extract_symbols(source)
        if diff_range:
            diff_hunks = get_diff_hunks(file_str, diff_range)
            symbols = filter_symbols_by_diff(symbols, diff_hunks)
        if symbols:
            result[file_str] = symbols
    return result


def discover_changed_files(diff_range: str) -> list[str]:
    """Run git diff --name-only to find changed Python files."""
    try:
        proc = subprocess.run(
            ["git", "diff", diff_range, "--name-only", "--diff-filter=ACMR"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    return [f for f in proc.stdout.strip().splitlines() if f.endswith(".py")]


def discover(diff_range: str) -> DiscoverOutput:
    """Discover changed files and their diff-touched symbols."""
    files = discover_changed_files(diff_range)
    symbols = extract_symbols_batch(files, diff_range=diff_range)
    return DiscoverOutput(files=files, symbols=symbols)
