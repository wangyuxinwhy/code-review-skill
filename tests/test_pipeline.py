# pyright: reportArgumentType=false, reportCallIssue=false, reportPrivateUsage=false
"""Tests for the code-review pipeline (symbols, cache, and merge logic)."""

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest

from code_review_skill.cache import (
    _restore_symbol_target,
    build,
    check,
    compute_file_hash,
    compute_symbol_hash,
    load_cache,
)
from code_review_skill.checklist import load_checklist, resolve_checklist
from code_review_skill.staging import (
    _convert_annotations_to_offsets,
    _convert_offsets_to_lines,
    _compute_summary,
    _normalize_symbol_target,
    enrich_check,
    has_non_pass,
    load_staging_files,
    merge_staging,
    sort_checks,
    target_sort_key,
    write_staging_entry,
)
from code_review_skill.symbols import (
    _filter_symbols_by_diff,
    discover_changed_files,
    extract_symbols,
    extract_symbols_batch,
)
from code_review_skill.types import StagingEntry, SymbolDef, TargetEntry

STAGING_DIR = Path(__file__).parent / "staging"


def _make_checklist(path: Path, version: str = "2") -> Path:
    checklist = path / "checklist.yaml"
    checklist.write_text(f'version: "{version}"\nitems: []\n')
    return checklist


def _make_source_file(path: Path, name: str, content: str) -> Path:
    file_path = path / name
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content)
    return file_path


def _make_file_staging(file_str: str) -> dict[str, Any]:
    return {
        "stage": "file",
        "target": {"type": "file", "file": file_str},
        "checks": [
            {
                "id": "module-cohesion",
                "category": "design",
                "level": "advisory",
                "status": "passed",
                "description": "Module is cohesive",
                "note": "All good",
                "pass": True,
            }
        ],
    }


def _make_symbol_staging(file_str: str, symbol_name: str, lines: list[int]) -> dict[str, Any]:
    return {
        "stage": "symbol",
        "targets": [
            {
                "target": {
                    "type": "symbol",
                    "file": file_str,
                    "symbol": symbol_name,
                    "lines": lines,
                },
                "checks": [
                    {
                        "id": "single-responsibility",
                        "category": "design",
                        "level": "blocking",
                        "status": "passed",
                        "description": "SRP ok",
                        "note": "Fine",
                        "pass": True,
                    }
                ],
            }
        ],
    }


def _make_v3_cache(
    files: dict[str, Any] | None = None,
    symbols: dict[str, Any] | None = None,
    checklist_version: str = "2",
) -> dict[str, Any]:
    """Build a v3 cache structure for test fixtures."""
    return {
        "version": "3",
        "timestamp": "2026-01-01T00:00:00Z",
        "checklist_version": checklist_version,
        "summary": {
            "blocking_failures": 0,
            "advisory_failures": 0,
            "passed": 0,
            "blocked": 0,
            "symbols_reviewed": 0,
        },
        "targets": [],
        "files": files or {},
        "symbols": symbols or {},
    }


# --- Phase 1: AST Symbol Extraction ---


class TestExtractSymbols:
    def test_top_level_function(self) -> None:
        source = "def foo():\n    pass\n"

        symbols = extract_symbols(source)

        assert len(symbols) == 1
        assert symbols[0] == {"name": "foo", "type": "function", "lines": (1, 2)}

    def test_class_with_methods(self) -> None:
        source = "class Bar:\n    def method(self):\n        pass\n"

        symbols = extract_symbols(source)

        names = [symbol["name"] for symbol in symbols]
        assert "Bar" in names
        assert "Bar.method" in names

    def test_class_type_and_method_type(self) -> None:
        source = "class Bar:\n    def method(self):\n        pass\n"

        symbols = extract_symbols(source)

        by_name = {symbol["name"]: symbol for symbol in symbols}
        assert by_name["Bar"]["type"] == "class"
        assert by_name["Bar.method"]["type"] == "method"

    def test_nested_function(self) -> None:
        source = "def outer():\n    def inner():\n        pass\n    return inner\n"

        symbols = extract_symbols(source)

        names = [symbol["name"] for symbol in symbols]
        assert "outer" in names
        assert "outer.inner" in names

    def test_async_def(self) -> None:
        source = "async def fetch():\n    pass\n"

        symbols = extract_symbols(source)

        assert len(symbols) == 1
        assert symbols[0]["name"] == "fetch"
        assert symbols[0]["type"] == "function"

    def test_decorator_excluded_from_range(self) -> None:
        source = "@decorator\ndef decorated():\n    pass\n"

        symbols = extract_symbols(source)

        assert symbols[0]["lines"][0] == 2  # def line, not decorator line

    def test_qualified_names_nested_class(self) -> None:
        source = "class Outer:\n    class Inner:\n        def method(self):\n            pass\n"

        symbols = extract_symbols(source)

        names = {symbol["name"] for symbol in symbols}
        assert names == {"Outer", "Outer.Inner", "Outer.Inner.method"}

    def test_empty_file(self) -> None:
        assert extract_symbols("") == []

    def test_syntax_error_returns_empty(self) -> None:
        assert extract_symbols("def broken(") == []

    def test_module_level_assignments_ignored(self) -> None:
        source = "x = 1\ny = 'hello'\nPI = 3.14\n"

        symbols = extract_symbols(source)

        assert symbols == []


class TestFilterSymbolsByDiff:
    def test_filters_overlapping_symbols(self) -> None:
        symbols: list[SymbolDef] = [
            {"name": "a", "type": "function", "lines": (1, 5)},
            {"name": "b", "type": "function", "lines": (10, 20)},
            {"name": "c", "type": "function", "lines": (25, 30)},
        ]
        diff_hunks = [(3, 4), (27, 27)]

        filtered = _filter_symbols_by_diff(symbols, diff_hunks)

        names = [symbol["name"] for symbol in filtered]
        assert names == ["a", "c"]

    def test_no_overlap_returns_empty(self) -> None:
        symbols: list[SymbolDef] = [
            {"name": "a", "type": "function", "lines": (1, 5)},
        ]

        assert _filter_symbols_by_diff(symbols, [(10, 10), (20, 20)]) == []


# --- Hashing ---


