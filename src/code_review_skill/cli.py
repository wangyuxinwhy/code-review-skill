"""CLI entry point for code-review-skill.

The --help output serves as the complete agent instruction set —
agents read it to learn how to use the tool autonomously.
"""

import argparse
import json
import sys
from importlib.resources import files as pkg_files
from pathlib import Path
from typing import NamedTuple

import yaml

from code_review_skill.cache import build, check, refresh
from code_review_skill.render import show
from code_review_skill.staging import resolve_checklist, write_staging_entry
from code_review_skill.symbols import (
    discover,
    extract_symbols,
    extract_symbols_batch,
    filter_symbols_by_diff,
    get_diff_hunks,
)
from code_review_skill.types import ReviewPlan

DESCRIPTION = """\
Code review pipeline for coding agents.

Review code against a configurable checklist with incremental caching,
AST-based symbol extraction, and annotated source output.

You are a code review curator. You review code against a checklist,
orchestrate parallel subagents for large reviews, and apply your own
judgment to curate the final result.

CHECKLIST RESOLUTION (highest priority first)

  1. --checklist <path>              explicit override
  2. .code-review-checklist.yaml     project-local customization
  3. Built-in default                zero-config, ships with package

  To customize: code-review-skill init (outputs setup context for the agent)
  To verify setup: code-review-skill init check

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

    Single file:
      code-review-skill symbols --file <path>
      code-review-skill symbols --file <path> --diff HEAD

    Batch (grouped output by file):
      code-review-skill symbols --files <path1> <path2> ...
      code-review-skill symbols --files <path1> <path2> --diff main

    Single-file returns: [{ "name": "func", "type": "function", "lines": [10, 25] }]
    Batch returns: { "path/a.py": [symbols...], "path/b.py": [symbols...] }

  Discovery (code-review-skill discover):
    Find changed files and diff-touched symbols for a git range.

      code-review-skill discover main
      code-review-skill discover HEAD~3..HEAD

    Returns: { "files": ["a.py", ...], "symbols": { "a.py": [symbols...] } }
    files = all changed Python files; symbols = only files with diff-touched symbols.

  Staging directory (.code-review/staging/):
    All review findings are written here as JSON. Clean at review start:

      rm -rf .code-review/staging && mkdir -p .code-review/staging

    Write findings via the stage command (reads JSON from stdin):

      echo '<json>' | code-review-skill stage

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

  Quick path (recommended):
    code-review-skill review <range>
    Returns a complete review plan with changed files, diff-touched symbols,
    and cache status. Then dispatch subagents per review_symbols.

  Manual path (step by step):
    Step 1: code-review-skill discover <range>
    Step 2: code-review-skill check --files <paths...> --diff <range>
    Step 3: Dispatch subagents, write findings via: code-review-skill stage
    Step 4: code-review-skill build

PRINCIPLES

  Fail-fast gating:
    Blocking failure at a scope -> mark remaining advisory checks blocked.
    Blocking changeset failure blocks all downstream file/symbol checks.

  Parallel subagents:
    Always dispatch subagents for symbol-scope review.
    One subagent per symbol via Agent tool, all in one message.
    Subagents read the checklist themselves.

  Caching:
    MANDATORY: always run check (or review) before dispatching review.
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="code-review-skill",
        description=DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # symbols subcommand
    symbols_parser = subparsers.add_parser("symbols", help="Extract symbols from Python files using AST")
    symbols_group = symbols_parser.add_mutually_exclusive_group(required=True)
    symbols_group.add_argument("--file", help="Single Python file to analyze")
    symbols_group.add_argument("--files", nargs="+", help="Multiple Python files to analyze (grouped output)")
    symbols_parser.add_argument("--diff", default=None, help="Git diff range to filter by changed hunks")

    # check subcommand
    check_parser = subparsers.add_parser("check", help="Check files against cache")
    check_parser.add_argument("--files", nargs="+", required=True, help="Files to check")
    check_parser.add_argument("--diff", default=None, help="Git diff range to filter symbols by changed hunks")
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
    init_parser = subparsers.add_parser(
        "init",
        help="Output project initialization context, or verify setup with 'init check'",
    )
    init_parser.add_argument(
        "init_action",
        nargs="?",
        default=None,
        choices=["check"],
        help="Optional action: 'check' to verify project setup",
    )

    # discover subcommand
    discover_parser = subparsers.add_parser(
        "discover",
        help="Discover changed files and diff-touched symbols for a git range",
    )
    discover_parser.add_argument(
        "range",
        help="Git diff range (e.g., 'main', 'HEAD~3..HEAD')",
    )

    # stage subcommand
    stage_parser = subparsers.add_parser(
        "stage",
        help="Write a staging entry from stdin JSON",
    )
    stage_parser.add_argument("--staging", type=Path, default=DEFAULT_STAGING)

    # review subcommand
    review_parser = subparsers.add_parser(
        "review",
        help="Orchestrate full review: discover → check cache → output review plan",
    )
    review_parser.add_argument(
        "range",
        nargs="?",
        default="HEAD",
        help="Git diff range (default: HEAD)",
    )
    review_parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    review_parser.add_argument("--checklist", type=Path, default=None)
    review_parser.add_argument("--staging", type=Path, default=DEFAULT_STAGING)

    # checklist subcommand
    checklist_parser = subparsers.add_parser("checklist", help="Print the active checklist")
    checklist_parser.add_argument("--builtin", action="store_true", help="Print the built-in default checklist")

    return parser


def _cmd_symbols(args: argparse.Namespace) -> None:
    if args.files:
        result = extract_symbols_batch(args.files, diff_range=args.diff)
        print(json.dumps(result, indent=2))
    else:
        file_path = Path(args.file)
        if not file_path.exists():
            print(f"File not found: {args.file}", file=sys.stderr)
            sys.exit(1)
        source = file_path.read_text()
        symbols = extract_symbols(source)
        if args.diff:
            diff_hunks = get_diff_hunks(args.file, args.diff)
            symbols = filter_symbols_by_diff(symbols, diff_hunks)
        print(json.dumps(symbols, indent=2))


def _cmd_check(args: argparse.Namespace) -> None:
    checklist_path = resolve_checklist(args.checklist)
    diff_symbols = discover(args.diff)["symbols"] if args.diff else None
    result = check(
        files=args.files,
        cache_path=args.cache,
        checklist_path=checklist_path,
        staging_dir=args.staging,
        diff_symbols=diff_symbols,
    )
    print(json.dumps(result, indent=2))


def _cmd_build(args: argparse.Namespace) -> None:
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


def _cmd_show(args: argparse.Namespace) -> None:
    try:
        refresh(cache_path=args.cache, root=Path.cwd())
        report = show(cache_path=args.cache)
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    print(report)


def _cmd_refresh(args: argparse.Namespace) -> None:
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


def _cmd_discover(args: argparse.Namespace) -> None:
    discovery = discover(args.range)
    print(json.dumps(discovery, indent=2))


def _cmd_stage(args: argparse.Namespace) -> None:
    stdin_text = sys.stdin.read()
    try:
        entry = json.loads(stdin_text)
    except json.JSONDecodeError as exc:
        print(f"Invalid JSON on stdin: {exc}", file=sys.stderr)
        sys.exit(1)
    path = write_staging_entry(args.staging, entry)
    print(json.dumps({"written": str(path)}))


def _cmd_review(args: argparse.Namespace) -> None:
    checklist_path = resolve_checklist(args.checklist)
    discovery = discover(args.range)
    check_result = check(
        files=discovery["files"],
        cache_path=args.cache,
        checklist_path=checklist_path,
        staging_dir=args.staging,
        diff_symbols=discovery["symbols"],
    )
    plan = ReviewPlan(
        diff_range=args.range,
        changed_files=discovery["files"],
        diff_symbols=discovery["symbols"],
        review_files=check_result["review_files"],
        cached_files=check_result["cached_files"],
        review_symbols=check_result["review_symbols"],
        cached_symbols=check_result["cached_symbols"],
        stats=check_result["stats"],
    )
    print(json.dumps(plan, indent=2))


def _cmd_checklist(args: argparse.Namespace) -> None:
    if args.builtin:
        builtin = pkg_files("code_review_skill.data").joinpath("checklist.yaml")
        print(builtin.read_text())
    else:
        checklist_path = resolve_checklist()
        print(Path(checklist_path).read_text())


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    match args.command:
        case "symbols":
            _cmd_symbols(args)
        case "check":
            _cmd_check(args)
        case "build":
            _cmd_build(args)
        case "show":
            _cmd_show(args)
        case "refresh":
            _cmd_refresh(args)
        case "discover":
            _cmd_discover(args)
        case "stage":
            _cmd_stage(args)
        case "review":
            _cmd_review(args)
        case "init":
            if args.init_action == "check":
                _cmd_init_check()
            else:
                _cmd_init()
        case "checklist":
            _cmd_checklist(args)
        case _:
            pass


def _build_init_instructions(default_checklist: str) -> str:
    item_count = _count_items(default_checklist)
    return f"""\
