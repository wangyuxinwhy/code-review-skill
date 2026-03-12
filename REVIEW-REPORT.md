# Code Review Report

**Project:** code-review-skill
**Files:** 6 source | **Symbols:** 69 reviewed | **Checklist:** v2
**Agents:** 55+ sub-agents (1 per symbol, 1 per file, 1 changeset)

---

## Changeset

| Check | Level | Status |
|---|---|---|
| dependency-direction | blocking | [PASS] |
| naming-consistency | advisory | [FAIL] |

**[ADVISORY naming-consistency]** `StagingEntry` uses both `targets` and `symbols` fields (types.py:114-115) of type `list[StagingSymbolEntry]` for the same concept. `StagingSymbolEntry` has both `symbol` and `name` fields (types.py:101-102) for the symbol name. `_extract_symbol_entries` and `_normalize_symbol_target` must handle both variants.

---

## File Scope

| File | module-cohesion | file-naming |
|---|---|---|
| cli.py | [PASS] | [PASS] |
| symbols.py | [PASS] | [PASS] |
| cache.py | [PASS] | [PASS] |
| staging.py | [FAIL] | [PASS] |
| render.py | [PASS] | [PASS] |

**[ADVISORY module-cohesion] staging.py** Bundles 5 loosely related concerns: checklist resolution/loading, staging I/O, check enrichment/sorting, annotation offset conversion, and merge logic. The checklist resolution functions (`resolve_checklist`, `load_checklist`) are independently reusable and could live in their own module.

---

## Symbol Scope - Blocking Failures

### _cmd_init_check  cli.py:534-586
**[BLOCKING single-responsibility]** Mixes file I/O (reading/parsing YAML, .gitignore), validation rules, and stdout presentation with `sys.exit`. Extract validation into a pure function returning `list[_InitCheckResult]`.
```
  534 | def _cmd_init_check() -> None:
       ^ Mixes I/O, validation, and presentation
```

### _write_cached_staging  cache.py:230-240
**[BLOCKING single-responsibility]** Mixes data transformation (offset-to-line conversion) with file I/O (JSON serialization + write to disk). Separate the staging dict construction from the file write.

### _rescan_files  cache.py:441-532
**[BLOCKING single-responsibility]** 91-line function with 4 responsibilities: (1) scanning files and matching hashes, (2) pruning orphaned entries, (3) assembling CacheFile, (4) writing to disk. Extract scan/prune/write into separate helpers.

### build  cache.py:306-352
**[BLOCKING single-responsibility]** Performs 4 responsibilities: loading/merging staging, converting annotations, building hash-keyed caches, and writing the combined CacheFile. Should delegate the annotation conversion loop and file write to helpers.

### merge_staging  staging.py:228-275
**[BLOCKING single-responsibility]** Handles 3 concerns: dispatching by stage type, enriching/sorting checks, and filtering all-pass symbols. The enrichment loop and the filtering logic could be separated.

### check (cache.py)  cache.py:177-227
**[BLOCKING single-responsibility]** Mixes cache lookup/partitioning with writing file-level staging JSON to disk. The staging side-effect should be separated from the pure partitioning logic.

---

## Symbol Scope - Advisory Findings (curated)

### Recurring Patterns (cross-cutting)

**[ADVISORY no-primitive-obsession]** `str` used pervasively for file paths where `Path` would be more consistent (affects: `_check_file_cache`, `_check_symbol_cache`, `check`, `_write_cached_staging`, `_restore_symbol_target`, `extract_symbols_batch`, `_read_source_lines`). The codebase uses `Path` for function params in some places but `str` in others.

**[ADVISORY no-primitive-obsession]** `tuple[int, int]` used across the codebase for line ranges without a named type alias. Introducing `LineRange = tuple[int, int]` would clarify (start, end) semantics and the 1-indexed inclusive convention.

**[ADVISORY type-annotations]** `_convert_annotations_to_offsets` and `_convert_offsets_to_lines` use `Mapping[str, Any]` / `dict[str, Any]` instead of the domain TypedDicts (`StagingCheck`, `CheckResult`) already defined in types.py. This erases static analysis value.

**[ADVISORY type-annotations]** Multiple functions assign `json.loads()` / `yaml.safe_load()` to untyped locals (`Any`), losing type safety: `load_cache:61`, `load_checklist:53`, `load_staging_files:71`.

**[ADVISORY redundant-docstrings]** Several docstrings restate implementation details visible from the code rather than documenting the contract: `extract_symbols`, `discover_changed_files`, `compute_symbol_hash`, `_build_symbols_cache`, `_discover_python_files`, `check`, `_normalize_symbol_target`, `_count_items`.

