import { describe, it } from "node:test";
import assert from "node:assert/strict";
import type { Annotation, Check, ReviewResult, TargetDescriptor } from "../../src/types/review.js";
import { resolveAnnotationLine, aggregateFindings, aggregateAnnotations } from "../../src/utils/dataTransform.js";

const RESULT: ReviewResult = {
  version: "3",
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
    assert.ok(!lines.includes(-1));
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
    const handleFinding = findings.find((f) => f.line === 9);
    assert.ok(handleFinding);
    assert.equal(handleFinding.blockingCount, 1);
    assert.equal(handleFinding.advisoryCount, 0);
  });

  it("does not count blocked checks in badge numbers", () => {
    const findings = aggregateFindings(RESULT, "/ws/handler.py");
    const handleFinding = findings.find((f) => f.line === 9)!;
    assert.equal(handleFinding.blockingCount, 1);
    assert.equal(handleFinding.advisoryCount, 0);
    assert.equal(handleFinding.checks.length, 2);
  });

  it("counts advisory failures correctly", () => {
    const findings = aggregateFindings(RESULT, "/ws/handler.py");
    const parseFinding = findings.find((f) => f.line === 34)!;
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
    assert.equal(resolveAnnotationLine(target, annotation), 12);
  });

  it("offset 0 points to the def line itself", () => {
    const target: TargetDescriptor = { type: "symbol", file: "a.py", symbol: "foo", lines: [10, 20] };
    const annotation: Annotation = { offset: 0, message: "fix" };
    assert.equal(resolveAnnotationLine(target, annotation), 9);
  });
});

describe("aggregateAnnotations", () => {
  const resultWithAnnotations: ReviewResult = {
    version: "3",
    targets: [
      {
        target: { type: "symbol", file: "/ws/handler.py", symbol: "handle", lines: [10, 30] },
        checks: [
          {
            id: "srp", category: "design", level: "blocking", description: "SRP fail", pass: false, status: "failed", note: "",
            annotations: [
              { offset: 2, message: "Split this responsibility" },
              { offset: 5, message: "Too much logic here" },
            ],
          },
        ],
      },
    ],
    summary: { blocking_failures: 1, advisory_failures: 0, passed: 0, blocked: 0 },
  };

  it("aggregates annotations by line", () => {
    const annotations = aggregateAnnotations(resultWithAnnotations, "/ws/handler.py");
    assert.equal(annotations.length, 2);

    // line = 10-1+2 = 11
    const ann11 = annotations.find((a) => a.line === 11);
    assert.ok(ann11);
    assert.equal(ann11.entries.length, 1);
    assert.equal(ann11.entries[0].message, "Split this responsibility");

    // line = 10-1+5 = 14
    const ann14 = annotations.find((a) => a.line === 14);
    assert.ok(ann14);
    assert.equal(ann14.entries[0].message, "Too much logic here");
  });

  it("returns empty for files without annotations", () => {
    const annotations = aggregateAnnotations(resultWithAnnotations, "/ws/other.py");
    assert.equal(annotations.length, 0);
  });
});