CODE REVIEW SKILL — PROJECT INITIALIZATION CONTEXT

This document provides the context you need to set up code-review-skill
for this project. It explains what each component does and why it exists,
so you can configure it appropriately for this project.

After setup, run: code-review-skill init check

══════════════════════════════════════════════════════════════════
PROJECT STRUCTURE
══════════════════════════════════════════════════════════════════

code-review-skill uses a local .code-review/ directory and a project-level
checklist file. Here is what each piece does:

  .code-review/staging/
    The staging area for review findings. During a review, each subagent
    writes its findings here as JSON files (one per file or symbol reviewed).
    The staging directory is cleaned at the start of each review and merged
    into the cache by `code-review-skill build`. This directory must exist
    before running any review.

  .code-review/cache.json
    Incremental review cache. Stores results keyed by content hash so that
    unchanged files and symbols are skipped on subsequent reviews. Created
    automatically by the build command.

  .code-review-checklist.yaml
    The checklist file — the sole source of truth for what gets reviewed.
    Contains the pre_check command, review categories, and individual
    check items. This file should be committed to version control so the
    team shares the same review standards.

  .gitignore
    The .code-review/ directory contains runtime artifacts (staging files,
    cache) and should be excluded from version control.

══════════════════════════════════════════════════════════════════
DEFAULT CHECKLIST TEMPLATE
══════════════════════════════════════════════════════════════════

