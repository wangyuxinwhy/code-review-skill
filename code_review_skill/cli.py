"""CLI entry point for code-review-skill.

The --help output serves as the complete agent instruction set —
agents read it to learn how to use the tool autonomously.
"""

import argparse
import json
import sys
from importlib.resources import files as pkg_files
from pathlib import Path

from code_review_skill.cache import build, check, refresh
from code_review_skill.render import show
from code_review_skill.symbols import (
    _filter_symbols_by_diff,
    _get_diff_hunks,
    extract_symbols,
)

DESCRIPTION = """\
Code review pipeline for Claude Code.

Review code against a configurable checklist with incremental caching,
AST-based symbol extraction, and annotated source output.

You are a code review curator. You review code against a checklist,
orchestrate parallel subagents for large reviews, and apply your own
judgment to curate the final result.

GATE 0: PRE-CHECK

  Mandatory in all modes. Run the pre_check command defined in the
  checklist before any LLM review:

    Read the pre_check field from .claude/review/checklist.yaml
    If pre_check is not defined, skip Gate 0 entirely.

  If pre_check fails and failures are related to the files under review:
  stop and fix. If unrelated: note and proceed.

REVIEW MODES

  Detect the mode from the user's argument:

  (none)                           diff mode (changeset, file, symbol)
  branch name, PR number, range    diff mode (changeset, file, symbol)
  file path(s) or directory        file mode (file, symbol)
  file:symbol_name                 symbol mode (symbol only)

INFRASTRUCTURE

  Checklist (.claude/review/checklist.yaml):
    Sole source of truth for what to check. Do not invent checks.
    Structure: pre_check, categories (design > correctness > readability),
    items (id, category, scope, level, when, prompt, description).

  Symbol extraction (code-review-skill symbols):
    AST-based deterministic symbol boundary detection.

      code-review-skill symbols --file <path>
      code-review-skill symbols --file <path> --diff HEAD

    Returns: [{ "name": "func", "type": "function", "lines": [10, 25] }]

  Staging directory (.claude/review/staging/):
    All review findings are written here as JSON. Clean at review start:

      rm -rf .claude/review/staging && mkdir -p .claude/review/staging

    Check format:
      Passed/blocked (compact): { "id": "check-id", "pass": true/null }
      Failed (with annotations):
        { "id": "check-id", "pass": false,
          "note": "actionable description",
          "annotations": [{ "line": 114, "message": "<60 chars" }] }

    Staging file schemas:
      Changeset:  staging/changeset.json
        { "stage": "changeset", "target": { "type": "changeset" },
          "checks": [...] }
      File:       staging/file-{sanitized}.json
        { "stage": "file", "target": { "type": "file", "file": "<path>" },
          "checks": [...] }
      Symbol:     staging/symbol-{sanitized}-{name}.json
        { "stage": "symbol",
          "target": { "type": "symbol", "file": "<path>",
                      "symbol": "name", "lines": [start, end] },
          "checks": [...] }

PIPELINE WORKFLOW

  Step 1: Discover symbols
    code-review-skill symbols --file <path>

  Step 2: Check cache
    code-review-skill check --files <paths...>

  Step 3: Dispatch subagents
    File subagents for files in "review_files" (file-scope checks only).
    Symbol subagents for symbols in "review_symbols" (AST boundaries).
    Skip cached files and cached symbols (staging pre-written).

  Step 4 (MANDATORY): Build cache
    code-review-skill build

PRINCIPLES

  Fail-fast gating:
    Blocking failure at a scope -> mark remaining advisory checks blocked.
    Blocking changeset failure blocks all downstream file/symbol checks.

  Parallel subagents:
    Always dispatch subagents for symbol-scope review.
    One subagent per symbol via Agent tool, all in one message.
    Subagents read the checklist themselves.

  Caching:
    MANDATORY: always run check before dispatching review.
    MANDATORY: always run build after staging is complete.

  Curator judgment:
    You are the curator. Override dubious findings, drop false positives.
    Do NOT add checks beyond the checklist.

REPORT FORMAT

  ## Review Result
  **Files:** {count} | **Symbols:** {reviewed}/{total} | **Checklist:** v{ver}
  ### Changeset / File / Symbol tables with [PASS] [FAIL] [SKIP] markers
  Omit all-pass symbols. No emoji.
"""


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="code-review-skill",
        description=DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
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

    # build subcommand
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

    # checklist subcommand
    subparsers.add_parser("checklist", help="Print the built-in default checklist")

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
        case "checklist":
            checklist_file = pkg_files("code_review_skill.data").joinpath("checklist.yaml")
            print(checklist_file.read_text())
        case _:
            pass
