import { describe, it } from "node:test";
import assert from "node:assert/strict";
import type { Annotation, Check, ReviewResult, ReviewTarget, TargetDescriptor } from "../src/types.js";

// Replicate the pure aggregation logic for testing without vscode dependency.

interface LineFinding {
  line: number;
  blockingCount: number;
  advisoryCount: number;
  checks: { check: Check; symbolName?: string }[];
}

/**
 * Resolve an annotation's offset to a 0-indexed absolute line number.
 * Mirrors the production resolveAnnotationLine.
 */
function resolveAnnotationLine(target: TargetDescriptor, annotation: Annotation): number {
  if (target.type === "symbol") {
    return target.lines[0] - 1 + annotation.offset;
  }
  return annotation.offset;
}

function aggregateFindings(result: ReviewResult, filePath: string): LineFinding[] {
  const lineMap = new Map<number, LineFinding>();

  for (const entry of result.targets) {
    const target = entry.target;
    if (target.type === "changeset") continue;
    if (target.file !== filePath) continue;

    const line = target.type === "symbol" ? target.lines[0] - 1 : 0;
    const symbolName = target.type === "symbol" ? target.symbol : undefined;

    if (!lineMap.has(line)) {
      lineMap.set(line, { line, blockingCount: 0, advisoryCount: 0, checks: [] });
    }
    const finding = lineMap.get(line)!;

    for (const check of entry.checks) {
      if (check.status === "passed") continue;
      if (check.status === "failed" && check.level === "blocking") {
        finding.blockingCount++;
      } else if (check.status === "failed" && check.level === "advisory") {
        finding.advisoryCount++;
      }
      finding.checks.push({ check, symbolName });
    }
  }

  return [...lineMap.values()].filter(
    (finding) => finding.blockingCount > 0 || finding.advisoryCount > 0 || finding.checks.length > 0,
  );
}

const RESULT: ReviewResult = {
  version: "3",
  timestamp: "2026-03-10T00:00:00Z",
  checklist_version: "2",
  targets: [
    {
      target: { type: "changeset" },
      checks: [
        { id: "dep", category: "design", level: "blocking", description: "OK", pass: true, status: "passed", note: "" },
      ],
    },
    {
      target: { type: "file", file: "/ws/handler.py" },
      checks: [
        { id: "error-handling", category: "correctness", level: "advisory", description: "Swallowed", pass: false, status: "failed", note: "bare except" },
        { id: "type-annotations", category: "correctness", level: "advisory", description: "OK", pass: true, status: "passed", note: "" },
      ],
    },
    {
      target: { type: "symbol", file: "/ws/handler.py", symbol: "handle", lines: [10, 30] },
      checks: [
        { id: "srp", category: "design", level: "blocking", description: "SRP fail", pass: false, status: "failed", note: "split it" },
        { id: "naming", category: "readability", level: "advisory", description: "Names", pass: null, status: "blocked", note: "Blocked" },
      ],
    },
    {
      target: { type: "symbol", file: "/ws/handler.py", symbol: "parse", lines: [35, 45] },
      checks: [
        { id: "naming", category: "readability", level: "advisory", description: "Vague", pass: false, status: "failed", note: "rename" },
      ],
    },
  ],
  summary: { blocking_failures: 1, advisory_failures: 2, passed: 2, blocked: 1 },
};

describe("aggregateFindings", () => {
  it("skips changeset targets", () => {
    const findings = aggregateFindings(RESULT, "/ws/handler.py");
    const lines = findings.map((f) => f.line);
    assert.ok(!lines.includes(-1)); // changeset has no line
  });

  it("places file-level findings at line 0", () => {
    const findings = aggregateFindings(RESULT, "/ws/handler.py");
    const fileFinding = findings.find((f) => f.line === 0);
    assert.ok(fileFinding);
    assert.equal(fileFinding.advisoryCount, 1);
    assert.equal(fileFinding.blockingCount, 0);
  });

  it("places symbol findings at def line (0-indexed)", () => {
    const findings = aggregateFindings(RESULT, "/ws/handler.py");
    const handleFinding = findings.find((f) => f.line === 9); // line 10 -> 0-indexed 9
    assert.ok(handleFinding);
    assert.equal(handleFinding.blockingCount, 1);
    assert.equal(handleFinding.advisoryCount, 0);
  });

  it("does not count blocked checks in badge numbers", () => {
    const findings = aggregateFindings(RESULT, "/ws/handler.py");
    const handleFinding = findings.find((f) => f.line === 9)!;
    // 1 blocking failure + 1 blocked -> only blockingCount=1
    assert.equal(handleFinding.blockingCount, 1);
    assert.equal(handleFinding.advisoryCount, 0);
    // But blocked check is still in checks list for hover
    assert.equal(handleFinding.checks.length, 2);
  });

  it("counts advisory failures correctly", () => {
    const findings = aggregateFindings(RESULT, "/ws/handler.py");
    const parseFinding = findings.find((f) => f.line === 34)!; // line 35 -> 34
    assert.equal(parseFinding.advisoryCount, 1);
    assert.equal(parseFinding.blockingCount, 0);
  });

  it("returns empty for unrelated files", () => {
    const findings = aggregateFindings(RESULT, "/ws/other.py");
    assert.equal(findings.length, 0);
  });

  it("skips passed checks entirely", () => {
    const findings = aggregateFindings(RESULT, "/ws/handler.py");
    const fileFinding = findings.find((f) => f.line === 0)!;
    // File target has 2 checks: 1 failed (error-handling), 1 passed (type-annotations)
    // Only the failed one should appear
    assert.equal(fileFinding.checks.length, 1);
    assert.equal(fileFinding.checks[0].check.id, "error-handling");
  });
});

describe("resolveAnnotationLine", () => {
  it("resolves file annotation offset to 0-indexed line", () => {
    const target: TargetDescriptor = { type: "file", file: "a.py" };
    const annotation: Annotation = { offset: 4, message: "fix" };

    assert.equal(resolveAnnotationLine(target, annotation), 4);
  });

  it("resolves symbol annotation offset to 0-indexed line", () => {
    const target: TargetDescriptor = { type: "symbol", file: "a.py", symbol: "foo", lines: [10, 20] };
    const annotation: Annotation = { offset: 3, message: "fix" };

    // target.lines[0] - 1 + offset = 10 - 1 + 3 = 12
    assert.equal(resolveAnnotationLine(target, annotation), 12);
  });

  it("offset 0 points to the def line itself", () => {
    const target: TargetDescriptor = { type: "symbol", file: "a.py", symbol: "foo", lines: [10, 20] };
    const annotation: Annotation = { offset: 0, message: "fix" };

    // target.lines[0] - 1 + 0 = 9 (0-indexed def line)
    assert.equal(resolveAnnotationLine(target, annotation), 9);
  });
});