class TestComputeFileHash:
    def test_returns_sha256_prefixed_hash(self, tmp_path: Path) -> None:
        file_path = _make_source_file(tmp_path, "a.py", "print('hello')\n")

        result = compute_file_hash(file_path)

        assert result.startswith("sha256:")
        assert len(result) == len("sha256:") + 64

    def test_different_content_produces_different_hash(self, tmp_path: Path) -> None:
        file_a = _make_source_file(tmp_path, "a.py", "x = 1\n")
        file_b = _make_source_file(tmp_path, "b.py", "x = 2\n")

        assert compute_file_hash(file_a) != compute_file_hash(file_b)

    def test_same_content_produces_same_hash(self, tmp_path: Path) -> None:
        content = "def foo(): pass\n"
        file_a = _make_source_file(tmp_path, "a.py", content)
        file_b = _make_source_file(tmp_path, "b.py", content)

        assert compute_file_hash(file_a) == compute_file_hash(file_b)


class TestComputeSymbolHash:
    def test_extracts_correct_lines(self, tmp_path: Path) -> None:
        file_path = _make_source_file(tmp_path, "mod.py", "line1\nline2\nline3\nline4\nline5\n")

        hash_2_4 = compute_symbol_hash(file_path, [2, 4])
        # Manually compute expected: lines 2-4 -> "line2\nline3\nline4"
        expected = "sha256:" + hashlib.sha256(b"line2\nline3\nline4").hexdigest()

        assert hash_2_4 == expected

    def test_single_line_symbol(self, tmp_path: Path) -> None:
        file_path = _make_source_file(tmp_path, "mod.py", "a\nb\nc\n")

        result = compute_symbol_hash(file_path, [2, 2])

        assert result.startswith("sha256:")

    def test_different_lines_produce_different_hash(self, tmp_path: Path) -> None:
        file_path = _make_source_file(tmp_path, "mod.py", "aaa\nbbb\nccc\n")

        hash_1 = compute_symbol_hash(file_path, [1, 1])
        hash_2 = compute_symbol_hash(file_path, [2, 2])

        assert hash_1 != hash_2


# --- Annotation conversion ---


class TestAnnotationConversion:
    def test_line_to_offset_for_file(self) -> None:
        checks = [{"id": "a", "pass": False, "annotations": [{"line": 5, "message": "fix"}]}]

        result = _convert_annotations_to_offsets(checks, base_line=1)

        assert result[0]["annotations"] == [{"offset": 4, "message": "fix"}]

    def test_line_to_offset_for_symbol(self) -> None:
        checks = [{"id": "a", "pass": False, "annotations": [{"line": 15, "message": "fix"}]}]

        result = _convert_annotations_to_offsets(checks, base_line=10)

        assert result[0]["annotations"] == [{"offset": 5, "message": "fix"}]

    def test_offset_to_line_for_file(self) -> None:
        checks = [{"id": "a", "pass": False, "annotations": [{"offset": 4, "message": "fix"}]}]

        result = _convert_offsets_to_lines(checks, base_line=1)

        assert result[0]["annotations"] == [{"line": 5, "message": "fix"}]

    def test_offset_to_line_for_symbol(self) -> None:
        checks = [{"id": "a", "pass": False, "annotations": [{"offset": 5, "message": "fix"}]}]

        result = _convert_offsets_to_lines(checks, base_line=10)

        assert result[0]["annotations"] == [{"line": 15, "message": "fix"}]

    def test_roundtrip_identity(self) -> None:
        original = [
            {
                "id": "a",
                "pass": False,
                "annotations": [
                    {"line": 12, "message": "msg1"},
                    {"line": 18, "message": "msg2"},
                ],
            }
        ]

        offsets = _convert_annotations_to_offsets(original, base_line=10)
        restored = _convert_offsets_to_lines(offsets, base_line=10)

        assert restored[0]["annotations"] == original[0]["annotations"]

    def test_checks_without_annotations_unchanged(self) -> None:
        checks = [{"id": "a", "pass": True}]

        result = _convert_annotations_to_offsets(checks, base_line=1)

        assert result == checks

    def test_does_not_mutate_original(self) -> None:
        checks: list[dict[str, Any]] = [{"id": "a", "pass": False, "annotations": [{"line": 5, "message": "fix"}]}]

        _convert_annotations_to_offsets(checks, base_line=1)

        assert checks[0]["annotations"][0]["line"] == 5


# --- Cache: load ---


class TestLoadCache:
    def test_returns_none_when_no_cache_file(self, tmp_path: Path) -> None:
        checklist = _make_checklist(tmp_path)
        cache_path = tmp_path / "cache.json"

        assert load_cache(cache_path, checklist) is None

    def test_returns_none_on_checklist_version_mismatch(self, tmp_path: Path) -> None:
        checklist = _make_checklist(tmp_path, version="3")
        cache_path = tmp_path / "cache.json"
        cache_path.write_text(json.dumps(_make_v3_cache(checklist_version="2")))

        assert load_cache(cache_path, checklist) is None

    def test_returns_cache_on_version_match(self, tmp_path: Path) -> None:
        checklist = _make_checklist(tmp_path, version="2")
        cache_path = tmp_path / "cache.json"
        cache_path.write_text(json.dumps(_make_v3_cache()))

        result = load_cache(cache_path, checklist)

        assert result is not None
        assert result["files"] == {}
        assert result["symbols"] == {}

    def test_returns_none_on_corrupt_json(self, tmp_path: Path) -> None:
        checklist = _make_checklist(tmp_path)
        cache_path = tmp_path / "cache.json"
        cache_path.write_text("not json{{{")

        assert load_cache(cache_path, checklist) is None

    def test_rejects_v2_cache(self, tmp_path: Path) -> None:
        checklist = _make_checklist(tmp_path, version="2")
        cache_path = tmp_path / "cache.json"
        cache_path.write_text(
            json.dumps(
                {
                    "version": "2",
                    "checklist_version": "2",
                    "entries": {},
                }
            )
        )

        assert load_cache(cache_path, checklist) is None

    def test_rejects_v1_cache(self, tmp_path: Path) -> None:
        checklist = _make_checklist(tmp_path, version="2")
        cache_path = tmp_path / "cache.json"
        cache_path.write_text(
            json.dumps(
                {
                    "version": "1",
                    "checklist_version": "2",
                    "entries": {},
                }
            )
        )

        assert load_cache(cache_path, checklist) is None


# --- Cache: check ---