Below is the built-in default checklist. It is designed for Python projects
and serves as a starting point — the items are common examples, not a
definitive list. You should adapt it to this project's language, framework,
and domain.

Write the checklist to: .code-review-checklist.yaml

--- BEGIN DEFAULT CHECKLIST ---
{default_checklist}\
--- END DEFAULT CHECKLIST ---

══════════════════════════════════════════════════════════════════
ABOUT pre_check
══════════════════════════════════════════════════════════════════

The pre_check field defines a shell command that runs as Gate 0 — before
any LLM-based review begins. Its purpose is to ensure baseline quality
(linting, type checking, tests) passes before investing in deeper review.
The command should exit 0 on success.

The default value "make check" is a placeholder. Configure it to match
this project's actual quality checks. Common choices:

  Python:      pytest, ruff check ., mypy ., ruff check . && pytest
  JavaScript:  npm test, npm run lint
  TypeScript:  npx tsc --noEmit && npm test
  Rust:        cargo test
  Go:          go test ./...
  Make-based:  make check, make lint

If the project has no automated checks yet, remove the pre_check field
entirely — Gate 0 will be skipped.

══════════════════════════════════════════════════════════════════
CHECKLIST CUSTOMIZATION
══════════════════════════════════════════════════════════════════

The default checklist contains {item_count} items oriented toward
Python projects. Different project types benefit from different checks.

