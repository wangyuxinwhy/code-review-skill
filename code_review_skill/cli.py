"""CLI entry point for code-review-skill.

The --help output serves as the complete agent instruction set —
agents read it to learn how to use the tool autonomously.
"""

import argparse
import json
import shutil
import sys
from importlib.resources import files as pkg_files
from pathlib import Path

from code_review_skill.cache import build, check, refresh
from code_review_skill.render import show
from code_review_skill.staging import resolve_checklist
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

CHECKLIST RESOLUTION (highest priority first)

  1. --checklist <path>              explicit override
  2. .code-review-checklist.yaml     project-local customization
  3. Built-in default                zero-config, ships with package

  To customize: code-review-skill init

GATE 0: PRE-CHECK

  Mandatory in all modes. Run the pre_check command defined in the
  checklist before any LLM review:

    Read the pre_check field from the active checklist.
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

  Checklist:
    Sole source of truth for what to check. Do not invent checks.
    Structure: pre_check, categories (design > correctness > readability),
    items (id, category, scope, level, when, prompt, description).

  Symbol extraction (code-review-skill symbols):
    AST-based deterministic symbol boundary detection.

      code-review-skill symbols --file <path>
      code-review-skill symbols --file <path> --diff HEAD

    Returns: [{ "name": "func", "type": "function", "lines": [10, 25] }]

  Staging directory (.code-review/staging/):
    All review findings are written here as JSON. Clean at review start:

      rm -rf .code-review/staging && mkdir -p .code-review/staging

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

DEFAULT_CACHE = Path(".code-review/cache.json")
DEFAULT_STAGING = Path(".code-review/staging")


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
    check_parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    check_parser.add_argument("--checklist", type=Path, default=None, help="Override checklist path")
    check_parser.add_argument("--staging", type=Path, default=DEFAULT_STAGING)

    # build subcommand
    build_parser = subparsers.add_parser("build", help="Merge staging and build cache")
    build_parser.add_argument("--staging", type=Path, default=DEFAULT_STAGING)
    build_parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    build_parser.add_argument("--checklist", type=Path, default=None, help="Override checklist path")

    # show subcommand
    show_parser = subparsers.add_parser("show", help="Show findings from cache.json")
    show_parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)

    # refresh subcommand
    refresh_parser = subparsers.add_parser(
        "refresh", help="Self-heal cache — rescan files, match by hash, rebuild targets"
    )
    refresh_parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    refresh_parser.add_argument(
        "--root",
        type=Path,
        default=Path("."),
        help="Project root to scan for Python files",
    )

    # init subcommand
    subparsers.add_parser("init", help="Initialize project with checklist and .code-review/ directory")

    # checklist subcommand
    checklist_parser = subparsers.add_parser("checklist", help="Print the active checklist")
    checklist_parser.add_argument("--builtin", action="store_true", help="Print the built-in default checklist")

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
            checklist_path = resolve_checklist(args.checklist)
            result = check(
                files=args.files,
                cache_path=args.cache,
                checklist_path=checklist_path,
                staging_dir=args.staging,
            )
            print(json.dumps(result, indent=2))
        case "build":
            checklist_path = resolve_checklist(args.checklist)
            try:
                cache_data = build(
                    staging_dir=args.staging,
                    cache_path=args.cache,
                    checklist_path=checklist_path,
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
        case "init":
            _cmd_init()
        case "checklist":
            if args.builtin:
                builtin = pkg_files("code_review_skill.data").joinpath("checklist.yaml")
                print(builtin.read_text())
            else:
                checklist_path = resolve_checklist()
                print(Path(checklist_path).read_text())
        case _:
            pass


def _cmd_init() -> None:
    """Initialize project with checklist and .code-review/ directory."""
    checklist_dest = Path(".code-review-checklist.yaml")
    code_review_dir = Path(".code-review")
    staging_dir = code_review_dir / "staging"
    gitignore = Path(".gitignore")

    # Copy built-in checklist
    if checklist_dest.exists():
        print(f"Already exists: {checklist_dest}")
    else:
        builtin = pkg_files("code_review_skill.data").joinpath("checklist.yaml")
        shutil.copy2(str(builtin), str(checklist_dest))
        print(f"Created: {checklist_dest}")

    # Create .code-review/staging/
    staging_dir.mkdir(parents=True, exist_ok=True)
    print(f"Created: {staging_dir}/")

    # Add .code-review/ to .gitignore
    marker = ".code-review/"
    if gitignore.exists():
        content = gitignore.read_text()
        if marker not in content:
            with gitignore.open("a") as f:
                if not content.endswith("\n"):
                    f.write("\n")
                f.write(f"{marker}\n")
            print(f"Added {marker} to .gitignore")
    else:
        gitignore.write_text(f"{marker}\n")
        print(f"Created .gitignore with {marker}")