class TestCheck:
    def test_all_uncached_when_no_cache_file(self, tmp_path: Path) -> None:
        checklist = _make_checklist(tmp_path)
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        file_a = _make_source_file(tmp_path, "a.py", "x = 1\n")
        file_b = _make_source_file(tmp_path, "b.py", "x = 2\n")

        result = check(
            files=[str(file_a), str(file_b)],
            cache_path=tmp_path / "cache.json",
            checklist_path=checklist,
            staging_dir=staging_dir,
        )

        assert result["cached_files"] == []
        assert set(result["review_files"]) == {str(file_a), str(file_b)}
        assert result["stats"]["file_hit"] == 0
        assert result["stats"]["file_miss"] == 2

    def test_cache_hit_when_hash_matches(self, tmp_path: Path) -> None:
        checklist = _make_checklist(tmp_path)
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        file_path = _make_source_file(tmp_path, "a.py", "x = 1\n")
        file_str = str(file_path)
        content_hash = compute_file_hash(file_path)

        cache_path = tmp_path / "cache.json"
        cache_path.write_text(
            json.dumps(
                _make_v3_cache(
                    files={content_hash: {"checks": [{"id": "module-cohesion", "pass": True}]}},
                )
            )
        )

        result = check(
            files=[file_str],
            cache_path=cache_path,
            checklist_path=checklist,
            staging_dir=staging_dir,
        )

        assert result["cached_files"] == [file_str]
        assert result["review_files"] == []
        assert result["stats"]["file_hit"] == 1
        assert result["stats"]["file_miss"] == 0

    def test_cache_miss_when_hash_differs(self, tmp_path: Path) -> None:
        checklist = _make_checklist(tmp_path)
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        file_path = _make_source_file(tmp_path, "a.py", "x = 1\n")
        file_str = str(file_path)

        cache_path = tmp_path / "cache.json"
        cache_path.write_text(
            json.dumps(
                _make_v3_cache(
                    files={"sha256:stale_hash": {"checks": []}},
                )
            )
        )

        result = check(
            files=[file_str],
            cache_path=cache_path,
            checklist_path=checklist,
            staging_dir=staging_dir,
        )

        assert result["cached_files"] == []
        assert result["review_files"] == [file_str]

    def test_writes_staging_file_for_cached_file(self, tmp_path: Path) -> None:
        checklist = _make_checklist(tmp_path)
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        file_path = _make_source_file(tmp_path, "src/mod.py", "x = 1\n")
        file_str = str(file_path)
        content_hash = compute_file_hash(file_path)

        cache_path = tmp_path / "cache.json"
        cache_path.write_text(
            json.dumps(
                _make_v3_cache(
                    files={content_hash: {"checks": [{"id": "module-cohesion", "pass": True}]}},
                )
            )
        )

        check(
            files=[file_str],
            cache_path=cache_path,
            checklist_path=checklist,
            staging_dir=staging_dir,
        )

        staging_files = list(staging_dir.glob("file-*.json"))
        assert len(staging_files) == 1
        written = json.loads(staging_files[0].read_text())
        assert written["stage"] == "file"
        assert written["target"]["file"] == file_str

    def test_staging_file_converts_offsets_to_lines(self, tmp_path: Path) -> None:
        checklist = _make_checklist(tmp_path)
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        file_path = _make_source_file(tmp_path, "a.py", "x = 1\n")
        file_str = str(file_path)
        content_hash = compute_file_hash(file_path)

        cache_path = tmp_path / "cache.json"
        cache_path.write_text(
            json.dumps(
                _make_v3_cache(
                    files={
                        content_hash: {
                            "checks": [
                                {
                                    "id": "test",
                                    "pass": False,
                                    "annotations": [{"offset": 4, "message": "fix"}],
                                }
                            ]
                        }
                    },
                )
            )
        )

        check(
            files=[file_str],
            cache_path=cache_path,
            checklist_path=checklist,
            staging_dir=staging_dir,
        )

        staging_files = list(staging_dir.glob("file-*.json"))
        written = json.loads(staging_files[0].read_text())
        # offset 4 + base_line 1 = line 5
        assert written["checks"][0]["annotations"][0]["line"] == 5

    def test_writes_symbol_cached_json(self, tmp_path: Path) -> None:
        checklist = _make_checklist(tmp_path)
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        file_path = _make_source_file(tmp_path, "a.py", "def foo():\n    pass\n")
        file_str = str(file_path)
        content_hash = compute_file_hash(file_path)
        symbol_hash = compute_symbol_hash(file_path, [1, 2])

        symbol_checks = [{"id": "srp", "pass": True}]
        cache_path = tmp_path / "cache.json"
        cache_path.write_text(
            json.dumps(
                _make_v3_cache(
                    files={content_hash: {"checks": [{"id": "module-cohesion", "pass": True}]}},
                    symbols={symbol_hash: {"checks": symbol_checks}},
                )
            )
        )

        check(
            files=[file_str],
            cache_path=cache_path,
            checklist_path=checklist,
            staging_dir=staging_dir,
        )

        symbol_cached_path = staging_dir / "symbol-cached.json"
        assert symbol_cached_path.exists()
        data = json.loads(symbol_cached_path.read_text())
        assert data["stage"] == "symbol"
        assert len(data["targets"]) == 1
        assert data["targets"][0]["target"]["symbol"] == "foo"

    def test_no_symbol_cached_when_no_symbols_in_cache(self, tmp_path: Path) -> None:
        checklist = _make_checklist(tmp_path)
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        file_path = _make_source_file(tmp_path, "a.py", "x = 1\n")
        file_str = str(file_path)
        content_hash = compute_file_hash(file_path)

        cache_path = tmp_path / "cache.json"
        cache_path.write_text(
            json.dumps(
                _make_v3_cache(
                    files={content_hash: {"checks": [{"id": "module-cohesion", "pass": True}]}},
                )
            )
        )

        check(
            files=[file_str],
            cache_path=cache_path,
            checklist_path=checklist,
            staging_dir=staging_dir,
        )

        assert not (staging_dir / "symbol-cached.json").exists()

    def test_mixed_hit_and_miss(self, tmp_path: Path) -> None:
        checklist = _make_checklist(tmp_path)
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        file_hit = _make_source_file(tmp_path, "hit.py", "cached\n")
        file_miss = _make_source_file(tmp_path, "miss.py", "changed\n")
        hit_str = str(file_hit)
        miss_str = str(file_miss)
        hit_hash = compute_file_hash(file_hit)

        cache_path = tmp_path / "cache.json"
        cache_path.write_text(
            json.dumps(
                _make_v3_cache(
                    files={
                        hit_hash: {"checks": []},
                        "sha256:old_hash": {"checks": []},
                    },
                )
            )
        )

        result = check(
            files=[hit_str, miss_str],
            cache_path=cache_path,
            checklist_path=checklist,
            staging_dir=staging_dir,
        )

        assert result["cached_files"] == [hit_str]
        assert result["review_files"] == [miss_str]
        assert result["stats"]["file_hit"] == 1
        assert result["stats"]["file_miss"] == 1

    def test_nonexistent_file_treated_as_miss(self, tmp_path: Path) -> None:
        checklist = _make_checklist(tmp_path)
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        result = check(
            files=["does/not/exist.py"],
            cache_path=tmp_path / "cache.json",
            checklist_path=checklist,
            staging_dir=staging_dir,
        )

        assert result["review_files"] == ["does/not/exist.py"]


