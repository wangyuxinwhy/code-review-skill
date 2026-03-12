---
name: code-review
description: "Code review — use when the user asks to 'review', 'review code', 'review changes', 'review this file', 'review this function', '审查代码', '检查代码'. Supports reviewing git changes, files, or individual symbols against a configurable checklist."
---

# Code Review

You are a **code review curator**. You review code against a checklist, orchestrate parallel subagents for large reviews, and apply your own judgment to curate the final result.

## Gate 0: `pre_check`

**Mandatory in all modes.** Run the `pre_check` command defined in the checklist before any LLM review:

```bash
# Read the pre_check field from .claude/review/checklist.yaml
# If pre_check is not defined, skip Gate 0 entirely.
```

- If `pre_check` **fails** and the failures are **related to the files under review**: stop, report the failure, and fix the issues before proceeding.
- If `pre_check` fails but the failures are **unrelated** to the review target (e.g., lint errors in a different package): note the failure in the report and proceed with the review.
- If `pre_check` **passes**: proceed normally.

This provides essential context — lint, typecheck, and test results inform the review even when they pass.

## Review Modes

Detect the mode from the user's argument:

| Argument | Mode | Applicable scopes |
|----------|------|--------------------|
| (none) | diff | changeset, file, symbol |
| branch name, PR number, `commit1..commit2` | diff | changeset, file, symbol |
| file path(s) or directory | file | file, symbol |
| `file:symbol_name` or a specific function/class name | symbol | symbol |

### Diff mode

**Purpose:** Review a set of git changes — catch architectural violations, file-level design issues, and symbol-level problems in changed code.

- Resolve the diff target: working tree (`git diff HEAD`), branch (`git diff main...{branch}`), PR (`gh pr diff {number}`), or commit range (`git diff {range}`).
- All three checklist scopes apply: changeset → file → symbol.
- Focus review on **changed code** (use the diff to identify what changed, full file for context).
- Untracked (new) files: treat entire content as "changed."

### File mode

**Purpose:** Review one or more complete files as-is — useful for auditing existing code or reviewing files that aren't part of a git change.

- No diff needed. The entire file content is the review target.
- Skip changeset scope (no cross-file architectural review). Run file-scope and symbol-scope checks.
- If a directory is given, find reviewable code files within it (skip markdown, config, data, lockfiles).

### Symbol mode

**Purpose:** Deep-dive review of a specific function, class, or method — the most focused review.

- Locate the symbol in its file. Read the surrounding context.
- Skip changeset and file scopes. Run only symbol-scope checks.
- If the symbol is part of a recent diff, read the diff for additional context.

## Infrastructure

### Checklist — `.claude/review/checklist.yaml`

The **sole source of truth** for what to check. Do not invent checks beyond what it defines.

Structure:
- `pre_check`: shell command for Gate 0 (all modes — see below)
- `categories`: ordered list (design → correctness → readability)
- `items`: each has `id`, `category`, `scope` (changeset/file/symbol), `level` (blocking/advisory), `when` (natural language applicability condition), `prompt` (what to evaluate), `description`

### Symbol extraction — `pipeline.py symbols`

**AST-based deterministic symbol boundary detection.** Use this instead of estimating symbol boundaries from diffs or file reads.

```bash
# Extract all symbols from a Python file:
uv run .claude/skills/code-review/pipeline.py symbols --file <path>
# Returns: [{ "name": "func", "type": "function", "lines": [10, 25] }, ...]

# Filter to symbols overlapping with diff changes:
uv run .claude/skills/code-review/pipeline.py symbols --file <path> --diff HEAD
```

- Handles: functions, async functions, classes, methods, nested symbols
- Qualified names: `ClassName.method`, `outer.inner`, `Outer.Inner.method`
- Start line: the `def`/`class` keyword line (**excludes decorators**)
- End line: last line of the body (Python 3.8+ AST `end_lineno`)

### Staging directory — `.claude/review/staging/`

All review findings are written here as JSON files. Clean it at the start of each review:

```bash
rm -rf .claude/review/staging && mkdir -p .claude/review/staging
```

#### Check format

Checks use a compact or full format depending on the result:

**Passed or blocked** — compact, only `id` and `pass`:

```json
{ "id": "module-cohesion", "pass": true }
{ "id": "naming-clarity", "pass": null }
```

**Failed** — must include `note` and `annotations`:

```json
{
  "id": "redundant-docstrings",
  "pass": false,
  "note": "Docstring restates function name. Remove or add non-obvious detail.",
  "annotations": [{ "line": 114, "message": "Remove redundant docstring" }]
}
```

Rules:
- `pass`: `true` = passed, `false` = failed, `null` = blocked.
- **`annotations` is REQUIRED for failed checks** — the VSCode extension uses it for inline rendering. Each annotation needs `line` (1-indexed absolute) and `message` (<60 chars).
- `category`, `level`, `description`, `status` are **NOT needed** in staging — `pipeline.py build` enriches them automatically by looking up the check `id` in the checklist.
- **`pipeline.py build` converts** annotation `line` to `offset` (relative to target scope start) in the final cache.json. Subagents always write absolute `line` — the conversion is automatic.

#### Staging file schemas

These formats are a **hard contract** with `pipeline.py build` — follow them exactly.

**Changeset findings** (`staging/changeset.json`):

```json
{
  "stage": "changeset",
  "target": { "type": "changeset" },
  "checks": [
    { "id": "dependency-direction", "pass": true }
  ]
}
```

**File findings** (`staging/file-{sanitized_name}.json`):

```json
{
  "stage": "file",
  "target": { "type": "file", "file": "<path>" },
  "checks": [
    { "id": "module-cohesion", "pass": true }
  ]
}
```

File subagents only do file-scope checks. Symbol discovery is handled by `pipeline.py symbols` (AST-based), not by the file subagent.

**Symbol findings** (`staging/symbol-{sanitized_file}-{symbol_name}.json`):

```json
{
  "stage": "symbol",
  "target": { "type": "symbol", "file": "<path>", "symbol": "name", "lines": [start, end] },
  "checks": [
    { "id": "single-responsibility", "pass": true },
    {
      "id": "naming-clarity",
      "pass": false,
      "note": "rename `idxs` to `indices`",
      "annotations": [{ "line": 231, "message": "rename `idxs` to `indices`" }]
    }
  ]
}
```

### Pipeline — `pipeline.py`

Avoids re-reviewing unchanged files and symbols across sessions. `cache.json` (v3) lives at `.claude/review/cache.json` (outside `staging/`, safe from cleanup). It serves both as cache (content-hash keyed) and as the review result (for the VSCode extension and curator report).

#### Workflow

```bash
# Step 1: Discover symbols (AST-based, deterministic boundaries)
uv run .claude/skills/code-review/pipeline.py symbols --file <path>
# → [{ "name": "func_a", "type": "function", "lines": [10, 25] }, ...]

# Step 2: Check file-level + symbol-level cache
uv run .claude/skills/code-review/pipeline.py check --files <space-separated paths>
# → {
#     "cached": ["unchanged.py"],
#     "review": ["changed.py"],
#     "cached_symbols": {"changed.py": ["unchanged_func"]},
#     "review_symbols": {"changed.py": ["modified_func"]},
#     "stats": { "file_hit": 1, "file_miss": 1, "symbol_hit": 1, "symbol_miss": 1 }
#   }

# Step 3: Dispatch subagents
# - File subagents for files in "review" (file-scope checks only)
# - Symbol subagents for symbols in "review_symbols" (using AST boundaries from Step 1)
# - Skip files in "cached" and symbols in "cached_symbols" (staging pre-written)

# Step 4 (MANDATORY): Build combined cache.json v3
uv run .claude/skills/code-review/pipeline.py build
# Merges staging + computes hash-keyed cache entries + converts annotations to offsets
```

If `pipeline.py check` fails (script error), proceed without cache (review all files).

`pipeline.py build` reads `staging/*.json`, sorts targets (changeset → files → symbols), sorts checks by category order, filters all-pass symbols, computes summary counts, builds hash-keyed cache sections (`files` and `symbols`), converts annotation `line` → `offset`, and writes the combined `cache.json` v3.

## Principles

