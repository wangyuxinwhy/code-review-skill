# code-review-skill

Incremental, checklist-driven code review pipeline for [Claude Code](https://docs.anthropic.com/en/docs/claude-code).

Reviews Python code against a configurable checklist at three scopes — **changeset**, **file**, and **symbol** (function/class) — with content-hash caching so unchanged code is never re-reviewed.

## Features

- **AST-based symbol extraction** — deterministic function/class/method boundaries from Python source
- **Incremental caching** — content-hash keyed; unchanged files and symbols are skipped automatically
- **Checklist-driven** — all review checks defined in YAML; no hardcoded or invented checks
- **Multi-scope** — changeset, file, and symbol-level checks with blocking/advisory severity
- **Parallel dispatch** — outputs structured JSON for orchestrating subagent review

## Install

```bash
pip install code-review-skill
```

Or run directly with [`uvx`](https://docs.astral.sh/uv/):

```bash
uvx code-review-skill <command>
```

## Quick start

```bash
# Initialize project (creates .code-review-checklist.yaml)
code-review-skill init

# Review changes against main branch
code-review-skill review main

# Review last 3 commits
code-review-skill review HEAD~3..HEAD
```

## Commands

| Command | Description |
|---------|-------------|
| `init` | Output project initialization context |
| `review <range>` | Discover, check cache, and output review plan |
| `discover <range>` | Find changed files and diff-touched symbols |
| `check` | Compare files/symbols against cache; output review plan |
| `symbols` | Extract symbols from Python files |
| `stage` | Write review findings from stdin JSON |
| `build` | Merge staging files into cache |
| `show` | Display cached findings as annotated source |
| `refresh` | Self-heal cache (match files by hash, rebuild stale targets) |
| `checklist` | Print the active checklist |

## Checklist

The review checklist is resolved in order:

1. `--checklist <path>` (explicit override)
2. `.code-review-checklist.yaml` (project-local, committed to git)
3. Built-in default (shipped with package)

The built-in checklist includes 13 checks across three scopes:

- **Changeset** — dependency direction (blocking), naming consistency
- **File** — module cohesion, file naming
- **Symbol** — single responsibility (blocking), constructor purity (blocking), type annotations, parameter breadth, data structure fit, naming clarity, idiomatic constructs, primitive obsession, redundant docstrings

## Review pipeline

```
code-review-skill review <range>
  │
  ├─ discover changed files + diff-touched symbols
  ├─ check cache for previously reviewed content
  ├─ output review plan (JSON)
  │
  ├─ dispatch subagents for uncached symbols
  │   └─ each writes findings via: code-review-skill stage
  │
  └─ code-review-skill build  (merge staging → cache)
```

## License

MIT