class TestCheckSymbolLevel:
    """Phase B: symbol-level cache within changed files."""

    def test_file_miss_but_symbol_hit(self, tmp_path: Path) -> None:
        checklist = _make_checklist(tmp_path)
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        # File content changed (added a new function), but foo() is unchanged
        file_path = _make_source_file(tmp_path, "m.py", "def foo():\n    pass\n\ndef bar():\n    pass\n")
        file_str = str(file_path)
        symbol_hash = compute_symbol_hash(file_path, [1, 2])

        cache_path = tmp_path / "cache.json"
        cache_path.write_text(
            json.dumps(
                _make_v3_cache(
                    files={},
                    symbols={symbol_hash: {"checks": [{"id": "srp", "pass": True}]}},
                )
            )
        )

        result = check(
            files=[file_str],
            cache_path=cache_path,
            checklist_path=checklist,
            staging_dir=staging_dir,
        )

        assert result["review_files"] == [file_str]
        assert file_str in result["cached_symbols"]
        assert "foo" in result["cached_symbols"][file_str]
        assert file_str in result["review_symbols"]
        assert "bar" in result["review_symbols"][file_str]
        assert result["stats"]["symbol_hit"] == 1
        assert result["stats"]["symbol_miss"] == 1

    def test_symbol_hit_writes_staging(self, tmp_path: Path) -> None:
        checklist = _make_checklist(tmp_path)
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        file_path = _make_source_file(tmp_path, "m.py", "def foo():\n    pass\n\ndef bar():\n    pass\n")
        file_str = str(file_path)
        symbol_hash = compute_symbol_hash(file_path, [1, 2])

        cache_path = tmp_path / "cache.json"
        cache_path.write_text(
            json.dumps(
                _make_v3_cache(
                    symbols={symbol_hash: {"checks": [{"id": "srp", "pass": True}]}},
                )
            )
        )

        check(
            files=[file_str],
            cache_path=cache_path,
            checklist_path=checklist,
            staging_dir=staging_dir,
        )

        symbol_cached = staging_dir / "symbol-cached.json"
        assert symbol_cached.exists()
        data = json.loads(symbol_cached.read_text())
        assert data["targets"][0]["target"]["symbol"] == "foo"

    def test_symbol_hit_converts_offset_to_line(self, tmp_path: Path) -> None:
        checklist = _make_checklist(tmp_path)
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        file_path = _make_source_file(tmp_path, "m.py", "def foo():\n    x = 1\n    return x\n")
        file_str = str(file_path)
        symbol_hash = compute_symbol_hash(file_path, [1, 3])

        cache_path = tmp_path / "cache.json"
        cache_path.write_text(
            json.dumps(
                _make_v3_cache(
                    symbols={
                        symbol_hash: {
                            "checks": [
                                {
                                    "id": "a",
                                    "pass": False,
                                    "annotations": [{"offset": 1, "message": "fix"}],
                                }
                            ]
                        }
                    },
                )
            )
        )

        check(
            files=[file_str],
            cache_path=cache_path,
            checklist_path=checklist,
            staging_dir=staging_dir,
        )

        data = json.loads((staging_dir / "symbol-cached.json").read_text())
        # offset 1 + base_line 1 (symbol starts at line 1) = line 2
        assert data["targets"][0]["checks"][0]["annotations"][0]["line"] == 2


# --- Cache: build ---