Consider the project's language, framework, and domain:

  - Items like constructor-purity, type-annotations, and
    idiomatic-constructs are Python-specific and may not apply to
    other languages.
  - Frontend projects often benefit from checks around accessibility,
    component structure, and state management patterns.
  - Data and ML projects may need checks for data validation,
    reproducibility, and pipeline correctness.
  - API projects may need checks for error response consistency,
    input validation, and authentication handling.

Each checklist item follows this schema:

  - id: kebab-case-unique-identifier
    category: design | correctness | readability
    scope: changeset | file | symbol
    level: blocking | advisory
    when: "optional condition for when this check applies"
    description: "one-line summary shown in reports"
    prompt: |
      Multi-line instructions for the reviewing agent.
      Be specific about what to flag and what NOT to flag.

  scope meanings:
    changeset — evaluated against the entire set of changes
    file      — evaluated per file
    symbol    — evaluated per function/class (AST-extracted)

  level meanings:
    blocking  — failure stops downstream checks at this scope
    advisory  — reported but does not block

══════════════════════════════════════════════════════════════════
VALIDATION
══════════════════════════════════════════════════════════════════

After configuring the checklist, verify everything is set up correctly:

  code-review-skill init check

This command checks that all required files and directories exist,
the checklist parses correctly, and pre_check is configured.
"""


def _count_items(checklist_content: str) -> int:
    """Count the number of checklist items by looking for '- id:' lines."""
    return sum(1 for line in checklist_content.splitlines() if line.strip().startswith("- id:"))


def _cmd_init() -> None:
    builtin = pkg_files("code_review_skill.data").joinpath("checklist.yaml")
    default_checklist = builtin.read_text()
    print(_build_init_instructions(default_checklist))


class _InitCheckResult(NamedTuple):
    name: str
    passed: bool
    detail: str


def _run_init_checks() -> list[_InitCheckResult]:
    """Validate project setup, returning a list of check results."""
    checklist_path = Path(".code-review-checklist.yaml")
    staging_dir = Path(".code-review/staging")
    gitignore = Path(".gitignore")

    checks: list[_InitCheckResult] = []

    # Check checklist file exists and parses
    if checklist_path.exists():
        try:
            raw_checklist = yaml.safe_load(checklist_path.read_text())
            items = raw_checklist.get("items", [])
            pre_check = raw_checklist.get("pre_check", "")
            checks.append(_InitCheckResult("checklist file", True, str(checklist_path)))
            checks.append(_InitCheckResult("checklist items", len(items) > 0, f"{len(items)} items"))
            checks.append(
                _InitCheckResult(
                    "pre_check configured",
                    bool(pre_check) and pre_check != "make check",
                    repr(pre_check) if pre_check else "(not set)",
                )
            )
        except Exception as exc:
            checks.append(_InitCheckResult("checklist file", False, f"parse error: {exc}"))
            checks.extend(
                _InitCheckResult(name, False, "skipped") for name in ("checklist items", "pre_check configured")
            )
    else:
        checks.append(_InitCheckResult("checklist file", False, "not found"))
        checks.extend(_InitCheckResult(name, False, "skipped") for name in ("checklist items", "pre_check configured"))

    # Check staging directory
    checks.append(_InitCheckResult("staging directory", staging_dir.is_dir(), str(staging_dir)))

    # Check .gitignore
    if gitignore.exists():
        content = gitignore.read_text()
        has_marker = ".code-review/" in content
        checks.append(_InitCheckResult(".gitignore entry", has_marker, ".code-review/ in .gitignore"))
    else:
        checks.append(_InitCheckResult(".gitignore entry", False, ".gitignore not found"))

    return checks


def _cmd_init_check() -> None:
    """Run init checks, print results, exit(1) on failure."""
    checks = _run_init_checks()
    all_passed = all(result.passed for result in checks)

    for result in checks:
        status = "PASS" if result.passed else "FAIL"
        print(f"  [{status}] {result.name}: {result.detail}")

    if all_passed:
        print("\nInit check passed. Project is ready for code review.")
    else:
        print("\nSome checks failed. Review the items above and complete setup.")
        sys.exit(1)