### Per-Symbol Notable Findings

| Symbol | File | Check | Note |
|---|---|---|---|
| `main` | cli.py:169 | data-structure-fit | `plan` dict (line 361) has fixed schema; use TypedDict |
| `main` | cli.py:169 | naming-clarity | `raw` (line 343) is generic; prefer `stdin_text` |
| `_build_init_instructions` | cli.py:388 | idiomatic-constructs | Extract `_count_items()` call from f-string to local variable |
| `_cmd_init_check` | cli.py:534 | idiomatic-constructs | Duplicate "skipped" append blocks; use `all()` for `all_passed` |
| `_visit_symbols` | symbols.py:29 | data-structure-fit | Accumulator pattern; generator with `yield from` would be cleaner |
| `_get_diff_hunks` | symbols.py:53 | idiomatic-constructs | Fragile hunk parsing via chained `split()`; use regex |
| `_get_diff_hunks` | symbols.py:53 | naming-clarity | `parts`, `new_range_parts`, `diff_output` are generic/inconsistent |
| `_ranges_overlap` | symbols.py:81 | naming-clarity | `a`/`b` params are opaque; use `range_a`/`range_b` |
| `_filter_symbols_by_diff` | symbols.py:85 | naming-clarity | Line 85 is 131 chars; break signature |
| `_rescan_files` | cache.py:441 | parameter-breadth | 6 params; `files_cache`/`symbols_cache` redundant with `data` fields |
| `_rescan_files` | cache.py:441 | naming-clarity | `new_targets`/`all_entries` are ambiguous |
| `_rescan_files` | cache.py:466 | idiomatic-constructs | File read twice (hash + symbol extraction) |
| `_check_symbol_cache` | cache.py:156 | idiomatic-constructs | `compute_symbol_hash` re-reads file for each symbol |
| `_verify_targets` | cache.py:398 | idiomatic-constructs | Silent `pass` on unknown target types; should return False |
| `_discover_python_files` | cache.py:358 | parameter-breadth | Hard-coded exclude set; accept optional param |
| `refresh` | cache.py:420 | idiomatic-constructs | Intermediary variables forwarded to `_rescan_files` |
| `_FileCacheResult` | cache.py:72 | no-primitive-obsession | `hit`/`miss` duplicated with `_SymbolCacheResult` |
| `resolve_checklist` | staging.py:29 | naming-clarity | `explicit` param vague; prefer `override_path` |
| `write_staging_entry` | staging.py:74 | naming-clarity | Several local names are vague |
| `enrich_check` | staging.py:111 | data-structure-fit | Copies to plain `dict`, losing TypedDict guarantees |
| `has_non_pass` | staging.py:149 | idiomatic-constructs | Manual loop; `any()` is more idiomatic |
| `_normalize_symbol_target` | staging.py:185 | naming-clarity | Silent double fallback `symbol`/`name` hides bugs |
| `_count_checks` | staging.py:278 | naming-clarity | Name understates scope; also resolves statuses |
| `_annotate_source` | render.py:28 | idiomatic-constructs | No guard for `start < 1` (negative index) |
| `show` | render.py:50 | single-responsibility | Curator note: acceptable as rendering orchestrator |
| `StagingEntry` | types.py:107 | no-primitive-obsession | `stage: str` should be `Literal["changeset", "file", "symbol"]` |
| `StagingSymbolEntry` | types.py:98 | naming-clarity | `symbol`/`name` fields ambiguous |
| `Annotation` | types.py:59 | naming-clarity | `offset` field is ambiguous |
| `ReviewSummary` | types.py:123 | naming-clarity | `blocked` ambiguous alongside `blocking_failures` |
| `Checklist` | types.py:144 | type-annotations | `lines` field as `tuple[int, int]` won't deserialize from JSON lists |

---

## Summary

| | Blocking | Advisory |
|---|---|---|
| Changeset | 0 | 1 |
| File | 0 | 1 |
| Symbol | 6 | ~40 |
| **Total** | **6** | **~42** |

**Top priorities:**
1. Fix 6 blocking single-responsibility violations by extracting I/O from pure logic
2. Standardize `str` vs `Path` for file paths across the API surface
3. Introduce `LineRange` type alias for `tuple[int, int]`
4. Tighten `Mapping[str, Any]` signatures to use domain TypedDicts
5. Resolve `targets`/`symbols` and `symbol`/`name` naming inconsistency in types.py