class TestBuild:
    def test_creates_v3_cache_with_files_and_symbols(self, tmp_path: Path) -> None:
        checklist = _make_checklist(tmp_path, version="2")
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        source = _make_source_file(tmp_path, "a.py", "def foo():\n    return 1\n")
        source_str = str(source)

        file_staging = _make_file_staging(source_str)
        (staging_dir / "file-a-py.json").write_text(json.dumps(file_staging))

        symbol_staging = _make_symbol_staging(source_str, "foo", [1, 2])
        (staging_dir / "symbol-group1.json").write_text(json.dumps(symbol_staging))

        cache_path = tmp_path / "cache.json"
        build(staging_dir=staging_dir, cache_path=cache_path, checklist_path=checklist)

        assert cache_path.exists()
        cache_data = json.loads(cache_path.read_text())
        assert cache_data["version"] == "3"
        assert cache_data["checklist_version"] == "2"
        assert "files" in cache_data
        assert "symbols" in cache_data
        assert "entries" not in cache_data
        assert "timestamp" in cache_data
        assert "summary" in cache_data
        assert "targets" in cache_data

    def test_files_section_keyed_by_hash(self, tmp_path: Path) -> None:
        checklist = _make_checklist(tmp_path)
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        source = _make_source_file(tmp_path, "b.py", "hello\n")
        source_str = str(source)
        expected_hash = compute_file_hash(source)

        (staging_dir / "file-b-py.json").write_text(json.dumps(_make_file_staging(source_str)))

        cache_path = tmp_path / "cache.json"
        build(staging_dir=staging_dir, cache_path=cache_path, checklist_path=checklist)

        cache_data = json.loads(cache_path.read_text())
        assert expected_hash in cache_data["files"]
        assert "checks" in cache_data["files"][expected_hash]

    def test_symbols_section_keyed_by_hash(self, tmp_path: Path) -> None:
        checklist = _make_checklist(tmp_path)
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        source = _make_source_file(tmp_path, "c.py", "def bar():\n    pass\nother\n")
        source_str = str(source)
        expected_hash = compute_symbol_hash(source, [1, 2])

        (staging_dir / "file-c-py.json").write_text(json.dumps(_make_file_staging(source_str)))
        symbol_staging = _make_symbol_staging(source_str, "bar", [1, 2])
        (staging_dir / "symbol-g1.json").write_text(json.dumps(symbol_staging))

        cache_path = tmp_path / "cache.json"
        build(staging_dir=staging_dir, cache_path=cache_path, checklist_path=checklist)

        cache_data = json.loads(cache_path.read_text())
        assert expected_hash in cache_data["symbols"]

    def test_targets_annotations_converted_to_offsets(self, tmp_path: Path) -> None:
        checklist = _make_checklist(tmp_path)
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        source = _make_source_file(tmp_path, "d.py", "def baz():\n    bad\n    pass\n")
        source_str = str(source)

        # File staging with an annotation at absolute line 5
        file_staging = {
            "stage": "file",
            "target": {"type": "file", "file": source_str},
            "checks": [
                {
                    "id": "test-check",
                    "pass": False,
                    "note": "problem",
                    "annotations": [{"line": 5, "message": "issue here"}],
                }
            ],
        }
        (staging_dir / "file-d-py.json").write_text(json.dumps(file_staging))

        # Symbol staging with an annotation at absolute line 12 (symbol starts at line 10)
        symbol_staging = {
            "stage": "symbol",
            "targets": [
                {
                    "target": {
                        "type": "symbol",
                        "file": source_str,
                        "symbol": "baz",
                        "lines": [1, 3],
                    },
                    "checks": [
                        {
                            "id": "naming",
                            "pass": False,
                            "note": "rename",
                            "annotations": [{"line": 2, "message": "rename this"}],
                        }
                    ],
                }
            ],
        }
        (staging_dir / "symbol-g1.json").write_text(json.dumps(symbol_staging))

        cache_path = tmp_path / "cache.json"
        build(staging_dir=staging_dir, cache_path=cache_path, checklist_path=checklist)

        cache_data = json.loads(cache_path.read_text())

        # File target: line 5 with base_line 1 -> offset 4
        file_targets = [
            target_entry for target_entry in cache_data["targets"] if target_entry["target"]["type"] == "file"
        ]
        assert len(file_targets) == 1
        file_ann = file_targets[0]["checks"][0]["annotations"][0]
        assert file_ann["offset"] == 4
        assert "line" not in file_ann

        # Symbol target: line 2 with base_line 1 (symbol starts line 1) -> offset 1
        symbol_targets = [
            target_entry for target_entry in cache_data["targets"] if target_entry["target"]["type"] == "symbol"
        ]
        assert len(symbol_targets) == 1
        sym_ann = symbol_targets[0]["checks"][0]["annotations"][0]
        assert sym_ann["offset"] == 1
        assert "line" not in sym_ann

    def test_propagates_checklist_version(self, tmp_path: Path) -> None:
        checklist = _make_checklist(tmp_path, version="5")
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        source = _make_source_file(tmp_path, "d.py", "x\n")
        (staging_dir / "file-d-py.json").write_text(json.dumps(_make_file_staging(str(source))))

        cache_path = tmp_path / "cache.json"
        build(staging_dir=staging_dir, cache_path=cache_path, checklist_path=checklist)

        cache_data = json.loads(cache_path.read_text())
        assert cache_data["checklist_version"] == "5"

    def test_skips_nonexistent_source_files(self, tmp_path: Path) -> None:
        checklist = _make_checklist(tmp_path)
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        (staging_dir / "file-gone-py.json").write_text(json.dumps(_make_file_staging("gone.py")))

        cache_path = tmp_path / "cache.json"
        build(staging_dir=staging_dir, cache_path=cache_path, checklist_path=checklist)

        cache_data = json.loads(cache_path.read_text())
        assert cache_data["files"] == {}

    def test_summary_computed_correctly(self, tmp_path: Path) -> None:
        checklist = _make_checklist(tmp_path)
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        source = _make_source_file(tmp_path, "f.py", "def baz():\n    pass\n")
        source_str = str(source)

        (staging_dir / "file-f-py.json").write_text(json.dumps(_make_file_staging(source_str)))
        symbol_staging = _make_symbol_staging(source_str, "baz", [1, 2])
        (staging_dir / "symbol-g1.json").write_text(json.dumps(symbol_staging))

        cache_path = tmp_path / "cache.json"
        build(staging_dir=staging_dir, cache_path=cache_path, checklist_path=checklist)

        cache_data = json.loads(cache_path.read_text())
        summary = cache_data["summary"]
        assert summary["symbols_reviewed"] == 1
        assert summary["passed"] == 2


# --- Tests from merge.py ---


@pytest.fixture
def staging_files() -> list[StagingEntry]:
    return load_staging_files(STAGING_DIR)


class TestLoadStagingFiles:
    def test_loads_all_json_files_excluding_meta(self) -> None:
        files = load_staging_files(STAGING_DIR)

        assert len(files) == 6

    def test_files_sorted_alphabetically(self) -> None:
        files = load_staging_files(STAGING_DIR)
        stages = [staging_file.get("stage") for staging_file in files]

        assert stages[0] == "changeset"
        assert stages[1] == "file"


class TestLoadChecklist:
    def test_reads_version(self, tmp_path: Path) -> None:
        checklist = tmp_path / "checklist.yaml"
        checklist.write_text(
            'version: "3"\nitems:\n  - id: foo\n    category: design\n    level: blocking\n    description: "Test"\n'
        )

        result = load_checklist(checklist)

        assert result["version"] == "3"

    def test_builds_items_lookup(self, tmp_path: Path) -> None:
        checklist = tmp_path / "checklist.yaml"
        checklist.write_text(
            "version: 2\nitems:\n"
            "  - id: srp\n    category: design\n    level: blocking\n    description: SRP check\n"
            "  - id: naming\n    category: readability\n    level: advisory\n"
            "    description: Naming check\n"
        )

        result = load_checklist(checklist)

        assert "srp" in result["items"]
        assert result["items"]["srp"]["category"] == "design"
        assert result["items"]["srp"]["level"] == "blocking"
        assert result["items"]["naming"]["description"] == "Naming check"

    def test_missing_version(self, tmp_path: Path) -> None:
        checklist = tmp_path / "checklist.yaml"
        checklist.write_text("items: []\n")

        assert load_checklist(checklist)["version"] == "unknown"


class TestResolveChecklist:
    def test_explicit_path_exists(self, tmp_path: Path) -> None:
        checklist = tmp_path / "custom.yaml"
        checklist.write_text("version: 1\nitems: []\n")

        result = resolve_checklist(checklist)

        assert result == checklist

    def test_explicit_path_not_found_raises(self, tmp_path: Path) -> None:
        missing = tmp_path / "does_not_exist.yaml"

        with pytest.raises(FileNotFoundError, match="Checklist not found"):
            resolve_checklist(missing)

    def test_local_file_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        local = tmp_path / ".code-review-checklist.yaml"
        local.write_text("version: 1\nitems: []\n")

        result = resolve_checklist()

        assert result == Path(".code-review-checklist.yaml")

    def test_falls_back_to_builtin(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)

        result = resolve_checklist()

        assert result.exists()
        assert "checklist.yaml" in str(result)


