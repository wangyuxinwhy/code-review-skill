"""AST-based symbol extraction and diff filtering."""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from code_review_skill.types import DiscoverOutput, SymbolDef

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence


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


def extract_symbols_batch(
    files: Sequence[str],
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
            diff_hunks = _get_diff_hunks(file_str, diff_range)
            symbols = _filter_symbols_by_diff(symbols, diff_hunks)
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