### Fail-fast gating

- If any **blocking** check fails at a scope, mark remaining **advisory** checks at that scope as `blocked` (status: `"blocked"`, pass: `null`). Don't evaluate them.
- In diff mode: a blocking changeset failure blocks all downstream file and symbol checks. A blocking file failure blocks only that file's symbols. Other files are not affected.

### Parallel subagents

- **Always dispatch subagents for symbol-scope review.** The curator does NOT review symbols directly — the curator orchestrates subagents and curates their output. This is a hard rule, not a guideline.
- Dispatch **one subagent per symbol** via the Agent tool. Launch all independent subagents in a **single message** for maximum concurrency.
- Subagents read the checklist themselves — **do not copy checklist content into prompts**. Tell them: the file path, the symbol name and line range, the staging output path, which scope to evaluate, and the diff command if applicable.
- Per-symbol dispatch maximizes review depth. Cross-symbol concerns (naming consistency, type flow) are handled by checklist Method sections that instruct the subagent to inspect call sites and sibling functions.

### Caching

- **MANDATORY: always run `pipeline.py check` before dispatching review**, even if you believe it's the first review. Reviews run across different Claude sessions — you cannot know the cache state without checking.
- Skip cached files (staging is pre-written by `pipeline.py check`). Only dispatch file subagents for files in the `"review"` list.
- For cache-miss files, use `"review_symbols"` to dispatch only symbol subagents for uncached symbols. Symbols in `"cached_symbols"` are already pre-written.
- **MANDATORY: always run `pipeline.py build` after staging is complete.** Without this, all review work is wasted and every subsequent review re-dispatches subagents for already-reviewed files.

### Thoroughness

- Review **all** changed symbols — including dataclasses, small helpers, and type definitions. Small violations accumulate (broken windows effect).
- `when` fields are natural language. Use judgment to decide applicability — don't mechanically pattern-match.

### Curator judgment

- You are the **curator**, not a mechanical executor. After build, read `cache.json` and apply your judgment.
- **Override** dubious subagent findings — edit `cache.json` and note why in the report.
- **Drop** false positives by removing the check entry.
- Do NOT add new checks beyond the checklist.
- Every `fail` note must be **actionable** — tell the developer what to change.

## Report

After curating, print a markdown summary:

```
## Review Result

**Files:** {count} | **Symbols:** {reviewed}/{total} with findings | **Checklist:** v{version}

### Changeset
| Check | Level | Status | Note |
|-------|-------|--------|------|

### File: `{path}`
| Check | Level | Status | Note |
|-------|-------|--------|------|

### Symbol: `{path}:{symbol}` L{start}-{end}
| Check | Level | Status | Note |
|-------|-------|--------|------|

(only symbols with findings are shown)

---
**Summary:** {blocking} blocking [FAIL] · {advisory} advisory [FAIL] · {passed} [PASS] · {blocked} [SKIP] · Cache: {file_hit}/{total} files, {symbol_hit}/{total} symbols hit
```

Status markers: `[PASS]`, `[FAIL]`, `[SKIP]`. Do NOT use emoji — they render poorly in terminal.

Omit all-pass symbols from the report. `.claude/review/cache.json` is the **primary artifact**; the markdown report is a convenience view.

### Process checklist

Append a process self-audit at the end of every review report. This confirms critical steps were executed:

```
### Process
| Step | Done? | Detail |
|------|-------|--------|
| `make check` | [PASS]/[FAIL]/[SKIP] | Result summary or reason for skip |
| Symbol extraction | [PASS]/[SKIP] | AST-based via `pipeline.py symbols` |
| Cache check | [PASS]/[SKIP] | {file_hit}/{total} files, {symbol_hit}/{total} symbols hit |
| Staging cleanup | [PASS] | Cleaned before review |
| Subagent dispatch | [PASS]/[SKIP] | {count} symbols launched, or reason for direct review |
| Build | [PASS]/[SKIP] | {count} staging files merged, {count} files + symbols cached |
```

- `[PASS]` = step executed successfully
- `[FAIL]` = step executed but failed (explain in Detail)
- `[SKIP]` = step not executed (must explain why)