class TestSortChecks:
    def test_sorts_by_category_order(self) -> None:
        checks = [
            {"id": "naming", "category": "readability"},
            {"id": "type-ann", "category": "correctness"},
            {"id": "srp", "category": "design"},
        ]
        sorted_ids = [check["id"] for check in sort_checks(checks)]

        assert sorted_ids == ["srp", "type-ann", "naming"]


class TestTargetSortKey:
    def test_changeset_before_file(self) -> None:
        changeset = {"target": {"type": "changeset"}}
        file_entry = {"target": {"type": "file", "file": "b.py"}}

        assert target_sort_key(changeset) < target_sort_key(file_entry)

    def test_file_before_symbol(self) -> None:
        file_entry = {"target": {"type": "file", "file": "z.py"}}
        symbol = {"target": {"type": "symbol", "file": "a.py", "lines": [1, 10]}}

        assert target_sort_key(file_entry) < target_sort_key(symbol)

    def test_files_sorted_by_path(self) -> None:
        file_a = {"target": {"type": "file", "file": "a.py"}}
        file_b = {"target": {"type": "file", "file": "b.py"}}

        assert target_sort_key(file_a) < target_sort_key(file_b)

    def test_symbols_sorted_by_file_then_line(self) -> None:
        sym_a = cast("TargetEntry", {"target": {"type": "symbol", "file": "a.py", "lines": [100, 110]}})
        sym_b = cast("TargetEntry", {"target": {"type": "symbol", "file": "a.py", "lines": [50, 60]}})
        sym_c = cast("TargetEntry", {"target": {"type": "symbol", "file": "b.py", "lines": [1, 5]}})

        keys = sorted([sym_a, sym_b, sym_c], key=target_sort_key)
        lines: list[int] = [cast("dict[str, Any]", entry["target"])["lines"][0] for entry in keys]

        assert lines == [50, 100, 1]


class TestHasNonPass:
    def test_all_pass_legacy(self) -> None:
        checks = [{"status": "passed"}, {"status": "passed"}]

        assert has_non_pass(checks) is False

    def test_has_failed_legacy(self) -> None:
        checks = [{"status": "passed"}, {"status": "failed"}]

        assert has_non_pass(checks) is True

    def test_has_blocked_legacy(self) -> None:
        checks = [{"status": "passed"}, {"status": "blocked"}]

        assert has_non_pass(checks) is True

    def test_all_pass_compact(self) -> None:
        checks = [{"id": "a", "pass": True}, {"id": "b", "pass": True}]

        assert has_non_pass(checks) is False

    def test_has_failed_compact(self) -> None:
        checks = [{"id": "a", "pass": True}, {"id": "b", "pass": False}]

        assert has_non_pass(checks) is True

    def test_has_blocked_compact(self) -> None:
        checks = [{"id": "a", "pass": True}, {"id": "b", "pass": None}]

        assert has_non_pass(checks) is True


class TestMergeStaging:
    def test_filters_all_pass_symbols(self, staging_files: list[dict[str, Any]]) -> None:
        targets, _symbols_reviewed, _ = merge_staging(staging_files)
        symbol_targets = [target_entry for target_entry in targets if target_entry["target"]["type"] == "symbol"]

        assert len(symbol_targets) == 1
        target = symbol_targets[0]["target"]
        assert target["type"] == "symbol"
        assert target["symbol"] == "Bitable.__init__"

    def test_counts_all_symbols_including_filtered(self, staging_files: list[dict[str, Any]]) -> None:
        _, symbols_reviewed, _ = merge_staging(staging_files)

        assert symbols_reviewed == 7

    def test_preserves_changeset_and_file_targets(self, staging_files: list[dict[str, Any]]) -> None:
        targets, _, _ = merge_staging(staging_files)
        types = [target_entry["target"]["type"] for target_entry in targets]

        assert types.count("changeset") == 1
        assert types.count("file") == 2

    def test_targets_sorted_correctly(self, staging_files: list[dict[str, Any]]) -> None:
        targets, _, _ = merge_staging(staging_files)
        types = [target_entry["target"]["type"] for target_entry in targets]

        assert types == ["changeset", "file", "file", "symbol"]

    def test_file_targets_alphabetical(self, staging_files: list[dict[str, Any]]) -> None:
        targets, _, _ = merge_staging(staging_files)
        file_targets = [target_entry for target_entry in targets if target_entry["target"]["type"] == "file"]
        files = [cast("dict[str, Any]", file_target["target"])["file"] for file_target in file_targets]

        assert files == sorted(files)

    def test_summary_counts_all_checks(self, staging_files: list[dict[str, Any]]) -> None:
        _, _, summary = merge_staging(staging_files)

        assert summary == {
            "blocking_failures": 1,
            "advisory_failures": 0,
            "passed": 23,
            "blocked": 2,
            "symbols_reviewed": 7,
        }

    def test_checks_sorted_by_category(self, staging_files: list[dict[str, Any]]) -> None:
        targets, _, _ = merge_staging(staging_files)
        symbol_target = next(target_entry for target_entry in targets if target_entry["target"]["type"] == "symbol")
        categories = [cast("dict[str, Any]", check)["category"] for check in symbol_target["checks"]]

        assert categories == ["design", "design", "correctness", "readability"]


class TestEnrichCheck:
    ITEMS: ClassVar[dict[str, Any]] = {
        "srp": {
            "id": "srp",
            "category": "design",
            "level": "blocking",
            "description": "SRP check",
        },
        "naming": {
            "id": "naming",
            "category": "readability",
            "level": "advisory",
            "description": "Naming",
        },
    }

    def test_fills_missing_fields_from_checklist(self) -> None:
        check = {"id": "srp", "pass": True}

        enriched = cast("dict[str, Any]", enrich_check(check, self.ITEMS))

        assert enriched["category"] == "design"
        assert enriched["level"] == "blocking"
        assert enriched["description"] == "SRP check"
        assert enriched["status"] == "passed"

    def test_derives_failed_status(self) -> None:
        check = {"id": "naming", "pass": False, "note": "bad name"}

        enriched = cast("dict[str, Any]", enrich_check(check, self.ITEMS))

        assert enriched["status"] == "failed"
        assert enriched["level"] == "advisory"

    def test_derives_blocked_status(self) -> None:
        check = {"id": "srp", "pass": None}

        enriched = cast("dict[str, Any]", enrich_check(check, self.ITEMS))

        assert enriched["status"] == "blocked"

    def test_preserves_existing_fields(self) -> None:
        check = {"id": "srp", "pass": False, "category": "custom", "level": "advisory"}

        enriched = cast("dict[str, Any]", enrich_check(check, self.ITEMS))

        assert enriched["category"] == "custom"
        assert enriched["level"] == "advisory"

    def test_unknown_id_still_derives_status(self) -> None:
        check = {"id": "unknown-check", "pass": True}

        enriched = cast("dict[str, Any]", enrich_check(check, {}))

        assert enriched["status"] == "passed"
        assert "category" not in enriched


class TestCountChecks:
    def test_counts_each_status(self) -> None:
        entries = [
            {
                "checks": [
                    {"status": "passed", "level": "blocking"},
                    {"status": "failed", "level": "blocking"},
                    {"status": "failed", "level": "advisory"},
                    {"status": "blocked", "level": "advisory"},
                ]
            }
        ]
        summary = _compute_summary(entries, symbols_reviewed=1)

        assert summary == {
            "blocking_failures": 1,
            "advisory_failures": 1,
            "passed": 1,
            "blocked": 1,
            "symbols_reviewed": 1,
        }

    def test_empty_entries(self) -> None:
        summary = _compute_summary([], symbols_reviewed=0)

        assert summary["passed"] == 0
        assert summary["blocking_failures"] == 0

    def test_counts_compact_format(self) -> None:
        entries = [
            {
                "checks": [
                    {"id": "a", "pass": True},
                    {"id": "b", "pass": False, "level": "blocking"},
                    {"id": "c", "pass": False, "level": "advisory"},
                    {"id": "d", "pass": None},
                ]
            }
        ]
        summary = _compute_summary(entries, symbols_reviewed=1)

        assert summary == {
            "blocking_failures": 1,
            "advisory_failures": 1,
            "passed": 1,
            "blocked": 1,
            "symbols_reviewed": 1,
        }


class TestNormalizeSymbolTarget:
    def test_nested_target_passes_through(self) -> None:
        entry = {"target": {"type": "symbol", "file": "a.py", "symbol": "foo", "lines": [1, 5]}}

        result = _normalize_symbol_target(entry, fallback_file="b.py")

        assert result == entry["target"]

    def test_flat_entry_builds_target(self) -> None:
        entry = {"symbol": "bar", "lines": [10, 20], "checks": []}

        result = _normalize_symbol_target(entry, fallback_file="src/mod.py")

        assert result == {
            "type": "symbol",
            "file": "src/mod.py",
            "symbol": "bar",
            "lines": (10, 20),
        }

    def test_flat_entry_with_name_key(self) -> None:
        entry = {"name": "Baz", "lines": [1, 3], "checks": []}

        result = _normalize_symbol_target(entry, fallback_file="x.py")

        assert result["symbol"] == "Baz"


class TestMergeStagingNormalization:
    def test_symbols_key_accepted(self) -> None:
        staging_files = [
            {
                "stage": "symbol",
                "file": "a.py",
                "symbols": [
                    {
                        "symbol": "func_a",
                        "lines": [1, 5],
                        "checks": [{"status": "passed", "level": "advisory", "category": "design"}],
                    }
                ],
            }
        ]

        _targets, symbols_reviewed, _ = merge_staging(staging_files)

        assert symbols_reviewed == 1

    def test_flat_symbol_entries_normalized(self) -> None:
        staging_files = [
            {
                "stage": "symbol",
                "file": "b.py",
                "targets": [
                    {
                        "symbol": "my_func",
                        "lines": [10, 20],
                        "checks": [{"status": "failed", "level": "advisory", "category": "readability"}],
                    }
                ],
            }
        ]

        targets, _, _ = merge_staging(staging_files)

        target = targets[0]["target"]
        assert target["type"] == "symbol"
        assert target["file"] == "b.py"
        assert target["symbol"] == "my_func"


class TestRestoreSymbolTarget:
    def test_converts_offsets_to_lines(self) -> None:
        symbol_def = SymbolDef(name="foo", type="function", lines=[10, 20])
        cache_checks = {"checks": [{"id": "a", "pass": False, "annotations": [{"offset": 3, "message": "fix"}]}]}

        result = _restore_symbol_target("src/a.py", symbol_def, cache_checks)

        target = cast("dict[str, Any]", result["target"])
        assert target["symbol"] == "foo"
        assert target["lines"] == (10, 20)
        checks = cast("list[dict[str, Any]]", result["checks"])
        assert checks[0]["annotations"][0]["line"] == 13  # 10 + 3

    def test_preserves_checks_without_annotations(self) -> None:
        symbol_def = SymbolDef(name="bar", type="function", lines=[5, 10])
        cache_checks = {"checks": [{"id": "a", "pass": True}]}

        result = _restore_symbol_target("b.py", symbol_def, cache_checks)

        assert result["checks"] == [{"id": "a", "pass": True}]


class TestBuildInitInstructions:
    def test_contains_default_checklist(self) -> None:
        from code_review_skill.cli import _build_init_instructions

        checklist = "version: \"2\"\nitems:\n  - id: test-item\n"
        result = _build_init_instructions(checklist)

        assert "- id: test-item" in result
        assert "--- BEGIN DEFAULT CHECKLIST ---" in result
        assert "--- END DEFAULT CHECKLIST ---" in result

    def test_contains_project_structure_context(self) -> None:
        from code_review_skill.cli import _build_init_instructions

        result = _build_init_instructions("version: \"2\"\nitems: []\n")

        assert ".code-review/staging/" in result
        assert ".code-review/cache.json" in result
        assert ".code-review-checklist.yaml" in result
        assert ".gitignore" in result

    def test_contains_precheck_context(self) -> None:
        from code_review_skill.cli import _build_init_instructions

        result = _build_init_instructions("version: \"2\"\nitems: []\n")

        assert "pre_check" in result
        assert "Gate 0" in result
        assert "pytest" in result
        assert "npm test" in result

    def test_contains_checklist_schema(self) -> None:
        from code_review_skill.cli import _build_init_instructions

        result = _build_init_instructions("version: \"2\"\nitems: []\n")

        assert "changeset" in result
        assert "blocking" in result
        assert "advisory" in result

    def test_contains_validation_command(self) -> None:
        from code_review_skill.cli import _build_init_instructions

        result = _build_init_instructions("version: \"2\"\nitems: []\n")

        assert "code-review-skill init check" in result

    def test_counts_items_correctly(self) -> None:
        from code_review_skill.cli import _count_items

        checklist = "items:\n  - id: foo\n  - id: bar\n  - id: baz\n"
        assert _count_items(checklist) == 3

    def test_counts_zero_items(self) -> None:
        from code_review_skill.cli import _count_items

        assert _count_items("items: []\n") == 0


class TestInitCheck:
    def test_fails_when_nothing_configured(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        from code_review_skill.cli import _cmd_init_check

        with pytest.raises(SystemExit) as exc_info:
            _cmd_init_check()
        assert exc_info.value.code == 1

    def test_passes_when_fully_configured(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)

        # Create staging dir
        (tmp_path / ".code-review" / "staging").mkdir(parents=True)

        # Create .gitignore
        (tmp_path / ".gitignore").write_text(".code-review/\n")

        # Create checklist with custom pre_check
        (tmp_path / ".code-review-checklist.yaml").write_text(
            'version: "2"\npre_check: "pytest"\nitems:\n  - id: test\n    category: design\n    scope: symbol\n    level: advisory\n    description: "test"\n    prompt: "test"\n'
        )

        from code_review_skill.cli import _cmd_init_check

        # Should not raise
        _cmd_init_check()

    def test_fails_with_default_precheck(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)

        (tmp_path / ".code-review" / "staging").mkdir(parents=True)
        (tmp_path / ".gitignore").write_text(".code-review/\n")
        (tmp_path / ".code-review-checklist.yaml").write_text(
            'version: "2"\npre_check: "make check"\nitems:\n  - id: test\n    category: design\n    scope: symbol\n    level: advisory\n    description: "test"\n    prompt: "test"\n'
        )

        from code_review_skill.cli import _cmd_init_check

        with pytest.raises(SystemExit) as exc_info:
            _cmd_init_check()
        assert exc_info.value.code == 1


# --- Batch symbols tests ---


class TestExtractSymbolsBatch:
    MULTI_SOURCE: ClassVar[str] = (
        "def foo():\n    pass\n\ndef bar():\n    pass\n"
    )

    def test_batch_multiple_files(self, tmp_path: Path) -> None:
        f1 = _make_source_file(tmp_path, "a.py", self.MULTI_SOURCE)
        f2 = _make_source_file(tmp_path, "b.py", "class Baz:\n    pass\n")
        result = extract_symbols_batch([str(f1), str(f2)])
        assert str(f1) in result
        assert str(f2) in result
        assert len(result[str(f1)]) == 2
        assert result[str(f2)][0]["name"] == "Baz"

    def test_batch_skips_missing_files(self, tmp_path: Path) -> None:
        f1 = _make_source_file(tmp_path, "a.py", self.MULTI_SOURCE)
        result = extract_symbols_batch([str(f1), str(tmp_path / "nonexistent.py")])
        assert str(f1) in result
        assert str(tmp_path / "nonexistent.py") not in result

    def test_batch_empty_file_excluded(self, tmp_path: Path) -> None:
        """Files with no symbols are excluded from output."""
        f1 = _make_source_file(tmp_path, "empty.py", "# just a comment\nx = 1\n")
        result = extract_symbols_batch([str(f1)])
        assert str(f1) not in result

    def test_batch_no_diff(self, tmp_path: Path) -> None:
        f1 = _make_source_file(tmp_path, "a.py", self.MULTI_SOURCE)
        result = extract_symbols_batch([str(f1)], diff_range=None)
        assert len(result[str(f1)]) == 2


class TestDiscoverChangedFiles:
    def test_filters_python_files(self, monkeypatch: pytest.MonkeyPatch) -> None:
        original_run = subprocess.run

        def mock_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if "diff" in cmd and "--name-only" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="a.py\nb.js\nc.py\nREADME.md\n", stderr=""
                )
            return original_run(cmd, **kwargs)

        monkeypatch.setattr(subprocess, "run", mock_run)
        result = discover_changed_files("main")
        assert result == ["a.py", "c.py"]

    def test_empty_diff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        original_run = subprocess.run

        def mock_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if "diff" in cmd and "--name-only" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return original_run(cmd, **kwargs)

        monkeypatch.setattr(subprocess, "run", mock_run)
        result = discover_changed_files("main")
        assert result == []


class TestWriteStagingEntry:
    def test_writes_changeset(self, tmp_path: Path) -> None:
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        entry: StagingEntry = {
            "stage": "changeset",
            "target": {"type": "changeset"},
            "checks": [],
        }
        path = write_staging_entry(staging_dir, entry)
        assert path.name == "changeset.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["stage"] == "changeset"

    def test_writes_file_entry(self, tmp_path: Path) -> None:
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        entry: StagingEntry = {
            "stage": "file",
            "target": {"type": "file", "file": "src/foo.py"},
            "checks": [],
        }
        path = write_staging_entry(staging_dir, entry)
        assert path.name == "file-src-foo-py.json"
        assert json.loads(path.read_text())["target"]["file"] == "src/foo.py"

    def test_writes_symbol_entry(self, tmp_path: Path) -> None:
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        entry: StagingEntry = {
            "stage": "symbol",
            "target": {"type": "symbol", "file": "foo.py", "symbol": "bar", "lines": [1, 5]},
            "checks": [],
        }
        path = write_staging_entry(staging_dir, entry)
        assert path.name == "symbol-foo-py-bar.json"

    def test_creates_staging_dir(self, tmp_path: Path) -> None:
        staging_dir = tmp_path / "new" / "staging"
        entry: StagingEntry = {
            "stage": "changeset",
            "target": {"type": "changeset"},
            "checks": [],
        }
        path = write_staging_entry(staging_dir, entry)
        assert path.exists()


class TestCheckWithDiff:
    """Test that check() with diff_symbols filters symbols."""

    SOURCE: ClassVar[str] = "def changed():\n    pass\n\ndef unchanged():\n    pass\n"

    def test_without_diff_returns_all_symbols(self, tmp_path: Path) -> None:
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        checklist_path = _make_checklist(tmp_path)
        source_file = _make_source_file(tmp_path, "code.py", self.SOURCE)

        result = check(
            files=[str(source_file)],
            cache_path=tmp_path / "cache.json",
            checklist_path=checklist_path,
            staging_dir=staging_dir,
            diff_symbols=None,
        )
        # Without diff, all symbols should be in review_symbols
        all_review_symbols = []
        for symbols in result["review_symbols"].values():
            all_review_symbols.extend(symbols)
        assert "changed" in all_review_symbols
        assert "unchanged" in all_review_symbols
